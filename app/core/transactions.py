"""Cross-device transactions: ONE task that spans the PC and a phone, with one id, one owner per step, checkpoints and honest completion.

This is a ledger and a decision aid, not a second executor: the steps are carried out by the existing tools / phone commands. What it adds:

* every step belongs to exactly one device and may depend on other steps;
* a step is VERIFIED only with evidence reported BY THE DEVICE THAT OWNS IT (the PC cannot confirm a phone step, and the reverse);
* the transaction is COMPLETE only when every step is verified - a tool "success" is not enough;
* when a device drops, the steps it was running become UNCERTAIN (never replayed blindly) and the transaction gets recovery options
  (wait for it, move the step to another capable device, or stop) - the user / mission decides, nothing is chosen silently;
* a checkpoint (what is verified so far) is written after every state change, so a restart resumes from the last verified step;
* nothing here grants a permission: a step names an action, the normal approval path still applies when it is carried out.

Stored in the settings store (no new table); at most 30 transactions, the oldest finished ones dropped. Pure functions over the stored dict,
tested with made-up devices.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable

KEY = "transactions"
MAX_KEPT = 30
MAX_STEPS = 20

PENDING, RUNNING, VERIFIED, FAILED, UNCERTAIN, SKIPPED = "pending", "running", "verified", "failed", "uncertain", "skipped"
OPEN_STATES = {PENDING, RUNNING, UNCERTAIN}
ACTIVE, COMPLETE, STOPPED, NEEDS_DECISION = "active", "complete", "stopped", "needs_decision"
MAX_ATTEMPTS = 2


class TransactionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(title: str, steps: list[dict[str, Any]], *, mission_id: str = "") -> dict[str, Any]:
    """``steps``: [{"title", "device", "action", "after": [step numbers 1..n]}]. Devices are keys such as ``pc`` or a paired phone's id."""
    title = (title or "").strip()
    if not title or len(title) > 120:
        raise TransactionError("Give the task a short title.")
    if not steps or len(steps) > MAX_STEPS:
        raise TransactionError(f"A transaction needs 1 to {MAX_STEPS} steps.")
    built = []
    for number, raw in enumerate(steps, 1):
        device = str(raw.get("device", "")).strip()[:60]
        text = str(raw.get("title", "")).strip()[:160]
        if not device or not text:
            raise TransactionError(f"Step {number} needs a title and the device that owns it.")
        after = [int(a) for a in raw.get("after", []) or []]
        if any(a < 1 or a >= number for a in after):
            raise TransactionError(f"Step {number} can only wait for earlier steps.")        # forward references = no cycles possible
        built.append({"n": number, "title": text, "device": device, "action": str(raw.get("action", ""))[:80], "after": after,
                      "status": PENDING, "attempts": 0, "evidence": "", "note": "", "began": "", "command": 0, "updated": _now()})
    tx = {"id": "tx-" + secrets.token_hex(4), "title": title, "mission_id": mission_id[:60], "state": ACTIVE, "created": _now(), "steps": built,
          "checkpoint": {"verified": [], "at": _now()}, "decisions": []}
    return tx


def _step(tx: dict[str, Any], n: int) -> dict[str, Any]:
    for s in tx["steps"]:
        if s["n"] == n:
            return s
    raise TransactionError(f"No step {n}.")


def _checkpoint(tx: dict[str, Any]) -> None:
    tx["checkpoint"] = {"verified": [s["n"] for s in tx["steps"] if s["status"] == VERIFIED], "at": _now()}
    tx["state"] = _state(tx)


def _state(tx: dict[str, Any]) -> str:
    if tx["state"] == STOPPED:
        return STOPPED
    steps = tx["steps"]
    if all(s["status"] in (VERIFIED, SKIPPED) for s in steps) and any(s["status"] == VERIFIED for s in steps):
        return COMPLETE
    if any(s["status"] in (UNCERTAIN, FAILED) for s in steps):
        return NEEDS_DECISION
    return ACTIVE


def runnable(tx: dict[str, Any], online: Callable[[str], bool]) -> list[dict[str, Any]]:
    """Steps that may start now: pending, every earlier step they wait for is verified, and their own device is reachable."""
    if tx["state"] in (STOPPED, COMPLETE, NEEDS_DECISION):
        return []
    done = {s["n"] for s in tx["steps"] if s["status"] in (VERIFIED, SKIPPED)}
    return [s for s in tx["steps"] if s["status"] == PENDING and set(s["after"]) <= done and online(s["device"])]


