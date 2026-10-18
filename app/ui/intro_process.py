"""The intro in its own process, so it stays smooth while Jarvis loads.

Jarvis loads the speech models, the voice and the AI connection at start. In the same process that
work starves the animation of frames (the name sat on "J.A.R.V.D.E." for two seconds). So the intro
runs as a second copy of the program (``--intro-child``) with its own interpreter and its own main
thread. It starts first - before Jarvis has even built its window.

Protocol (one JSON object per line):
  Jarvis -> intro (stdin):  {"lines": [[label, text, kind], ...]}  the real states, several times a second
                            {"ready": true}                       loaded: play the finale now
  intro -> Jarvis (stdout): {"finale": true}                      show the command center now

The intro waits at its hold point (rings still turning) until "ready", at most MAX_WAIT seconds. If
Jarvis goes away (stdin closes), the intro closes too. If the intro can't start, Jarvis just appears.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Callable

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ui.intro")


# ------------------------------------------------------------------ the child (intro process)

def run_intro_child(title: str, subtitle: str, owner: str = "") -> int:
    import os

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.tools import winapi
    from app.ui.intro import IntroScreen

    from app.ui.app_icon import app_icon, set_app_id

    set_app_id()                      # the intro belongs to the same taskbar button as Jarvis
    app = QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    states: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    flags = {"ready": False, "gone": False}

    def reader() -> None:  # the parent's messages
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            for label, text, kind in message.get("lines", []):
                if label not in states:
                    order.append(label)
                states[label] = (str(text), str(kind))
            if message.get("ready"):
                flags["ready"] = True
        flags["gone"] = True  # Jarvis closed our stdin (it quit, or crashed)

    threading.Thread(target=reader, name="intro-stdin", daemon=True).start()

    def line(label: str) -> Callable[[], tuple[str, str]]:
        return lambda: states.get(label, ("STARTING", "wait"))

    labels = ["Speech output", "Speech recognition", "AI model", "Hands-free", "Wake word", "Memory"]
    intro = IntroScreen(title, subtitle, [(label, line(label)) for label in labels], wait_for_ready=True, owner=owner)

    def finale() -> None:
        winapi.allow_any_foreground()  # we are in front: let Jarvis take over the focus
        try:
            sys.stdout.write(json.dumps({"finale": True}) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    intro.finished.connect(finale)
    intro.destroyed.connect(app.quit)

    def poll() -> None:
        if flags["ready"]:
            intro.ready()
        if flags["gone"] and not flags["ready"]:
            intro.close()

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(50)
    intro.start()
    return app.exec()


# ------------------------------------------------------------------ the parent (Jarvis)

def child_command(title: str, subtitle: str, owner: str = "") -> list[str]:
    from app.utils.paths import is_frozen, project_root

    args = ["--intro-child", "--intro-title", title, "--intro-subtitle", subtitle]
    if owner:
        args += ["--intro-owner", owner]
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, str(project_root() / "run.py"), *args]


class IntroLauncher:
    """Starts the intro process, feeds it the real states, and calls ``on_finale`` when it's time."""

    def __init__(self, title: str, subtitle: str, on_finale: Callable[[], None],
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, owner: str = "") -> None:
        self.title, self.subtitle, self.owner = title, subtitle, owner
        self.on_finale = on_finale
        self._popen = popen
        self.process: subprocess.Popen | None = None
        self._finale = threading.Event()

    def start(self) -> bool:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self.process = self._popen(child_command(self.title, self.subtitle, self.owner), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                       encoding="utf-8", creationflags=flags)
        except OSError:
            _log.warning("the intro could not start; Jarvis appears without it", exc_info=True)
            return False
        threading.Thread(target=self._read, name="intro-stdout", daemon=True).start()
        log_event(_log, "intro_process_started")
        return True

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("finale"):
                self._finale.set()
                return
        self._finale.set()  # the intro ended without a finale (closed, crashed): appear anyway

    @property
    def finale_reached(self) -> bool:
        return self._finale.is_set()

    def send(self, lines: list[tuple[str, str, str]] | None = None, ready: bool = False) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            return
        message: dict = {}
        if lines is not None:
            message["lines"] = [list(line) for line in lines]
        if ready:
            message["ready"] = True
        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        process = self.process
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
