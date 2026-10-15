"""YouTube control in the tab Jarvis plays music in: skip ad, like, seek, captions, what's playing, a queue.

"skip the ad", "like this song", "forward 30 seconds", "go back 10 seconds", "start it again", "captions on",
"full screen video", "what song is this", "after this play Arijit Singh", "what's in the queue", "play this
again" (repeat once), "clear the queue". Pause / next / previous keep using the media keys (they also work
for Spotify and other players); "next song" takes the next queued song when there is one.

How it works, with no browser extension and nothing sent anywhere:
* keys go to YouTube's own keyboard shortcuts (k, l, j, f, c, m, 0, Shift+N) in the YouTube tab, only
  after checking that the browser window has the focus and the page (not the address bar) has it;
* buttons (Skip Ad, Like) are pressed through Windows UI Automation, the accessibility interface screen
  readers use - found by their accessible name, never by guessing a screen position;
* the queue: a small watcher reads the YouTube tab's title every 2 seconds (read-only). When the title
  changes because the song ended (YouTube moved on), the next queued song is loaded into that tab - only
  while you haven't touched the keyboard or mouse for a moment, and your window gets the focus back.
  If autoplay is off, YouTube stays on the finished song and the watcher can't tell it ended; say
  "next song" then.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import browser_tabs, winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.youtube")

_SUFFIXES = re.compile(r"\s*[-–—]\s*(google chrome|microsoft​? edge|microsoft edge|brave|mozilla firefox|"
                       r"opera|vivaldi)\s*$", re.I)


def clean_title(title: str) -> str:
    """'(3) Tum Hi Ho - Arijit Singh - YouTube - Google Chrome' -> 'Tum Hi Ho - Arijit Singh'."""
    text = _SUFFIXES.sub("", title.strip())
    text = re.sub(r"\s*[-–—]\s*youtube\s*$", "", text, flags=re.I)
    text = re.sub(r"^\(\d+\)\s*", "", text)
    return text.strip() or "YouTube"


@dataclass
class Song:
    url: str
    label: str


class Player:
    """What Jarvis started on YouTube, and what plays next."""

    POLL_SECONDS = 2.0
    IDLE_SECONDS = 2.0  # don't steal the focus while you are typing or moving the mouse
    KNOWN_FOR = 90.0  # a title seen this soon after Jarvis started a song is that song

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now: Song | None = None
        self.now_title: str | None = None  # the tab title while it plays (known a few seconds after it starts)
        self.started_at = 0.0
        self.queue: list[Song] = []
        self.exe: str | None = None
        self.step_aside = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self.sleep = time.sleep

    # -------------------------------------------------------------- state

    def started(self, url: str, label: str, exe: str | None) -> None:
        with self.lock:
            self.now, self.now_title, self.started_at = Song(url, label), None, time.monotonic()
            if exe:
                self.exe = exe

    def current_title(self) -> str | None:
        if self.exe is None:
            return None
        titles = browser_tabs.site_titles(self.exe, "youtube")
        title = clean_title(titles[0]) if titles else None
        if title and self.now is not None and self.now_title is None and time.monotonic() - self.started_at < self.KNOWN_FOR:
            self.now_title = title  # the first title seen soon after Jarvis started it is Jarvis's song
        return title

    # -------------------------------------------------------------- queue

    def add(self, song: Song, front: bool = False) -> int:
        with self.lock:
            self.queue.insert(0, song) if front else self.queue.append(song)
            count = len(self.queue)
        self.watch()
        return count

    def clear(self) -> int:
        with self.lock:
            count, self.queue = len(self.queue), []
        return count

    def pop(self) -> Song | None:
        with self.lock:
            return self.queue.pop(0) if self.queue else None

    def play(self, song: Song, restore_focus: bool = False) -> bool:
        """Load ``song`` into the YouTube tab (which must exist). False = couldn't."""
        if self.exe is None:
            return False
        before = winapi.foreground_window() if restore_focus else 0
        ok = browser_tabs.reuse_tab(self.exe, "youtube", song.url, None if restore_focus else self.step_aside)
        if ok:
            self.started(song.url, song.label, self.exe)
            log_event(_log, "queue_played")
            if restore_focus and before and winapi.foreground_window() != before:
                winapi.force_foreground(before)
        return ok

    # -------------------------------------------------------------- watcher

    def watch(self) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop.clear()
        self._watcher = threading.Thread(target=self._run, name="youtube-queue", daemon=True)
        self._watcher.start()

    def stop(self) -> None:
        self._stop.set()

    def check_once(self, baseline: str | None) -> str | None:
        """One look at the tab: returns the new baseline (the title that's playing now)."""
        title = self.current_title()
        if title is None:  # the YouTube tab was closed: forget the queue
            self.clear()
            return None
        if baseline is None or title == baseline:
            return title
        if not self.queue or winapi.seconds_since_input() < self.IDLE_SECONDS:
            return baseline if self.queue else title  # try again next time (you are busy)
        song = self.pop()
        if song is not None and not self.play(song, restore_focus=True):
            with self.lock:
                self.queue.insert(0, song)
            return baseline
        return None  # re-read after the new song's title shows

    def _run(self) -> None:
        baseline: str | None = None
        settle = 0
        while not self._stop.is_set() and self.queue:
            try:
                if settle > 0:
                    settle -= 1
                else:
                    new = self.check_once(baseline)
                    if new is None and baseline is not None:
                        settle = 2  # a song was just loaded: give the page a few seconds to show its title
                    baseline = new
            except Exception:
                _log.warning("queue watcher check failed", exc_info=True)
            self._stop.wait(self.POLL_SECONDS)


