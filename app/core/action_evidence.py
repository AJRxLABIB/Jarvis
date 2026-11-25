"""Reality verification: evidence records for actions, and a verdict that an action's own "done" message can never produce.

Three things an assistant must not confuse: it INTENDED to do something, it EXECUTED something, and the result was VERIFIED. This module keeps the third
separate. Each independent check of an action is one evidence row (table ``action_evidence``, migration 48): what was expected, what was observed, which
verifier looked, where the evidence came from, pass / fail / unreadable, and a hash of the observation.

``verdict`` combines the rows of one action:
    verified            at least one independent check passed and none failed
    partially_verified  some passed, some could not be read
    failed              an independent check failed (and none passed)
    conflicting         independent checks disagree - reported as such, never averaged away
    unverified          there is no independent evidence (a self-report alone always ends here)

Independence rule: evidence whose source is in ``SELF_SOURCES`` (the acting tool's own message, the model's statement) is recorded but NEVER counts toward
"verified". A green state may only be shown for ``verified``.
``verifier_for`` names the independent check that fits a kind of action, built from what exists (``claim_verify`` for files and git, ``screen_verify`` for
screen actions, the phone's acknowledgement + state query, a re-read of a setting). Kinds without an independent reader say so ("none available").
``recovery_options`` lists what may be done after a failed check; it never undoes anything itself, and never offers an automatic undo of an irreversible action.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

STATUSES = ("pass", "fail", "unreadable")
SELF_SOURCES = frozenset({"self_report", "tool_message", "model_statement"})
VERIFIERS = {
    "file": ("claim_verify.check_file", "re-opens the file: exists, size, expected text, opens as its format"),
    "document": ("claim_verify.check_file", "re-opens the document and looks for the expected text"),
    "git": ("claim_verify.check_git", "asks git itself: branch, last commit, files touched, clean tree"),
    "code": ("claim_verify.check_git + project tests", "diff of touched files plus the project's own test command"),
    "screen": ("screen_verify.verify", "before/after snapshot of the screen and foreground window"),
    "browser": ("screen_verify.verify", "the resulting page state is read again"),
    "phone": ("phone acknowledgement + state query", "the phone's own 'done' plus a follow-up state read where the app supports it"),
    "setting": ("settings re-read", "the setting is read back through the system interface"),
    "install": ("installed-programs query", "installed state, executable present, version"),
}
NO_INDEPENDENT = {"email": "sending cannot be confirmed without a sent-items read through an authorised mailbox integration; clicking Send is not proof",
                  "message": "delivery is only known from the receiving app; not readable here",
                  "network": "only the reply of the remote service is available, which is not independent"}
IRREVERSIBLE = frozenset({"email", "message", "delete", "purchase", "payment"})


@dataclass
class Evidence:
    action: str
    verifier: str
    source: str
    status: str
    execution: str = ""
    target: str = ""
    expected: str = ""
    observed: str = ""
    recovery: str = ""
    ts: float = 0.0
    digest: str = ""
    id: int = 0

    @property
    def independent(self) -> bool:
        return self.source not in SELF_SOURCES


@dataclass
class Verdict:
    status: str
    reasons: list[str]
    independent: int
    total: int

    @property
    def green(self) -> bool:
        return self.status == "verified"

    def text(self) -> str:
        return f"{self.status.replace('_', ' ').upper()} ({self.independent} independent check(s) of {self.total} record(s)): " + "; ".join(self.reasons)


def verifier_for(kind: str) -> tuple[str, str, bool]:
    """(verifier name, what it does, available?) for a kind of action."""
    k = (kind or "").lower()
    if k in VERIFIERS:
        return (*VERIFIERS[k], True)
    if k in NO_INDEPENDENT:
        return "none available", NO_INDEPENDENT[k], False
    return "none available", "no independent reader is registered for this kind of action", False


def verdict(rows: list[Evidence]) -> Verdict:
    ind = [r for r in rows if r.independent]
    if not ind:
        why = "only the action's own report exists - that is not proof" if rows else "nothing was checked"
        return Verdict("unverified", [why], 0, len(rows))
    passed = [r for r in ind if r.status == "pass"]
    failed = [r for r in ind if r.status == "fail"]
    unread = [r for r in ind if r.status == "unreadable"]
    if passed and failed:
        return Verdict("conflicting", [f"{passed[0].verifier} passed but {failed[0].verifier} failed: {failed[0].observed[:80] or 'see record'}"], len(ind), len(rows))
    if failed:
        return Verdict("failed", [f"{r.verifier}: {r.observed[:80] or 'check failed'}" for r in failed[:3]], len(ind), len(rows))
    if passed and unread:
        return Verdict("partially_verified", [f"{len(passed)} check(s) passed; {len(unread)} could not be read ({unread[0].verifier})"], len(ind), len(rows))
    if passed:
        return Verdict("verified", [f"{r.verifier} confirmed it" for r in passed[:3]], len(ind), len(rows))
    return Verdict("unverified", [f"the evidence could not be read ({unread[0].verifier})"], len(ind), len(rows))


def recovery_options(kind: str, v: Verdict, reversible: bool | None = None) -> list[str]:
    """What may be done after a failed / conflicting check. Suggestions only; anything that changes state still needs the normal approval."""
    if v.status in ("verified",):
        return []
    k = (kind or "").lower()
    out = ["inspect the current state first (what actually exists now)"]
    if v.status == "unverified":
        return out + ["run the independent check for this kind of action" if verifier_for(k)[2] else "ask the user to confirm: no independent check exists for this"]
    irreversible = k in IRREVERSIBLE if reversible is None else not reversible
    if irreversible:
        return out + ["do NOT repeat or undo automatically: this kind of action cannot be taken back", "tell the user what was and was not confirmed"]
    return out + ["retry once if the action is safe to repeat", "restore the checkpoint / undo if one was recorded (asks first)", "replan the remaining steps"]


class Store:
    def __init__(self, db: Any) -> None:
        self.db = db

    def add(self, e: Evidence) -> int:
        if e.status not in STATUSES:
            raise ValueError(f"unknown evidence status '{e.status}'")
        if not (e.action and e.verifier and e.source):
            raise ValueError("action, verifier and source are required: evidence must have a known source")
        e.ts = e.ts or time.time()
        e.digest = e.digest or hashlib.sha256(f"{e.action}|{e.target}|{e.observed}".encode("utf-8", "replace")).hexdigest()[:32]
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO action_evidence (ts, execution, action, target, expected, observed, verifier, source, status, digest, recovery) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (e.ts, e.execution[:80], e.action[:120], e.target[:300], e.expected[:400], e.observed[:400], e.verifier[:80], e.source[:60], e.status, e.digest, e.recovery[:200]))
            e.id = int(cur.lastrowid)
        return e.id

    def _rows(self, where: str = "", args: tuple = (), limit: int = 200) -> list[Evidence]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, ts, execution, action, target, expected, observed, verifier, source, status, digest, recovery FROM action_evidence "
                                + where + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [Evidence(id=r[0], ts=r[1], execution=r[2], action=r[3], target=r[4], expected=r[5], observed=r[6], verifier=r[7], source=r[8], status=r[9],
                         digest=r[10], recovery=r[11]) for r in rows]

    def for_action(self, action: str, execution: str = "") -> list[Evidence]:
        if execution:
            return self._rows("WHERE action = ? AND execution = ?", (action, execution))
        return self._rows("WHERE action = ?", (action,))

    def recent(self, limit: int = 30) -> list[Evidence]:
        return self._rows(limit=limit)

    def stats(self, since: float = 0.0) -> dict[str, int]:
        """Verdict counts per (execution, action) since a time - what the Verification Center shows. Counts actions, not rows."""
        groups: dict[tuple[str, str], list[Evidence]] = {}
        for r in self._rows("WHERE ts >= ?", (since,), limit=2000):
            groups.setdefault((r.execution, r.action), []).append(r)
        out = {"verified": 0, "partially_verified": 0, "unverified": 0, "failed": 0, "conflicting": 0}
        for rows in groups.values():
            out[verdict(rows).status] += 1
        return out

    def prune(self, before: float) -> int:
        with self.db.transaction() as conn:
            return int(conn.execute("DELETE FROM action_evidence WHERE ts < ?", (before,)).rowcount)


def describe(rows: list[Evidence]) -> str:
    if not rows:
        return "No evidence recorded."
    lines = [f"#{r.id} {r.action}" + (f" -> {r.target}" if r.target else "") + f": {r.status.upper()} by {r.verifier} (source: {r.source}"
             + ("" if r.independent else ", NOT independent") + ")" + (f" - {r.observed[:70]}" if r.observed else "") for r in rows[:15]]
    return "\n".join(lines)
