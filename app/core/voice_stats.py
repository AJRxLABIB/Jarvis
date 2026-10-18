"""How voice interaction is really going - measured, not guessed.

One small row per thing heard (status, language, recognition confidence, recognition time, and the time until the
first part of the reply). It never stores audio or what was said. Rows older than 60 days are dropped.

What can and cannot be measured from here, honestly:

* recognition time, time to first reply, low-confidence rate, wakes that produced no command - measured.
* real false wake-ups and recognition *accuracy* need a person to say which were wrong; the ``--benchmark`` command
  (recorded phrases) measures accuracy, and "wakes with no command" is only a hint at false wake-ups.
* wake-word latency and interruption latency need the microphone hardware and are not recorded here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

RETENTION = timedelta(days=60)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class VoiceStats:
    def __init__(self, database) -> None:
        self._db = database

    def record(self, status: str, *, language: str | None = None, confidence: float | None = None,
               stt_ms: int | None = None, handled: bool | None = None, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        with self._db.transaction() as conn:
            cursor = conn.execute("INSERT INTO voice_events (ts, status, language, confidence, stt_ms, handled) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (_iso(now), status[:20], (language or "")[:8] or None, confidence, stt_ms,
                                   None if handled is None else int(handled)))
            conn.execute("DELETE FROM voice_events WHERE ts < ?", (_iso(now - RETENTION),))
            return int(cursor.lastrowid)

    def first_reply(self, event_id: int, ms: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE voice_events SET first_reply_ms = ? WHERE id = ?", (int(ms), event_id))

    def report(self, days: int = 14, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self._db.connect() as conn:
            rows = conn.execute("SELECT status, language, confidence, stt_ms, first_reply_ms, handled FROM voice_events "
                                "WHERE ts >= ?", (_iso(now - timedelta(days=days)),)).fetchall()
        total = len(rows)
        out: dict[str, Any] = {"days": days, "heard": total, "by_language": {}}
        if not total:
            return out

        def avg(values: list[float]) -> float | None:
            return round(sum(values) / len(values)) if values else None

        for lang in sorted({r["language"] or "unknown" for r in rows}):
            mine = [r for r in rows if (r["language"] or "unknown") == lang]
            commands = [r for r in mine if r["status"] == "ok"]
            done = [r for r in commands if r["handled"] is not None]
            out["by_language"][lang] = {
                "heard": len(mine), "commands": len(commands),
                "low_confidence": sum(1 for r in mine if r["status"] == "low_confidence"),
                "avg_recognition_ms": avg([r["stt_ms"] for r in mine if r["stt_ms"] is not None]),
                "avg_first_reply_ms": avg([r["first_reply_ms"] for r in commands if r["first_reply_ms"] is not None]),
                "handled_rate": round(100 * sum(1 for r in done if r["handled"]) / len(done)) if done else None}
        out["wakes_without_command"] = sum(1 for r in rows if r["status"] in ("wake_only", "no_speech"))
        out["low_confidence"] = sum(1 for r in rows if r["status"] == "low_confidence")
        return out

    def describe(self, days: int = 14) -> str:
        data = self.report(days)
        if not data["heard"]:
            return f"Nothing has been heard in the last {days} days (voice use is recorded as it happens)."
        parts = []
        for lang, v in data["by_language"].items():
            line = f"{lang}: {v['commands']} command(s) of {v['heard']} heard"
            if v["avg_recognition_ms"] is not None:
                line += f", recognition {v['avg_recognition_ms']} ms"
            if v["avg_first_reply_ms"] is not None:
                line += f", first reply {v['avg_first_reply_ms']} ms"
            if v["low_confidence"]:
                line += f", {v['low_confidence']} low-confidence"
            if v["handled_rate"] is not None:
                line += f", {v['handled_rate']}% handled"
            parts.append(line)
        return (f"Voice, last {days} days - " + "; ".join(parts) + f". Wakes with no command: "
                f"{data['wakes_without_command']} (a hint at false wake-ups, not proof).")
