"""Memory tools: remember, recall, forget. Used only when the user explicitly asks."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field

from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult


def _memory(ctx: ToolContext):
    manager = ctx.get_memory()
    if manager is None:
        raise ToolError("Memory is not available (no database).", "unavailable")
    return manager


class RememberArgs(ToolArgs):
    fact: str = Field(min_length=1, max_length=500, validation_alias=AliasChoices("fact", "text", "content", "memory"),
                      description="The fact in the user's words, e.g. 'my project folder is D:\\\\Projects'")
    topic: str | None = Field(default=None, max_length=40, description="Optional short topic, e.g. 'project folder'")
    private: bool = Field(default=False, description="true only if the user says to keep it private / just between us")
    until: str | None = Field(default=None, max_length=40,
                              description="Only if the user limits it: 'today', 'this week', 'Friday', '15 October'")
    kind: Literal["semantic", "episodic", "procedural", "goal"] | None = Field(
        default=None, description="Only if clear: semantic = a fact, episodic = something that happened, procedural "
                                  "= how the user likes something done, goal = what they want to achieve")
    important: bool = Field(default=False, description="true if the user says it's important")


def _expiry(text: str | None, ctx: ToolContext) -> str | None:
    """'today' / 'Friday' -> the end of that day, as UTC (memories expire then)."""
    if not text:
        return None
    from datetime import datetime, time, timezone

    from app.core.goals import parse_deadline

    day = parse_deadline(text, ctx.clock().date())
    if day is None:
        return None
    local_end = datetime.combine(datetime.fromisoformat(day).date(), time(23, 59, 59)).astimezone()
    return local_end.astimezone(timezone.utc).isoformat(timespec="seconds")


def remember(args: RememberArgs, ctx: ToolContext) -> ToolResult:
    from app.core.runs import current

    run = current()
    provenance = "confirmed_after_untrusted" if run is not None and run.confirmed_after_untrusted else "user"
    manager = _memory(ctx)
    memory, replaced = manager.remember(args.fact, args.topic, provenance=provenance, private=args.private,
                                        expires_at=_expiry(args.until, ctx), kind=args.kind,
                                        importance=3 if args.important else 2)
    verb = "Updated" if replaced else "I'll remember"
    extra = " (private: I won't give it to the AI model)" if args.private else ""
    if memory.expires_at:
        extra += f" until {args.until}"
    notes = []
    for found in manager.last_conflicts:
        if found["relation"] == "supersedes":
            notes.append(f"That replaces \"{found['old']}\" - I keep the old one in its history but won't use it.")
        else:
            notes.append(f"That may contradict \"{found['old']}\" ({found['reason']}). Which is true now? Say 'keep "
                         "the new one', 'keep the old one' or 'both are true'.")
    message = f"{verb}: {memory.content}{extra}" + (". " + " ".join(notes) if notes else "")
    return ToolResult.success(message, **memory.to_dict(), replaced=replaced, conflicts=manager.last_conflicts)


class ConflictArgs(ToolArgs):
    keep: Literal["new", "old", "both"] = Field(description="new = the newer memory is true now; old = the older "
                                                            "one; both = both are true")
    conflict_id: int | None = Field(default=None, ge=1, description="Which conflict; empty = the latest one")


def resolve_memory_conflict(args: ConflictArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    open_ones = manager.conflicts()
    if not open_ones:
        raise ToolError("There's no memory conflict waiting.", "not_found")
    target = next((c for c in open_ones if c["id"] == args.conflict_id), None) if args.conflict_id else open_ones[0]
    if target is None:
        raise ToolError(f"There's no open conflict #{args.conflict_id}.", "not_found")
    manager.resolve_conflict(target["id"], args.keep)
    message = {"new": f"Okay: \"{target['new']}\" is true now; \"{target['old']}\" is kept only as history.",
               "old": f"Okay: \"{target['old']}\" stays; \"{target['new']}\" is kept only as history.",
               "both": "Okay, both stay."}[args.keep]
    return ToolResult.success(message, conflict=target["id"], kept=args.keep)


def memory_conflicts(_: NoArgs, ctx: ToolContext) -> ToolResult:
    open_ones = _memory(ctx).conflicts()
    if not open_ones:
        return ToolResult.success("No memory contradicts another one.", conflicts=[])
    shown = "; ".join(f"#{c['id']}: \"{c['old']}\" vs \"{c['new']}\" ({c['reason']})" for c in open_ones[:5])
    return ToolResult.success(f"{len(open_ones)} possible contradiction(s): {shown}.", conflicts=open_ones)


class RecallArgs(ToolArgs):
    query: str | None = Field(default=None, max_length=100,
                              validation_alias=AliasChoices("query", "topic", "about", "search"),
                              description="What to look for; empty = everything")


def recall(args: RecallArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    memories = manager.search(args.query) if args.query else manager.all(50)
    if not memories:
        about = f" about '{args.query}'" if args.query else ""
        return ToolResult.success(f"I don't have anything saved{about}.", memories=[])
    manager.last_touched = memories[0].id
    return ToolResult.success(f"I have {len(memories)} saved fact(s).", memories=[m.to_dict() for m in memories])


class MemoryAdminArgs(ToolArgs):
    action: Literal["categories", "export", "import", "disable_category", "enable_category", "forget_category"] = Field(
        description="list categories, export everything to a file, import an export file, stop/resume using a "
                    "category, or delete a category")
    file: str | None = Field(default=None, max_length=300,
                             description="For import: the export file's name in the exports folder (or a full path)")
    category: str | None = Field(default=None, max_length=40, description="The memory category")
    include_private: bool = Field(default=False, description="Include private memories in the export")


def _admin_precheck(args: MemoryAdminArgs, ctx: ToolContext) -> None:
    if args.action in ("disable_category", "enable_category", "forget_category") and not (args.category or "").strip():
        raise ToolError("Which category? Say 'what categories of memory do you have' to list them.", "missing_category")


def _admin_risk(args: MemoryAdminArgs, ctx: ToolContext) -> RiskLevel:
    return {"forget_category": RiskLevel.DANGEROUS, "export": RiskLevel.MODERATE,
            "import": RiskLevel.MODERATE}.get(args.action, RiskLevel.SAFE)


def _admin_question(args: MemoryAdminArgs, ctx: ToolContext) -> str:
    if args.action == "export":
        extra = " INCLUDING private ones" if args.include_private else ""
        return f"Write all your saved memories{extra} to a file in Jarvis's data folder?"
    if args.action == "import":
        return f"Import the memories in '{args.file}'? Existing facts are updated, nothing is deleted."
    count = _memory(ctx).categories().get((args.category or "").strip().lower(), {}).get("total", 0)
    return f"Delete all {count} memories in the category '{args.category}'? This can't be undone."


def memory_admin(args: MemoryAdminArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    if args.action == "categories":
        cats = manager.categories()
        text = ", ".join(f"{c} ({v['active']}/{v['total']} in use)" for c, v in cats.items())
        return ToolResult.success(f"Memory categories: {text}." if cats else "No memories are saved.", categories=cats)
    if args.action == "export":
        import json

        rows = manager.export(args.include_private)
        base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
        folder = base / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"memories-{ctx.clock():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult.success(f"Exported {len(rows)} memories to {path.name} in the exports folder"
                                  + ("" if args.include_private else " (private ones left out)") + ".",
                                  path=str(path), count=len(rows))
    if args.action == "import":
        import json

        base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
        name = (args.file or "").strip().strip('"')
        if not name:
            exports = sorted((base / "exports").glob("memories-*.json"))
            if not exports:
                raise ToolError("Which file? There is no memory export in the exports folder.", "missing_file")
            path = exports[-1]
        else:
            path = Path(name) if Path(name).is_absolute() else base / "exports" / name
        if path.suffix.lower() != ".json" or not path.is_file():
            raise ToolError(f"I can't find the memory file '{name or path.name}' (a .json export).", "not_found")
        if path.stat().st_size > 5_000_000:
            raise ToolError("That file is too big for a memory export.", "too_big")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(f"That file isn't a readable memory export: {exc}", "invalid") from exc
        counts = manager.import_rows(rows)
        return ToolResult.success(f"Imported from {path.name}: {counts['added']} new, {counts['updated']} updated, "
                                  f"{counts['skipped']} skipped (empty, too long or secret-looking).", **counts)
    category = (args.category or "").strip().lower()
    if category not in manager.categories():
        raise ToolError(f"There is no memory category '{category}'.", "not_found")
    if args.action == "forget_category":
        return ToolResult.success(f"Deleted {manager.forget_category(category)} memories in '{category}'.")
    changed = manager.set_category_disabled(category, args.action == "disable_category")
    verb = "won't use" if args.action == "disable_category" else "will use"
    return ToolResult.success(f"I {verb} the {changed} memories in '{category}' "
                              + ("(they are kept; say 'enable' to use them again)." if args.action == "disable_category"
                                 else "again."))


class ForgetArgs(ToolArgs):
    what: str | None = Field(default=None, max_length=100, validation_alias=AliasChoices("what", "query", "topic", "about"),
                             description="What to forget, e.g. 'my project folder', or 'that' for the last one")
    memory_id: int | None = Field(default=None, ge=1, validation_alias=AliasChoices("memory_id", "id"),
                                  description="Memory number, if known")


def forget(args: ForgetArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    if args.memory_id is not None:
        target = manager.get(args.memory_id)
        if target is None:
            raise ToolError(f"There is no memory number {args.memory_id}.", "not_found")
    elif args.what:
        target = manager.find_one(args.what)
    else:
        target = manager.find_one("that")
    manager.forget(target.id)
    return ToolResult.success(f"Forgotten: {target.content}", id=target.id)


class ExplainArgs(ToolArgs):
    what: str | None = Field(default=None, max_length=100, description="Which memory; empty or 'that' = the last one")


def explain_memory(args: ExplainArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    memory = manager.find_one(args.what or "that", include_hidden=True)
    manager.last_touched = memory.id
    return ToolResult.success(manager.explain(memory), id=memory.id, provenance=memory.provenance,
                              revision=memory.revision)


class ManageMemoryArgs(ToolArgs):
    what: str | None = Field(default=None, max_length=100, description="Which memory; empty or 'that' = the last one")
    action: Literal["disable", "enable", "private", "not_private", "expire", "history", "edit"] = Field(
        description="disable = stop using it (kept), enable, private = never give it to the AI model, expire = "
                    "forget it on the 'until' day, history = its earlier versions, edit = change its words to 'text'")
    until: str | None = Field(default=None, max_length=40, description="For expire: 'today', 'Friday', '15 October'")
    text: str | None = Field(default=None, max_length=500, description="For edit: the new wording")


def manage_memory(args: ManageMemoryArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    memory = manager.find_one(args.what or "that", include_hidden=True)
    manager.last_touched = memory.id
    if args.action in ("disable", "enable"):
        manager.set_disabled(memory, args.action == "disable")
        return ToolResult.success(("I won't use it any more (it's kept; say 'enable it' to use it again): "
                                   if args.action == "disable" else "I'll use it again: ") + memory.content)
    if args.action in ("private", "not_private"):
        manager.set_private(memory, args.action == "private")
        return ToolResult.success(("Private now - I won't give it to the AI model: " if args.action == "private"
                                   else "Not private any more: ") + memory.content)
    if args.action == "edit":
        before = memory.content
        changed = manager.edit(memory, args.text or "")
        return ToolResult.success(f"Changed. Before: {before}. Now: {changed.content}.", id=changed.id)
    if args.action == "expire":
        expires = _expiry(args.until or "today", ctx)
        manager.set_expiry(memory, expires)
        return ToolResult.success(f"I'll forget it after {args.until or 'today'}: {memory.content}")
    versions = manager.history(memory)
    if not versions:
        return ToolResult.success(f"It has never changed: {memory.content}", versions=[])
    shown = "; ".join(f"v{v['revision']}: {v['content']}" for v in versions[:5])
    return ToolResult.success(f"Now: {memory.content}. Before: {shown}.", versions=versions[:10])


class KnowArgs(ToolArgs):
    about: Literal["everything", "project", "goal", "mission", "app", "person", "layout", "nickname"] = Field(
        default="everything", description="What kind of thing")


def answer_sources(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """Where the last answer's facts came from (from the record of what really ran, not from the model's words)."""
    conversation = getattr(ctx, "conversation", None)
    sources = getattr(conversation, "last_sources", None) if conversation is not None else None
    if not sources:
        return ToolResult.success("I have no record for the last answer yet.", sources=None)
    parts = []
    for t in sources.get("tools", []):
        check = (" and I checked the result afterwards" if t["verified"] is True else
                 " but I couldn't confirm it afterwards" if t["verified"] is False else "")
        parts.append(f"{t['tool']} {'worked' if t['ok'] else 'failed'}{check}")
    memories = sources.get("memories", [])
    if memories:
        parts.append("saved memories you told me earlier: " + "; ".join(m["content"] for m in memories[:5]))
    model = sources.get("model") or "the AI model"
    if not parts:
        return ToolResult.success(f"That answer came only from the AI model ({model}): no tool ran and no saved memory "
                                  "matched, so treat it as unchecked.", sources=sources)
    return ToolResult.success("Facts from tools/memory: " + ". ".join(parts) + f". The wording (and anything else) is "
                              f"from the AI model ({model}), not checked.", sources=sources)


def memory_used(_: NoArgs, ctx: ToolContext) -> ToolResult:
    manager = _memory(ctx)
    used = list(getattr(manager, "last_used", []) or [])
    if not used:
        return ToolResult.success("I didn't use any saved memories for my last answer.", memories=[])
    lines = "; ".join(f"#{i} \"{text}\" ({why})" for i, text, why in used[:12])
    return ToolResult.success(f"For my last answer I had these saved memories: {lines}. They are what you told me "
                              "earlier, not something I just checked.",
                              memories=[{"id": i, "content": t, "why": w} for i, t, w in used])


def what_do_you_know(args: KnowArgs, ctx: ToolContext) -> ToolResult:
    from app.core.world import describe

    return ToolResult.success(describe(ctx, args.about))


def what_can_you_do(_: NoArgs, ctx: ToolContext) -> ToolResult:
    """Capability discovery from the tools that are really registered (never a made-up list)."""
    registry = ctx.executor.registry if ctx.executor is not None else None
    if registry is None:
        from app.tools.catalog import build_default_registry

        registry = build_default_registry()
    groups: dict[str, list[str]] = {}
    for tool in registry.tools:
        groups.setdefault(tool.category, []).append(tool.name.replace("_", " "))
    names = {"apps": "apps and windows", "files": "files", "windows": "Windows settings, volume, brightness, power",
             "system": "system status and processes", "web": "websites and search", "browser": "my own browser",
             "info": "weather, news, music, briefing", "memory": "memory", "automation": "reminders, workflows, "
             "goals and missions", "dev": "developer tools", "vision": "the screen", "screen": "mouse and keyboard",
             "phone": "calls, texts and WhatsApp", "home": "smart home", "office": "Outlook email and calendar",
             "todo": "to-do list and focus", "notes": "meeting notes", "voice": "my own settings by voice",
             "clipboard": "the clipboard", "basic": "time, maths, undo"}
    parts = [f"{names.get(cat, cat.replace('_', ' '))} ({len(tools)})" for cat, tools in sorted(groups.items())]
    return ToolResult.success(f"I have {len(registry.tools)} tools: " + ", ".join(parts) + ". Everything that "
                              "changes something goes through the permission checks.",
                              groups={k: sorted(v) for k, v in groups.items()})


def forget_everything(_: NoArgs, ctx: ToolContext) -> ToolResult:
    count = _memory(ctx).clear()
    return ToolResult.success(f"Cleared all {count} saved fact(s).", cleared=count)


TOOLS: tuple[Tool, ...] = (
    Tool("remember", "Save a fact ONLY when the user explicitly asks you to remember something. "
         "Never for passwords or other secrets.", remember, RememberArgs, RiskLevel.SAFE, "memory"),
    Tool("recall", "Look up what the user asked you to remember ('what do you remember about me?').",
         recall, RecallArgs, RiskLevel.SAFE, "memory"),
    Tool("forget", "Delete one saved fact when the user asks you to forget it ('forget that', 'forget my project folder').",
         forget, ForgetArgs, RiskLevel.SAFE, "memory"),
    Tool("memory_conflicts", "Memories that may contradict each other (e.g. 'uses MongoDB' vs 'uses PostgreSQL').",
         memory_conflicts, NoArgs, RiskLevel.SAFE, category="memory"),
    Tool("resolve_memory_conflict", "Settle a memory contradiction: keep the new one, the old one, or both.",
         resolve_memory_conflict, ConflictArgs, RiskLevel.SAFE, category="memory"),
    Tool("explain_memory", "Why Jarvis remembers something and where it came from ('why do you remember that?', "
         "'where did you learn my project folder?').", explain_memory, ExplainArgs, RiskLevel.SAFE, "memory"),
    Tool("memory_admin", "Memory housekeeping: list categories, export all memories to a file, stop or resume using "
         "a whole category, or delete a category.", memory_admin, MemoryAdminArgs, RiskLevel.SAFE, "memory",
         precheck=_admin_precheck, risk=_admin_risk, confirmation=_admin_question),
    Tool("manage_memory", "Control one memory: stop using it, use it again, make it private, let it expire, or show "
         "its earlier versions.", manage_memory, ManageMemoryArgs, RiskLevel.SAFE, "memory"),
    Tool("answer_sources", "Where the last answer's facts came from: which tools ran (and whether their result was "
         "checked), which saved memories matched, and what was only the AI's wording ('where did you get that?', "
         "'is that verified?').", answer_sources, NoArgs, RiskLevel.SAFE, "memory"),
    Tool("memory_used", "Which saved memories Jarvis gave the AI for its last answer, and why each one ('why did you "
         "mention that?', 'what do you remember that you used just now?').", memory_used, NoArgs, RiskLevel.SAFE,
         "memory"),
    Tool("what_do_you_know", "What Jarvis knows about the user's environment - projects (and their stack), goals, "
         "the unfinished mission, most-used apps, contacts' names, layouts, nicknames - and where each fact comes from.",
         what_do_you_know, KnowArgs, RiskLevel.SAFE, "memory"),
    Tool("what_can_you_do", "List Jarvis's real capabilities (the registered tools, by area).", what_can_you_do,
         NoArgs, RiskLevel.SAFE, "memory"),
    Tool("forget_everything", "Delete ALL saved facts. Only when the user asks to forget everything; always asks first.",
         forget_everything, NoArgs, RiskLevel.DANGEROUS, "memory",
         confirmation=lambda a, c: f"Delete all {len(c.get_memory().all(10_000))} saved memories? This can't be undone."),
)
