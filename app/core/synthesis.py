"""Personal knowledge synthesis: a local research brief, gap detection and proposed (never confirmed) relationships.

Everything is read from what is already on this PC - memories, recorded decisions, indexed documents and the knowledge
graph - so it works offline and says so. A brief lists findings WITH their source (memory #, document file and chunk,
decision #, graph link), marks old information as possibly outdated instead of dropping or rewriting it, shows
contradictions between findings side by side, and names what is unresolved. Nothing is inferred into a "fact": a proposed
relationship stays a proposal with the references that suggested it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.goal_contract import tokens

STALE_DAYS = 180
SNIPPET = 220
_GENERIC = {"does", "do", "how", "what", "which", "when", "where", "why", "who", "work", "works", "is", "are", "was", "about", "tell", "explain", "show", "find"}
_README = ("readme", "readme.md", "readme.txt", "readme.rst")


@dataclass(frozen=True)
class Finding:
    text: str
    source: str                          # e.g. "memory #4", "notes.pdf #2", "decision #1", "graph"
    kind: str                            # memory | document | decision | graph
    date: str = ""
    flags: tuple[str, ...] = ()          # "possibly outdated - verify", "replaced by a newer memory"
    hits: int = 0


@dataclass
class Brief:
    question: str
    findings: list[Finding] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    note: str = "Built only from local information on this PC; nothing was looked up online."

    def describe(self) -> str:
        lines = [f"Question: {self.question}", self.note]
        if self.findings:
            lines.append("Findings:")
            lines += [f"  - {f.text}  [{f.source}{', ' + f.date if f.date else ''}]" + (f"  ({'; '.join(f.flags)})" if f.flags else "")
                      for f in self.findings]
        if self.conflicts:
            lines.append("Conflicting information:")
            lines += [f"  - {c}" for c in self.conflicts]
        if self.unresolved:
            lines.append("Unresolved:")
            lines += [f"  - {u}" for u in self.unresolved]
        if self.next_actions:
            lines.append("Suggested next actions:")
            lines += [f"  - {a}" for a in self.next_actions]
        return "\n".join(lines)


def _hits(text: str, wanted: set[str]) -> int:
    return len(wanted & tokens(text))


def _age_flags(stamp: str, now: datetime, days: int) -> tuple[str, ...]:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when > timedelta(days=days):
            return (f"possibly outdated: last updated {stamp[:10]} - verify before relying on it",)
    except ValueError:
        pass
    return ()


def _snippet(text: str, wanted: set[str]) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    start = min((low.find(w) for w in wanted if w in low), default=0)
    start = max(0, start - 60)
    return ("..." if start else "") + flat[start:start + SNIPPET] + ("..." if len(flat) > start + SNIPPET else "")


def build_brief(ctx: Any, question: str, limit: int = 6, now: datetime | None = None, stale_days: int = STALE_DAYS) -> Brief:
    now = now or datetime.now(timezone.utc)
    wanted = tokens(question)
    brief = Brief(question=" ".join(question.split()))
    if not wanted or ctx.database is None:
        brief.unresolved.append("Ask a specific question (a project, a technology, a decision).")
        return brief
    found: list[Finding] = []
    memories: list[Any] = []
    try:
        from app.memory.manager import MemoryManager

        for m in MemoryManager(ctx.database).all(500, include_hidden=True):
            if m.disabled or m.sensitivity == "private":
                continue
            score = _hits(f"{m.content} {m.topic or ''}", wanted)
            if score:
                flags = (("replaced by a newer memory",) if m.superseded_by else ()) + _age_flags(m.updated_at or "", now, stale_days)
                found.append(Finding(m.content, f"memory #{m.id}", "memory", (m.updated_at or "")[:10], flags, score))
                if not m.superseded_by:
                    memories.append(m)
    except Exception:
        brief.unresolved.append("Memories could not be read.")
    try:
        from app.core.decisions import DecisionStore

        for record, _age in DecisionStore(ctx.database).relevant(question, limit=5):
            flags = (() if record.status == "active" else (record.status,)) + _age_flags(record.updated_at, now, stale_days)
            found.append(Finding(record.describe(), f"decision #{record.id}", "decision", record.created_at[:10], flags,
                                 _hits(f"{record.title} {record.chosen}", wanted) + 1))
    except Exception:
        brief.unresolved.append("Decision records could not be read.")
    try:
        with ctx.database.connect() as conn:
            for row in conn.execute("SELECT source, chunk, text, created_at FROM semantic_items WHERE kind = 'document' LIMIT 4000"):
                score = _hits(row["text"], wanted)
                if score:
                    found.append(Finding(_snippet(row["text"], wanted), f"{Path(row['source']).name} #{row['chunk']}", "document",
                                         (row["created_at"] or "")[:10], _age_flags(row["created_at"] or "", now, stale_days * 2), score))
    except Exception:
        brief.unresolved.append("Indexed documents could not be read.")
    try:
        from app.core import knowledge_graph as kg

        graph = kg.build(ctx, with_files=False)
        for node in graph.nodes.values():
            if node.kind in ("project", "technology", "goal", "mission") and _hits(node.name, wanted):
                links = [f"{e.relation} {o.kind} {o.name[:50]}" for e, o in graph.neighbours(node.id)[:4]]
                if links:
                    found.append(Finding(f"{node.name} ({node.kind}): " + "; ".join(links), "knowledge graph", "graph", "", (),
                                         _hits(node.name, wanted)))
    except Exception:
        brief.unresolved.append("The knowledge graph could not be read.")
    found.sort(key=lambda f: (-f.hits, "outdated" in " ".join(f.flags), f.source))
    brief.findings = found[:limit]
    _conflicts(brief, memories, ctx)
    covered = set().union(*(tokens(f.text) for f in found)) if found else set()
    missing = sorted(wanted - covered - _GENERIC)
    if not found:
        brief.unresolved.append("Nothing on this PC mentions this. Online research is a separate step you must ask for.")
    elif missing:
        brief.unresolved.append("No local source mentions: " + ", ".join(missing[:6]))
    if any(f.flags and "outdated" in " ".join(f.flags) for f in brief.findings):
        brief.next_actions.append("Verify the items marked possibly outdated, then update or delete them.")
    if brief.conflicts:
        brief.next_actions.append("Settle the conflicting memories (Memory > Conflicts: keep new, keep old, or both).")
    if not found:
        brief.next_actions.append("Add a note or index the relevant documents, then ask again.")
    return brief


def _conflicts(brief: Brief, memories: list[Any], ctx: Any) -> None:
    try:
        from app.memory.memory2 import conflict

        ordered = sorted(memories, key=lambda m: m.updated_at or "")
        for i, newer in enumerate(ordered):
            for older in ordered[:i]:
                hit = conflict(newer.content, older.content)
                if hit and hit[0] == "contradicts":
                    brief.conflicts.append(f"\"{older.content[:70]}\" (memory #{older.id}) vs \"{newer.content[:70]}\" (memory #{newer.id}): {hit[1]}")
    except Exception:
        pass
    try:
        from app.memory.manager import MemoryManager

        ids = {m.id for m in memories}
        for c in MemoryManager(ctx.database).conflicts("open"):
            if c["old_id"] in ids or c["new_id"] in ids:
                brief.conflicts.append(f"\"{c['old'][:70]}\" (memory #{c['old_id']}) vs \"{c['new'][:70]}\" (memory #{c['new_id']}): {c['reason']} [open conflict]")
    except Exception:
        pass
    brief.conflicts = list(dict.fromkeys(brief.conflicts))[:5]


# ------------------------------------------------------------------ gaps and proposals


def knowledge_gaps(ctx: Any) -> list[str]:
    """Targeted observations, not a defect list: a known project without a README, a recorded decision with neither a
    reason nor evidence. Missing things that nobody needs are not reported."""
    gaps: list[str] = []
    try:
        from app.core.projects import known_project_folders

        for folder in known_project_folders(ctx):
            if folder.exists() and not any(child.name.lower() in _README for child in folder.iterdir() if child.is_file()):
                gaps.append(f"Project {folder.name} has no README, so nothing explains how to run or understand it.")
    except Exception:
        pass
    try:
        from app.core.decisions import DecisionStore

        for r in DecisionStore(ctx.database).list(limit=100):
            if not r.criteria and not r.evidence:
                gaps.append(f"Decision #{r.id} '{r.title}' has no recorded reason or evidence.")
    except Exception:
        pass
    return gaps


def propose_relations(ctx: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Possible links the graph does not have: a memory or decision that names a technology a project uses, without
    naming the project. Each proposal carries the references that suggested it and ``status: proposed`` - it is never added
    to the graph or treated as a fact."""
    from app.core import knowledge_graph as kg

    graph = kg.build(ctx, with_files=False)
    out: list[dict[str, Any]] = []
    linked = {(e.source, e.target) for e in graph.edges}
    for project in (n for n in graph.nodes.values() if n.kind == "project"):
        for edge, tech in graph.neighbours(project.id):
            if edge.relation != "uses" or edge.source != project.id:
                continue
            for e2, other in graph.neighbours(tech.id):
                if other.kind in ("memory", "decision") and e2.relation == "about" and (other.id, project.id) not in linked:
                    out.append({"from": other.id, "to": project.id, "relation": "about", "status": "proposed",
                                "because": f"it mentions {tech.name}, which {project.name} uses",
                                "references": [other.source, edge.evidence]})
    seen, unique = set(), []
    for item in out:
        key = (item["from"], item["to"])
        if key not in seen and item["from"] in graph.nodes and item["to"] in graph.nodes:     # references must exist
            seen.add(key)
            unique.append(item)
    return unique[:limit]
