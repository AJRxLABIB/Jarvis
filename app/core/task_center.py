"""Background task center: one list of everything Jarvis is doing or has queued - missions, scheduled
automations, self-improvement tests and requests still running - each with one plain status.

Read-only: it looks at the local database (missions, automation_tasks, proposals, agent_runs). Every action
the dialog offers (continue, cancel, roll back) is sent as a normal request, so it goes through the usual
permission and confirmation checks. Nothing here starts, stops or approves anything by itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_APPROVAL", "PAUSED", "RECOVERY_REQUIRED", "COMPLETED", "FAILED",
            "CANCELLED", "ROLLED_BACK")
ACTIVE = frozenset({"QUEUED", "RUNNING", "WAITING_FOR_APPROVAL", "PAUSED", "RECOVERY_REQUIRED"})

_MISSION = {"planned": "QUEUED", "running": "RUNNING", "verifying": "RUNNING", "paused": "PAUSED", "waiting": "WAITING_FOR_APPROVAL",
            "completed": "COMPLETED", "failed": "FAILED", "cancelled": "CANCELLED", "rolled_back": "ROLLED_BACK"}
_PROPOSAL = {"proposed": "QUEUED", "testing": "RUNNING", "tested": "WAITING_FOR_APPROVAL", "failed": "FAILED",
             "applied": "COMPLETED", "rejected": "CANCELLED", "rolled_back": "ROLLED_BACK"}


@dataclass(frozen=True)
class Task:
    kind: str  # mission | automation | improvement | request
    title: str
    status: str
    detail: str = ""
    updated: str = ""
    commands: tuple[str, ...] = ()  # requests the user can send about it (each still asks when it should)

    def line(self) -> str:
        extra = f" - {self.detail}" if self.detail else ""
        return f"[{self.status}] {self.kind}: {self.title}{extra}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["commands"] = list(self.commands)
        return data


def mission_status(mission) -> str:
    if mission.state == "paused" and any(s.state == "waiting" for s in mission.steps):
        return "WAITING_FOR_APPROVAL"
    if mission.state == "paused" and any(s.state == "failed" and (s.result or "").startswith("interrupted")
                                         for s in mission.steps):
        return "RECOVERY_REQUIRED"  # a step was cut off and may already have happened: a person must decide
    return _MISSION.get(mission.state, "PAUSED")


def _missions(database, limit: int) -> list[Task]:
    from app.core.missions import MissionStore

    tasks = []
    for m in MissionStore(database).recent(limit):
        status = mission_status(m)
        commands: tuple[str, ...] = ()
        if status in ("PAUSED", "WAITING_FOR_APPROVAL", "QUEUED", "RECOVERY_REQUIRED"):
            commands = ("continue my mission", "cancel the mission")
        if any(s.state == "done" for s in m.steps) and status not in ("RUNNING", "ROLLED_BACK"):
            commands += ("roll back the mission",)
        detail = f"{m.progress()} steps" + (f", {m.note}" if m.note else "")
        tasks.append(Task("mission", m.title, status, detail, m.updated_at or "", commands))
    return tasks


def _automations(database) -> list[Task]:
    from app.automation.tasks import AutomationStore

    tasks = []
    for a in AutomationStore(database).all():
        if a.is_workflow:  # a saved workflow only runs when asked; it isn't background work
            continue
        status = "QUEUED" if a.enabled else "PAUSED"
        detail = a.trigger.describe()
        if a.next_run_at and a.enabled:
            detail += f", next {a.next_run_at[:16].replace('T', ' ')}"
        if a.last_status:
            detail += f", last run {a.last_status}"
        tasks.append(Task("automation", a.name, status, detail, a.last_run_at or ""))
    return tasks


def _improvements(database, limit: int) -> list[Task]:
    from app.evolution.proposals import ProposalStore

    tasks = []
    for p in ProposalStore(database).list(limit=limit):
        status = _PROPOSAL.get(p.state, "QUEUED")
        commands = (f"test proposal {p.id}",) if p.state == "proposed" and p.edits else ()
        tasks.append(Task("improvement", f"#{p.id} {p.title}", status, p.kind, p.applied_at or p.created_at,
                          commands))
    return tasks


def _requests(database, exclude: str | None) -> list[Task]:
    from app.core.runs import RunStore

    tasks = []
    for r in RunStore(database).recent(20):
        if r.get("state") != "running" or r.get("id") == exclude:
            continue
        tasks.append(Task("request", (r.get("text") or "")[:80] or r.get("source", "request"), "RUNNING",
                          f"from {r.get('source', '?')}", r.get("started_at") or ""))
    return tasks


def collect(database, *, include_finished: bool = True, limit: int = 10) -> list[Task]:
    """Every task, active ones first (then newest). A source that can't be read is left out, not guessed."""
    from app.core import runs

    current = runs.current()
    tasks: list[Task] = []
    for source in (lambda: _requests(database, current.id if current else None),
                   lambda: _missions(database, limit), lambda: _automations(database),
                   lambda: _improvements(database, limit)):
        try:
            tasks.extend(source())
        except Exception:  # a missing table in an old database, say - show what can be shown
            continue
    if not include_finished:
        tasks = [t for t in tasks if t.status in ACTIVE]
    tasks.sort(key=lambda t: t.updated, reverse=True)
    tasks.sort(key=lambda t: t.status not in ACTIVE)
    return tasks


def counts(tasks: list[Task]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[t.status] = out.get(t.status, 0) + 1
    return out


def describe(tasks: list[Task]) -> str:
    if not tasks:
        return "Nothing is running or queued in the background."
    active = [t for t in tasks if t.status in ACTIVE]
    head = (f"{len(active)} active background task(s)" if active else "No active background tasks") + \
        (f", {len(tasks) - len(active)} finished" if len(tasks) > len(active) else "")
    return head + ": " + "; ".join(t.line() for t in tasks[:12]) + "."