def blocked(tx: dict[str, Any], online: Callable[[str], bool]) -> list[tuple[dict[str, Any], str]]:
    """Pending steps that cannot start, with the honest reason."""
    done = {s["n"] for s in tx["steps"] if s["status"] in (VERIFIED, SKIPPED)}
    out = []
    for s in tx["steps"]:
        if s["status"] != PENDING:
            continue
        waiting = sorted(set(s["after"]) - done)
        if waiting:
            out.append((s, f"waits for step {', '.join(map(str, waiting))}"))
        elif not online(s["device"]):
            out.append((s, f"{s['device']} is not reachable"))
    return out


def begin(tx: dict[str, Any], n: int, online: Callable[[str], bool]) -> dict[str, Any]:
    s = _step(tx, n)
    if s["status"] not in (PENDING, UNCERTAIN, FAILED):
        raise TransactionError(f"Step {n} is {s['status']}.")
    if not online(s["device"]):
        raise TransactionError(f"{s['device']} is not reachable, so step {n} cannot start.")
    done = {x["n"] for x in tx["steps"] if x["status"] in (VERIFIED, SKIPPED)}
    if not set(s["after"]) <= done:
        raise TransactionError(f"Step {n} must wait for step {', '.join(str(a) for a in s['after'] if a not in done)}.")
    if s["attempts"] >= MAX_ATTEMPTS:
        raise TransactionError(f"Step {n} already failed {s['attempts']} times; choose another way (move it, skip it or stop).")
    s.update(status=RUNNING, attempts=s["attempts"] + 1, began=_now(), command=0, updated=_now())
    tx["state"] = ACTIVE
    _checkpoint(tx)
    return s


def verify(tx: dict[str, Any], n: int, evidence: str, reported_by: str) -> dict[str, Any]:
    """A step becomes verified only with evidence AND only when the device that owns it reported it."""
    s = _step(tx, n)
    if reported_by != s["device"]:
        raise TransactionError(f"Step {n} belongs to {s['device']}; a report from {reported_by} cannot verify it.")
    if not (evidence or "").strip():
        raise TransactionError("No evidence given, so the step stays unverified.")
    if s["status"] not in (RUNNING, UNCERTAIN):
        raise TransactionError(f"Step {n} is {s['status']}, not running.")
    s.update(status=VERIFIED, evidence=evidence.strip()[:300], updated=_now())
    _checkpoint(tx)
    return s


def fail(tx: dict[str, Any], n: int, reason: str) -> dict[str, Any]:
    s = _step(tx, n)
    s.update(status=FAILED, note=(reason or "")[:200], updated=_now())
    _checkpoint(tx)
    return s


FINAL_BAD = {"failed", "unsupported", "expired", "cancelled"}


def reconcile(tx: dict[str, Any], find: Callable[[str, str, str, int], dict[str, Any] | None]) -> list[str]:
    """Settle running PHONE steps from what the phone itself reported - no command id needs to be passed.

    ``find(device, action, since, command_id)`` returns the phone's command row ({id, status, result, action}) or None: the explicit command if one was
    attached, otherwise the newest command of that action sent to that device after the step began. ``done`` verifies the step with the phone's own
    result as evidence; failed / unsupported / expired / cancelled marks it failed (the options then offer retry / move / skip); pending / sent changes
    nothing. PC steps are never settled here. Returns what changed, one line each.
    """
    changed: list[str] = []
    for s in tx["steps"]:
        if s["status"] != RUNNING or s["device"] == "pc" or not s["action"]:
            continue
        row = find(s["device"], s["action"], s.get("began", ""), int(s.get("command") or 0))
        if row is None:
            continue
        s["command"] = int(row["id"])
        if row["status"] == "done":
            verify(tx, s["n"], f"{row['action']} finished on the phone (command {row['id']}): {(row.get('result') or 'no output')[:200]}", s["device"])
            changed.append(f"step {s['n']} verified by {s['device']}")
        elif row["status"] in FINAL_BAD:
            fail(tx, s["n"], f"phone command {row['id']} {row['status']}: {(row.get('result') or '')[:120]}")
            changed.append(f"step {s['n']} failed ({row['status']})")
    return changed


def device_lost(tx: dict[str, Any], device: str) -> list[dict[str, Any]]:
    """The device dropped: whatever it was running is UNCERTAIN (it may or may not have finished) - never assumed done, never replayed."""
    hit = []
    for s in tx["steps"]:
        if s["device"] == device and s["status"] == RUNNING:
            s.update(status=UNCERTAIN, note=f"{device} dropped while this step was running", updated=_now())
            hit.append(s)
    if hit:
        _checkpoint(tx)
    return hit


