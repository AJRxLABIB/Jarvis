"""Tool registry and executor: the only path from a model request to an action.

    ToolCall (untrusted) -> known tool? -> arguments valid? -> network allowed?
      -> permission decision -> user confirmation if needed -> handler with timeout
      -> audit entry -> ToolResult (sent back to the model)
"""

from __future__ import annotations

import asyncio
import json
import copy
import inspect
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.ai.base import ToolCall, ToolSpec
from app.memory.audit import AuditLog
from app.core.action_record import State
from app.security.confirmations import ConfirmationRequest, Confirmer, DenyConfirmer
from app.security.permissions import Decision, PermissionEngine, RiskLevel
from app.security.rate_limit import RateLimiter
from app.tools.base import Tool, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CAPTURE_TOOLS = frozenset({"take_screenshot", "read_screen", "describe_screen", "click_text", "click_thing",
                           "screen_recording"})
MAX_ARGUMENT_CHARS = 200_000  # a model-written call this large is a runaway, not a request

# Tools whose effect must not be repeated by a retry or a model repeating itself within ONE request: an identical call
# (same tool, same arguments) that already succeeded in this request returns the earlier result instead of running
# again. A separate request can still repeat it. Tools where repeating is normal (keys, volume, next track) are not here.
ONCE_PER_RUN_TOOLS = frozenset({"send_email", "whatsapp_message", "text_contact", "call_contact", "git_commit",
                                "add_todo", "write_document"})


# ---------------------------------------------------------------------- schema


def _simplify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $refs and drop titles: small local models handle flat schemas better."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs.get(node["$ref"].split("/")[-1], {})
                return walk(copy.deepcopy(target))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(schema)
    result.setdefault("type", "object")
    result.setdefault("properties", {})
    return result


