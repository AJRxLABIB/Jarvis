"""3.1.1: Jarvis in front at start, no terminal pop-ups from Ollama, a smooth intro (own process),
cheaper HUD and orb frames."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut


# ------------------------------------------------------------------ Ollama without terminal windows

def test_ollama_server_starts_with_a_hidden_console(monkeypatch) -> None:
    from app.tools import winapi
    from app.utils import ollama_service

    seen = {}
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(ollama_service, "ollama_running", lambda: False)
    monkeypatch.setattr(ollama_service, "find_ollama_server", lambda: "C:/Ollama/ollama.exe")
    monkeypatch.setattr(ollama_service, "find_ollama_app", lambda: None)
    monkeypatch.setattr(winapi, "start_program", lambda exe, args, **kw: seen.update(exe=exe, args=args, **kw))
    assert ollama_service.start_ollama()
    assert seen["args"] == ["serve"] and seen["hidden_console"] is True


def test_hidden_console_flag(monkeypatch) -> None:
    from app.tools import winapi

    calls = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append(kw["creationflags"]))
    winapi.start_program("ollama.exe", ["serve"], hidden_console=True)
    winapi.start_program("chrome.exe", [])
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    assert calls[0] & no_window and not (detached and calls[0] & detached)
    assert not (calls[1] & no_window) or not no_window


def test_foreground_helpers_are_harmless_elsewhere(monkeypatch) -> None:
    from app.tools import winapi

    monkeypatch.setattr(winapi, "is_windows", lambda: False)
    assert winapi.force_foreground(123) is False
    winapi.set_topmost(123, True)
    winapi.allow_any_foreground()


# ------------------------------------------------------------------ stay on top by voice

@pytest.mark.parametrize("said, action", [
    ("stay on top", "stay_on_top"), ("stay on top of everything", "stay_on_top"), ("সবার উপরে থাকো", "stay_on_top"),
    ("stop staying on top", "not_on_top"),
])
def test_on_top_commands(said, action) -> None:
    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.arguments["action"] == action


# ------------------------------------------------------------------ the intro process protocol

class FakeProcess:
    def __init__(self, output: str) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)
        self._code = None

    def poll(self):
        return self._code


def test_launcher_feeds_states_and_hears_the_finale() -> None:
    from app.ui.intro_process import IntroLauncher

    process = FakeProcess(json.dumps({"finale": True}) + "\n")
    launcher = IntroLauncher("J", "sub", lambda: None, popen=lambda *a, **kw: process)
    assert launcher.start()
    launcher.send([("Speech output", "READY", "ok")], ready=True)
    sent = json.loads(process.stdin.getvalue().splitlines()[0])
    assert sent == {"lines": [["Speech output", "READY", "ok"]], "ready": True}
    import time

    for _ in range(100):
        if launcher.finale_reached:
            break
        time.sleep(0.01)
    assert launcher.finale_reached


def test_launcher_appears_anyway_when_the_intro_dies() -> None:
    from app.ui.intro_process import IntroLauncher

    launcher = IntroLauncher("J", "", lambda: None, popen=lambda *a, **kw: FakeProcess(""))
    launcher.start()
    import time

    for _ in range(100):
        if launcher.finale_reached:
            break
        time.sleep(0.01)
    assert launcher.finale_reached  # no finale message, stdout closed: don't wait for ever


def test_launcher_without_an_intro() -> None:
    from app.ui.intro_process import IntroLauncher

    def fail(*a, **kw):
        raise OSError("no")

    assert IntroLauncher("J", "", lambda: None, popen=fail).start() is False


def test_child_command(monkeypatch) -> None:
    from app.ui import intro_process
    from app.utils import paths

    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    command = intro_process.child_command("J.A.R.V.I.S.", "sub")
    assert command[0] == sys.executable and command[1:3] == ["--intro-child", "--intro-title"]
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    assert intro_process.child_command("J", "s")[1].endswith("run.py")


def test_intro_child_is_handled_before_anything_else(monkeypatch) -> None:
    from app import main as app_main
    from app.ui import intro_process

    seen = []
    monkeypatch.setattr(intro_process, "run_intro_child", lambda title, sub: seen.append((title, sub)) or 0)
    monkeypatch.setattr(app_main, "bootstrap", lambda *a, **kw: pytest.fail("no bootstrap for the intro"))
    assert app_main.main(["--intro-child", "--intro-title", "J.A.R.V.I.S.", "--intro-subtitle", "x"]) == 0
    assert seen == [("J.A.R.V.I.S.", "x")]


# ------------------------------------------------------------------ Qt parts

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    return QApplication.instance() or QApplication([])


def test_intro_holds_until_ready(qapp) -> None:
    from app.ui.intro import FINALE, HOLD, MAX_WAIT, IntroScreen

    now = [0.0]
    intro = IntroScreen("J", "", [], clock=lambda: now[0], wait_for_ready=True)
    intro._t0 = intro._spin0 = 0.0
    now[0] = 10.0
    assert intro.elapsed() == HOLD and intro._spin_elapsed() == 10.0  # waiting; the rings keep turning
    intro.ready()
    assert intro.elapsed() == HOLD
    now[0] = 10.0 + (FINALE - HOLD) + 0.01
    assert intro.elapsed() > FINALE
    stuck = IntroScreen("J", "", [], clock=lambda: now[0], wait_for_ready=True)
    stuck._t0 = stuck._spin0 = 0.0
    now[0] = MAX_WAIT + 1
    stuck.elapsed()
    assert not stuck._waiting  # never waits for ever


def test_orb_layers_are_cached_and_look_the_same(qapp) -> None:
    import numpy as np
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QConicalGradient, QImage, QPainter

    from app.ui.visualizer import OrbWidget, _alpha

    orb = OrbWidget()
    orb.resize(400, 400)
    image = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    orb.render(image)
    layers = len(orb._layers)
    orb.render(image)
    assert layers and len(orb._layers) == layers  # the second frame reuses them

    c, r, color, t = QPointF(200, 200), 150.0, QColor("#22d3ee"), 4.2
    old = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    old.fill(0)
    p = QPainter(old)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    sweep = QConicalGradient(c, -(t * 25) % 360)
    sweep.setColorAt(0.0, _alpha(color, 0.10))
    sweep.setColorAt(0.08, QColor(0, 0, 0, 0))
    sweep.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(sweep)
    p.drawEllipse(c, r, r)
    p.end()
    new = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
    new.fill(0)
    p = QPainter(new)
    side = int(2 * r) + 2
    wedge = orb._layer(("sweep-test", side), (side, side), lambda q: orb._draw_sweep(q, QPointF(side / 2, side / 2), r, color))
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.translate(c)
    p.rotate((t * 25) % 360)
    p.drawPixmap(QPointF(-side / 2, -side / 2), wedge)
    p.end()

    def pixels(img):
        return np.frombuffer(img.constBits(), np.uint8).reshape(400, 400, 4).astype(int)

    assert np.abs(pixels(old) - pixels(new)).mean() < 0.2  # the pre-drawn sweep matches the original


def test_background_repaints_only_the_gaps(qapp) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QRegion

    from app.ui.hud import HexBackground

    bg = HexBackground()
    bg.resize(400, 300)
    updated = []
    bg.update = lambda region=None, *a: updated.append(region)
    bg._covered = QRegion(0, 0, 400, 200)  # a panel over the top part
    bg._update_gaps(QRect(0, 0, 400, 100))
    assert updated == []  # all under the panel: nothing to repaint
    bg._update_gaps(QRect(0, 150, 400, 100))
    assert updated and updated[0].boundingRect().top() >= 200


def test_window_waits_for_the_intro(qapp) -> None:
    from app.ui.main_window import MainWindow

    shown = []
    fake = SimpleNamespace(_started=False, _intro=None, wants_full_screen=True,
                           showFullScreen=lambda: shown.append("full"), showNormal=lambda: shown.append("normal"))
    MainWindow.bring_back(fake)
    assert shown == []  # not before the intro hands over
