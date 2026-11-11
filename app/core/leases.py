"""Mission-scoped capability leases: a mission may use only the tools it was granted, for a limited time and number of calls.

A lease can only RESTRICT. It is checked after the normal permission system and never replaces it: approvals, autonomy limits, the emergency stop and the
risk levels still apply to every call. A mission with no lease behaves exactly as before (backward compatible).

Automatic leases (made from a mission's approved plan) are also narrowed step by step: while step N runs, only step N's tool is allowed.

A lease fixes: the mission, the allowed tools (exact names), an optional folder scope for path arguments, whether network tools are allowed, the highest
risk level, an expiry time and a call budget. It ends when it expires, runs out of calls, or is revoked - a revoked or expired lease BLOCKS the mission's
calls (it does not fall back to "no lease"). Nothing a model, a web page, a document or another agent says can widen a lease: ``grant`` is only reachable
from a confirmed tool call by you. Every denial is returned with its reason and audited by the executor.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

MAX_HOURS = 72
PATH_KEYS = ("path", "folder", "file", "directory", "source", "destination", "target", "dest", "src")


@dataclass
class Lease:
    mission: str
    tools: tuple[str, ...]
    folder: str = ""
    network: bool = False
    max_level: int = 2
    expires: float = 0.0
    max_calls: int = 100
    calls: int = 0
    revoked: bool = False
    created: float = 0.0
    auto: bool = False
    focus: str = ""

    def state(self, now: float) -> str:
        if self.revoked:
            return "revoked"
        if now >= self.expires:
            return "expired"
        if self.calls >= self.max_calls:
            return "used up"
        return "active"


def _inside(folder: str, value: str) -> bool:
    try:
        base = os.path.normcase(os.path.abspath(folder))
        p = os.path.normcase(os.path.abspath(os.path.join(folder, value) if not os.path.isabs(value) else value))
        return os.path.commonpath([base, p]) == base
    except (ValueError, OSError):
        return False


def check(lease: Lease, tool: str, level: int, needs_network: bool, args: dict[str, Any] | None, now: float) -> tuple[bool, str]:
    st = lease.state(now)
    if st != "active":
        return False, f"this mission's permission lease is {st}"
    if lease.focus and tool != lease.focus:
        return False, f"'{tool}' is not the step in progress (this mission is on '{lease.focus}')"
    if tool not in lease.tools:
        return False, f"'{tool}' is not in this mission's lease (allowed: {', '.join(lease.tools) or 'nothing'})"
    if level > lease.max_level:
        return False, f"'{tool}' is risk level {level}; this mission's lease stops at level {lease.max_level}"
    if needs_network and not lease.network:
        return False, "this mission's lease does not include network access"
    if lease.folder:
        for k, v in (args or {}).items():
            if k in PATH_KEYS and isinstance(v, str) and v and not _inside(lease.folder, v):
                return False, f"'{v}' is outside this mission's folder ({lease.folder})"
    return True, ""


class LeaseStore:
    def __init__(self, db: Any, clock: Callable[[], float] = time.time) -> None:
        self.db, self.clock = db, clock

    def grant(self, mission: str, tools: list[str], hours: float = 8.0, folder: str = "", network: bool = False, max_level: int = 2, max_calls: int = 100,
              auto: bool = False) -> Lease:
        if not mission or not tools:
            raise ValueError("a lease needs a mission and at least one tool")
        now = self.clock()
        lease = Lease(str(mission), tuple(sorted(set(tools))), folder, bool(network), max(0, min(int(max_level), 3)),
                      now + max(0.05, min(float(hours), MAX_HOURS)) * 3600, max(1, min(int(max_calls), 5000)), 0, False, now, bool(auto))
        with self.db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO capability_leases (mission, tools, folder, network, max_level, expires, max_calls, calls, revoked, created, auto) "
                         "VALUES (?,?,?,?,?,?,?,?,0,?,?)", (lease.mission, json.dumps(lease.tools), folder, int(lease.network), lease.max_level, lease.expires, lease.max_calls, 0, now, int(lease.auto)))
        return lease

    def get(self, mission: str) -> Lease | None:
        with self.db.connect() as conn:
            r = conn.execute("SELECT mission, tools, folder, network, max_level, expires, max_calls, calls, revoked, created, auto, focus FROM capability_leases WHERE mission = ?", (str(mission),)).fetchone()
        return Lease(r[0], tuple(json.loads(r[1])), r[2], bool(r[3]), r[4], r[5], r[6], r[7], bool(r[8]), r[9], bool(r[10]), r[11] or "") if r else None

    def all(self) -> list[Lease]:
        with self.db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT mission FROM capability_leases ORDER BY created DESC LIMIT 50")]
        return [x for x in (self.get(i) for i in ids) if x]

    def focus(self, mission: str, tool: str) -> None:
        """Narrow an AUTOMATIC lease to the step in progress: until the next step, only ``tool`` is allowed. A lease you made is not narrowed."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE capability_leases SET focus = ? WHERE mission = ? AND auto = 1", (tool, str(mission)))

    def revoke(self, mission: str) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("UPDATE capability_leases SET revoked = 1 WHERE mission = ? AND revoked = 0", (str(mission),)).rowcount > 0

    def authorize(self, mission: str, tool: str, level: int, needs_network: bool, args: dict[str, Any] | None) -> tuple[bool, str]:
        """(True, "") when the mission has no lease or the lease allows the call (one call is then spent)."""
        lease = self.get(mission)
        if lease is None:
            return True, ""
        ok, why = check(lease, tool, level, needs_network, args, self.clock())
        if ok:
            with self.db.transaction() as conn:
                conn.execute("UPDATE capability_leases SET calls = calls + 1 WHERE mission = ?", (str(mission),))
        return ok, why


AUTO_HOURS = 24.0


def ensure_auto(store: LeaseStore, mission: str, steps: list[tuple[str, int, bool]]) -> Lease | None:
    """Give a mission a lease that covers exactly its approved plan: ``steps`` = (tool, risk level, needs network) per step.

    * no lease yet                    -> one is made from the plan (tools in the plan only, the plan's highest level, network only if a step needs it);
    * a lease YOU made                -> never touched (stricter or wider, it is your decision);
    * an automatic lease that expired or ran out while the mission was paused -> renewed from the SAME plan (a resumed mission is not locked out);
    * a revoked lease                 -> stays revoked: only you can grant again.
    Returns the lease in force, or None when there are no steps.
    """
    if not steps:
        return None
    now = store.clock()
    cur = store.get(mission)
    if cur is not None and (not cur.auto or cur.revoked or cur.state(now) == "active"):
        return cur
    tools = sorted({t for t, _, _ in steps})
    return store.grant(mission, tools, AUTO_HOURS, "", any(n for _, _, n in steps), max(lv for _, lv, _ in steps), max(20, len(steps) * 6), auto=True)


def describe(lease: Lease, now: float) -> str:
    left = max(0, int((lease.expires - now) / 60))
    return (f"mission {lease.mission}: {lease.state(now)}; tools {', '.join(lease.tools)}; up to risk level {lease.max_level}; "
            f"network {'yes' if lease.network else 'no'}; folder {lease.folder or 'not limited'}; {lease.calls}/{lease.max_calls} calls; {left} min left; " + ("made automatically from the approved plan" if lease.auto else "made by you"))
