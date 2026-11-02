"""Screen watch: tell me when something on a window changes or a phrase appears - cheaply, privately, and only while you allow it.

The rules this is built on:

* EXPLICIT AND VISIBLE. A watch exists only because you asked for it (tool ``screen_watch``). While any watch runs the host's
  "screen capture active" indicator stays on; ``pause``, ``stop`` and ``stop all`` work at any time, every watch ends by itself after
  its time limit (default 30 minutes, at most 4 hours), and at most three run at once.
* PRIVATE. The same blocked-window list as every screen capture (password managers, banking...) applies on each look: if a blocked
  window is on screen the watch captures nothing and stops itself, saying why. Screen capture must be enabled in Settings.
  Images stay in memory and are never saved; only a tiny grey thumbnail is compared, and text is read (OCR) only when the picture
  changed, never on every look.
* CHEAP. It looks every ``base`` seconds and backs off (x1.5, up to ``slowest``) while nothing changes, snapping back when something does.
  A changed look is a mean-brightness difference of the thumbnail (like the screen-action verifier), not a model call.
* NOT NOISY. After an alert the watch waits out a cooldown; a watch for text alerts once and stops unless ``repeat`` is set.
* HONEST. An alert says what was seen and when. "Changed" means pixels changed, not that anything meaningful happened; a phrase match is
  OCR-forgiving, so it can miss text the OCR cannot read.
* STOPS FOR SAFETY. Jarvis being locked in the emergency controls ends every watch.

Limits: Windows only for real capture (the engine itself is platform-independent and tested with injected observations); a watch does not
survive an application restart (it is a convenience for the current session, not a stored schedule).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.screen_verify import pixels_changed, text_appeared
from app.tools.base import ToolError

MAX_WATCHES = 3
DEFAULT_MINUTES = 30
MAX_MINUTES = 240
BASE_SECONDS = 3.0
SLOWEST_SECONDS = 30.0
COOLDOWN_SECONDS = 60.0
BACKOFF = 1.5


@dataclass
class Observation:
    thumb: Any = None                                   # small grey image (PIL/array) or None
    title: str = ""
    read_text: Callable[[], Awaitable[str]] | None = None   # OCR on demand (only called after a change)
    blocked: str = ""                                    # set when a blocked window prevented the look
    describe: Callable[[], Awaitable[str]] | None = None   # assist mode: ask a vision model what is going on (only after the screen settles)


@dataclass
class Watch:
    id: int
    target: str                       # window name ("" = the screen)
    condition: str                    # "change" | "text" | "assist"
    phrase: str = ""
    repeat: bool = False
    base: float = BASE_SECONDS
    slowest: float = SLOWEST_SECONDS
    cooldown: float = COOLDOWN_SECONDS
    ends_at: float = 0.0
    state: str = "running"            # running | paused | triggered | ended | blocked
    interval: float = BASE_SECONDS
    looks: int = 0
    ocr_runs: int = 0
    alerts: int = 0
    last_thumb: Any = None
    last_alert: float = -1e9
    last_look: float = 0.0
    note: str = ""
    pending: bool = False             # assist: the screen changed and has not been looked at in detail yet
    history: list[str] = field(default_factory=list)

    def describe(self) -> str:
        what = f"text '{self.phrase}'" if self.condition == "text" else "what you are doing (to help)" if self.condition == "assist" else "any change"
        where = self.target or "the screen"
        left = max(0, int(self.ends_at - time.monotonic())) if self.state in ("running", "paused") else 0
        return (f"#{self.id} {where}: waiting for {what} - {self.state}"
                + (f" ({left // 60} min left)" if left else "") + f"; {self.looks} look(s), {self.ocr_runs} text read(s), {self.alerts} alert(s)"
                + (f". {self.note}" if self.note else ""))


async def step(w: Watch, obs: Observation, now: float) -> str | None:
    """Process one look. Returns an alert message, or None. Mutates the watch (interval, state, counters)."""
    w.looks += 1
    w.last_look = now
    if obs.blocked:
        w.state, w.note = "blocked", f"paused: {obs.blocked} was on screen, so nothing was captured"
        return None
    if now >= w.ends_at:
        w.state, w.note = "ended", "time limit reached"
        return None
    changed = pixels_changed(w.last_thumb, obs.thumb) if w.last_thumb is not None else True   # the first look counts as new
    if obs.thumb is not None:
        w.last_thumb = obs.thumb
    if w.condition == "assist":
        return await _assist(w, obs, now, changed)
    if not changed:
        w.interval = min(w.slowest, w.interval * BACKOFF)
        return None
    w.interval = w.base
    if w.looks == 1:
        changed_alert = False       # the first look only sets the baseline
    else:
        changed_alert = True
    if w.condition == "change":
        if changed_alert and now - w.last_alert >= w.cooldown:
            w.alerts += 1
            w.last_alert = now
            msg = f"The screen of {w.target or 'your display'} changed (window: {obs.title or 'unknown'})."
            if not w.repeat:
                w.state = "triggered"
            return msg
        return None
    # text: read only after a change (or on the first look)
    if obs.read_text is None:
        return None
    w.ocr_runs += 1
    text = await obs.read_text()
    if text_appeared(text, w.phrase):
        if now - w.last_alert >= w.cooldown:
            w.alerts += 1
            w.last_alert = now
            if not w.repeat:
                w.state = "triggered"
            return f"I can see '{w.phrase}' in {w.target or 'your display'} now."
    return None


NOTHING = "NOTHING"


async def _assist(w: Watch, obs: Observation, now: float, changed: bool) -> str | None:
    """Assist mode: wait until the screen has SETTLED after a change (not mid-typing), then - at most once per cooldown - ask the vision model what
    you are doing and whether there is something worth helping with. Silent when there is nothing useful to say."""
    if changed:
        w.interval, w.pending = w.base, True
        return None
    w.interval = min(w.slowest, w.interval * BACKOFF)
    if not w.pending or obs.describe is None or now - w.last_alert < w.cooldown:
        return None
    w.pending = False
    text = (await obs.describe() or "").strip()
    w.last_alert = now
    if not text or text.upper().startswith(NOTHING):
        return None
    w.alerts += 1
    return text


Observer = Callable[[str], Awaitable[Observation]]
Notifier = Callable[[str, str], None]


class WatchManager:
    def __init__(self) -> None:
        self.watches: dict[int, Watch] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._next = 1
        self.indicator: Callable[[bool], None] | None = None
        self._lit = False

    # ------------------------------------------------------------ control
    def start(self, observe: Observer, notify: Notifier, *, target: str = "", condition: str = "change", phrase: str = "",
              minutes: int = DEFAULT_MINUTES, repeat: bool = False, base: float = BASE_SECONDS, locked: Callable[[], bool] = lambda: False) -> Watch:
        if condition not in ("change", "text", "assist"):
            raise ToolError("Watch for 'change', 'text' or 'assist'.", "invalid_condition")
        if condition == "text" and len(phrase.strip()) < 3:
            raise ToolError("Say which text to wait for (at least 3 characters).", "missing_phrase")
        if not 1 <= minutes <= MAX_MINUTES:
            raise ToolError(f"Watch for 1 to {MAX_MINUTES} minutes.", "invalid_duration")
        if sum(w.state in ("running", "paused") for w in self.watches.values()) >= MAX_WATCHES:
            raise ToolError(f"At most {MAX_WATCHES} watches at once; stop one first.", "too_many")
        w = Watch(self._next, target.strip(), condition, phrase.strip(), repeat, base=max(1.0, base), interval=max(1.0, base),
                  ends_at=time.monotonic() + minutes * 60)
        self._next += 1
        self.watches[w.id] = w
        self._tasks[w.id] = asyncio.get_running_loop().create_task(self._run(w, observe, notify, locked))
        self._light()
        return w

    def get(self, watch_id: int) -> Watch:
        if watch_id not in self.watches:
            raise ToolError(f"There's no watch #{watch_id}.", "not_found")
        return self.watches[watch_id]

    def pause(self, watch_id: int, paused: bool) -> Watch:
        w = self.get(watch_id)
        if w.state in ("running", "paused"):
            w.state = "paused" if paused else "running"
            w.last_thumb = None if not paused else w.last_thumb    # after a pause the next look is a new baseline
            w.looks = 0 if not paused else w.looks
        self._light()
        return w

    def stop(self, watch_id: int | None = None, reason: str = "stopped by you") -> int:
        ids = [watch_id] if watch_id is not None else list(self.watches)
        n = 0
        for i in ids:
            w = self.get(i)
            if w.state in ("running", "paused"):
                w.state, w.note = "ended", reason
                n += 1
            task = self._tasks.get(i)
            if task is not None and not task.done():
                task.cancel()
        self._light()
        return n

    def active(self) -> list[Watch]:
        return [w for w in self.watches.values() if w.state in ("running", "paused")]

    def _light(self) -> None:
        """The 'screen capture active' indicator follows whether any watch is running (paused watches capture nothing)."""
        on = any(w.state == "running" for w in self.watches.values())
        if self.indicator is not None and on != self._lit:
            self._lit = on
            try:
                self.indicator(on)
            except Exception:
                pass

    # ------------------------------------------------------------ the loop
    async def _run(self, w: Watch, observe: Observer, notify: Notifier, locked: Callable[[], bool]) -> None:
        try:
            while w.state in ("running", "paused"):
                if locked():
                    w.state, w.note = "ended", "Jarvis is locked in the emergency controls"
                    break
                if w.state == "running":
                    try:
                        obs = await observe(w.target)
                    except ToolError as exc:
                        w.state, w.note = "ended", str(exc)
                        break
                    except Exception as exc:     # one bad look must not kill the watch silently
                        w.note = f"a look failed ({type(exc).__name__}); will try again"
                        obs = None
                    if obs is not None:
                        message = await step(w, obs, time.monotonic())
                        if message:
                            w.history.append(message)
                            try:
                                notify("Screen watch", message)
                            except Exception:
                                pass
                        if w.state in ("triggered", "ended", "blocked"):
                            break
                await asyncio.sleep(w.interval if w.state == "running" else 1.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._light()


MANAGER = WatchManager()
