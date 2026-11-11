"""Screen history ("what was on my screen?") - opt-in, text only, local, bounded. OFF unless you switch it on AND start it.

What is kept: the TEXT read from the window in front (OCR), with its title, the program and the time. Never a picture.
When: at most every ``MIN_GAP`` seconds, and only when the window in front changed or ``MAX_GAP`` passed; identical text is not stored twice.
Never captured: anything the capture layer already blocks (password managers, banking, private browsing, meetings, your own privacy zones, windows you
blocked), and a window while a password field has the focus. Text that looks like a secret (keys, tokens, card numbers) is replaced before it is stored.
Bounds: rows older than SCREEN_HISTORY_DAYS are deleted; each text is cut to ``MAX_TEXT`` characters; the capture indicator is on while it runs; it stops
with 'stop', when screen capture is switched off, and when Jarvis closes (it does not restart by itself).
You can search it, see how much is stored, and delete everything at any time.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

MIN_GAP, MAX_GAP, MAX_TEXT = 20.0, 180.0, 4000


@dataclass
class Shot:
    app: str = ""
    title: str = ""
    text: str = ""
    blocked: str = ""             # why nothing was read (privacy zone, password field, capture off)


def should_capture(title: str, last_title: str, since_last: float, min_gap: float = MIN_GAP, max_gap: float = MAX_GAP) -> bool:
    if since_last < min_gap:
        return False
    return title != last_title or since_last >= max_gap


class Store:
    def __init__(self, db: Any) -> None:
        self.db = db

    def add(self, ts: float, app: str, title: str, text: str) -> bool:
        text = " ".join(text.split())[:MAX_TEXT]
        if len(text) < 12:
            return False                                           # an empty or near-empty read is not worth a row
        digest = hashlib.sha256(text.encode()).hexdigest()[:20]
        with self.db.transaction() as conn:
            last = conn.execute("SELECT digest FROM screen_history ORDER BY id DESC LIMIT 1").fetchone()
            if last and last[0] == digest:
                return False
            conn.execute("INSERT INTO screen_history (ts, app, title, text, digest) VALUES (?,?,?,?,?)", (ts, app[:80], title[:200], text, digest))
        return True

    def search(self, query: str, since: float = 0.0, limit: int = 8) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if len(w) > 1][:6]
        if not words:
            return []
        clause = " AND ".join("(lower(text) LIKE ? OR lower(title) LIKE ?)" for _ in words)
        args: list[Any] = []
        for w in words:
            args += [f"%{w}%", f"%{w}%"]
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT ts, app, title, text FROM screen_history WHERE ts >= ? AND {clause} ORDER BY ts DESC LIMIT ?", (since, *args, limit)).fetchall()
        out = []
        for ts, app, title, text in rows:
            i = text.lower().find(words[0])
            out.append({"ts": ts, "app": app, "title": title, "snippet": text[max(0, i - 80): i + 160] if i >= 0 else text[:200]})
        return out

    def stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            n, first, last = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM screen_history").fetchone()
        return {"rows": int(n or 0), "first": first, "last": last}

    def purge(self, days: int, now: float | None = None) -> int:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM screen_history WHERE ts < ?", ((now or time.time()) - max(1, days) * 86400,)).rowcount

    def delete_all(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM screen_history").rowcount


class Recorder:
    """The loop. ``peek()`` -> the title in front (cheap); ``read()`` -> a Shot (capture + OCR, already privacy-checked and redacted)."""

    def __init__(self, store: Store, peek: Callable[[], str], read: Callable[[], Awaitable[Shot]], *, days: int = 7, enabled: Callable[[], bool] = lambda: True,
                 indicator: Callable[[bool], None] | None = None, clock: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval: float = 5.0) -> None:
        self.store, self.peek, self.read, self.days, self.enabled, self.indicator = store, peek, read, days, enabled, indicator
        self.clock, self.sleep, self.interval = clock, sleep, interval
        self.task: asyncio.Task | None = None
        self.saved = self.blocked = self.errors = 0
        self.last_block = ""
        self.started: float | None = None
        self._last_title, self._last_at = "", 0.0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def _light(self, on: bool) -> None:
        if self.indicator is not None:
            try:
                self.indicator(on)
            except Exception:
                pass

    async def tick(self) -> str:
        """One step: 'saved' | 'same' | 'blocked' | 'wait' | 'error'."""
        now = self.clock()
        try:
            title = self.peek()
        except Exception:
            title = ""
        if not should_capture(title, self._last_title, now - self._last_at):
            return "wait"
        self._last_title, self._last_at = title, now
        try:
            shot = await self.read()
        except Exception:
            self.errors += 1
            return "error"
        if shot.blocked:
            self.blocked += 1
            self.last_block = shot.blocked
            return "blocked"
        if self.store.add(now, shot.app, shot.title or title, shot.text):
            self.saved += 1
            return "saved"
        return "same"

    async def _loop(self) -> None:
        self._light(True)
        try:
            self.store.purge(self.days, self.clock())
            n = 0
            while self.enabled():
                await self.tick()
                n += 1
                if n % 500 == 0:
                    self.store.purge(self.days, self.clock())
                await self.sleep(self.interval)
        finally:
            self._light(False)

    def start(self) -> None:
        if not self.running:
            self.started = self.clock()
            self.task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> bool:
        was = self.running
        if self.task is not None:
            self.task.cancel()
        self.task = None
        self._light(False)
        return was


RECORDER: Recorder | None = None


def describe(stats: dict[str, Any], rec: Recorder | None, allowed: bool, days: int) -> str:
    def when(ts: float | None) -> str:
        return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "-"

    state = "RECORDING (the capture indicator is on)" if rec is not None and rec.running else "not recording"
    lines = [f"Screen history: {state}. " + ("" if allowed else "It is switched off in .env (SCREEN_HISTORY_ENABLED=false). "),
             f"Stored: {stats['rows']} text snapshot(s), from {when(stats['first'])} to {when(stats['last'])}; kept {days} day(s); text only, no pictures."]
    if rec is not None and rec.started:
        lines.append(f"This session: {rec.saved} saved, {rec.blocked} skipped for privacy" + (f" (last: {rec.last_block})" if rec.last_block else "") + f", {rec.errors} read error(s).")
    return "\n".join(lines)
