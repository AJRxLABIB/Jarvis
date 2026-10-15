"""The personal world model: what Jarvis knows about your digital environment, and how it knows it.

Built on demand from sources Jarvis already keeps - never guessed by the AI, never copied into a second
store that could go stale:

* projects   - DEV_PROJECT_DIR, the project folder you told Jarvis to remember, projects you trusted; the
               stack is read from the folder's own files (package.json, manage.py, go.mod, .git...)
* apps       - the apps you open through Jarvis most (last 30 days, usage_events)
* people     - contact names (never their numbers)
* goals, missions, layouts, nicknames - their own stores

Every entity carries its **source** and a **confidence** (1.0 = you said it or it's read from a file;
lower = derived, e.g. "you often use"). Relations link them: project -uses-> Python, goal -about-> project.

``compile_context`` is the context compiler: from all that, it picks only what is relevant to the current
request (word overlap + importance + freshness) within a small budget, and the prompt marks it as facts,
not instructions. Private things (people) never go to the prompt unless the request names them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.utils.logging_setup import get_logger

_log = get_logger("core.world")

STACK_MARKERS = (
    ("package.json", "Node.js"), ("manage.py", "Django"), ("artisan", "Laravel"), ("composer.json", "PHP"),
    ("pyproject.toml", "Python"), ("requirements.txt", "Python"), ("go.mod", "Go"), ("Cargo.toml", "Rust"),
    ("pubspec.yaml", "Flutter"), ("pom.xml", "Java"), ("build.gradle", "Java/Kotlin"), ("*.csproj", ".NET"),
    ("index.php", "PHP"), ("*.sln", ".NET"),
)


@dataclass
class Entity:
    kind: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    confidence: float = 1.0
    private: bool = False

    def line(self) -> str:
        extras = ", ".join(f"{k}: {v}" for k, v in self.attrs.items() if v not in (None, "", [], {}))
        return f"{self.name}" + (f" ({extras})" if extras else "")


@dataclass(frozen=True)
class Relation:
    subject: str
    relation: str
    object: str


def project_stack(folder: Path) -> list[str]:
    found: list[str] = []
    try:
        for marker, stack in STACK_MARKERS:
            hit = any(folder.glob(marker)) if "*" in marker else (folder / marker).exists()
            if hit and stack not in found:
                found.append(stack)
    except OSError:
        pass
    return found


def _projects(ctx) -> list[Entity]:
    from app.tools import developer

    seen: dict[str, Entity] = {}

    def add(path: str | Path | None, source: str) -> None:
        if not path:
            return
        folder = Path(path)
        key = str(folder).lower()
        if key in seen or not folder.is_dir():
            return
        attrs = {"folder": str(folder), "stack": ", ".join(project_stack(folder)),
                 "git": "yes" if (folder / ".git").exists() else ""}
        seen[key] = Entity("project", folder.name, attrs, source)

    add(ctx.settings.dev_project_dir, "your DEV_PROJECT_DIR setting")
    try:
        add(developer._remembered_project(ctx), "you told me your project folder")
    except Exception:
        pass
    try:
        for path in sorted(developer.trusted_projects(ctx))[:10]:
            add(path, "you trusted it for running code")
    except Exception:
        pass
    return list(seen.values())


def _apps(ctx) -> list[Entity]:
    if ctx.database is None:
        return []
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    with ctx.database.connect() as conn:
        rows = conn.execute("SELECT name, COUNT(*) AS n FROM usage_events WHERE day >= ? GROUP BY name "
                            "ORDER BY n DESC LIMIT 8", (since,)).fetchall()
    return [Entity("app", r["name"], {"opened": f"{r['n']} times in 30 days"}, "apps you opened through me", 0.8)
            for r in rows]


def _people(ctx) -> list[Entity]:
    try:
        from app.tools.phone import load_contacts

        return [Entity("person", name, {}, "your contacts", 1.0, private=True) for name in sorted(load_contacts(ctx))[:30]]
    except Exception:
        return []


def _goals_and_missions(ctx) -> list[Entity]:
    if ctx.database is None:
        return []
    from datetime import date

    from app.core.goals import GoalStore
    from app.core.missions import MissionStore

    found = []
    try:
        today = date.today()
        for goal in GoalStore(ctx.database).active():
            left = goal.days_left(today)
            found.append(Entity("goal", goal.title, {"progress": f"{goal.progress}%",
                                                      "due": goal.deadline or "", "days_left": left if left is not None else ""},
                                "you set it", 1.0))
        mission = MissionStore(ctx.database).unfinished()
        if mission is not None:
            found.append(Entity("mission", mission.title, {"state": mission.state, "done": mission.progress(),
                                                           "note": mission.note}, "a mission you approved", 1.0))
    except Exception:
        _log.warning("could not read goals / missions", exc_info=True)
    return found


def _layouts_and_nicknames(ctx) -> list[Entity]:
    found = []
    try:
        from app.tools.layouts import load_layouts

        found += [Entity("layout", name, {}, "you saved it") for name in sorted(load_layouts(ctx))]
    except Exception:
        pass
    try:
        from app.agent.aliases import AliasStore

        data_dir = ctx.paths.data_dir if ctx.paths is not None else None
        for name, target in list(AliasStore(data_dir).items().items())[:20]:
            found.append(Entity("nickname", name, {"means": target.get("target", "")}, "you named it"))
    except Exception:
        pass
    return found


def _facts(ctx) -> list[Entity]:
    if ctx.database is None:
        return []
    from app.memory.manager import MemoryManager

    return [Entity("fact", m.content, {}, "you asked me to remember", 1.0, private=m.sensitivity == "private")
            for m in MemoryManager(ctx.database).all(20)]


def collect(ctx) -> list[Entity]:
    entities: list[Entity] = []
    for source in (_projects, _goals_and_missions, _apps, _layouts_and_nicknames, _people, _facts):
        try:
            entities += source(ctx)
        except Exception:
            _log.warning("world model source failed: %s", source.__name__, exc_info=True)
    return entities


def relations(entities: list[Entity]) -> list[Relation]:
    found: list[Relation] = []
    projects = [e for e in entities if e.kind == "project"]
    for project in projects:
        for stack in filter(None, (project.attrs.get("stack") or "").split(", ")):
            found.append(Relation(project.name, "uses", stack))
    for goal in (e for e in entities if e.kind == "goal"):
        words = set(re.findall(r"\w{3,}", goal.name.lower()))
        for project in projects:
            if project.name.lower() in words or words & set(re.findall(r"\w{3,}", project.name.lower())):
                found.append(Relation(goal.name, "about", project.name))
    return found


KIND_TITLES = {"fact": "Facts", "project": "Projects", "goal": "Goals", "mission": "Unfinished mission", "app": "Apps you use most",
               "person": "People", "layout": "Window layouts", "nickname": "Nicknames"}


def describe(ctx, kind: str | None = None) -> str:
    entities = [e for e in collect(ctx) if kind in (None, "everything", e.kind)]
    if not entities:
        return "I don't know anything about that yet."
    parts = []
    for k, title in KIND_TITLES.items():
        group = [e for e in entities if e.kind == k]
        if k == "fact":
            group = [e for e in group if not e.private]  # private ones only when asked for directly (recall)
        if group:
            source = group[0].source
            parts.append(f"{title} ({source}): " + "; ".join(e.line() for e in group[:8]) + ".")
    links = relations(entities)
    if links and kind in (None, "everything", "project", "goal"):
        parts.append("Links: " + "; ".join(f"{r.subject} {r.relation} {r.object}" for r in links[:8]) + ".")
    return " ".join(parts)


# ------------------------------------------------------------------ context compiler


IMPORTANCE = {"mission": 3.0, "goal": 2.0, "project": 1.5, "layout": 0.5, "app": 0.5, "nickname": 0.3, "person": 0.2}
_TRIGGERS = {"project": ("project", "code", "run", "test", "build", "commit", "git", "repo", "folder", "bug", "error"),
             "goal": ("goal", "deadline", "finish", "plan", "progress", "month", "week"),
             "mission": ("mission", "continue", "prepare", "task", "step"),
             "app": ("open", "app", "usual"), "layout": ("layout", "window", "screen")}


def compile_context(ctx, request: str, budget_chars: int = 600) -> str:
    """The few facts about your environment that matter for this request (or "" when none do)."""
    words = set(re.findall(r"\w{3,}", (request or "").lower()))
    scored: list[tuple[float, Entity]] = []
    for entity in collect(ctx):
        if entity.kind == "fact":
            continue  # remembered facts have their own section in the prompt
        overlap = len(words & set(re.findall(r"\w{3,}", entity.name.lower())))
        triggered = any(t in words or any(w.startswith(t) for w in words) for t in _TRIGGERS.get(entity.kind, ()))
        if entity.private and not overlap:
            continue  # people only when the request names them
        urgent = entity.kind == "goal" and isinstance(entity.attrs.get("days_left"), int) and entity.attrs["days_left"] <= 3
        score = overlap * 3 + (2 if triggered else 0) + (IMPORTANCE.get(entity.kind, 0) if triggered or urgent else 0)
        if entity.kind == "mission":
            score += 1.0  # an unfinished mission is always worth knowing about
        if urgent:
            score += 1.5
        if score >= 2:
            scored.append((score * entity.confidence, entity))
    scored.sort(key=lambda item: -item[0])
    lines, used = [], 0
    for _, entity in scored:
        line = f"- {entity.kind}: {entity.line()}"
        if used + len(line) > budget_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def as_json(ctx) -> str:
    entities = collect(ctx)
    return json.dumps({"entities": [vars(e) for e in entities if not e.private],
                       "relations": [vars(r) for r in relations(entities)]}, ensure_ascii=False, default=str)
