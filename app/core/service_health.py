"""Dependency-aware health: when ONE part fails, say exactly what still works and what does not, and which recovery level applies.

``GRAPH`` is a fixed table of what each service needs (written from how Jarvis is wired, not guessed at run time). ``impact`` takes the failed
services (from the existing diagnostics checks) and returns, for every service, working / degraded / unavailable with the failed dependency named.
Nothing is repaired here: the recovery ladder is a statement of what level of help applies (1 report, 2 investigate, 3 prepare a repair and ask,
4 an approved reversible repair, 5 verify). Existing ``diagnostics.repair`` only touches Jarvis's own bookkeeping and is level 4 for those items.
"""

from __future__ import annotations

from dataclasses import dataclass

# service -> (what it needs to be fully working, what it needs at minimum, plain label)
GRAPH: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "database": ((), (), "local database (history, memory, settings)"),
    "disk": ((), (), "disk space"),
    "ai_engine": ((), (), "local AI engine (Ollama)"),
    "reminders": (("database",), ("database",), "reminders"),
    "device_monitor": (("database",), (), "PC health monitoring"),
    "phone_link": (("database",), ("database",), "phone link"),
    "notifications": (("database",), ("database",), "notification center"),
    "automations": (("database",), ("database",), "scheduled automations (non-AI steps)"),
    "document_search": (("database",), ("database",), "document search (keyword)"),
    "conversation": (("ai_engine", "database"), ("ai_engine",), "AI conversation"),
    "semantic_search": (("ai_engine", "database"), ("database",), "meaning-based search (falls back to keywords)"),
    "missions": (("database", "ai_engine"), ("database",), "missions (AI planning needs the engine; saved steps do not)"),
    "backups": (("disk", "database"), ("disk",), "backups"),
    "guardian": (("database",), (), "cyber guardian"),
}
CHECK_TO_SERVICE = {"database": "database", "disk": "disk", "ollama": "ai_engine", "ai engine": "ai_engine", "ai": "ai_engine"}
LEVELS = ("1 detect and report", "2 investigate and recommend", "3 prepare a repair and ask", "4 run an approved reversible repair", "5 verify and record")


@dataclass
class State:
    service: str
    label: str
    status: str          # working | degraded | unavailable
    why: str = ""


def failed_from_checks(checks) -> set[str]:
    out = set()
    for c in checks:
        if getattr(c, "status", "ok") != "fail":
            continue
        name = c.name.lower()
        for key, svc in CHECK_TO_SERVICE.items():
            if key in name:
                out.add(svc)
    return out


def impact(failed: set[str]) -> list[State]:
    states = []
    for svc, (full, minimum, label) in GRAPH.items():
        if svc in failed:
            states.append(State(svc, label, "unavailable", "it failed its check"))
            continue
        lost_min = [d for d in minimum if d in failed]
        lost_full = [d for d in full if d in failed]
        if lost_min:
            states.append(State(svc, label, "unavailable", f"needs {', '.join(GRAPH[d][2] for d in lost_min)}"))
        elif lost_full:
            states.append(State(svc, label, "degraded", f"without {', '.join(GRAPH[d][2] for d in lost_full)}"))
        else:
            states.append(State(svc, label, "working"))
    return states


def describe(failed: set[str]) -> str:
    if not failed:
        return "Everything I check is working."
    states = impact(failed)
    group = lambda s: [x for x in states if x.status == s]          # noqa: E731
    lines = ["Failed: " + ", ".join(GRAPH[f][2] for f in sorted(failed) if f in GRAPH) + "."]
    if group("working"):
        lines.append("Still working: " + ", ".join(x.label for x in group("working")) + ".")
    if group("degraded"):
        lines.append("Working with limits: " + "; ".join(f"{x.label} ({x.why})" for x in group("degraded")) + ".")
    if group("unavailable"):
        lines.append("Not available: " + "; ".join(f"{x.label} ({x.why})" for x in group("unavailable")) + ".")
    lines.append("Your task history is not touched by recovery. Recovery steps: " + "; ".join(LEVELS) + ". I stop at level 3 until you approve.")
    return "\n".join(lines)
