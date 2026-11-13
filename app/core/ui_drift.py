"""Adaptive application intelligence: notice that a program's interface changed, and find the likely replacement control - without guessing.

Sits ABOVE the existing control layer (``app_profiles`` data, ``app_adapters`` routes, ``tools/ui_elements`` pressing). It adds three things:

* ``Fingerprint``  - a versioned snapshot of an application's controls (name, role, automation id, parent, neighbouring label).
* ``diff``         - what changed between two fingerprints: removed, added, renamed/moved candidates, with a compatibility score.
* ``resolve``      - when an expected control is gone, ranked replacement candidates with a confidence and the evidence for it.

Safety rule (the point of this module): a candidate is only ``safe`` when it is clearly the best AND clearly separated from the second best AND
shares an identity signal (the same automation id, or the same role with a very similar name). Anything weaker is returned as a SUGGESTION that needs
the user. Nothing here presses anything; profile updates are proposals the user approves, and every stored version can be rolled back.
Honest status: pure logic with tests on fake controls. The live reader (``read_live``) uses Windows UI Automation and is unverified on real programs.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

SAFE_CONFIDENCE = 0.85
SAFE_MARGIN = 0.15
MAX_CONTROLS = 400
MAX_VERSIONS = 10
KEY = "ui_profiles"


@dataclass(frozen=True)
class Control:
    name: str
    role: str = ""
    auto_id: str = ""
    parent: str = ""        # name of the containing group / menu
    label: str = ""         # nearest preceding label (helps for unnamed boxes)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role, "auto_id": self.auto_id, "parent": self.parent, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> "Control":
        return cls(*(str(d.get(k, ""))[:120] for k in ("name", "role", "auto_id", "parent", "label")))


@dataclass
class Fingerprint:
    app: str
    version: str
    controls: list[Control]
    taken: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"app": self.app, "version": self.version, "taken": self.taken, "controls": [c.to_dict() for c in self.controls]}

    @classmethod
    def from_dict(cls, d: dict) -> "Fingerprint":
        return cls(str(d.get("app", "")), str(d.get("version", "")), [Control.from_dict(c) for c in d.get("controls", [])][:MAX_CONTROLS], float(d.get("taken", 0)))


@dataclass
class Candidate:
    control: Control
    confidence: float
    why: list[str]


@dataclass
class Resolution:
    wanted: Control
    candidates: list[Candidate]
    safe: bool
    note: str


@dataclass
class Drift:
    removed: list[Control] = field(default_factory=list)
    added: list[Control] = field(default_factory=list)
    changed: list[tuple[Control, Control, float]] = field(default_factory=list)   # (old, new, confidence)
    compatibility: float = 1.0
    version_changed: bool = False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower().replace("&", "")).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def score(old: Control, new: Control) -> tuple[float, list[str]]:
    """How likely ``new`` is the same control as ``old``. Evidence is returned with the number so a person can judge it."""
    why: list[str] = []
    s = 0.0
    if old.auto_id and old.auto_id == new.auto_id:
        s += 0.55
        why.append("same automation id")
    name = _sim(old.name, new.name)
    if name:
        s += 0.35 * name
        why.append(f"name similarity {name:.0%}")
    if old.role and old.role == new.role:
        s += 0.15
        why.append("same role")
    elif old.role and new.role and old.role != new.role:
        s -= 0.35
        why.append(f"different role ({old.role} -> {new.role})")
    if old.parent and _sim(old.parent, new.parent) > 0.8:
        s += 0.10
        why.append("same container")
    if old.label and _sim(old.label, new.label) > 0.8:
        s += 0.05
        why.append("same nearby label")
    return max(0.0, min(1.0, s)), why


def resolve(wanted: Control, current: list[Control]) -> Resolution:
    """Candidates for a missing control. ``safe`` only for a clear, separated, identity-backed winner; otherwise a suggestion for the user."""
    exact = [c for c in current if c == wanted]
    if exact:
        return Resolution(wanted, [Candidate(exact[0], 1.0, ["the control is still there, unchanged"])], True, "unchanged")
    ranked = sorted((Candidate(c, *score(wanted, c)) for c in current), key=lambda c: -c.confidence)
    ranked = [c for c in ranked if c.confidence >= 0.4][:5]
    if not ranked:
        return Resolution(wanted, [], False, "No similar control found. The control may have been removed; do not guess.")
    best = ranked[0]
    second = ranked[1].confidence if len(ranked) > 1 else 0.0
    identity = any(w.startswith("same automation id") for w in best.why) or (any(w == "same role" for w in best.why) and _sim(wanted.name, best.control.name) >= 0.8)
    safe = best.confidence >= SAFE_CONFIDENCE and best.confidence - second >= SAFE_MARGIN and identity
    note = "clear replacement (still verify the result after using it)" if safe else "uncertain: this is a suggestion, ask the user before using it"
    return Resolution(wanted, ranked, safe, note)


def diff(old: Fingerprint, new: Fingerprint) -> Drift:
    """Removed / added / renamed controls and a 0-1 compatibility score (1 = nothing the recipes use has moved)."""
    d = Drift(version_changed=old.version != new.version)
    old_set, new_set = set(old.controls), set(new.controls)
    gone = [c for c in old.controls if c not in new_set]
    fresh = [c for c in new.controls if c not in old_set]
    left = list(fresh)
    for c in gone:
        r = resolve(c, left)
        if r.candidates and r.candidates[0].confidence >= 0.6:
            match = r.candidates[0]
            d.changed.append((c, match.control, match.confidence))
            left = [x for x in left if x != match.control]
        else:
            d.removed.append(c)
    d.added = left
    total = max(1, len(old.controls))
    d.compatibility = round(max(0.0, 1 - (len(d.removed) + 0.5 * len(d.changed)) / total), 3)
    return d


def describe(app: str, d: Drift) -> str:
    if not (d.removed or d.added or d.changed or d.version_changed):
        return f"{app}: the interface matches the saved profile."
    lines = [f"{app}: compatibility {d.compatibility:.0%}" + (" (the program version changed)" if d.version_changed else "") + "."]
    lines += [f"  renamed or moved ({c:.0%} sure): '{o.name}' -> '{n.name}'" for o, n, c in d.changed[:8]]
    lines += [f"  gone: '{c.name}' ({c.role or 'control'})" for c in d.removed[:8]]
    if d.added:
        lines.append(f"  new: {', '.join(repr(c.name) for c in d.added[:8])}" + (f" and {len(d.added) - 8} more" if len(d.added) > 8 else ""))
    lines.append("Nothing was changed. A profile update is a proposal you approve.")
    return "\n".join(lines)


class ProfileStore:
    """Versioned fingerprints per application, kept in the settings store: ``learn`` adds a version, ``rollback`` drops the newest."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _all(self) -> dict[str, list[dict]]:
        data = self.store.get(KEY, {})
        return data if isinstance(data, dict) else {}

    def versions(self, app: str) -> list[Fingerprint]:
        return [Fingerprint.from_dict(x) for x in self._all().get(_norm(app), [])]

    def current(self, app: str) -> Fingerprint | None:
        v = self.versions(app)
        return v[-1] if v else None

    def learn(self, fp: Fingerprint) -> int:
        fp.taken = fp.taken or time.time()
        fp.controls = fp.controls[:MAX_CONTROLS]
        data = self._all()
        items = data.setdefault(_norm(fp.app), [])
        items.append(fp.to_dict())
        del items[:-MAX_VERSIONS]
        self.store.set(KEY, data)
        return len(items)

    def rollback(self, app: str) -> bool:
        data = self._all()
        items = data.get(_norm(app), [])
        if len(items) < 2:
            return False
        items.pop()
        self.store.set(KEY, data)
        return True

    def apps(self) -> list[str]:
        return sorted(self._all())


