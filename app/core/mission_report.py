"""After a mission stops: what failed, why it probably failed, what succeeded, what remains, whether a retry is safe - and honest numbers across missions.

``report`` works from the stored mission (steps, states, results, attempts) and the tool registry:
* a retry is called SAFE only for a step whose tool is read-only and has no side effect; for anything that changes something the answer is
  "check first" with what to check (never "just retry": a step that failed may still have taken effect);
* the probable cause comes from the error text through the existing ``retry`` classifier and is labelled a probable cause, not a finding;
* steps skipped because they depend on the failed one are listed as "not run", never as failed or done.
``analytics`` counts finished missions from the mission and task-event tables: completion rate, median duration, the tools that fail most.
Under ``MIN_MISSIONS`` it says there is not enough history instead of printing a rate.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

MIN_MISSIONS = 5
CAUSE_TEXT = {"transient": "a temporary problem (time-out, connection, busy service)", "resource": "not enough memory or disk, or a busy resource",
              "verification": "the action ran but its result could not be confirmed", "permission": "a permission, approval or policy stopped it",
              "validation": "the step's arguments were not acceptable", "permanent": "something that will not change by itself (missing file, unknown name, unsupported)",
              "unknown": "not clear from the record"}
ALTERNATIVE = {"transient": "retry later, or when the service is reachable again", "resource": "close other programs or free space, then retry",
               "verification": "check the real state yourself, then mark the step done or retry it", "permission": "approve it, widen the mission's lease, or do that step yourself",
               "validation": "correct the step's arguments (edit the plan) and retry", "permanent": "change the plan: a different tool or target is needed",
               "unknown": "run 'root cause' on the mission's request for the evidence"}


def _retry_note(tool: Any) -> tuple[bool, str]:
    if tool is None:
        return False, "the tool no longer exists, so the step cannot be retried as it is"
    level = int(getattr(tool, "level", 1))
    if level == 0 and getattr(tool, "verify", None) is None:
        return True, "safe to retry: the step only reads"
    return False, "check first: this step changes something, and a failed attempt may still have taken effect - look at the real state before retrying"


def report(mission: Any, registry: Any = None) -> dict[str, Any]:
    from app.core import retry

    done = [s for s in mission.steps if s.state == "done"]
    failed = [s for s in mission.steps if s.state == "failed"]
    skipped = [s for s in mission.steps if s.state == "skipped"]
    waiting = [s for s in mission.steps if s.state in ("pending", "waiting", "approved", "running")]
    out: dict[str, Any] = {"title": mission.title, "state": mission.state, "done": [f"{s.n}. {s.title}" for s in done],
                           "not_run": [f"{s.n}. {s.title}" + (f" ({s.result})" if s.result else "") for s in skipped], "remaining": [f"{s.n}. {s.title}" for s in waiting], "failed": []}
    for s in failed:
        kind = retry.classify(None, s.result or "")
        safe, note = _retry_note(registry.get(s.tool) if registry is not None else None)
        out["failed"].append({"step": f"{s.n}. {s.title}", "tool": s.tool, "what": (s.result or "no message")[:200], "attempts": s.attempts, "probable_cause": CAUSE_TEXT.get(kind, CAUSE_TEXT["unknown"]),
                              "retry_safe": safe, "retry_note": note, "alternative": ALTERNATIVE.get(kind, ALTERNATIVE["unknown"])})
    return out


def describe(r: dict[str, Any]) -> str:
    lines = [f"Mission '{r['title']}' - {r['state']}."]
    if r["done"]:
        lines.append(f"Succeeded ({len(r['done'])}): " + "; ".join(r["done"][:8]) + ("..." if len(r["done"]) > 8 else ""))
    for f in r["failed"]:
        lines += [f"FAILED: {f['step']} (tool {f['tool']}, {f['attempts']} attempt(s))", f"  what happened: {f['what']}", f"  probable cause: {f['probable_cause']} (from the error text, not proven)",
                  f"  retry: {f['retry_note']}", f"  other way: {f['alternative']}"]
    if r["not_run"]:
        lines.append(f"Not run ({len(r['not_run'])}): " + "; ".join(r["not_run"][:6]))
    if r["remaining"]:
        lines.append(f"Still to do ({len(r['remaining'])}): " + "; ".join(r["remaining"][:8]))
    if not r["failed"] and not r["remaining"] and not r["not_run"]:
        lines.append("Every step finished." if r["state"] == "completed" else "No step failed.")
    return "\n".join(lines)


def _ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def analytics(db: Any) -> dict[str, Any]:
    with db.connect() as conn:
        missions = [dict(r) for r in conn.execute("SELECT id, state, created_at, updated_at FROM missions")]
        steps = [dict(r) for r in conn.execute("SELECT tool, state FROM mission_steps")]
    finished = [m for m in missions if m["state"] in ("completed", "failed", "cancelled", "rolled_back")]
    out: dict[str, Any] = {"total": len(missions), "finished": len(finished), "open": len(missions) - len(finished), "enough": len(finished) >= MIN_MISSIONS}
    if not out["enough"]:
        return out
    completed = [m for m in finished if m["state"] == "completed"]
    durations = [(b - a).total_seconds() / 60 for m in completed if (a := _ts(m["created_at"])) and (b := _ts(m["updated_at"])) and b >= a]
    by_tool: dict[str, list[int]] = {}
    for s in steps:
        if s["state"] in ("done", "failed"):
            by_tool.setdefault(s["tool"], [0, 0])[0 if s["state"] == "done" else 1] += 1
    worst = sorted(((t, f, d + f) for t, (d, f) in by_tool.items() if f), key=lambda x: (-x[1], x[0]))[:5]
    out.update(completion_rate=round(len(completed) / len(finished), 2), completed=len(completed), median_minutes=round(statistics.median(durations), 1) if durations else None,
               failing_tools=[{"tool": t, "failed": f, "of": n} for t, f, n in worst])
    return out


def describe_analytics(a: dict[str, Any]) -> str:
    if not a["enough"]:
        return f"Not enough finished missions for statistics yet: {a['finished']} finished, {MIN_MISSIONS} needed ({a['open']} still open)."
    lines = [f"{a['finished']} finished missions: {a['completed']} completed ({a['completion_rate']:.0%}); {a['open']} still open.",
             "Median time from creation to completion: " + (f"{a['median_minutes']} minutes (includes time waiting for you)." if a["median_minutes"] is not None else "not available.")]
    if a["failing_tools"]:
        lines.append("Steps that fail most: " + "; ".join(f"{t['tool']} {t['failed']} of {t['of']}" for t in a["failing_tools"]) + ".")
    return "\n".join(lines)
