"""Experiment records: hypothesis, what was changed, what was expected, what was seen - and a conclusion that cannot claim more than the evidence allows.

Used for "why is it slow / why did it fail": Jarvis (or you) writes down the hypothesis and the single change, records the observations, and
``conclude`` grades the result. It never certifies cause from correlation:

* ``correlation only`` - more than one variable changed at once, there is no baseline, or fewer than two observations;
* ``supported``       - exactly one variable changed, a baseline exists, at least two observations, and they agree with the expectation;
* ``contradicted``    - the observations disagree with the expectation;
* ``inconclusive``    - observations point both ways.
A conclusion is a note you can correct or delete; nothing here changes the PC. Stored in the settings store (no new database table); at most ``MAX``
records, oldest dropped. Pure logic; tested without a model.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

KEY = "experiments"
MAX = 50
MAX_OBS = 20


@dataclass
class Experiment:
    id: int
    hypothesis: str
    variables_changed: list[str]
    method: str = ""
    baseline: str = ""                  # the state/measurement BEFORE the change
    expected: str = ""
    observations: list[dict] = field(default_factory=list)   # {"value": str, "matches": bool}
    limitations: list[str] = field(default_factory=list)
    conclusion: str = ""
    strength: str = "open"
    created: float = 0.0


def grade(e: Experiment) -> tuple[str, str]:
    obs = e.observations
    if not obs:
        return "open", "no observations yet"
    matches = [bool(o.get("matches")) for o in obs]
    if not any(matches):
        return "contradicted", "every observation disagrees with the expectation"
    if any(matches) and not all(matches):
        return "inconclusive", "observations point both ways"
    reasons = []
    if len(e.variables_changed) != 1:
        reasons.append(f"{len(e.variables_changed)} variables changed at once")
    if not e.baseline.strip():
        reasons.append("no baseline recorded")
    if len(obs) < 2:
        reasons.append("only one observation")
    if reasons:
        return "correlation only", "; ".join(reasons) + " - this shows an association, not a cause"
    return "supported", "one variable changed, baseline recorded, repeated observations agree (still not proof: other hidden factors are possible)"


class Store:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _all(self) -> list[dict]:
        data = self.store.get(KEY, [])
        return data if isinstance(data, list) else []

    def _save(self, items: list[dict]) -> None:
        self.store.set(KEY, items[-MAX:])

    def start(self, hypothesis: str, variables: list[str], method: str = "", baseline: str = "", expected: str = "") -> Experiment:
        items = self._all()
        e = Experiment((items[-1]["id"] + 1) if items else 1, hypothesis.strip()[:300], [v.strip()[:80] for v in variables if v.strip()][:6],
                       method[:300], baseline[:300], expected[:300], created=time.time())
        items.append(asdict(e))
        self._save(items)
        return e

    def get(self, eid: int) -> Experiment | None:
        for d in self._all():
            if d.get("id") == eid:
                return Experiment(**d)
        return None

    def update(self, e: Experiment) -> None:
        self._save([asdict(e) if d.get("id") == e.id else d for d in self._all()])

    def observe(self, eid: int, value: str, matches: bool) -> Experiment | None:
        e = self.get(eid)
        if e is None:
            return None
        e.observations = (e.observations + [{"value": value[:200], "matches": bool(matches)}])[-MAX_OBS:]
        self.update(e)
        return e

    def conclude(self, eid: int, note: str = "") -> Experiment | None:
        e = self.get(eid)
        if e is None:
            return None
        e.strength, why = grade(e)
        e.conclusion = (note.strip()[:300] + " " if note.strip() else "") + f"[{e.strength}] {why}"
        self.update(e)
        return e

    def remove(self, eid: int) -> bool:
        items = self._all()
        kept = [d for d in items if d.get("id") != eid]
        self._save(kept)
        return len(kept) != len(items)

    def all(self) -> list[Experiment]:
        return [Experiment(**d) for d in self._all()]


def describe(e: Experiment) -> str:
    seen = "; ".join(("matches" if o["matches"] else "differs") + f": {o['value']}" for o in e.observations[-4:]) or "none yet"
    return (f"#{e.id} [{e.strength}] {e.hypothesis}\n  changed: {', '.join(e.variables_changed) or 'nothing recorded'}; baseline: {e.baseline or 'none'}; expected: {e.expected or 'unstated'}\n"
            f"  observed: {seen}\n  conclusion: {e.conclusion or 'not concluded'}")
