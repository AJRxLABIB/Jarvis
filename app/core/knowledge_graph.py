"""The personal knowledge graph: projects, files, goals, missions, decisions, memories, your requests and what
Jarvis did - linked, and answerable with evidence.

Built on demand from what Jarvis already has (nothing new is collected, nothing is stored twice):

* projects you told Jarvis about -> their stack and dependencies (project files), recently changed files
  (``git log``, fixed arguments), key files (entry points), notes you added;
* memories (Memory 2.0) that mention a project or a technology -> "about" links; memories that replaced others ->
  decisions ("switched from MongoDB to PostgreSQL");
* goals and missions -> linked to the projects their titles name;
* your requests (agent runs) and Jarvis's actions (audit log) -> what you worked on, when.

Every answer lists where each item comes from. Nothing is inferred beyond word matches and the recorded links;
when nothing matches, the answer says so.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.utils.logging_setup import get_logger

_log = get_logger("core.graph")

DECISION_WORDS = re.compile(r"\b(?:decid\w*|decision|chose|chosen|choose|picked|going\s+with|went\s+with|switch\w*|"
                            r"migrat\w*|moved\s+to|instead\s+of|because|we\s+use|i\s+use|use\s+\w+\s+for)\b", re.I)
_WORDS = re.compile(r"[a-z0-9][a-z0-9.+#_-]{1,}")


@dataclass
class Node:
    id: str
    kind: str  # project, file, goal, mission, memory, decision, request, technology
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class Edge:
    source: str
    relation: str  # uses, contains, about, related_to, replaced, worked_on
    target: str
    evidence: str = ""


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def add(self, node: Node) -> Node:
        return self.nodes.setdefault(node.id, node)

    def link(self, source: str, relation: str, target: str, evidence: str = "") -> None:
        if source in self.nodes and target in self.nodes and source != target:
            self.edges.append(Edge(source, relation, target, evidence))

    def neighbours(self, node_id: str) -> list[tuple[Edge, Node]]:
        out = []
        for edge in self.edges:
            if edge.source == node_id and edge.target in self.nodes:
                out.append((edge, self.nodes[edge.target]))
            elif edge.target == node_id and edge.source in self.nodes:
                out.append((edge, self.nodes[edge.source]))
        return out

    def find(self, text: str, kinds: tuple[str, ...] | None = None) -> list[Node]:
        """Nodes whose name matches ``text`` (whole name first, then shared words)."""
        wanted = " ".join(text.lower().split()).strip(" ?.!")
        candidates = [n for n in self.nodes.values() if kinds is None or n.kind in kinds]
        exact = [n for n in candidates if n.name.lower() == wanted]
        if exact:
            return exact
        words = set(_WORDS.findall(wanted)) - {"the", "my", "project", "our", "this", "that"}
        scored = []
        for node in candidates:
            hits = len(words & set(_WORDS.findall(node.name.lower())))
            if hits:
                scored.append((hits, node))
        scored.sort(key=lambda item: -item[0])
        return [n for _, n in scored]


def _mentions(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", text.lower()) is not None


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ building


def recent_files(folder: Path, ctx, days: int = 14, limit: int = 15) -> list[tuple[str, int]]:
    """Files changed in the last ``days`` days, most-changed first (git log, fixed arguments)."""
    if not (folder / ".git").exists():
        return []
    try:
        from app.tools.developer import _executor

        result = _executor(ctx).run(["git", "log", f"--since={days}.days", "--name-only", "--pretty=format:"],
                                    folder, timeout=20)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    counts = Counter(line.strip() for line in result.output.splitlines() if line.strip()
                     and not line.startswith("...("))
    return counts.most_common(limit)


def build(ctx, *, with_files: bool = True) -> Graph:
    graph = Graph()
    projects: list[Node] = []
    try:
        from app.core.projects import known_project_folders, notes_for, _dependencies, _entry_points
        from app.core.world import project_stack

        for folder in known_project_folders(ctx):
            node = graph.add(Node(f"project:{folder.name.lower()}", "project", folder.name,
                                  {"folder": str(folder)}, "a project you told me about"))
            projects.append(node)
            for tech in project_stack(folder) + _dependencies(folder)[:25]:
                tech_node = graph.add(Node(f"tech:{tech.lower()}", "technology", tech, {}, "the project's files"))
                graph.link(node.id, "uses", tech_node.id, f"{folder.name}'s project files")
            for entry in _entry_points(folder)[:5]:
                file_node = graph.add(Node(f"file:{folder.name.lower()}/{entry}", "file", entry,
                                           {"project": folder.name}, "the project's entry points"))
                graph.link(node.id, "contains", file_node.id, "entry point")
            if with_files:
                for path, changes in recent_files(folder, ctx):
                    file_node = graph.add(Node(f"file:{folder.name.lower()}/{path}", "file", path,
                                               {"project": folder.name, "changes": changes}, "git log (14 days)"))
                    graph.link(node.id, "contains", file_node.id, f"changed {changes} time(s) in 14 days (git log)")
            for i, note in enumerate(notes_for(folder, ctx)):
                note_node = graph.add(Node(f"note:{folder.name.lower()}:{i}", "decision" if DECISION_WORDS.search(note)
                                           else "note", note, {"project": folder.name}, "a project note you added"))
                graph.link(note_node.id, "about", node.id, "project note")
    except Exception:
        _log.warning("graph: projects unreadable", exc_info=True)
    if ctx.database is None:
        return graph
    _memories(ctx, graph, projects)
    _goals(ctx, graph, projects)
    return graph


def _memories(ctx, graph: Graph, projects: list[Node]) -> None:
    try:
        from app.memory.manager import MemoryManager

        manager = MemoryManager(ctx.database)
        technologies = [n for n in graph.nodes.values() if n.kind == "technology"]
        for memory in manager.all(500, include_hidden=True):
            if memory.disabled or memory.sensitivity == "private":
                continue
            kind = "decision" if (DECISION_WORDS.search(memory.content) or memory.superseded_by) else "memory"
            node = graph.add(Node(f"memory:{memory.id}", kind, memory.content,
                                  {"id": memory.id, "saved": (memory.created_at or memory.updated_at)[:10],
                                   "current": memory.superseded_by is None, "type": memory.kind},
                                  f"memory #{memory.id}"))
            for project in projects:
                if _mentions(memory.content, project.name):
                    graph.link(node.id, "about", project.id, f"memory #{memory.id} names {project.name}")
            from app.memory.memory2 import mentions

            for group in mentions(memory.content).values():
                for tech in group:
                    tech_node = graph.add(Node(f"tech:{tech}", "technology", tech, {}, "your memories"))
                    graph.link(node.id, "about", tech_node.id, f"memory #{memory.id} names {tech}")
            for tech in technologies:
                if _mentions(memory.content, tech.name):
                    graph.link(node.id, "about", tech.id, f"memory #{memory.id} names {tech.name}")
            if memory.superseded_by:
                graph.link(f"memory:{memory.superseded_by}", "replaced", node.id, "Memory 2.0: the newer one replaced it")
    except Exception:
        _log.warning("graph: memories unreadable", exc_info=True)


def _goals(ctx, graph: Graph, projects: list[Node]) -> None:
    try:
        with ctx.database.connect() as conn:
            goals = conn.execute("SELECT id, title, status, progress, deadline, updated_at FROM goals").fetchall()
            missions = conn.execute("SELECT id, title, state, updated_at FROM missions").fetchall()
        for goal in goals:
            node = graph.add(Node(f"goal:{goal['id']}", "goal", goal["title"],
                                  {"status": goal["status"], "progress": goal["progress"], "due": goal["deadline"],
                                   "updated": goal["updated_at"]}, "your goals"))
            for project in projects:
                if _mentions(goal["title"], project.name):
                    graph.link(node.id, "about", project.id, "the goal's title names it")
        for mission in missions:
            node = graph.add(Node(f"mission:{mission['id']}", "mission", mission["title"],
                                  {"state": mission["state"], "updated": mission["updated_at"]}, "Mission Control"))
            for project in projects:
                if _mentions(mission["title"], project.name):
                    graph.link(node.id, "about", project.id, "the mission's title names it")
    except Exception:
        _log.warning("graph: goals unreadable", exc_info=True)


# ------------------------------------------------------------------ questions


def files_of(graph: Graph, name: str) -> tuple[str, list[str]]:
    found = graph.find(name, ("project",))
    if not found:
        return f"I don't know a project called {name}.", []
    project = found[0]
    lines = [f"{node.name} ({edge.evidence})" for edge, node in graph.neighbours(project.id)
             if edge.relation == "contains" and node.kind == "file"]
    if not lines:
        return f"I know {project.name}, but no files for it yet (no git history in the last 14 days).", []
    return f"Files of {project.name}: " + "; ".join(lines[:12]) + ".", lines


def decisions_about(graph: Graph, topic: str) -> tuple[str, list[str]]:
    from app.memory.memory2 import GROUPS, mentions

    targets = {t.id for t in graph.find(topic, ("project", "technology"))}
    lowered = topic.lower()
    groups = {g for g in GROUPS if g in lowered or g.split()[0] in lowered}  # "the database", "my editor"
    words = set(_WORDS.findall(lowered)) - {"the", "my", "our", "about", "project"} - {w for g in groups for w in g.split()}
    lines = []
    for node in graph.nodes.values():
        if node.kind != "decision":
            continue
        linked = any((edge.source in targets or edge.target in targets) for edge, _ in graph.neighbours(node.id))
        shared = bool(words & set(_WORDS.findall(node.name.lower())))
        in_group = bool(groups & set(mentions(node.name)))
        if not (linked or shared or in_group):
            continue
        state = "" if node.attrs.get("current", True) else " (no longer true)"
        when = f", {node.attrs['saved']}" if node.attrs.get("saved") else ""
        lines.append(f"\"{node.name}\"{state} ({node.source}{when})")
    if not lines:
        return f"I have no recorded decision about {topic}.", []
    return f"About {topic}: " + "; ".join(lines[:8]) + ".", lines


def project_with(graph: Graph, thing: str, ctx=None) -> tuple[str, list[str]]:
    lines = []
    tech = graph.find(thing, ("technology",))
    for node in tech[:1]:
        for edge, other in graph.neighbours(node.id):
            if other.kind == "project":
                lines.append(f"{other.name} ({edge.evidence})")
    wanted = thing.strip().lower()
    for node in graph.nodes.values():
        if node.kind == "file" and wanted in node.name.lower():
            lines.append(f"{node.attrs.get('project')} (file {node.name})")
    for node in graph.nodes.values():
        if node.kind in ("memory", "decision") and _mentions(node.name, thing):
            for _edge, other in graph.neighbours(node.id):
                if other.kind == "project":
                    lines.append(f"{other.name} ({node.source}: \"{node.name}\")")
    if ctx is not None and not lines:  # a file name: look inside the projects themselves
        try:
            from app.core.projects import code_files

            for project in (n for n in graph.nodes.values() if n.kind == "project"):
                for path in code_files(Path(project.attrs["folder"]), limit=3000):
                    if wanted in path.name.lower() or wanted in str(path).lower().replace("\\", "/"):
                        lines.append(f"{project.name} (file {path.name})")
                        break
        except Exception:
            pass
    unique = list(dict.fromkeys(lines))
    if not unique:
        return f"None of your projects has anything I can connect to {thing}.", []
    return f"{thing}: " + "; ".join(unique[:8]) + ".", unique


def worked_on(graph: Graph, ctx, start: datetime, end: datetime, label: str) -> tuple[str, list[str]]:
    projects = [n for n in graph.nodes.values() if n.kind == "project"]
    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}
    lo, hi = _utc(start), _utc(end)
    with ctx.database.connect() as conn:
        runs = conn.execute("SELECT text, started_at FROM agent_runs WHERE started_at >= ? AND started_at < ?",
                            (lo, hi)).fetchall()
        audits = conn.execute("SELECT details FROM audit_log WHERE event = 'tool_result' AND ts >= ? AND ts < ?",
                              (lo, hi)).fetchall()
        goals = conn.execute("SELECT title, progress FROM goals WHERE updated_at >= ? AND updated_at < ?",
                             (lo, hi)).fetchall()
        missions = conn.execute("SELECT title, state FROM missions WHERE updated_at >= ? AND updated_at < ?",
                                (lo, hi)).fetchall()
    for run in runs:
        for project in projects:
            if _mentions(run["text"] or "", project.name):
                counts[project.name] += 1
                evidence.setdefault(project.name, []).append("your requests")
    for audit in audits:
        try:
            arguments = json.dumps(json.loads(audit["details"] or "{}").get("arguments", {})).lower()
        except ValueError:
            continue
        for project in projects:
            folder = project.attrs.get("folder", "").lower().replace("\\", "\\\\")
            if (folder and folder in arguments) or _mentions(arguments, project.name):
                counts[project.name] += 1
                evidence.setdefault(project.name, []).append("Jarvis's actions")
    lines = [f"{name} ({count} request(s)/action(s): {', '.join(sorted(set(evidence[name])))})"
             for name, count in counts.most_common(6)]
    lines += [f"goal \"{g['title']}\" ({g['progress']}%)" for g in goals[:4]]
    lines += [f"mission \"{m['title']}\" ({m['state']})" for m in missions[:4]]
    if not lines:
        return (f"I have no record of what you worked on {label}. I only know what you did through me (requests, "
                "actions, goals, missions) - not work you did by hand.", [])
    return f"{label.capitalize()} you worked on: " + "; ".join(lines) + ".", lines


def related(graph: Graph, name: str) -> tuple[str, list[str]]:
    found = graph.find(name)
    if not found:
        return f"I don't have anything connected to {name}.", []
    node = found[0]
    lines = [f"{edge.relation.replace('_', ' ')} {other.kind} \"{other.name}\" ({edge.evidence})"
             for edge, other in graph.neighbours(node.id)]
    if not lines:
        return f"I know \"{node.name}\" ({node.source}) but nothing linked to it.", []
    return f"{node.name} ({node.kind}): " + "; ".join(lines[:10]) + ".", lines


_FILES_Q = re.compile(r"(?:which|what)\s+files\s+(?:are\s+)?(?:related\s+to|in|of|for|belong\s+to|are\s+in|"
                      r"does)\s+(?:the\s+|my\s+)?(?P<x>.+?)(?:\s+project)?(?:\s+have)?[?.!]*$", re.I)
_DECISION_Q = re.compile(r"(?:what|which)\s+decisions?\s+(?:did\s+(?:i|we)\s+make\s+|have\s+(?:i|we)\s+made\s+|"
                         r"were\s+made\s+|was\s+made\s+)?(?:about|on|for|regarding)\s+(?:the\s+|my\s+)?(?P<x>.+?)"
                         r"[?.!]*$|why\s+did\s+(?:i|we)\s+(?:choose|pick|use|switch\s+to)\s+(?P<y>.+?)[?.!]*$", re.I)
_CONTAINS_Q = re.compile(r"(?:which|what)\s+projects?\s+(?:contains?|uses?|has|have|is\s+using|includes?)\s+(?:the\s+)?"
                         r"(?P<x>.+?)[?.!]*$", re.I)
_WORKED_Q = re.compile(r"what\s+(?:was|were|have)\s+i\s+(?:been\s+)?working\s+on(?P<when>.*?)[?.!]*$|what\s+did\s+i\s+"
                       r"work\s+on(?P<when2>.*?)[?.!]*$", re.I)
_RELATED_Q = re.compile(r"(?:what(?:'s|\s+is)?|everything|show\s+me\s+everything)\s+(?:is\s+)?(?:related|connected|"
                        r"linked)\s+to\s+(?:the\s+|my\s+)?(?P<x>.+?)[?.!]*$|find\s+everything\s+(?:related\s+to|about)\s+"
                        r"(?:the\s+|my\s+)?(?P<y>.+?)[?.!]*$", re.I)


# ------------------------------------------------------------------ "what is blocking...", "what is left", "what keeps failing"


def blockers(ctx, name: str) -> tuple[str, list[str]]:
    """What is holding a project (or anything named) back: paused / failed missions that name it, goals not done and past
    due, open to-dos that are overdue or waiting on another item, and the known problems you noted."""
    lines: list[str] = []
    today = ctx.clock().date().isoformat()
    try:
        from app.core.projects import known_project_folders, notes_for

        for folder in known_project_folders(ctx):
            if _mentions(name, folder.name) or _mentions(folder.name, name):
                lines += [f"known problem (your note): {note}" for note in notes_for(folder, ctx)]
    except Exception:
        _log.warning("blockers: project notes unreadable", exc_info=True)
    try:
        with ctx.database.connect() as conn:
            for m in conn.execute("SELECT title, state, note FROM missions WHERE state IN ('paused', 'failed', 'waiting')"):
                if _mentions(m["title"], name):
                    lines.append(f"mission \"{m['title']}\" is {m['state']}" + (f": {m['note']}" if m["note"] else "")
                                 + " (Mission Control)")
            for g in conn.execute("SELECT title, status, deadline FROM goals WHERE status NOT IN ('done', 'completed', 'dropped')"):
                if _mentions(g["title"], name) and g["deadline"] and g["deadline"] < today:
                    lines.append(f"goal \"{g['title']}\" is past its deadline ({g['deadline']}) (your goals)")
            todos = {r["id"]: dict(r) for r in conn.execute("SELECT id, text, due, depends_on FROM todos WHERE done = 0")}
        for item in todos.values():
            if not _mentions(item["text"], name):
                continue
            if item["depends_on"] in todos:
                lines.append(f"to-do \"{item['text']}\" waits for \"{todos[item['depends_on']]['text']}\" (your to-do list)")
            elif item["due"] and item["due"] < today:
                lines.append(f"to-do \"{item['text']}\" is overdue (due {item['due']}) (your to-do list)")
    except Exception:
        _log.warning("blockers: data unreadable", exc_info=True)
    if not lines:
        return (f"I don't see anything blocking {name}: no paused mission, overdue goal or waiting to-do mentions it. "
                "That only covers what I track; I can't see work done by hand."), []
    return f"{len(lines)} thing(s) are holding back {name}: " + "; ".join(lines[:8]) + ".", lines


def unfinished(ctx) -> tuple[str, list[str]]:
    """What remains from your last work: unfinished missions (and their open steps), overdue / due to-dos, open conflicts."""
    lines: list[str] = []
    today = ctx.clock().date().isoformat()
    try:
        from app.core.missions import MissionStore

        for mission in MissionStore(ctx.database).recent(10, ("planned", "running", "paused", "failed", "verifying")):
            open_steps = [st for st in mission.steps if st.state not in ("done", "skipped", "undone")]
            lines.append(f"mission \"{mission.title}\" ({mission.state}): {len(open_steps)} step(s) left"
                         + (f", next: {open_steps[0].title}" if open_steps else ""))
        with ctx.database.connect() as conn:
            for t in conn.execute("SELECT text, due FROM todos WHERE done = 0 AND due IS NOT NULL AND due <= ? "
                                  "ORDER BY due LIMIT 8", (today,)):
                lines.append(f"to-do \"{t['text']}\" (due {t['due']})")
    except Exception:
        _log.warning("unfinished: data unreadable", exc_info=True)
    if not lines:
        return "Nothing is left open that I know of: no unfinished mission and nothing overdue.", []
    return f"{len(lines)} thing(s) are still open: " + "; ".join(lines[:8]) + ".", lines


def recurring_failures(ctx, days: int = 14) -> tuple[str, list[str]]:
    """The tool + error that failed most often recently (from the audit log), so a pattern can be fixed once."""
    since = _utc(ctx.clock() - timedelta(days=days))
    counts: Counter = Counter()
    try:
        with ctx.database.connect() as conn:
            for row in conn.execute("SELECT tool, details FROM audit_log WHERE event = 'tool_result' AND ts >= ?", (since,)):
                try:
                    details = json.loads(row["details"] or "{}")
                except ValueError:
                    continue
                if details.get("ok") is False and details.get("error") not in (None, "declined"):
                    counts[(row["tool"] or "?", details.get("error") or "failed")] += 1
    except Exception:
        _log.warning("failures: audit log unreadable", exc_info=True)
    if not counts:
        return f"No tool failures in the last {days} days that I can see.", []
    lines = [f"{tool}: {error} x{n}" for (tool, error), n in counts.most_common(5)]
    (tool, error), n = counts.most_common(1)[0]
    text = f"The most frequent failure in the last {days} days: {tool} ({error}), {n} time(s)"
    if len(lines) > 1:
        text += "; next: " + "; ".join(lines[1:4])
    return text + ".", lines


def stale_or_conflicting(ctx, older_than_days: int = 180) -> tuple[str, list[str]]:
    """Memories that contradict each other (open conflicts) or haven't been confirmed for a long time."""
    lines: list[str] = []
    try:
        from app.memory.manager import MemoryManager

        manager = MemoryManager(ctx.database)
        for c in manager.conflicts("open")[:5]:
            lines.append(f"conflict: \"{c['old']}\" vs \"{c['new']}\" ({c['reason']}) (memory #{c['old_id']}, #{c['new_id']})")
        cutoff = _utc(ctx.clock() - timedelta(days=older_than_days))
        with ctx.database.connect() as conn:
            for m in conn.execute("SELECT id, content, updated_at FROM memories WHERE disabled = 0 AND superseded_by IS NULL "
                                  "AND kind = 'semantic' AND updated_at < ? ORDER BY updated_at LIMIT 5", (cutoff,)):
                lines.append(f"possibly outdated: \"{m['content'][:80]}\" (last updated {m['updated_at'][:10]}, memory #{m['id']})")
    except Exception:
        _log.warning("stale: memories unreadable", exc_info=True)
    if not lines:
        return "I don't know of any contradictory or outdated memories.", []
    return f"{len(lines)} memory item(s) need a look: " + "; ".join(lines[:6]) + ".", lines


