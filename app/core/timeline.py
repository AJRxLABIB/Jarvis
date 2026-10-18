"""The activity timeline: what Jarvis did, for whom, and whether the result was checked - rebuilt from the audit trail.

Every entry says who started it (you by voice, text or phone; a scheduled automation; Jarvis itself), what ran, the
outcome, and whether an independent check confirmed it. "Unverified" lists actions that changed something and succeeded
but were never confirmed - the honest answer to "what did it say it did that I should double-check?".

Only what the audit log already holds is used (tool names, risk, decisions, short redacted details); nothing new is
collected: no screenshots, audio, keystrokes or window contents. You can search it, export it and delete old entries.
Deleting is a deliberate, confirmed action and removes the records for good (it is the one way entries are ever removed).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.auditchain import reset_if_empty
from app.utils.redaction import redact, redact_value

_ACTORS = {"voice": "you (voice)", "text": "you (typed)", "phone": "you (phone)", "cli": "you (terminal)",
           "automation": "a scheduled automation", "internal": "Jarvis itself"}


def _actor(source: str | None) -> str:
    return _ACTORS.get(source or "", "Jarvis itself" if source is None else str(source))


def entries(database, query: str | None = None, since: datetime | None = None, until: datetime | None = None,
            actor: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Tool results (what ran), newest first, each with its actor and verification state."""
    sql = ("SELECT a.id, a.ts, a.tool, a.permission_level AS level, a.decision, a.details, a.run_id, r.source, r.text "
           "FROM audit_log a LEFT JOIN agent_runs r ON r.id = a.run_id WHERE a.event = 'tool_result'")
    params: list[Any] = []
    if since is not None:
        sql += " AND a.ts >= ?"
        params.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
    if until is not None:
        sql += " AND a.ts < ?"
        params.append(until.astimezone(timezone.utc).isoformat(timespec="seconds"))
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(max(limit, 1) * 5 if (query or actor) else limit)
    with database.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        checks = {}
        run_ids = {r["run_id"] for r in rows if r["run_id"]}
        for run_id in run_ids:
            for c in conn.execute("SELECT tool, details FROM audit_log WHERE run_id = ? AND event = 'verification'",
                                  (run_id,)):
                try:
                    checks[(run_id, c["tool"])] = bool(json.loads(c["details"]).get("ok"))
                except ValueError:
                    checks[(run_id, c["tool"])] = False
    out = []
    for r in rows:
        try:
            details = json.loads(r["details"] or "{}")
        except ValueError:
            details = {}
        who = _actor(r["source"])
        entry = {"id": r["id"], "time": r["ts"], "actor": who, "tool": r["tool"], "risk": r["level"],
                 "ok": bool(details.get("ok")), "outcome": redact(str(details.get("message") or details.get("error") or ""))[:200],
                 "verified": checks.get((r["run_id"], r["tool"])), "request": redact(r["text"] or "")[:120],
                 "run_id": r["run_id"],
                 "arguments": redact_value(details.get("arguments")) if isinstance(details.get("arguments"), dict) else {}}
        haystack = f"{entry['tool']} {entry['outcome']} {entry['request']} {who}".lower()
        if query and query.lower() not in haystack:
            continue
        if actor and actor.lower() not in who.lower():
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def unverified(database, since: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Successful actions that change things (risk 1+) with no confirming check."""
    return [e for e in entries(database, since=since, limit=limit * 4)
            if e["ok"] and (e["risk"] or 0) >= 1 and e["verified"] is None][:limit]


def describe(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No matching activity."
    lines = []
    for e in items[:12]:
        check = {True: "verified", False: "check FAILED", None: "not verified"}[e["verified"]]
        lines.append(f"{e['time'][:16].replace('T', ' ')} {e['actor']}: {e['tool']} -> "
                     f"{'ok' if e['ok'] else 'failed'} ({check}). {e['outcome'][:90]}")
    more = f" (+{len(items) - 12} more)" if len(items) > 12 else ""
    return "\n".join(lines) + more


def export(database, folder: Path, **filters: Any) -> tuple[Path, int]:
    items = entries(database, **filters)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"activity-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, len(items)


def delete_older_than(database, days: int, now: datetime | None = None) -> int:
    """Permanently remove audit entries (and finished request records) older than ``days``."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    with database.transaction() as conn:
        removed = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount
        reset_if_empty(conn)
        conn.execute("DELETE FROM agent_runs WHERE started_at < ? AND state != 'running'", (cutoff,))
    return removed
