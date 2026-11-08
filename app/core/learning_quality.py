"""Learning quality control: keep what Jarvis learns TRUE, not just large. Pure functions over growth rows (``agent/growth.py``).

Research behind it (2025-26): consolidated memory can end up worse than no memory when it is updated blindly (arXiv 2605.12978);
wrong experience propagates through replay, and selective add/delete policies fix it (arXiv 2505.16067); forgetting works best as a
decay of how reachable an item is, not as deletion (arXiv 2604.00131). So, without any model call:

* ``strength``   - retrievability 0..1: decays with days since the item was last shown (Ebbinghaus-style), slows down with every good use;
* ``harmful``    - error-propagation guard: an item whose shown-for requests failed clearly more often than the baseline is flagged early
                   (before the old "4 uses under half good" rule would catch it);
* ``conflicts``  - two active items of one kind that answer a similar request with different tools: the one with the worse record is flagged;
* ``hard_cases`` - requests that went wrong and were then corrected: the most valuable test cases for ``tune_ai`` / self-improvement;
* ``audit``      - all of the above in one report; ``retire`` / ``restore`` change only the ``retired`` flag and write a ledger entry, so every
                   change is visible and reversible. Raw turns are never touched. Nothing here edits code, permissions or settings.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from app.agent.growth import _similarity

BASE_DAYS = 14.0            # stability of an item with no good use yet
DORMANT_BELOW = 0.12
HARM_MIN_USES = 3
HARM_MARGIN = 0.25          # item's failure rate must exceed the baseline by this much
CONFLICT_SIMILARITY = 0.6
LEDGER_KEY = "learning_ledger"
LEDGER_MAX = 40


def _when(ts: str) -> datetime:
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _used(turn: dict) -> list[int]:
    try:
        v = json.loads(turn.get("used") or "[]")
        return [int(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def last_touch(item: dict, turns: list[dict]) -> str:
    seen = [t["ts"] for t in turns if item["id"] in _used(t)]
    return max(seen) if seen else item["created"]


def strength(item: dict, touched: str, now: datetime) -> float:
    if now.tzinfo is None:
        now = now.astimezone()
    days = max(0.0, (now - _when(touched)).total_seconds() / 86400)
    stability = BASE_DAYS * (1 + item.get("good", 0)) / (1 + 0.5 * item.get("bad", 0))
    return round(math.exp(-days / stability), 3)


def harmful(item: dict, turns: list[dict]) -> bool:
    shown = [t for t in turns if item["id"] in _used(t) and t.get("outcome") in ("good", "bad")]
    if len(shown) < HARM_MIN_USES:
        return False
    rated = [t for t in turns if t.get("outcome") in ("good", "bad")]
    baseline = sum(t["outcome"] == "bad" for t in rated) / len(rated)
    rate = sum(t["outcome"] == "bad" for t in shown) / len(shown)
    return rate >= baseline + HARM_MARGIN


def conflicts(items: list[dict]) -> list[tuple[dict, dict]]:
    """(keep, doubtful) pairs among active items. Better record keeps; a tie keeps the newer one."""
    live = [i for i in items if not i.get("retired")]
    out: list[tuple[dict, dict]] = []
    for n, a in enumerate(live):
        for b in live[n + 1:]:
            if a["kind"] != b["kind"] or a["tool"] == b["tool"] or _similarity(a["request"], b["request"]) < CONFLICT_SIMILARITY:
                continue
            def score(i: dict) -> tuple[float, int]:
                return ((i["good"] + 1) / (i["uses"] + 2), i["id"])
            keep, doubt = (a, b) if score(a) >= score(b) else (b, a)
            out.append((keep, doubt))
    return out


def hard_cases(turns: list[dict], limit: int = 20) -> list[dict]:
    """Bad turns that a later good turn corrected: {request, expect_tool, wrong_tool}. These are what the next self-test should include."""
    by_id = {t["id"]: t for t in turns}
    out = []
    for t in sorted(turns, key=lambda x: x["id"], reverse=True):
        wrong = by_id.get(t.get("corrects") or -1)
        if t.get("outcome") != "good" or wrong is None or wrong.get("outcome") != "bad":
            continue
        right = [x for x in json.loads(t.get("tools") or "[]") if x.get("ok")]
        if not right:
            continue
        before = json.loads(wrong.get("tools") or "[]")
        out.append({"request": wrong["request"], "expect_tool": right[0]["name"], "wrong_tool": before[0]["name"] if before else ""})
        if len(out) >= limit:
            break
    return out


def reflect(bad: dict, good: dict, clock=None, within_seconds: int = 600) -> tuple[str, str, str] | None:
    """Failure reflection (Reflexion-style, rule-based): ``bad`` ended in a failed step, ``good`` is the next turn and used the same tool
    successfully with different arguments. -> (request, lesson text, tool) or None. Only what the logs show is stated."""
    if bad.get("outcome") != "bad" or good.get("outcome") != "good":
        return None
    if abs((_when(good["ts"]) - _when(bad["ts"])).total_seconds()) > within_seconds:
        return None
    failed = [t for t in json.loads(bad.get("tools") or "[]") if not t.get("ok") and t.get("error") not in (None, "", "declined")]
    worked = [t for t in json.loads(good.get("tools") or "[]") if t.get("ok")]
    for f in failed:
        for w in worked:
            if w["name"] == f["name"] and w.get("args") != f.get("args"):
                a = ", ".join(f"{k}={v!r}" for k, v in list((f.get("args") or {}).items())[:3])
                b = ", ".join(f"{k}={v!r}" for k, v in list((w.get("args") or {}).items())[:3])
                return (bad["request"], f"{f['name']}({a}) failed with '{str(f['error'])[:80]}'; {w['name']}({b}) worked next time", w["name"])
    return None


def audit(items: list[dict], turns: list[dict], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    live = [i for i in items if not i.get("retired")]
    dormant, harm = [], []
    for i in live:
        s = strength(i, last_touch(i, turns), now)
        if s < DORMANT_BELOW and i.get("uses", 0) == 0:
            dormant.append({"id": i["id"], "request": i["request"], "strength": s})
        if harmful(i, turns):
            harm.append({"id": i["id"], "request": i["request"], "tool": i["tool"]})
    pairs = [{"keep": k["id"], "doubtful": d["id"], "request": d["request"]} for k, d in conflicts(items)]
    return {"active": len(live), "dormant": dormant, "harmful": harm, "conflicts": pairs, "hard_cases": hard_cases(turns),
            "retire": sorted({x["id"] for x in dormant} | {x["id"] for x in harm} | {p["doubtful"] for p in pairs})}


def describe(report: dict[str, Any]) -> str:
    if not report["retire"] and not report["hard_cases"]:
        return f"Learning looks healthy: {report['active']} active items, nothing dormant, harmful or contradicting."
    parts = [f"{report['active']} active items."]
    if report["harmful"]:
        parts.append(f"{len(report['harmful'])} made things worse than usual when shown.")
    if report["conflicts"]:
        parts.append(f"{len(report['conflicts'])} contradict a better item.")
    if report["dormant"]:
        parts.append(f"{len(report['dormant'])} were never used and have faded.")
    if report["hard_cases"]:
        parts.append(f"{len(report['hard_cases'])} corrected mistakes are kept as hard test cases.")
    if report["retire"]:
        parts.append(f"Say 'clean up what you learned' to retire {len(report['retire'])} (reversible).")
    return " ".join(parts)


AUDIT_EVERY_DAYS = 7
AUDIT_KEY = "learning_audit_last"


def scheduled(db: Any, store: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Weekly check for upkeep: reports (never retires) and returns the audit when something needs a decision, else None."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    last = store.get(AUDIT_KEY, "") or ""
    if last and (now - _when(last)).days < AUDIT_EVERY_DAYS:
        return None
    store.set(AUDIT_KEY, now.isoformat(timespec="seconds"))
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute("SELECT * FROM growth_items")]
        turns = [dict(r) for r in conn.execute("SELECT * FROM learning_turns ORDER BY id DESC LIMIT 1500")]
    report = audit(items, turns, now)
    return report if report["retire"] else None


def _ledger(store: Any, entry: dict[str, Any]) -> None:
    entries = list(store.get(LEDGER_KEY, []) or [])
    entries.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry})
    store.set(LEDGER_KEY, entries[-LEDGER_MAX:])


def retire(db: Any, store: Any, ids: list[int], why: str) -> int:
    ids = [int(i) for i in ids][:200]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with db.transaction() as conn:
        changed = conn.execute(f"UPDATE growth_items SET retired = 1 WHERE retired = 0 AND id IN ({marks})", ids).rowcount
    if changed:
        _ledger(store, {"action": "retire", "ids": ids, "why": why})
    return changed


def restore_last(db: Any, store: Any) -> int:
    """Undo the most recent retire entry in the ledger."""
    entries = list(store.get(LEDGER_KEY, []) or [])
    for pos in range(len(entries) - 1, -1, -1):
        if entries[pos].get("action") == "retire":
            ids = entries.pop(pos)["ids"]
            marks = ",".join("?" * len(ids))
            with db.transaction() as conn:
                n = conn.execute(f"UPDATE growth_items SET retired = 0 WHERE id IN ({marks})", ids).rowcount
            store.set(LEDGER_KEY, entries)
            return n
    return 0