def read_live(limit: int = MAX_CONTROLS) -> Fingerprint:
    """Fingerprint the foreground window through Windows UI Automation (Windows only; unverified on real programs)."""
    from app.tools import winapi
    from app.tools.browser_tabs import _automation
    from app.tools.ui_elements import PRESSABLE, PROP_CONTROL_TYPE, SCOPE_DESCENDANTS

    found = _automation()
    if found is None:
        raise RuntimeError("UI Automation is not available on this PC")
    uia, _ = found
    hwnd = winapi.foreground_window()
    root = uia.ElementFromHandle(hwnd)
    conds = [uia.CreatePropertyCondition(PROP_CONTROL_TYPE, cid) for cid in PRESSABLE]
    cond = conds[0]
    for extra in conds[1:]:
        cond = uia.CreateOrCondition(cond, extra)
    items = root.FindAll(SCOPE_DESCENDANTS, cond)
    controls = []
    for i in range(min(items.Length, limit)):
        el = items.GetElement(i)
        if el.CurrentIsOffscreen:
            continue
        controls.append(Control(str(el.CurrentName or "")[:120], str(el.CurrentLocalizedControlType or "")[:40], str(el.CurrentAutomationId or "")[:120]))
    title = str(root.CurrentName or "")
    app = title.rsplit(" - ", 1)[-1] if " - " in title else title
    return Fingerprint(app[:60] or "unknown", "", controls, time.time())