PLAYER = Player()


# ------------------------------------------------------------------ the tool


class YouTubeArgs(ToolArgs):
    action: Literal["now_playing", "skip_ad", "like", "forward", "back", "restart", "fullscreen", "captions",
                    "mute_video", "next", "queue", "show_queue", "clear_queue", "repeat"] = Field(
        description="now_playing = which song; queue = play 'query' after the current song; repeat = play the "
                    "current song once more; forward/back = seek by 'seconds'")
    query: str | None = Field(default=None, max_length=120, description="For 'queue': the song or artist")
    seconds: int = Field(default=10, ge=1, le=600, description="For forward/back")


_KEYS = {"restart": "0", "fullscreen": "f", "captions": "c", "mute_video": "m"}
_KEY_MESSAGES = {"restart": "Started the video again.", "fullscreen": "Toggled full screen.",
                 "captions": "Toggled captions.", "mute_video": "Toggled the video's sound."}


def _browser_exe(ctx: ToolContext) -> str:
    from app.tools.applications import browser_target

    if not winapi.is_windows():
        raise ToolError("YouTube control needs Windows.", "unsupported")
    target = browser_target(ctx)
    exe = PureWindowsPath(target.target).name.lower() if target is not None else (PLAYER.exe or "")
    if exe not in browser_tabs.TAB_BROWSERS:
        raise ToolError("I can only control YouTube in Chrome, Edge, Brave, Firefox, Opera or Vivaldi.", "unsupported")
    PLAYER.exe = PLAYER.exe or exe
    PLAYER.step_aside = ctx.step_aside
    return PLAYER.exe


def _tab(ctx: ToolContext) -> int:
    """Bring the YouTube tab in front (Jarvis steps aside first); its window."""
    exe = _browser_exe(ctx)
    if ctx.step_aside is not None and winapi.foreground_is_ours():
        try:
            ctx.step_aside()
        except Exception:
            pass
    hwnd = browser_tabs.find_site_tab(exe, "youtube")
    if hwnd is None:
        raise ToolError("There's no YouTube tab open. Say 'play' and a song first.", "no_tab")
    return hwnd


def _press(hwnd: int, keys: list[str]) -> None:
    browser_tabs.focus_page(hwnd)
    time.sleep(0.1)
    for key in keys:
        if winapi.foreground_window() != hwnd:
            raise ToolError("The browser lost the focus, so I stopped sending keys.", "focus_lost")
        winapi.press_combo(key)
        time.sleep(0.05)


def _seek_keys(seconds: int, forward: bool) -> list[str]:
    tens, rest = divmod(seconds, 10)
    fives = math.ceil(rest / 5)
    return ["l" if forward else "j"] * min(tens, 60) + ["right" if forward else "left"] * fives


