"""Capability discovery: an honest inventory, and what the failures say is really missing.

A failure is not automatically a reason to add a tool. Each recurring failure from the audit log is classified by its
error code into one of: a missing tool, a missing permission, missing information, unclear intent, a model limitation
(malformed calls), a platform restriction, a resource / availability limit, or an unreliable existing implementation -
and the proposal that follows depends on the cause. A missing PERMISSION never leads to "widen it": the proposal is to
ask you. Proposals are text only: nothing is generated, installed or enabled here, and any code change still goes
through the reviewed propose -> test -> approve path.

The inventory is derived: risk level, read-only, network need from the registry; how many test files mention a tool
(only when the tests folder is present - otherwise "unknown", never "untested"); and its real failure rate from the audit log.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LISTING_FILE = 60       # a test file naming more tools than this is a registry listing, not a test of them
CAUSES = {
    "missing_tool": "no tool exists for this",
    "missing_permission": "a permission or approval was withheld",
    "missing_information": "information it needed wasn't available",
    "unclear_intent": "the request was ambiguous",
    "model_limitation": "the model produced a malformed or wrong call",
    "platform_restriction": "this platform doesn't allow it",
    "resource_limitation": "something it depends on was unavailable or too heavy",
    "unreliable_implementation": "the tool ran but its own result was wrong or unverified",
    "unclassified": "the cause isn't clear from the error code",
}
_CODES: dict[str, tuple[str, ...]] = {
    "missing_tool": ("unknown_tool",),
    "missing_permission": ("blocked", "declined", "protected", "auth", "denied", "not_allowed", "sensitive", "secret"),
    "missing_information": ("not_found", "missing_name", "missing_time", "missing_target", "missing_query", "missing_level",
                            "missing_id", "empty", "no_context", "no_data_dir", "invalid_time", "bad_date", "invalid_path",
                            "invalid_url", "no_backup", "no_tab"),
    "unclear_intent": ("ambiguous", "invalid_command"),
    "model_limitation": ("invalid_arguments", "invalid_steps", "invalid_plan", "wrong_tool", "bad_value", "invalid", "too_long"),
    "platform_restriction": ("unsupported", "unsupported_os"),
    "resource_limitation": ("timeout", "too_large", "too_big", "unavailable", "service_error", "unreachable", "network",
                            "not_running", "missing_dependency", "no_model", "no_database"),
    "unreliable_implementation": ("not_verified", "failed", "launch_failed", "no_window", "wrong_state", "stale"),
}
_BY_CODE = {code: cause for cause, codes in _CODES.items() for code in codes}
_APPROACH = {
    "missing_tool": "Add a small, registered tool with a schema, a risk level, a verification and tests.",
    "missing_permission": "Do NOT widen permissions. Ask you whether this should be allowed, and use a narrow, time-limited standing approval if yes.",
    "missing_information": "Ask for the missing detail earlier (clarification), or let the user store it (a known folder, nickname or project).",
    "unclear_intent": "Ask one concise question before acting, using the goal contract's clarification rules.",
    "model_limitation": "Tighten the tool description and argument names, add argument repair, and add the failing call to the quality-eval set. Try a larger model only as an experiment.",
    "platform_restriction": "Explain the limit to the user and offer the supported alternative; do not try to bypass the platform.",
    "resource_limitation": "Check the dependency first (investigate), degrade gracefully with a clear message, or retry later within a bound.",
    "unreliable_implementation": "Reproduce it, fix the tool or its verification, and add a regression test.",
    "unclassified": "Collect more evidence (more occurrences, the exact messages) before proposing anything.",
}


def classify(error: str) -> str:
    return _BY_CODE.get(error, "unclassified")


@dataclass
class Proposal:
    tool: str
    cause: str
    count: int
    calls: int
    example: str
    approach: str
    security: str
    test_plan: str
    rollback: str
    benefit: str

    def describe(self) -> str:
        return (f"{self.tool}: failed {self.count} of {self.calls} calls - {CAUSES[self.cause]}.\n"
                f"  Example: {self.example or 'n/a'}\n  Approach: {self.approach}\n  Security: {self.security}\n"
                f"  Test plan: {self.test_plan}\n  Rollback: {self.rollback}\n  Benefit: {self.benefit}")


def proposals(database: Any, days: int = 14, now: Any = None) -> list[Proposal]:
    from app.evolution.analyze import failures

    out = []
    for f in failures(database, days=days, now=now):
        cause = classify(f["error"])
        security = ("Must not widen what Jarvis may do; any new capability keeps the executor's checks."
                    if cause in ("missing_permission", "missing_tool") else "No change to permissions or approvals.")
        out.append(Proposal(f["tool"], cause, f["count"], f["calls"], f["example"], _APPROACH[cause], security,
                            f"Reproduce '{f['error']}' for {f['tool']}, add a regression test, run the full suite and the lab.",
                            "Revert the change; nothing is deployed without your approval.",
                            f"Removes at most {f['count']} failures like this per {days} days (an upper bound, not a promise)."))
    return sorted(out, key=lambda p: (-p.count, p.tool))


def cause_summary(database: Any, days: int = 14) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for p in proposals(database, days):
        counts[p.cause] += p.count
    return dict(counts.most_common())


def inventory(registry: Any, database: Any = None, tests_dir: Path | None = None, days: int = 30) -> list[dict[str, Any]]:
    """One row per tool: risk, read-only, network, how many test files name it (None = tests not available here), and its
    failure rate in the audit log (None = no calls recorded)."""
    from app.security import autonomy
    from app.security.permissions import RiskLevel

    mentions: dict[str, int] | None = None
    if tests_dir is not None and tests_dir.is_dir():
        mentions = defaultdict(int)
        names = [t.name for t in registry.tools]
        texts = []
        for path in tests_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if sum(1 for n in names if n in text) <= LISTING_FILE:      # a file that merely lists every tool proves nothing
                texts.append(text)
        for name in names:
            mentions[name] = sum(1 for t in texts if name in t)
    calls: dict[str, list[int]] = {}
    if database is not None:
        try:
            import json
            from datetime import datetime, timedelta, timezone

            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            with database.connect() as conn:
                for tool, details in conn.execute("SELECT tool, details FROM audit_log WHERE event = 'tool_result' AND ts >= ?", (since,)):
                    try:
                        ok = bool(json.loads(details or "{}").get("ok"))
                    except ValueError:
                        continue
                    row = calls.setdefault(tool, [0, 0])
                    row[0] += 1
                    row[1] += 0 if ok else 1
        except Exception:
            calls = {}
    rows = []
    for t in sorted(registry.tools, key=lambda t: (t.category, t.name)):
        total, failed = calls.get(t.name, (0, 0))
        rows.append({"name": t.name, "category": t.category, "risk": RiskLevel(t.level).name.lower(),
                     "reads_only": autonomy.is_read_only(t.name), "network": bool(t.needs_network),
                     "test_files": None if mentions is None else mentions[t.name],
                     "calls": total, "failure_rate": round(failed / total, 3) if total else None})
    return rows


def inventory_summary(rows: list[dict[str, Any]]) -> str:
    known = [r for r in rows if r["test_files"] is not None]
    no_test = [r["name"] for r in known if r["test_files"] == 0]
    risky = [r for r in rows if r["failure_rate"] is not None and r["failure_rate"] >= 0.3 and r["calls"] >= 5]
    lines = [f"{len(rows)} tools registered."]
    lines.append((f"{len(no_test)} have no test file naming them" + (": " + ", ".join(no_test[:8]) if no_test else "")) if known
                 else "Test coverage is unknown here (the tests folder isn't present).")
    lines.append("Failing often (30%+ of at least 5 calls): " + (", ".join(f"{r['name']} {int(r['failure_rate'] * 100)}%" for r in risky) if risky else "none recorded."))
    lines.append("A test file naming a tool is a weak signal, not proof it is well tested.")
    return "\n".join(lines)