_BLOCKING_Q = re.compile(r"what(?:'s|\s+is|\s+are)\s+blocking\s+(?:the\s+|my\s+)?(?P<x>.+?)(?:\s+project)?[?.!]*$", re.I)
_LEFT_Q = re.compile(r"what\s+(?:remains|is\s+left|is\s+still\s+open|is\s+unfinished|did\s+i\s+leave\s+unfinished)"
                     r"(?:\s+(?:incomplete|unfinished|open))?(?:\s+from\s+(?:my\s+)?(?:last|previous)\s+(?:work\s+)?session)?"
                     r"[?.!]*$", re.I)
_FAILURES_Q = re.compile(r"which\s+(?:recurring\s+)?(?:issue|problem|error)s?\s+(?:has|have)\s+caused\s+the\s+most\s+"
                         r"failures|what\s+(?:keeps|is\s+always)\s+failing|what\s+fails\s+(?:the\s+)?most", re.I)
_STALE_Q = re.compile(r"what\s+(?:information|memories|facts)\s+(?:is|are)\s+(?:outdated|stale|contradict\w+|"
                      r"out\s+of\s+date)(?:\s+or\s+contradict\w+)?", re.I)


def ask(ctx, question: str) -> tuple[str, list[str], str]:
    """(answer, evidence lines, which question it was)."""
    text = " ".join(question.split())
    if (match := _BLOCKING_Q.search(text)) is not None:
        answer, lines = blockers(ctx, match.group("x").strip())
        return answer, lines, "blocking"
    if _LEFT_Q.search(text):
        answer, lines = unfinished(ctx)
        return answer, lines, "unfinished"
    if _FAILURES_Q.search(text):
        answer, lines = recurring_failures(ctx)
        return answer, lines, "failures"
    if _STALE_Q.search(text):
        answer, lines = stale_or_conflicting(ctx)
        return answer, lines, "stale"
    match = _WORKED_Q.search(text)
    if match:
        from app.agent.activity import period

        when = (match.group("when") or match.group("when2") or "").strip() or "this week"
        now = ctx.clock()
        found = period(when, now) or period("this week", now)
        graph = build(ctx, with_files=False)
        answer, lines = worked_on(graph, ctx, found[0], found[1], found[2])
        return answer, lines, "worked_on"
    graph = build(ctx)
    for pattern, kind in ((_FILES_Q, "files"), (_DECISION_Q, "decisions"), (_CONTAINS_Q, "contains"),
                          (_RELATED_Q, "related")):
        match = pattern.search(text)
        if not match:
            continue
        subject = next((g for g in match.groups() if g), "").strip()
        if kind == "files":
            answer, lines = files_of(graph, subject)
        elif kind == "decisions":
            answer, lines = decisions_about(graph, subject)
        elif kind == "contains":
            answer, lines = project_with(graph, subject, ctx)
        else:
            answer, lines = related(graph, subject)
        return answer, lines, kind
    answer, lines = related(graph, text)
    return answer, lines, "related"


def summary(ctx) -> dict[str, int]:
    graph = build(ctx, with_files=False)
    return dict(Counter(n.kind for n in graph.nodes.values()), links=len(graph.edges))