# -------------------------------------------------------------------- registry


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._spec_cache: dict[str, tuple[Tool, ToolSpec]] = {}   # building a JSON schema takes ~0.2 ms; specs() runs per request
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not _NAME_RE.match(tool.name):
            raise ValueError(f"invalid tool name {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        if not tool.description.strip():
            raise ValueError(f"tool {tool.name!r} needs a description")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[ToolSpec]:
        """One spec per tool, in registration order. Specs are built once per tool object and reused (they are frozen and
        nothing edits their schemas); a tool replaced under the same name gets a fresh one."""
        out = []
        for name, tool in self._tools.items():
            cached = self._spec_cache.get(name)
            if cached is None or cached[0] is not tool:
                cached = (tool, ToolSpec(tool.name, tool.description, _simplify_schema(tool.args_model.model_json_schema())))
                self._spec_cache[name] = cached
            out.append(cached[1])
        return out


# -------------------------------------------------------------------- executor


@dataclass(frozen=True)
class ToolExecution:
    call: ToolCall
    result: ToolResult
    level: RiskLevel | None = None
    decision: Decision | None = None
    elapsed_ms: int = 0
    action_id: str | None = None             # app/core/action_record.py: the lifecycle record of this call


def _repair(tool: Tool, call: ToolCall, error: ValidationError, context: ToolContext):
    """Fix a near-miss choice or a bad optional value instead of failing (app/agent/arg_repair.py) - only when
    the repaired call is SAFE for its exact arguments. power_control is "safe" for lock but DANGEROUS for
    restart, so "restat" -> "restart" is refused; a guess never picks a riskier action."""
    if tool.level != RiskLevel.SAFE:
        return None
    from app.agent.arg_repair import repair

    result = repair(tool.args_model, dict(call.arguments), error)
    if result is None:
        return None
    fixed, changes = result
    args = tool.args_model.model_validate(fixed)
    try:
        if tool.level_for(args, context) != RiskLevel.SAFE:
            return None
    except Exception:
        return None
    log_event(_log, "arguments_repaired", tool=tool.name, changes=changes)
    return ToolCall(call.name, fixed, id=call.id), args


def _validation_summary(error: ValidationError) -> str:
    parts = []
    for issue in error.errors()[:5]:
        where = ".".join(str(p) for p in issue["loc"]) or "arguments"
        parts.append(f"{where}: {issue['msg']}")
    return "; ".join(parts)


@dataclass
class _Gate:
    """What ToolExecutor._gate decided for one call (internal)."""

    decision: Decision | None = None
    level: RiskLevel | None = None
    result: ToolResult | None = None          # set: the call ends with this result
    run: Any = None                           # the current request (app.core.runs), if any
    once_key: str | None = None
    taint_reason: str | None = None
    strict: bool = False                      # autonomy: everything that changes something asks
    approved_by: str = "policy"               # who/what allowed it without asking (recorded on the action)
    note: str = ""                            # extra words for the confirmation (e.g. an earlier uncertain attempt)
    record: Any = None                        # the ActionRecord (app/core/action_record.py)

    def end(self, result: ToolResult) -> "_Gate":
        self.result = result
        return self


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        context: ToolContext,
        *,
        confirmer: Confirmer | None = None,
        audit: AuditLog | None = None,
        confirm_timeout: float = 60.0,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.rate_limiter = rate_limiter or RateLimiter()
        if getattr(context, "executor", None) is None:
            context.executor = self  # tools that run other tools (workflows) use the same checks
        self.registry = registry
        self.permissions = permissions
        self.context = context
        self.confirmer = confirmer or DenyConfirmer()
        self.audit = audit
        self.confirm_timeout = confirm_timeout
        self.approval_ttl_s = 120.0          # an approval covers its exact action for this long (app/core/action_record.py)

    def _full_power_allows(self) -> bool:
        """Full power applies only to requests from you at this PC (typed or spoken) - never to a phone, an automation,
        an agent or Jarvis's own internal steps."""
        if not getattr(self.context.settings, "full_power", False):
            return False
        from app.core import runs

        run = runs.current()
        return run is None or run.source in ("text", "voice", "cli")

    def _standing_grant(self, tool: Tool, args: Any):
        """A live "don't ask me again" approval covering this exact call (app/security/grants.py), or None. Never used while
        any emergency switch is on (and then every grant is revoked), and re-checked against the fixed grantable() rules."""
        database = getattr(self.context, "database", None)
        if database is None:
            return None
        try:
            from app.core import emergency
            from app.security.grants import GrantStore, grantable

            store = GrantStore(database)
            if emergency.STATE.active():
                store.revoke_all()
                return None
            from app.core import runs

            run = runs.current()
            if run is not None and run.source == "phone":
                return None          # grants were made on THIS PC: a request from a phone always asks (device-scoped)
            grant = store.find(tool.name, args.model_dump(mode="json"))
            if grant is None or not grantable(tool.name, tool.category, RiskLevel.MODERATE)[0]:
                return None
            store.used(grant)
            return grant
        except Exception:
            _log.warning("standing approval check failed; asking as usual", exc_info=True)
            return None

    def _audit(self, event: str, call: ToolCall, **kwargs: Any) -> None:
        if self.audit is not None:
            details = {"arguments": dict(call.arguments), **kwargs.pop("details", {})}
            self.audit.record(event, tool=call.name, details=details, **kwargs)

    async def execute(self, call: ToolCall, *, user_command: bool = False, preapproved: bool = False,
                      mission: str = "") -> ToolExecution:
        """Run one tool call through every check. ``user_command`` = the call was matched directly from
        the user's own words (a direct command), not chosen by the model. ``preapproved`` = a mission step the
        user approved as part of an exact plan (app/core/missions.py): MODERATE steps then don't ask again;
        DANGEROUS ones still do, CRITICAL stays blocked.

        The phases, in this order (each can end the call with a result; nothing runs unless all pass):
        1. ``_prepare``  - the tool exists, the arguments are well-formed / repaired, its own precheck;
        2. offline check - network tools refuse while offline;
        3. ``_gate``     - permission level, agent contract, once-per-run, prompt-injection taint, emergency stop /
                           modes, rate limit, FULL_POWER, block, autonomy, then the reasons a confirmation may be
                           skipped (your direct command, an approved plan, a standing approval);
        4. ``_ask``      - the confirmation, when one is still needed;
        5. ``_run_checked`` - run with a timeout, verify, record undo / taint / once-per-run."""
        started = time.perf_counter()
        record: Any = None

        def done(result: ToolResult, level: RiskLevel | None = None, decision: Decision | None = None) -> ToolExecution:
            elapsed = int((time.perf_counter() - started) * 1000)
            if record is not None:
                self._finish_record(record, result)
            log_event(
                _log, "tool_call", tool=call.name, ok=result.ok, error=result.error,
                risk_level=int(level) if level is not None else None,
                decision=decision.value if decision else None, elapsed_ms=elapsed,
            )
            self._audit(
                "tool_result", call, level=int(level) if level is not None else None,
                decision=decision.value if decision else None,
                details={"ok": result.ok, "error": result.error, "message": result.message, "elapsed_ms": elapsed},
            )
            return ToolExecution(call, result, level, decision, elapsed, record.id if record is not None else None)

        prepared = self._prepare(call)
        if isinstance(prepared, ToolResult):
            return done(prepared)
        tool, call, args = prepared
        record = self._new_record(tool, call, args, mission)

        if tool.needs_network:
            from app.utils import network

            reason = await asyncio.to_thread(network.offline_reason, self.context.settings)
            if reason is not None:
                return done(ToolResult.failure("offline", reason))

        gate = self._gate(tool, call, args, user_command=user_command, preapproved=preapproved)
        if gate.result is not None:
            return done(gate.result, gate.level, gate.decision)
        gate.record = record
        record.level = int(gate.level) if gate.level is not None else 0     # the real, argument-based risk
        self._guard_uncertain_retry(tool, record, gate)
        if gate.decision == Decision.CONFIRM:
            record.to(State.AWAITING_APPROVAL, "asking")
            refused = await self._ask(tool, call, args, gate)
            if refused is not None:
                return done(refused, gate.level, gate.decision)
            record.approve("user", self.approval_ttl_s)
        else:
            record.approve(gate.approved_by, self.approval_ttl_s)
        stale = self._stale_approval(record, args)
        if stale is not None:
            return done(stale, gate.level, gate.decision)
        outcome = await self._run_checked(tool, call, args, gate)
        return done(outcome, gate.level, gate.decision)

    def _prepare(self, call: ToolCall):
        """(tool, call, args) ready to check, or the ToolResult that ends the call (unknown / malformed / precheck)."""
        tool = self.registry.get(call.name)
        if tool is None:
            import difflib

            all_names = [t.name for t in self.registry.tools]
            close = difflib.get_close_matches(str(call.name), all_names, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else f" Available: {', '.join(all_names) or 'none'}."
            return ToolResult.failure("unknown_tool", f"There is no tool named '{call.name}'.{hint}")
        if call.parse_error:
            return ToolResult.failure("invalid_arguments", f"The arguments were malformed: {call.parse_error}.")
        try:
            too_big = len(json.dumps(call.arguments, default=str)) > MAX_ARGUMENT_CHARS
        except (TypeError, ValueError):
            too_big = True
        if too_big:
            return ToolResult.failure("invalid_arguments", "The tool was NOT run: its arguments are far too large "
                                      "or not plain data. Send smaller, simpler arguments.")
        try:
            args = tool.args_model.model_validate(dict(call.arguments))
        except ValidationError as exc:
            repaired = _repair(tool, call, exc, self.context)
            if repaired is None:
                return ToolResult.failure(
                    "invalid_arguments",
                    f"The tool was NOT run because of invalid arguments ({_validation_summary(exc)}). "
                    "Correct the arguments and call the tool again.",
                )
            call, args = repaired

        if tool.precheck is not None:
            try:
                tool.precheck(args, self.context)
            except ToolError as exc:
                return ToolResult.failure(exc.code, str(exc))
            except Exception:
                _log.exception("precheck of %s crashed", tool.name)
                return ToolResult.failure("internal", "The request could not be checked, so nothing was done.")
        return tool, call, args

    def _gate(self, tool: Tool, call: ToolCall, args, *, user_command: bool, preapproved: bool) -> "_Gate":
        """Every policy decision before anything runs. ``gate.result`` set = the call ends here."""
        gate = _Gate()
        try:
            gate.decision, gate.level = self.permissions.decide(tool.name, tool.level_for(args, self.context))
        except Exception:
            _log.exception("permission evaluation failed")
            gate.result = ToolResult.failure("permission_error", "The permission check failed, so nothing was done.")
            gate.level = gate.decision = None
            return gate
        level = gate.level

        contract = getattr(self.context, "agent_contract", None)
        if contract is not None:
            ok_contract, why = contract.allows(tool.name, tool.category, level)
            if not ok_contract:
                self._audit("agent_contract_denied", call, level=int(level), decision="blocked",
                            details={"agent": contract.name, "reason": why})
                return gate.end(ToolResult.failure("agent_not_allowed",
                                f"Not done: {why}. Do it yourself or give the task to a different agent."))

        from app.core import runs
        from app.security.taint import egress_reason

        run = gate.run = runs.current()
        if run is not None and tool.name in ONCE_PER_RUN_TOOLS:
            gate.once_key = tool.name + json.dumps(args.model_dump(mode="json"), sort_keys=True, default=str)
            earlier = run.completed.get(gate.once_key)
            if earlier is not None:
                self._audit("duplicate_call_skipped", call, level=int(level), decision="not_repeated")
                return gate.end(ToolResult.success(
                    f"Already done earlier in this request, so I did not do it again: {earlier}", duplicate=True))
        if run is not None and not user_command:
            # prompt-injection defence: after reading untrusted text, what the MODEL sends out asks first
            gate.taint_reason = egress_reason(tool.name, args.model_dump(), run.taint, set(run.user_words), tool.category)
            if gate.taint_reason is None:
                from app.security.taint import persistence_reason

                gate.taint_reason = persistence_reason(tool.name, run.taint)
            if gate.taint_reason is not None and gate.decision == Decision.ALLOW:
                gate.decision = Decision.CONFIRM
                self._audit("taint_confirmation", call, level=int(level), decision="confirm",
                            details={"reason": gate.taint_reason})

        from app.core import emergency, modes
        from app.security.taint import is_egress

        stop = emergency.STATE.blocks(level=int(level), needs_network=tool.needs_network,
                                      egress=is_egress(tool.name, tool.category))
        if stop is None and not modes.STATE.current.capture and tool.name in CAPTURE_TOOLS:
            stop = f"{modes.STATE.current.name.capitalize()} mode: no screen capture. Say 'normal mode' to allow it."
        if stop is None and tool.name in CAPTURE_TOOLS:
            from app.core import privacy_zones

            zone = privacy_zones.restriction("capture")
            if zone is not None:
                stop = f"{zone}: I won't look at the screen. Switch the zone off in 'privacy zones' if you really want that."
        if stop is not None:
            return gate.end(ToolResult.failure("blocked", stop))
        if not self.rate_limiter.allow(level):
            return gate.end(ToolResult.failure(
                "rate_limited", "Too many actions in a short time, so this one was not done. Wait a moment."))
        if gate.decision == Decision.BLOCK and level == RiskLevel.CRITICAL and self._full_power_allows():
            # FULL_POWER: nothing is off-limits, but a critical action always asks you - on screen, for this exact
            # call (no voice "yes", no phone, no standing approval, no direct-command shortcut, no automation)
            gate.decision = Decision.CONFIRM
            self._audit("critical_needs_you", call, level=int(level), decision="full_power")
        if gate.decision == Decision.BLOCK:
            return gate.end(ToolResult.failure(
                "blocked", "Not done: this action is blocked by the permission settings."
                + (" (FULL_POWER is off - Settings > Security.)" if level == RiskLevel.CRITICAL
                   and not getattr(self.context.settings, "full_power", False) else "")))
        if self._autonomy(tool, call, args, gate):
            return gate
        self._skip_confirmation_when_allowed(tool, call, args, gate, user_command=user_command, preapproved=preapproved)
        return gate

    def _autonomy(self, tool: Tool, call: ToolCall, args, gate: "_Gate") -> bool:
        """AUTONOMY (stricter for phone requests): may refuse (True = the call ends) or make it ask."""
        from app.security import autonomy

        settings = self.context.settings
        autonomy_level = autonomy.normalise(getattr(settings, "autonomy", "routine"))
        if gate.run is not None and gate.run.source == "phone":       # a phone request: the stricter level
            autonomy_level = autonomy.stricter(autonomy_level, getattr(settings, "autonomy_remote", "routine"))
        refusal = autonomy.blocked_in(autonomy_level, tool.name, args.model_dump(), tool.category,
                                      autonomy.categories(getattr(settings, "autonomy_block_categories", "")))
        if refusal is not None:
            self._audit("autonomy_blocked", call, level=int(gate.level), decision="blocked",
                        details={"mode": autonomy_level})
            code = "suggest_mode" if autonomy_level == "suggest" and refusal.startswith("Suggested") else "observe_mode"
            gate.end(ToolResult.failure(code, refusal))
            gate.decision = Decision.BLOCK
            return True
        gate.strict = autonomy.must_ask(autonomy_level, tool.name, args.model_dump(), tool.category,
                                        autonomy.categories(getattr(settings, "autonomy_ask_categories", "")))
        if gate.strict and gate.decision == Decision.ALLOW:
            gate.decision = Decision.CONFIRM                  # approval mode: everything that changes something asks
        return False

    def _skip_confirmation_when_allowed(self, tool: Tool, call: ToolCall, args, gate: "_Gate", *, user_command: bool,
                                        preapproved: bool) -> None:
        """A confirmation is skipped only for: your own direct command (non-critical, the tool allows it), a MODERATE
        step of a plan you approved, or a MODERATE call covered by a standing approval - never under strict
        autonomy, never after untrusted text for standing approvals, never for CRITICAL."""
        level, strict = gate.level, gate.strict
        if (gate.decision == Decision.CONFIRM and user_command and level < RiskLevel.CRITICAL and not strict
                and tool.direct_without_confirmation is not None and tool.direct_without_confirmation(args, self.context)):
            self._audit("confirmation_skipped", call, level=int(level), decision="user_command")
            gate.decision, gate.approved_by = Decision.ALLOW, "direct_command"
        if gate.decision == Decision.CONFIRM and preapproved and level == RiskLevel.MODERATE and not strict:
            self._audit("confirmation_preapproved", call, level=int(level), decision="approved_plan")
            gate.decision, gate.approved_by = Decision.ALLOW, "approved_plan"
        if gate.decision == Decision.CONFIRM and level == RiskLevel.MODERATE and gate.taint_reason is None and not strict:
            grant = self._standing_grant(tool, args)
            if grant is not None:
                self._audit("confirmation_skipped", call, level=int(level), decision="standing_approval",
                            details={"grant": grant.id})
                gate.decision, gate.approved_by = Decision.ALLOW, "standing_grant"

    async def _ask(self, tool: Tool, call: ToolCall, args, gate: "_Gate") -> ToolResult | None:
        """Ask the user. None = approved; else the result that ends the call."""
        level, taint_reason = gate.level, gate.taint_reason
        summary = tool.confirmation_text(args, self.context)
        if taint_reason is not None:
            summary = f"{summary} (I'm asking because {taint_reason}.)"
        if gate.note:
            summary = f"{summary} ({gate.note})"
        if level == RiskLevel.CRITICAL:
            summary = f"CRITICAL (normally refused - full power is on): {summary}"
        request = ConfirmationRequest(tool.name, level, summary, args.model_dump())
        self._audit("confirmation_requested", call, level=int(level), decision="confirm")
        try:
            approved = await asyncio.wait_for(self.confirmer.confirm(request), self.confirm_timeout)
        except asyncio.TimeoutError:
            approved = False
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("confirmation failed")
            approved = False
        self._audit("confirmation_answered", call, level=int(level), decision="approved" if approved else "denied")
        if approved:
            return None
        if taint_reason is not None:  # the user stopped something outside text was steering towards
            from app.core import events

            try:
                events.BUS.publish(events.SECURITY_EVENT, priority="normal",
                                   message=f"You refused '{tool.name}' after I had read outside text ({taint_reason}).")
            except Exception:
                pass
        return ToolResult.failure("declined", "Cancelled: the user said no, so nothing was done.")

    async def _run_checked(self, tool: Tool, call: ToolCall, args, gate: "_Gate") -> ToolResult:
        """Run it (timeout, errors contained), verify, then record undo / once-per-run / taint."""
        run = gate.run
        if run is not None:
            run.confirmed_after_untrusted = gate.taint_reason is not None  # the handler records it (memory provenance)
        if gate.record is not None:
            gate.record.to(State.RUNNING)
        try:
            outcome = await asyncio.wait_for(self._run(tool, args), tool.timeout)
        except asyncio.TimeoutError:
            return ToolResult.failure("timeout", f"'{tool.name}' took too long and was stopped. It may or may not have "
                                      "finished - check before trying again.", uncertain=True)
        except ToolError as exc:
            return ToolResult.failure(exc.code, str(exc), **getattr(exc, "data", {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("tool %s crashed", tool.name, extra={"event": "tool_crashed"})
            return ToolResult.failure("internal", "The action failed unexpectedly. The details are in the log.")
        if outcome.ok and tool.verify is not None:
            if gate.record is not None:
                gate.record.to(State.VERIFYING)
            outcome = await self._verify(tool, args, outcome, call)
        if outcome.ok:
            outcome = self._stamp_verification(tool, args, outcome)
            self._teach(tool, args)
            self._record_undo(call, outcome, run)
            if run is not None and gate.once_key is not None:
                run.completed[gate.once_key] = outcome.message[:200]
        if run is not None:
            run.tools.append(tool.name)
            if run.taint is not None and outcome.ok:
                try:
                    run.taint.absorb(tool.name, outcome.to_model_text())
                except Exception:
                    _log.warning("taint tracking failed", exc_info=True)
        return outcome

    # ---- the action record (app/core/action_record.py) -------------------------------------------------------------
    def _new_record(self, tool: Tool, call: ToolCall, args: Any, mission: str = ""):
        from app.core import runs
        from app.core.action_record import LEDGER, ActionRecord

        run = runs.current()
        source = run.source if run is not None else "text"
        record = ActionRecord(
            tool=tool.name, arguments=args.model_dump(mode="json"), level=0,
            origin_device="phone" if source == "phone" else "pc", input_method=str(source),
            target_device="phone" if tool.category == "phone" else "pc", capability=tool.category,
            mission=mission)
        record.on_change = self._mirror
        LEDGER.add(record)
        record.to(State.VALIDATED)
        return record

    def _mirror(self, record: Any, state: State, note: str) -> None:
        """Only the moments that matter go to the audit log: asking, approving, and every way an action can end."""
        if self.audit is None or state in (State.VALIDATED, State.RUNNING, State.VERIFYING):
            return
        self.audit.record("action_state", tool=record.tool, level=record.level, decision=state.value,
                          details={"action": record.id, "state": state.value, "note": note, "input": record.input_method,
                                   "device": record.target_device, "key": record.idempotency_key})

    def _guard_uncertain_retry(self, tool: Tool, record: Any, gate: "_Gate") -> None:
        """The same change attempted before and left UNCERTAIN (timeout, lost answer): repeating it blindly could do it twice,
        so a retry always asks - and says why."""
        from app.core.action_record import LEDGER, State as S

        if gate.level is None or int(gate.level) < int(RiskLevel.MODERATE):
            return
        prior = LEDGER.duplicate_of(record)
        if prior is not None and prior.state == S.UNCERTAIN:
            gate.note = "the same action was tried before and I could not confirm whether it happened"
            if gate.decision == Decision.ALLOW:
                gate.decision = Decision.CONFIRM

    def _stale_approval(self, record: Any, args: Any) -> ToolResult | None:
        """Last check before running: the approval must still cover exactly what is about to run."""
        from app.core.action_record import digest

        approval = record.approval
        now_digest = digest(record.tool, args.model_dump(mode="json"), target_device=record.target_device,
                            target_app=record.target_app, mission=record.mission, level=record.level)
        ok, why = approval.covers(now_digest) if approval is not None else (False, "no approval")
        if ok:
            return None
        record.to(State.REJECTED, why)
        return ToolResult.failure("approval_stale", f"Not done: {why}. Ask again.")

    def _finish_record(self, record: Any, result: ToolResult) -> None:
        if record.finished:
            return
        if result.ok:
            if record.state in (State.RUNNING, State.VERIFYING):
                if result.data.get("pending"):                    # queued for a device that has not answered: may still happen
                    record.recovery = "check the device (status), or withdraw the request, before sending it again"
                    record.to(State.UNCERTAIN, "queued, no answer yet")
                else:
                    record.to(State.SUCCEEDED, "ok")
            return
        running = record.state in (State.RUNNING, State.VERIFYING)
        if running and (result.error == "timeout" or result.data.get("uncertain")):
            record.recovery = "inspect the real state before retrying"
            record.to(State.UNCERTAIN, result.error or "")
        elif running:
            record.to(State.FAILED, result.error or "")
        elif result.error == "timeout":
            record.to(State.EXPIRED, "no answer in time")
        else:
            record.to(State.REJECTED, result.error or "")

    def _teach(self, tool: Tool, args: Any) -> None:
        """A teach session (app/automation/teach.py) remembers the tools Jarvis really ran - nothing else."""
        from app.automation.teach import TEACHER

        if not TEACHER.active:
            return
        try:
            TEACHER.record(tool.name, args.model_dump(exclude_defaults=True), tool.level_for(args, self.context))
        except Exception:
            _log.warning("teach session could not record %s", tool.name, exc_info=True)

    async def _verify(self, tool: Tool, args: Any, outcome: ToolResult, call: ToolCall) -> ToolResult:
        """The verification contract: the tool's own check of the real world after it said "done"."""
        from app.core import activity

        try:
            with activity.verifying():
                ok, found = await asyncio.wait_for(asyncio.to_thread(tool.verify, args, outcome, self.context), 10.0)
        except Exception:
            _log.warning("verification of %s failed to run", tool.name, exc_info=True)
            return ToolResult(True, outcome.message, {**outcome.data, "verification": "UNCERTAIN"})  # can't check: say what the tool said, claim no more
        self._audit("verification", call, details={"ok": ok, "found": found})
        if ok:
            return ToolResult(True, outcome.message, {**outcome.data, "verified": True, "verification": "VERIFIED"})
        return ToolResult.failure("not_verified", f"{outcome.message} But when I checked, {found}.",
                                  **{**outcome.data, "verified": False, "verification": "FAILED"})

    def _stamp_verification(self, tool: Tool, args: Any, outcome: ToolResult) -> ToolResult:
        """Every successful result carries one standard verification state (VERIFIED / FAILED / UNCERTAIN / NOT_SUPPORTED / NOT_REQUIRED).
        A tool that changes things but has no check of its own is NOT_SUPPORTED: its "done" is the tool's word, not independent evidence."""
        if "verification" in outcome.data:
            return outcome
        try:
            level = tool.level_for(args, self.context)
        except Exception:
            level = tool.level
        state = "NOT_REQUIRED" if level == RiskLevel.SAFE else "NOT_SUPPORTED"
        return ToolResult(True, outcome.message, {**outcome.data, "verification": state})

    def _record_undo(self, call: ToolCall, outcome: ToolResult, run: Any) -> None:
        database = getattr(self.context, "database", None)
        if database is None:
            return
        from app.core import undo

        if undo.UNDOING.get():
            return  # this call IS an undo: don't offer to undo the undo
        try:
            inverse = undo.inverse_of(call, outcome)
            if inverse is not None:
                undo.UndoStore(database).record(call, inverse, run.id if run is not None else None)
        except Exception:
            _log.warning("could not record undo information", exc_info=True)

    async def _run(self, tool: Tool, args: Any) -> ToolResult:
        if inspect.iscoroutinefunction(tool.handler):
            raw = await tool.handler(args, self.context)
        else:
            raw = await asyncio.to_thread(tool.handler, args, self.context)
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, dict):
            return ToolResult.success(**raw)
        return ToolResult.success(str(raw) if raw is not None else "")
