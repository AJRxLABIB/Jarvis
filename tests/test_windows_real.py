"""Phase 13: tests against the real Windows APIs. They run only on Windows.

Default (read-only, nothing on your PC changes): known folders, window list,
app lookup, volume reading, system stats, Windows voices, real screen capture
of a test window and real OCR.

Opt-in with environment variables:
  JARVIS_TEST_INTERACTIVE=1  opens and closes Notepad, registers and releases a
                             hotkey, toggles "Start with Windows" and restores it
  JARVIS_TEST_OLLAMA=1       asks your installed Ollama model real questions
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows API tests")

ROOT = Path(__file__).resolve().parents[1]
INTERACTIVE = os.environ.get("JARVIS_TEST_INTERACTIVE") == "1"
OLLAMA = os.environ.get("JARVIS_TEST_OLLAMA") == "1"


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower())


class Yes:
    def __init__(self) -> None:
        self.requests = []

    async def confirm(self, request) -> bool:
        self.requests.append(request)
        return True


def executor(settings=None, confirmer=None):
    from app.agent.tool_manager import ToolExecutor
    from app.config.files import load_config_bundle
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    config = load_config_bundle(ROOT / "config")
    ctx = ToolContext(settings or Settings(), config=config)
    return ToolExecutor(build_default_registry(), PermissionEngine(config.permissions), ctx, confirmer=confirmer)


def run_tool(ex, tool_name: str, /, **arguments):
    from app.ai.base import ToolCall

    return asyncio.run(ex.execute(ToolCall(tool_name, arguments))).result


# ------------------------------------------------------------------ system


def test_known_folders_are_real() -> None:
    from app.tools import winapi

    for key in ("DESKTOP", "DOCUMENTS", "DOWNLOADS", "PICTURES"):
        folder = winapi.known_folder(key)
        assert folder is not None and folder.is_dir(), key


def test_path_policy_uses_real_folders() -> None:
    from app.config.files import load_config_bundle
    from app.security.validator import PathPolicy, Zone

    policy = PathPolicy.build(load_config_bundle(ROOT / "config").permissions)
    assert policy.zone(policy.known["DOWNLOADS"] / "x.pdf") == Zone.SAFE
    assert policy.zone(Path(os.environ["SystemRoot"]) / "System32") == Zone.PROTECTED


def test_window_list_has_real_windows() -> None:
    from app.tools import winapi

    windows = winapi.top_level_windows()
    assert windows and all(w.hwnd and w.pid for w in windows)
    assert any(w.switchable for w in windows)
    assert any(w.process_name == "explorer.exe" for w in windows)


def test_common_apps_are_found() -> None:
    from app.config.files import load_config_bundle
    from app.tools.applications import resolve_application

    apps = load_config_bundle(ROOT / "config").apps
    for name in ("notepad", "calculator", "file explorer", "terminal"):
        assert resolve_application(name, apps) is not None, name


def test_system_status_and_volume_are_real() -> None:
    from app.tools import winapi

    result = run_tool(executor(), "system_status")
    assert result.ok and result.message
    volume, muted = winapi.get_volume()
    assert 0 <= volume <= 100 and isinstance(muted, bool)


def test_windows_voices() -> None:
    from app.tts.sapi import SapiTTS

    engine = SapiTTS()
    engine.load()
    assert engine.description.startswith("Windows voice (")


def test_start_with_windows_command_points_to_real_files() -> None:
    from app.utils import autostart

    assert isinstance(autostart.is_enabled(), bool)
    parts = re.findall(r'"([^"]+)"', autostart.launch_command())
    assert parts and all(Path(p).exists() for p in parts)


def test_allowed_command_runs_for_real() -> None:
    import shutil

    from app.security.command_executor import CommandExecutor

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    output = CommandExecutor().run(["git", "--version"], ROOT, timeout=30)
    assert output.returncode == 0 and "git version" in output.output


# ------------------------------------------------------------ screen + OCR


def _text_image(text: str):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 160), "white")
    font = ImageFont.truetype(str(Path(os.environ["SystemRoot"]) / "Fonts" / "arial.ttf"), 56)
    ImageDraw.Draw(image).text((30, 45), text, fill="black", font=font)
    return image


def _windows_ocr():
    from app.vision.ocr import WindowsOcr

    engine = WindowsOcr()
    problem = engine.problem()
    if problem:
        pytest.fail(f"Windows OCR unavailable: {problem}")
    return engine


def test_windows_ocr_reads_rendered_text() -> None:
    engine = _windows_ocr()
    result = asyncio.run(engine.recognize(_text_image("Jarvis reads 2468 today")))
    assert "jarvis" in normalized(result.text) and "2468" in normalized(result.text), result.text
    assert result.lines[0].box is not None


def test_ocr_then_qt_does_not_crash() -> None:
    """winrt ships an older msvcp140.dll; loading it before Qt used to crash Qt (access violation)."""
    import subprocess

    code = ("from app.vision.ocr import WindowsOcr; WindowsOcr()._modules(); "
            "from PySide6.QtWidgets import QApplication; print('both loaded')")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          shell=False)
    assert done.returncode == 0 and "both loaded" in done.stdout, done.stderr[-800:]


def test_tesseract_if_installed() -> None:
    from app.vision.ocr import TesseractOcr

    engine = TesseractOcr()
    if engine.problem():
        pytest.skip("Tesseract is not installed")
    result = asyncio.run(engine.recognize(_text_image("Jarvis reads 2468 today")))
    assert "2468" in normalized(result.text)


def test_monitors_and_full_screen_grab() -> None:
    from app.vision import capture as cap

    monitors = cap.backend.monitors()
    assert monitors and monitors[0].primary and monitors[0].width >= 640
    primary = cap.backend.grab(monitors[0].bbox)
    assert primary.size == (monitors[0].width, monitors[0].height)  # real pixels, whatever the scaling


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


_TEST_WINDOW = """
import sys
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication(sys.argv[:1])  # argv[1] = window title
label = QLabel("Screen test 7351 OK")
label.setWindowTitle(sys.argv[1])
label.setStyleSheet("background:white; color:black; font-size:48px; padding:40px;")
label.setAlignment(Qt.AlignmentFlag.AlignCenter)
label.resize(760, 240)
label.show()
QTimer.singleShot(20000, app.quit)
app.exec()
"""


def test_capture_a_real_window_and_read_it() -> None:
    """End to end on the real desktop: a real window (own process, real Qt platform), captured and read.

    Other GUI tests run Qt 'offscreen' in this process, so the window must live in a separate one.
    """
    import subprocess
    import uuid

    from app.tools import winapi
    from app.vision import capture as cap

    title = f"Jarvis capture test {uuid.uuid4().hex[:8]}"
    env = {**os.environ, "QT_QPA_PLATFORM": "windows"}
    # A venv's python.exe is a launcher that starts the real interpreter as another process,
    # so the window is found by its unique title, not by the child's process id.
    child = subprocess.Popen([sys.executable, "-c", _TEST_WINDOW, title], env=env, shell=False,
                             stderr=subprocess.PIPE)
    try:
        window = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and window is None and child.poll() is None:
            time.sleep(0.3)
            window = next((w for w in winapi.top_level_windows() if w.title == title), None)
        if window is None:
            error = child.stderr.read().decode("utf-8", "replace")[-800:] if child.poll() is not None else ""
            pytest.fail(f"the test window did not appear. {error}")
        time.sleep(0.5)  # let it paint
        shot = cap.capture_window(window, [])
        assert shot.image.width >= 300 and shot.image.getbbox() is not None
        text = normalized(asyncio.run(_windows_ocr().recognize(shot.image)).text)
        assert "7351" in text, text
        assert cap.active_window().pid != os.getpid()  # the assistant never picks its own window
        with pytest.raises(Exception, match="private-window"):
            cap.capture_window(window, ["capture test"])
    finally:
        child.kill()  # the launcher; the real interpreter quits by itself after 20 s
        child.wait(10)
        for w in winapi.top_level_windows():
            if w.title == title:
                winapi.close_window(w.hwnd)


# ------------------------------------------------------------- interactive


@pytest.mark.skipif(not INTERACTIVE, reason="set JARVIS_TEST_INTERACTIVE=1")
def test_open_and_close_notepad() -> None:
    from app.tools import winapi

    confirmer = Yes()
    ex = executor(confirmer=confirmer)
    opened = run_tool(ex, "open_application", name="notepad")
    assert opened.ok, f"{opened.error}: {opened.message}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(w.process_name == "notepad.exe" and w.switchable
                                                  for w in winapi.top_level_windows()):
        time.sleep(0.3)
    result = run_tool(ex, "close_application", name="notepad")
    notepads = [(w.title, w.process_name, w.switchable) for w in winapi.top_level_windows()
                if "notepad" in (w.process_name + w.title).lower()]
    assert result.ok, f"{result.error}: {result.message} | windows now: {notepads}"


@pytest.mark.skipif(not INTERACTIVE, reason="set JARVIS_TEST_INTERACTIVE=1")
def test_hotkey_register_and_release(qapp) -> None:
    from PySide6.QtWidgets import QWidget

    from app.ui.hotkeys import HotkeyManager

    widget = QWidget()
    manager = HotkeyManager()
    try:
        results = manager.register(int(widget.winId()), {"test": "Ctrl+Alt+Shift+F12"})
        assert results == {"test": True}, manager.failed
    finally:
        manager.unregister()
        widget.close()


@pytest.mark.skipif(not INTERACTIVE, reason="set JARVIS_TEST_INTERACTIVE=1")
def test_start_with_windows_toggle_is_restored() -> None:
    from app.utils import autostart

    before = autostart.is_enabled()
    try:
        autostart.set_enabled(True)
        assert autostart.is_enabled()
        autostart.set_enabled(False)
        assert not autostart.is_enabled()
    finally:
        autostart.set_enabled(before)
    assert autostart.is_enabled() == before


# ------------------------------------------------------------------ Ollama


@pytest.mark.skipif(not OLLAMA, reason="set JARVIS_TEST_OLLAMA=1 (needs Ollama running)")
def test_real_model_answers_and_uses_a_tool() -> None:
    from app.agent.conversation import ConversationManager
    from app.ai.manager import AIManager
    from app.config.settings import load_settings

    settings = load_settings(ROOT / ".env" if (ROOT / ".env").exists() else None)
    ai = AIManager.from_settings(settings)
    ex = executor(settings)
    conversation = ConversationManager(ai, settings, None, tools=ex)

    async def talk():
        try:
            greeting = await conversation.send("Say hello in five words or fewer.")
            timed = await conversation.send("What is the date today?")
            return greeting, timed
        finally:
            await ai.aclose()

    greeting, timed = asyncio.run(talk())
    assert greeting.ok and greeting.text.strip()
    assert timed.ok and any(e.call.name == "get_datetime" for e in timed.tools_used), timed
