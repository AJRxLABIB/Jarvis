"""transaction: one task across the PC and a phone (app/core/transactions.py). A ledger + decision aid; the steps themselves are run by the normal tools.

A PC step is verified with the evidence you give; a PHONE step only with a finished command the phone itself returned (phone_commands row for that
device with status done) - the PC's own word is never enough for the phone's work."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import Field

from app.core import transactions as T
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

PC = "pc"


class TransactionArgs(ToolArgs):
    action: Literal["create", "status", "next", "begin", "verify", "fail", "device_lost", "options", "move", "skip", "stop", "list", "reconcile"] = Field(
        default="status", description="create = new task across devices; next = steps that can start now; begin / verify / fail = move one step; "
                                      "device_lost = a device dropped; options = what to do about uncertain / failed steps; move / skip / stop = your decision")
    id: str = Field(default="", max_length=20, description="Transaction id (default: the latest open one)")
    title: str = Field(default="", max_length=120, description="For create: what the whole task is")
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=T.MAX_STEPS,
                                        description="For create: [{title, device ('pc' or a paired phone id), action, after:[earlier step numbers]}]")
    mission_id: str = Field(default="", max_length=60, description="For create: the related mission id, if any")
    step: int = Field(default=0, ge=0, le=T.MAX_STEPS, description="Step number for begin / verify / fail / move / skip")
    device: str = Field(default="", max_length=60, description="For device_lost / move: the device")
    evidence: str = Field(default="", max_length=300, description="For verify of a PC step: what you checked (a file exists, a command printed ...)")
    phone_command: int = Field(default=0, ge=0, description="For verify of a phone step: the id of the finished command the phone returned")
    reason: str = Field(default="", max_length=200, description="For fail / stop: why")


def _store(ctx: ToolContext):
    from app.memory.settings_store import SettingsStore

    if ctx.database is None:
        raise ToolError("Transactions need the local database.", "no_database")
    return SettingsStore(ctx.database)


def _online_map(ctx: ToolContext) -> tuple[Any, Any]:
    """(online(device), can(device, action)) from the unified device list; the PC is always reachable from itself."""
    online_keys: dict[str, Any] = {PC: True}
    abilities: dict[str, set[str]] = {}
    try:
        from app.core import device_mesh

        for n in device_mesh.snapshot(ctx.database):
            online_keys[n.key] = n.connection in ("local", "connected", "recent")
            abilities[n.key] = {a for a, s in n.abilities if s == "available"}
            if n.kind == "pc":
                online_keys[PC] = True
    except Exception:
        pass
    return (lambda d: bool(online_keys.get(d, False)), lambda d, action: action in abilities.get(d, set()))


def phone_result(db: Any, device: str, command_id: int) -> str | None:
    """The phone's own finished result for this command, or None (not found, another device's, not done)."""
    with db.connect() as conn:
        row = conn.execute("SELECT status, result, action FROM phone_commands WHERE id = ? AND device_id = ?", (command_id, device)).fetchone()
    if row is None or row["status"] != "done":
        return None
    return f"{row['action']} finished on the phone: {(row['result'] or 'no output')[:200]}"


def find_command(db: Any, device: str, action: str, since: str, command_id: int = 0) -> dict[str, Any] | None:
    """The phone's own command row for a step: the attached one, or the newest of this action for this device sent after the step began."""
    with db.connect() as conn:
        if command_id:
            row = conn.execute("SELECT id, status, result, action FROM phone_commands WHERE id = ? AND device_id = ?", (command_id, device)).fetchone()
        else:
            row = conn.execute("SELECT id, status, result, action FROM phone_commands WHERE device_id = ? AND action = ? AND created >= ? "
                               "ORDER BY id DESC LIMIT 1", (device, action, since or "")).fetchone()
    return dict(row) if row else None


