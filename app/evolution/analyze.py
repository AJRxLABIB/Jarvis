"""Where Jarvis could be better, from what really happened (the audit log) - no guessing, no invented numbers.

Each finding is an "insight" proposal: the evidence (how often, which error, an example message) and what it
probably means. Turning an insight into a code change is a separate, explicit step (propose_code_change).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.memory.database import Database

MIN_COUNT = 3
_MEANING = {
    "not_found": "people ask for things Jarvis can't find - names it should know, or a lookup that misses",
    "unsupported": "this doesn't work on this PC the way it's done now - maybe another method exists",
    "not_verified": "the action ran but the check afterwards didn't see the result",
    "timeout": "it takes too long - a slow method, or a wait that is too short",
    "invalid_arguments": "the model calls it wrongly - the description or the argument names may confuse it",
    "declined": "you often say no to it - maybe it asks when it shouldn't, or it's chosen wrongly",
    "wrong_tool": "the model picks the wrong tool for this request",
}
IGNORED_ERRORS = {"cancelled", "blocked", "disabled", "offline", "limit", "rate_limited", "emergency"}


def failures(database: "Database", days: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
    """Tool failures grouped by (tool, error), most frequent first, seen at least MIN_COUNT times."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    with database.connect() as conn:
        rows = conn.execute("SELECT ts, tool, details FROM audit_log WHERE event = 'tool_result' AND ts >= ? "
                            "ORDER BY ts", (since,)).fetchall()
    groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    totals: Counter[str] = Counter()
    for ts, tool, details in rows:
        try:
            data = json.loads(details or "{}")
        except ValueError:
            continue
        totals[tool] += 1
        error = data.get("error")
        if data.get("ok") or not error or error in IGNORED_ERRORS:
            continue
        groups[(tool, error)].append((ts, str(data.get("message") or "")[:160]))
    found = []
    for (tool, error), items in groups.items():
        if len(items) < MIN_COUNT:
            continue
        found.append({"key": f"failure:{tool}:{error}", "tool": tool, "error": error, "count": len(items),
                      "calls": totals[tool], "last": items[-1][0], "example": items[-1][1],
                      "meaning": _MEANING.get(error, "it keeps failing the same way")})
    return sorted(found, key=lambda f: (-f["count"], f["tool"]))


def insight_title(finding: dict[str, Any]) -> str:
    return (f"{finding['tool']} failed {finding['count']} of {finding['calls']} times with "
            f"'{finding['error']}' (last 14 days)")
