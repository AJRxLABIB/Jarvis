"""Places and geofence events, decided conservatively - and never from raw GPS that is not good enough.

Two parts, both pure logic (no GPS, no Android needed to test):

* ``Tracker`` turns a stream of position fixes into ENTER / EXIT / DWELL events per saved place. A fix whose accuracy is worse than the limit (or than the place's
  radius) says nothing: it never confirms an arrival or a departure. Entering needs ``confirm`` consecutive good fixes inside; leaving needs the same outside the
  radius PLUS a margin (hysteresis, so a phone resting on the edge does not flap). Events of one kind for one place are not repeated inside ``cooldown_s``.
* ``Hub`` is what the PC does with events from the phone. The phone evaluates places itself and sends only events - ``{id, place, kind, t, accuracy_m}`` - never a
  stream of coordinates. The hub validates them (known place, known kind, plausible time, unique id), keeps the last few as history (no coordinates are stored), and
  returns the actions of matching rules. Rules only SAY something or NOTIFY (a reminder text, a profile name to suggest): they never run a tool by themselves.

Honest status: tested with fake fixes. Nothing here reads a location. The Android side that would produce fixes/events is not built in this round (a Google
Play services geofence needs a build this project does not use; see docs/round75-new-capabilities.md).
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

KINDS = ("enter", "exit", "dwell")
MIN_RADIUS_M, MAX_RADIUS_M = 50.0, 5000.0           # below ~50 m a phone's own error decides; above 5 km it is not a "place"
MAX_PLACES = 50
MAX_SKEW_S = 3600.0
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    lat: float
    lon: float
    radius_m: float = 150.0

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise ValueError("a place id is 1-64 letters, digits or . _ : -")
        if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError("latitude must be -90..90 and longitude -180..180")
        if not (MIN_RADIUS_M <= self.radius_m <= MAX_RADIUS_M):
            raise ValueError(f"the radius must be {MIN_RADIUS_M:.0f}-{MAX_RADIUS_M:.0f} m")
        if not self.name.strip() or len(self.name) > 60:
            raise ValueError("a place needs a name of up to 60 characters")

    def public(self) -> dict[str, Any]:
        """What a screen or a reply may show: the name and radius, NOT the coordinates (``coordinates=True`` is an explicit act)."""
        return {"id": self.id, "name": self.name, "radius_m": self.radius_m}


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    accuracy_m: float
    t: float


@dataclass(frozen=True)
class Event:
    kind: str
    place_id: str
    t: float
    distance_m: float
    accuracy_m: float


class Tracker:
    def __init__(self, places: list[Place], *, max_accuracy_m: float = 100.0, confirm: int = 2, exit_margin: float = 0.25, dwell_s: float = 600.0, cooldown_s: float = 300.0) -> None:
        self.places = {p.id: p for p in places}
        self.max_accuracy_m, self.confirm, self.exit_margin, self.dwell_s, self.cooldown_s = max_accuracy_m, max(1, confirm), exit_margin, dwell_s, cooldown_s
        self._inside: dict[str, bool | None] = {pid: None for pid in self.places}      # None = not known yet (never assume)
        self._streak: dict[str, int] = {pid: 0 for pid in self.places}
        self._since: dict[str, float | None] = {pid: None for pid in self.places}
        self._dwelled: set[str] = set()
        self._last: dict[tuple[str, str], float] = {}

    def _emit(self, kind: str, pid: str, fix: Fix, dist: float) -> Event | None:
        last = self._last.get((pid, kind))
        if last is not None and fix.t - last < self.cooldown_s:
            return None
        self._last[(pid, kind)] = fix.t
        return Event(kind, pid, fix.t, round(dist, 1), fix.accuracy_m)

    def feed(self, fix: Fix) -> list[Event]:
        out: list[Event] = []
        for pid, place in self.places.items():
            if fix.accuracy_m > self.max_accuracy_m or fix.accuracy_m > place.radius_m:
                continue                                                  # too vague to say anything about THIS place
            dist = distance_m(fix.lat, fix.lon, place.lat, place.lon)
            if dist <= place.radius_m:
                side = True
            elif dist >= place.radius_m * (1 + self.exit_margin) + fix.accuracy_m:
                side = False
            else:
                self._streak[pid] = 0                                     # in the margin band: neither in nor out
                continue
            state = self._inside[pid]
            if state is None:                                             # the first good fix only says where we are: it is not an arrival or a departure
                self._inside[pid], self._since[pid], self._streak[pid] = side, (fix.t if side else None), 0
                continue
            if state is side:
                self._streak[pid] = 0
                since = self._since[pid]
                if side and since is not None and pid not in self._dwelled and fix.t - since >= self.dwell_s:
                    self._dwelled.add(pid)
                    ev = self._emit("dwell", pid, fix, dist)
                    out += [ev] if ev else []
                continue
            self._streak[pid] += 1
            if self._streak[pid] >= self.confirm:                         # a single good fix on the other side is not enough
                self._inside[pid], self._streak[pid] = side, 0
                self._since[pid] = fix.t if side else None
                self._dwelled.discard(pid)
                ev = self._emit("enter" if side else "exit", pid, fix, dist)
                out += [ev] if ev else []
        return out


@dataclass
class Rule:
    place_id: str
    on: str
    say: str = ""
    profile: str = ""                       # a profile NAME to suggest; applying it is a separate, confirmed step

    def __post_init__(self) -> None:
        if self.on not in KINDS:
            raise ValueError(f"on must be one of {KINDS}")
        if not (self.say or self.profile):
            raise ValueError("a rule needs something to say or a profile to suggest")


@dataclass
class Hub:
    places: dict[str, Place] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    clock: Callable[[], float] = time.time
    history: deque = field(default_factory=lambda: deque(maxlen=30))
    _seen: deque = field(default_factory=lambda: deque(maxlen=200))

    def add_place(self, place: Place) -> None:
        if place.id not in self.places and len(self.places) >= MAX_PLACES:
            raise ValueError(f"at most {MAX_PLACES} places")
        self.places[place.id] = place

    def remove_place(self, pid: str) -> bool:
        self.rules = [r for r in self.rules if r.place_id != pid]
        return self.places.pop(pid, None) is not None

    def add_rule(self, rule: Rule) -> None:
        if rule.place_id not in self.places:
            raise ValueError("that place does not exist")
        self.rules.append(rule)

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        """A phone event -> ``{"ok": bool, "reason": str, "actions": [...]}``. Never raises on a bad event."""
        try:
            eid, pid, kind = str(event.get("id", ""))[:64], str(event.get("place", "")), str(event.get("kind", ""))
            t, acc = float(event.get("t")), float(event.get("accuracy_m", 0))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "malformed event", "actions": []}
        if not _ID.match(eid):
            return {"ok": False, "reason": "missing or invalid event id", "actions": []}
        if eid in self._seen:
            return {"ok": False, "reason": "duplicate event (already handled)", "actions": []}
        if pid not in self.places:
            return {"ok": False, "reason": "unknown place", "actions": []}
        if kind not in KINDS:
            return {"ok": False, "reason": "unknown event kind", "actions": []}
        if abs(self.clock() - t) > MAX_SKEW_S:
            return {"ok": False, "reason": "event time is too far from now (stale or clock error)", "actions": []}
        if acc > self.places[pid].radius_m:
            return {"ok": False, "reason": "position accuracy was worse than the place radius: not accepted as confirmed", "actions": []}
        self._seen.append(eid)
        self.history.append({"place": self.places[pid].name, "kind": kind, "t": t})
        actions = [{"say": r.say.replace("{place}", self.places[pid].name), "profile": r.profile} for r in self.rules if r.place_id == pid and r.on == kind]
        return {"ok": True, "reason": "accepted", "actions": actions}

    def describe(self) -> str:
        if not self.places:
            return "No saved places yet."
        lines = [f"{len(self.places)} place(s), {len(self.rules)} rule(s)."]
        lines += [f"  {p.name} (radius {p.radius_m:.0f} m)" for p in self.places.values()]
        if self.history:
            last = self.history[-1]
            lines.append(f"Last event: {last['kind']} {last['place']} ({int(self.clock() - last['t'])} s ago).")
        return "\n".join(lines)

    def dump(self) -> dict[str, Any]:
        return {"places": [p.__dict__ for p in self.places.values()], "rules": [r.__dict__ for r in self.rules]}

    @classmethod
    def load(cls, data: Any, clock: Callable[[], float] = time.time) -> "Hub":
        hub = cls(clock=clock)
        if isinstance(data, dict):
            for p in data.get("places", []):
                try:
                    hub.add_place(Place(**p))
                except (TypeError, ValueError):
                    continue                                                # one corrupt entry never loses the rest
            for r in data.get("rules", []):
                try:
                    hub.add_rule(Rule(**r))
                except (TypeError, ValueError):
                    continue
        return hub
