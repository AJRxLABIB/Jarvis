"""Is another app making sound right now (a video, music, a call)? Windows only, via the audio sessions' own meters.

Used to keep the screen's voices from being taken for yours: while something else plays, Jarvis only accepts what
starts with its name ("Jarvis, ...") or "stop". Its own sound is not counted. Elsewhere / on failure: never "playing".
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

_log = logging.getLogger(__name__)
PEAK = 0.03            # a session louder than this is "playing"
HOLD_SECONDS = 1.5     # stays "playing" through short pauses in the video's audio


class OtherAudio:
    def __init__(self, poll: float = 0.25) -> None:
        self.poll = poll
        self.last_heard = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = sys.platform == "win32"

    @property
    def playing(self) -> bool:
        return time.monotonic() - self.last_heard < HOLD_SECONDS

    def start(self) -> None:
        if not self.available or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="other-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

            comtypes.CoInitialize()
        except Exception:
            _log.info("other-app audio can't be measured here", exc_info=True)
            self.available = False
            return
        me = os.getpid()
        meters: dict[int, object] = {}
        refreshed = 0.0
        while not self._stop.wait(self.poll):
            try:
                if time.monotonic() - refreshed > 3.0:          # apps start and stop: list the sessions again
                    meters = {}
                    for session in AudioUtilities.GetAllSessions():
                        process = getattr(session, "Process", None)
                        pid = getattr(process, "pid", None) if process is not None else None
                        if pid is None or pid == me:
                            continue
                        meters[pid] = session._ctl.QueryInterface(IAudioMeterInformation)
                    refreshed = time.monotonic()
                if any(meter.GetPeakValue() > PEAK for meter in meters.values()):
                    self.last_heard = time.monotonic()
            except Exception:
                refreshed = 0.0                                   # a session went away: list them again next time
