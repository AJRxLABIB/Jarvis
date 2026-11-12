"""Why did it fail, and is trying again sensible? One place for the answer.

Errors are sorted into kinds, and each kind has a policy:

* ``transient``   (timeout, offline, rate limit, a busy app): retry with growing waits, a small fixed number of times.
* ``resource``    (not enough memory, a busy file): wait longer, fewer tries.
* ``verification`` (the action ran but the result wasn't confirmed): one more try - never for anything that changes
  state unless the caller says the action is safe to repeat.
* ``permission`` / ``validation`` / ``permanent``: never retried - trying again cannot help; someone has to decide.
* ``unknown``: treated as permanent, so a new kind of failure is shown to you instead of being hammered.

Retries are bounded by attempts AND by a total wait, and the waits are computed (not slept) here so tests need no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

TRANSIENT = frozenset({"timeout", "offline", "rate_limited", "launch_failed", "embedding_unavailable", "still_running",
                       "internal"})
RESOURCE = frozenset({"out_of_memory", "resource_busy", "disk_full"})
VERIFICATION = frozenset({"not_verified", "not_visible", "still_open", "still_exists"})
PERMISSION = frozenset({"blocked", "denied", "locked", "protected", "permission_error", "agent_contract_denied", "agent_run_expired", "lease_denied",
                        "needs_approval", "sensitive"})
VALIDATION = frozenset({"invalid_arguments", "invalid_plan", "invalid_steps", "invalid_name", "invalid_time",
                        "invalid_days", "invalid_date", "invalid_watch", "unknown_tool", "missing_name",
                        "missing_project", "missing_time", "ambiguous", "not_found", "unsupported"})


@dataclass(frozen=True)
class Policy:
    kind: str
    max_attempts: int       # including the first try
    base_seconds: float
    factor: float = 2.0
    max_total_seconds: float = 30.0


POLICIES = {
    "transient": Policy("transient", 3, 1.0),
    "resource": Policy("resource", 2, 5.0, 2.0, 20.0),
    "verification": Policy("verification", 2, 1.0),
    "permission": Policy("permission", 1, 0.0),
    "validation": Policy("validation", 1, 0.0),
    "permanent": Policy("permanent", 1, 0.0),
    "unknown": Policy("unknown", 1, 0.0),
}


def classify(error: str | None, message: str = "") -> str:
    code = (error or "").strip().lower()
    if code in TRANSIENT:
        return "transient"
    if code in RESOURCE:
        return "resource"
    if code in VERIFICATION:
        return "verification"
    if code in PERMISSION:
        return "permission"
    if code in VALIDATION:
        return "validation"
    text = message.lower()
    if any(w in text for w in ("timed out", "timeout", "connection", "temporarily", "try again", "rate limit",
                               "429", "503")):
        return "transient"
    if any(w in text for w in ("out of memory", "not enough memory")):
        return "resource"
    if any(w in text for w in ("permission", "not allowed", "access is denied", "forbidden", "unauthorized")):
        return "permission"
    return "unknown" if not code else "permanent"


def delay_before(policy: Policy, attempt: int) -> float:
    """Seconds to wait before attempt number ``attempt`` + 1 (``attempt`` = tries made so far, from 1)."""
    return min(policy.base_seconds * (policy.factor ** max(attempt - 1, 0)), policy.max_total_seconds)


def should_retry(error: str | None, message: str, attempts_made: int, *, repeatable: bool,
                 waited_seconds: float = 0.0) -> tuple[bool, float, str]:
    """(retry?, seconds to wait, plain reason). ``repeatable`` = running the action again cannot do harm (read-only,
    or one that checks before acting). A state-changing action is never retried after an unverified result."""
    kind = classify(error, message)
    policy = POLICIES[kind]
    if attempts_made >= policy.max_attempts:
        return False, 0.0, f"{kind} error; no more tries" if policy.max_attempts > 1 else f"{kind} error; retrying can't help"
    if not repeatable:
        return False, 0.0, f"{kind} error, but this action changes things and could happen twice"
    wait = delay_before(policy, attempts_made)
    if waited_seconds + wait > policy.max_total_seconds:
        return False, 0.0, f"{kind} error; waited long enough"
    return True, wait, f"{kind} error; trying again in {wait:g} s"