def reconcile_all(db: Any) -> list[str]:
    """Settle every open transaction from the phone's reports (also run by the background upkeep loop). Never raises."""
    from app.memory.settings_store import SettingsStore

    notes: list[str] = []
    try:
        store = SettingsStore(db)
        for tx in T.listing(store):
            if tx["state"] in (T.COMPLETE, T.STOPPED):
                continue
            changed = T.reconcile(tx, lambda d, a, s, c: find_command(db, d, a, s, c))
            if changed:
                T.save(store, tx)
                notes += [f"{tx['title']}: {c}" for c in changed]
    except Exception:
        pass
    return notes


async def transaction(args: TransactionArgs, ctx: ToolContext) -> ToolResult:
    store = _store(ctx)
    online, can = _online_map(ctx)
    try:
        if args.action == "create":
            tx = T.create(args.title, args.steps, mission_id=args.mission_id)
            T.save(store, tx)
            return ToolResult.success(T.describe(tx, online), id=tx["id"])
        if args.action == "reconcile":
            notes = await asyncio.to_thread(reconcile_all, ctx.database)
            return ToolResult.success("\n".join(notes) or "Nothing new from the phone.", changed=len(notes))
        if args.action == "list":
            return ToolResult.success("\n".join(f"{t['id']}  {t['state']}  {t['title']}" for t in T.listing(store)[-15:]) or "No transactions.")
        tx = T.get(store, args.id)
        if tx is None:
            return ToolResult.failure("not_found", "No such transaction.")
        if tx["state"] not in (T.COMPLETE, T.STOPPED) and args.action in ("status", "next", "options"):
            if T.reconcile(tx, lambda d, a, s, c: find_command(ctx.database, d, a, s, c)):
                T.save(store, tx)                   # what the phone reported since the last look
        if args.action == "status":
            return ToolResult.success(T.describe(tx, online), state=tx["state"])
        if args.action == "next":
            ready = T.runnable(tx, online)
            lines = [f"{s['n']}. {s['title']} on {s['device']}" for s in ready]
            lines += [f"(waiting) {s['n']}. {s['title']}: {why}" for s, why in T.blocked(tx, online)]
            return ToolResult.success("\n".join(lines) or "Nothing can start now.", ready=[s["n"] for s in ready])
        if args.action == "options":
            opts = T.options(tx, online, can)
            return ToolResult.success("\n".join(f"Step {o['step']} ({o['why']}):\n  - " + "\n  - ".join(o["choices"]) for o in opts) or "No step needs a decision.",
                                      options=opts)
        if args.action == "stop":
            T.stop(tx, args.reason)
        elif args.action == "device_lost":
            hit = T.device_lost(tx, args.device)
            T.save(store, tx)
            return ToolResult.success((f"{len(hit)} step(s) are now uncertain (not repeated, not assumed done). " if hit else "No step was running there. ")
                                      + "Use 'options' to decide.", uncertain=[s["n"] for s in hit])
        elif args.action == "begin":
            T.begin(tx, args.step, online)
        elif args.action == "fail":
            T.fail(tx, args.step, args.reason)
        elif args.action == "move":
            T.move(tx, args.step, args.device)
        elif args.action == "skip":
            T.skip(tx, args.step)
        elif args.action == "verify":
            s = next((x for x in tx["steps"] if x["n"] == args.step), None)
            if s is None:
                raise T.TransactionError(f"No step {args.step}.")
            if s["device"] == PC:
                T.verify(tx, args.step, args.evidence, PC)
            else:
                proof = phone_result(ctx.database, s["device"], args.phone_command) if args.phone_command else None
                if proof is None:
                    raise T.TransactionError(f"Step {args.step} belongs to {s['device']}: it can only be verified by a finished command that device "
                                             "returned (give phone_command). I will not mark it done on my own word.")
                T.verify(tx, args.step, proof, s["device"])
    except T.TransactionError as exc:
        return ToolResult.failure("transaction", str(exc))
    T.save(store, tx)
    return ToolResult.success(T.describe(tx, online), state=tx["state"])


TOOLS: tuple[Tool, ...] = (
    Tool("transaction", "Coordinate one task across the PC and a paired phone: steps with an owner device, dependencies, checkpoints, honest "
         "completion (a phone step counts only with a result the phone returned) and recovery choices when a device drops.",
         transaction, TransactionArgs, RiskLevel.SAFE, "windows", timeout=30),
)
