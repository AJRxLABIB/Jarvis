"""The task timeline: a mission turned into rows a person can read - what is done, what is happening, what is next, on
which device, and what went wrong - plus the controls that REALLY apply right now (Cancel / Retry / Review / Undo).

A control is offered only when the operation supports it: Cancel while it can still be stopped, Retry when it paused or
failed, Undo when at least one finished step recorded how to be undone, Review always (it opens the full record).
"""

from __future__ import annotations

from app.ui.styles.tokens import COLOR

from dataclasses import dataclass, field

# step state -> (label, glyph): the glyph is a shape, so state never depends on colour alone
STEP_LOOK = {
    "done": ("Done", "✔"), "running": ("Running", "▶"), "pending": ("Waiting", "○"), "failed": ("Failed", "✖"),
    "skipped": ("Skipped", "↷"),
}
STEP_COLOR = {"done": COLOR["ok"], "running": COLOR["warn"], "pending": COLOR["dim"], "failed": COLOR["danger"], "skipped": "#94a3b8"}
PHONE_TOOLS = frozenset({"phone_control", "mobile_sync"})
OTHER_DEVICE_TOOLS = {"wake_device": "another computer", "check_device": "a network device", "device_toggle": "a device",
                      "home_control": "smart home", "local_device": "a network device"}
CAN_CANCEL = frozenset({"planned", "running", "verifying", "paused", "waiting", "failed"})
CAN_RETRY = frozenset({"paused", "failed"})
MISSION_LABEL = {"planned": "Planned", "running": "Running", "verifying": "Verifying", "paused": "Paused",
                 "completed": "Completed", "failed": "Failed", "cancelled": "Cancelled", "waiting": "Needs approval",
                 "rolled_back": "Rolled back"}


def target_of(tool: str, arguments: dict) -> str:
    """Which device a step acts on. Only what the step itself says: this PC unless it names a phone / another device."""
    if tool in PHONE_TOOLS:
        who = str(arguments.get("phone") or "").strip()
        return f"phone {who}" if who else "your phone"
    if tool in OTHER_DEVICE_TOOLS:
        name = str(arguments.get("name") or arguments.get("device") or "").strip()
        return f"{OTHER_DEVICE_TOOLS[tool]}{' ' + name if name else ''}"
    return "this PC"


@dataclass(frozen=True)
class Row:
    n: int
    title: str
    state: str
    label: str
    glyph: str
    color: str
    target: str
    detail: str = ""        # the error, or the result, in a short sentence
    checkpoint: bool = False
    undoable: bool = False

    @property
    def text(self) -> str:
        parts = [f"{self.glyph} {self.n}. {self.title}", f"[{self.label}]", f"on {self.target}"]
        if self.checkpoint and self.state == "pending":
            parts.append("(asks for your go-ahead first)")
        return " ".join(parts) + (f" - {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class Controls:
    cancel: bool = False
    retry: bool = False
    review: bool = True
    undo: bool = False
    pause: bool = False


@dataclass
class Timeline:
    mission_id: str
    title: str
    state: str
    state_label: str
    progress: str
    rows: list[Row] = field(default_factory=list)
    controls: Controls = field(default_factory=Controls)
    note: str = ""
    current: int | None = None       # n of the step happening now (or the next one to run)

    def summary(self) -> str:
        return f"{self.title} - {self.state_label} ({self.progress} steps)" + (f". {self.note}" if self.note else "")


def build(mission) -> Timeline:
    """``mission`` is an app.core.missions.Mission."""
    rows: list[Row] = []
    for step in mission.steps:
        label, glyph = STEP_LOOK.get(step.state, (step.state.title(), "○"))
        detail = (step.result or "").strip().splitlines()[0][:140] if step.result and step.state in ("failed", "done") else ""
        if step.state == "failed" and not detail:
            detail = "no reason was recorded"
        rows.append(Row(step.n, step.title, step.state, label, glyph, STEP_COLOR.get(step.state, COLOR["dim"]),
                        target_of(step.tool, step.arguments), detail, bool(step.checkpoint), bool(step.inverse)))
    nxt = mission.next_step
    running = next((s for s in mission.steps if s.state == "running"), None)
    state = mission.state
    undo = any(r.undoable and r.state == "done" for r in rows) and state not in ("rolled_back", "running")
    controls = Controls(cancel=state in CAN_CANCEL, retry=state in CAN_RETRY and nxt is not None, review=True, undo=undo,
                        pause=state == "running")
    return Timeline(mission.id, mission.title, state, MISSION_LABEL.get(state, state.title()), mission.progress(), rows,
                    controls, mission.note or "", (running or nxt).n if (running or nxt) else None)


def current_timeline(database) -> Timeline | None:
    """The mission worth showing: the one running or unfinished, else the most recent one. None without any."""
    from app.core.missions import MissionStore

    if database is None:
        return None
    try:
        store = MissionStore(database)
        found = store.unfinished() or next(iter(store.recent(1)), None)
    except Exception:
        return None
    return build(found) if found is not None else None


# what a control sends: the same checked requests a person could type (permissions and confirmations still apply)
COMMANDS = {"retry": "resume my mission", "undo": "roll back the mission", "cancel": "cancel the mission"}
