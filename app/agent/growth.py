"""Growth: Jarvis learns from EVERY request - not only the ones you rate - and measures whether it is improving.

1. Every request is recorded (redacted text, the tools it used and whether they worked).
2. Its outcome is decided by what happens NEXT, never by the model's opinion of itself:
   * bad  - a step failed, you said no to it, you corrected it ("no, I meant...", "wrong") or asked the same thing
            again straight away;
   * good - everything it did worked and you moved on (or said nothing for two minutes).
3. What it learns:
   * skills  - "for a request like this, these tools worked" (good turns that used tools);
   * lessons - "this went wrong, and this is what you wanted instead" (a bad turn followed by your correction).
   The most similar skills and lessons are shown to the model for new requests ("what worked before").
4. Every skill / lesson is scored by the outcomes of the requests it was shown for; ones that don't help (at least
   4 uses, under half good) are retired automatically.
5. ``report()`` shows the success rate week by week, so improvement is measured, not claimed.

Private mode, automations and Jarvis's own internal requests are never recorded. Nothing leaves this PC.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.memory.database import Database, utc_now_iso
from app.utils.redaction import redact

QUIET_SECONDS = 120          # no complaint within this time = the request went fine
RETIRE_AFTER = 4
SHOWN_SKILLS, SHOWN_LESSONS = 2, 2
MAX_TURNS = 5000
_STOP = frozenset({"the", "a", "an", "my", "please", "jarvis", "can", "you", "to", "me", "on", "is", "it", "of", "for",
                   "and", "in", "hey", "could", "would", "i", "this", "that"})
CORRECTION = re.compile(r"^(?:no+|nope|wrong|not that|that'?s (?:not|wrong)|that is (?:not|wrong)|i said|i meant|"
                        r"i asked|you got it wrong|incorrect|না|ভুল|আমি বলেছি)\b", re.I)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\wঀ-৿]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def _similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(text: str, limit: int = 300) -> str:
    return redact(" ".join((text or "").split()))[:limit]


def describe_tools(tools: list[dict]) -> str:
    parts = []
    for t in tools[:3]:
        args = ", ".join(f"{k}={v!r}" for k, v in list((t.get("args") or {}).items())[:3]
                         if v not in (None, "", [], {}))
        parts.append(f"{t['name']}({args})")
    return " then ".join(parts)


class Growth:
    def __init__(self, database: Database, clock=_now) -> None:
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ recording
    def record(self, request: str, executions, model: str = "", used: list[int] | None = None) -> int | None:
        """Record a finished request; first settle the previous one using this new message as evidence."""
        request = _clean(request)
        if not request:
            return None
        previous = self._pending()
        corrects = None
        if previous is not None:
            age = (self.clock() - datetime.fromisoformat(previous["ts"])).total_seconds()
            if age <= QUIET_SECONDS and (CORRECTION.match(request) or
                                         (_similarity(request, previous["request"]) >= 0.75
                                          and request.lower() != "yes")):
                self._settle(previous, "bad", "you corrected it" if CORRECTION.match(request) else "you asked again")
                corrects = previous["id"]
            else:
                self._settle_by_result(previous)
        tools = [{"name": e.call.name, "args": _safe_args(e.call.arguments), "ok": bool(e.result.ok),
                  "error": getattr(e.result, "error", None)} for e in (executions or ())]
        with self.db.transaction() as conn:
            cursor = conn.execute("INSERT INTO learning_turns (ts, request, tools, model, used, corrects) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (self.clock().isoformat(timespec="seconds"), request, json.dumps(tools),
                                   model or "", json.dumps(used or []), corrects))
            conn.execute("DELETE FROM learning_turns WHERE id NOT IN (SELECT id FROM learning_turns ORDER BY id DESC "
                         f"LIMIT {MAX_TURNS})")
            turn_id = int(cursor.lastrowid)
        failed = [t for t in tools if not t["ok"]]
        if failed:                                   # a failed step decides at once: no need to wait
            self._settle(self._turn(turn_id), "bad", "you said no" if any(t["error"] == "declined" for t in failed)
                         else f"{failed[0]['name']} failed")
        return turn_id

    def settle_quiet(self) -> int:
        """Pending turns older than QUIET_SECONDS with nothing against them: good (or bad if a step failed)."""
        cutoff = (self.clock() - timedelta(seconds=QUIET_SECONDS)).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM learning_turns WHERE outcome = 'pending' AND ts < ?", (cutoff,)).fetchall()
        for row in rows:
            self._settle_by_result(dict(row))
        return len(rows)

    def _pending(self) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM learning_turns WHERE outcome = 'pending' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def _turn(self, turn_id: int) -> dict:
        with self.db.connect() as conn:
            return dict(conn.execute("SELECT * FROM learning_turns WHERE id = ?", (turn_id,)).fetchone())

    def _settle_by_result(self, turn: dict) -> None:
        tools = json.loads(turn["tools"] or "[]")
        if any(not t["ok"] for t in tools):
            self._settle(turn, "bad", "a step failed")
        else:
            self._settle(turn, "good", "it worked and you moved on")

    def _settle(self, turn: dict, outcome: str, reason: str) -> None:
        if turn.get("outcome") != "pending":
            return
        with self.db.transaction() as conn:
            conn.execute("UPDATE learning_turns SET outcome = ?, reason = ? WHERE id = ? AND outcome = 'pending'",
                         (outcome, reason, turn["id"]))
            for item_id in json.loads(turn.get("used") or "[]"):     # score what was shown for this request
                conn.execute(f"UPDATE growth_items SET uses = uses + 1, {'good' if outcome == 'good' else 'bad'} = "
                             f"{'good' if outcome == 'good' else 'bad'} + 1 WHERE id = ?", (item_id,))
            conn.execute("UPDATE growth_items SET retired = 1 WHERE retired = 0 AND uses >= ? AND good * 2 < uses",
                         (RETIRE_AFTER,))
        turn = {**turn, "outcome": outcome}
        if outcome == "good":
            self._learn_skill(turn)
            if turn.get("corrects"):
                self._learn_lesson(self._turn(turn["corrects"]), turn)

    # ------------------------------------------------------------------ learning
    def _learn_skill(self, turn: dict) -> None:
        tools = [t for t in json.loads(turn["tools"] or "[]") if t["ok"]]
        if not tools or turn.get("model") == "direct":    # direct commands are already exact: nothing to learn
            return
        self._upsert("skill", turn["request"], describe_tools(tools), tools[0]["name"])

    def _learn_lesson(self, wrong: dict, right: dict) -> None:
        wrong_tools = describe_tools(json.loads(wrong["tools"] or "[]")) or "a plain answer"
        right_tools = describe_tools([t for t in json.loads(right["tools"] or "[]") if t["ok"]]) or "a plain answer"
        content = (f"for '{wrong['request']}' I did {wrong_tools}, which was wrong ({wrong.get('reason') or 'bad'}); "
                   f"the user then said '{right['request']}' and {right_tools} was right")
        tool = (json.loads(right["tools"] or "[]") or [{"name": ""}])[0]["name"]
        self._upsert("lesson", wrong["request"], content, tool)

    def _upsert(self, kind: str, request: str, content: str, tool: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO growth_items (kind, request, content, tool, created) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(kind, request, tool) DO UPDATE SET content = excluded.content, retired = 0",
                         (kind, request, content[:600], tool, utc_now_iso()))

    # ------------------------------------------------------------------ using it
    def relevant(self, request: str) -> list[dict]:
        """The skills and lessons most similar to ``request`` (not retired), best first."""
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM growth_items WHERE retired = 0 "
                                                   "ORDER BY id DESC LIMIT 1000")]
        scored = sorted(((_similarity(request, r["request"]), r) for r in rows), key=lambda x: -x[0])
        skills = [r for s, r in scored if r["kind"] == "skill" and s >= 0.4][:SHOWN_SKILLS]
        lessons = [r for s, r in scored if r["kind"] == "lesson" and s >= 0.3][:SHOWN_LESSONS]
        return lessons + skills

    @staticmethod
    def prompt(items: list[dict]) -> str:
        if not items:
            return ""
        lines = ["Learned from this user's own earlier requests (use it when it fits; the current request wins):"]
        for item in items:
            if item["kind"] == "lesson":
                lines.append(f"- Lesson: {item['content']}.")
            else:
                lines.append(f"- What worked for '{item['request']}': {item['content']}.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ measuring
    def report(self, weeks: int = 6) -> dict[str, Any]:
        self.settle_quiet()
        now = self.clock()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT ts, outcome FROM learning_turns WHERE outcome != 'pending' AND ts >= ?",
                                ((now - timedelta(weeks=weeks)).isoformat(timespec="seconds"),)).fetchall()
            items = {k: n for k, n in conn.execute("SELECT kind || CASE WHEN retired THEN '_retired' ELSE '' END, "
                                                   "COUNT(*) FROM growth_items GROUP BY 1")}
            lessons = [r["content"] for r in conn.execute("SELECT content FROM growth_items WHERE kind = 'lesson' "
                                                          "AND retired = 0 ORDER BY id DESC LIMIT 3")]
        by_week: dict[int, list[int]] = {}
        for r in rows:
            week = int((now - datetime.fromisoformat(r["ts"])).days // 7)
            good_bad = by_week.setdefault(week, [0, 0])
            good_bad[0 if r["outcome"] == "good" else 1] += 1
        trend = [{"weeks_ago": w, "good": g, "bad": b, "rate": round(g / (g + b), 3) if g + b else None}
                 for w, (g, b) in sorted(by_week.items(), reverse=True)]
        total_good = sum(t["good"] for t in trend)
        total = total_good + sum(t["bad"] for t in trend)
        return {"turns": total, "success_rate": round(total_good / total, 3) if total else None, "trend": trend,
                "skills": items.get("skill", 0), "lessons": items.get("lesson", 0),
                "retired": items.get("skill_retired", 0) + items.get("lesson_retired", 0), "latest_lessons": lessons}

    def forget(self) -> int:
        with self.db.transaction() as conn:
            removed = conn.execute("DELETE FROM growth_items").rowcount
            conn.execute("DELETE FROM learning_turns")
        return removed

    def cases(self, limit: int = 40) -> list[dict]:
        """A personal test set from real use: requests whose right tool is known (good turns with tools, and the
        corrected version of bad ones). Used to measure a model change before it is kept."""
        self.settle_quiet()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT request, tools, outcome, corrects FROM learning_turns WHERE outcome = 'good' "
                                "ORDER BY id DESC LIMIT 400").fetchall()
        seen, out = set(), []
        for r in rows:
            tools = [t for t in json.loads(r["tools"] or "[]") if t["ok"]]
            if not tools:
                continue
            key = (" ".join(sorted(_words(r["request"]))), tools[0]["name"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"request": r["request"], "expect_tool": tools[0]["name"]})
            if len(out) >= limit:
                break
        return out


def _safe_args(arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        return {}
    out = {}
    for k, v in list(arguments.items())[:8]:
        if isinstance(v, str):
            out[str(k)] = _clean(v, 120)
        elif isinstance(v, int | float | bool) or v is None:
            out[str(k)] = v
    return out