def options(tx: dict[str, Any], online: Callable[[str], bool], can: Callable[[str, str], bool] = lambda device, action: False) -> list[dict[str, Any]]:
    """What the user (or mission) can choose for each step that needs a decision. ``can(device, action)`` says if another device offers the action."""
    out = []
    devices = sorted({s["device"] for s in tx["steps"]})
    for s in tx["steps"]:
        if s["status"] not in (UNCERTAIN, FAILED):
            continue
        choices = []
        if s["status"] == UNCERTAIN:
            choices.append(f"check: ask {s['device']} whether step {s['n']} finished (verify only if it reports evidence)")
        if not online(s["device"]):
            choices.append(f"wait: resume when {s['device']} is back")
        for other in devices:
            if other != s["device"] and online(other) and s["action"] and can(other, s["action"]):
                choices.append(f"move: run step {s['n']} on {other}")
        if s["attempts"] < MAX_ATTEMPTS:
            choices.append(f"retry: run step {s['n']} again (attempt {s['attempts'] + 1} of {MAX_ATTEMPTS})")
        choices += [f"skip: leave step {s['n']} out (the result will not be complete)", "stop: end the transaction"]
        out.append({"step": s["n"], "why": s["note"] or s["status"], "choices": choices})
    return out


def move(tx: dict[str, Any], n: int, device: str) -> dict[str, Any]:
    s = _step(tx, n)
    if s["status"] not in (PENDING, UNCERTAIN, FAILED):
        raise TransactionError(f"Step {n} is {s['status']} and cannot be moved.")
    tx["decisions"].append(f"{_now()} step {n}: {s['device']} -> {device}")
    s.update(device=device, status=PENDING, note=f"moved from {s['device']}", updated=_now())
    _checkpoint(tx)
    return s


def skip(tx: dict[str, Any], n: int) -> dict[str, Any]:
    s = _step(tx, n)
    tx["decisions"].append(f"{_now()} step {n} skipped")
    s.update(status=SKIPPED, updated=_now())
    _checkpoint(tx)
    return s


def stop(tx: dict[str, Any], reason: str = "") -> None:
    tx["state"] = STOPPED
    tx["decisions"].append(f"{_now()} stopped {reason[:100]}".strip())


def describe(tx: dict[str, Any], online: Callable[[str], bool] = lambda d: True) -> str:
    mark = {PENDING: "[ ]", RUNNING: "[>]", VERIFIED: "[x]", FAILED: "[!]", UNCERTAIN: "[?]", SKIPPED: "[-]"}
    lines = [f"{tx['title']} ({tx['id']}): {tx['state'].replace('_', ' ')}"]
    for s in tx["steps"]:
        extra = f" - {s['evidence']}" if s["evidence"] else (f" - {s['note']}" if s["note"] else "")
        lines.append(f"  {mark[s['status']]} {s['n']}. {s['title']} [{s['device']}{'' if online(s['device']) else ', offline'}] {s['status']}{extra}")
    if tx["state"] == COMPLETE:
        lines.append("Every step was verified by the device that owns it.")
    elif tx["state"] == NEEDS_DECISION:
        lines.append("Needs your decision: a step failed or may or may not have finished. Nothing was repeated automatically.")
    return "\n".join(lines)


# ---- storage (settings store) ----------------------------------------------------------------------------------------------------------

def _load(store: Any) -> list[dict[str, Any]]:
    data = store.get(KEY, []) or []
    return data if isinstance(data, list) else []


def save(store: Any, tx: dict[str, Any]) -> None:
    items = [t for t in _load(store) if t.get("id") != tx["id"]] + [tx]
    while len(items) > MAX_KEPT:
        drop = next((i for i, t in enumerate(items) if t["state"] in (COMPLETE, STOPPED)), 0)
        items.pop(drop)
    store.set(KEY, items)


def get(store: Any, tx_id: str = "") -> dict[str, Any] | None:
    items = _load(store)
    if not tx_id:
        return next((t for t in reversed(items) if t["state"] in (ACTIVE, NEEDS_DECISION)), items[-1] if items else None)
    return next((t for t in items if t["id"] == tx_id or t["id"].startswith(tx_id)), None)


def listing(store: Any) -> list[dict[str, Any]]:
    return _load(store)