async def youtube_control(args: YouTubeArgs, ctx: ToolContext) -> ToolResult:
    action = args.action
    if action == "now_playing":
        _browser_exe(ctx)
        title = PLAYER.current_title()
        if title is None:
            return ToolResult.failure("no_tab", "Nothing is playing on YouTube (there's no YouTube tab open).")
        return ToolResult.success(f"Now playing: {title}.", title=title)
    if action == "show_queue":
        if not PLAYER.queue:
            return ToolResult.success("The queue is empty.", queue=[])
        names = [song.label for song in PLAYER.queue]
        return ToolResult.success(f"Next up: {', '.join(names[:5])}" + (f" and {len(names) - 5} more." if len(names) > 5
                                                                        else "."), queue=names)
    if action == "clear_queue":
        count = PLAYER.clear()
        return ToolResult.success(f"Cleared the queue ({count} song(s))." if count else "The queue was already empty.")
    if action == "queue":
        return await _queue(args, ctx)
    if action == "repeat":
        exe = _browser_exe(ctx)
        playing = PLAYER.current_title()
        now = PLAYER.now
        if playing is None or now is None or PLAYER.now_title != playing:
            return ToolResult.failure("unknown_song", "I don't know which video is playing now (it wasn't started by "
                                      "me), so I can't repeat it.")
        PLAYER.exe = exe
        PLAYER.add(Song(now.url, playing), front=True)
        return ToolResult.success(f"I'll play {playing} once more when it ends.")
    if action == "next":
        song = PLAYER.pop()
        if song is not None:
            _browser_exe(ctx)
            if PLAYER.play(song):
                return ToolResult.success(f"Playing {song.label} from your queue.")
            PLAYER.add(song, front=True)
        hwnd = _tab(ctx)
        _press(hwnd, ["shift+n"])
        return ToolResult.success("Next video.")
    hwnd = _tab(ctx)
    if action == "skip_ad":
        pressed = browser_tabs.click_page_button(hwnd, r"^skip(\s+ads?)?\b|skip ad")
        if pressed is None:
            return ToolResult.failure("no_button", "I don't see a Skip button. The ad may not be skippable yet - "
                                      "YouTube shows it after about 5 seconds.")
        return ToolResult.success("Skipped the ad.")
    if action == "like":
        pressed = browser_tabs.click_page_button(hwnd, r"^like this video")
        if pressed is None:
            return ToolResult.failure("no_button", "I couldn't find YouTube's Like button (you may need to be "
                                      "signed in to YouTube).")
        return ToolResult.success("Liked it.")
    if action in ("forward", "back"):
        _press(hwnd, _seek_keys(args.seconds, action == "forward"))
        return ToolResult.success(f"{'Skipped forward' if action == 'forward' else 'Went back'} {args.seconds} seconds.")
    _press(hwnd, [_KEYS[action]])
    return ToolResult.success(_KEY_MESSAGES[action])


async def _queue(args: YouTubeArgs, ctx: ToolContext) -> ToolResult:
    from app.tools.assistant import PlayMusicArgs, first_youtube_video, play_music

    if not args.query or not args.query.strip():
        raise ToolError("Which song should I queue?", "missing_query")
    exe = _browser_exe(ctx)
    query = args.query.strip()
    if not browser_tabs.site_titles(exe, "youtube"):  # nothing playing: just play it
        return await play_music(PlayMusicArgs(query=query), ctx)
    video = await first_youtube_video(f"{query} song" if len(query.split()) <= 3 else query)
    if video is None:
        return ToolResult.failure("not_found", f"I couldn't find {query} on YouTube.")
    count = PLAYER.add(Song(f"https://www.youtube.com/watch?v={video}", query))
    return ToolResult.success(f"Added {query} to the queue" + (" - it plays after this song." if count == 1 else
                                                              f" (number {count})."), queue=count)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="youtube_control",
        description="Control the YouTube video/music playing in the browser: skip ad, like, seek forward/back, "
                    "restart, full screen, captions, mute the video, what song is playing, queue a song to play "
                    "after this one, show/clear the queue, repeat the song, next video.",
        handler=youtube_control, args_model=YouTubeArgs, level=RiskLevel.SAFE, category="info", timeout=30,
        needs_network=False,
    ),
)
