"""GUI tests with a real (headless) Qt application. Skipped where Qt can't load."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except (ImportError, OSError) as exc:  # pragma: no cover - depends on the machine
    pytest.skip(f"Qt is not available here: {exc}", allow_module_level=True)

from app.ai.manager import AIManager
from app.main import bootstrap
from app.tts.manager import TTSManager
from app.ui.chat import ChatView
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.controller import AssistantController
from app.ui.hotkeys import MOD_CONTROL, MOD_SHIFT, is_global_combo, parse_hotkey
from app.ui.main_window import MainWindow
from app.ui.tray import TrayController, make_icon
from app.ui.visualizer import OrbWidget
from app.security.confirmations import ConfirmationRequest
from app.security.permissions import RiskLevel
from app.voice.microphone import Microphone
from tests.fake_audio import FakeSoundDevice, noise, tone
from tests.fake_ollama import FakeOllama, model_entry
from tests.test_tts import FakeEngine, FakePlayer
from tests.test_voice_pipeline import FakeSTT


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_until(app, condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    app.processEvents()
    return condition()


def make_controller(tmp_path: Path, replies=None, stt_text: str = "how is it going", audio=None):
    ctx = bootstrap(tmp_path, environ={"VAD_ENGINE": "energy"})  # the fake audio is tones, not speech
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=list(replies or []))
    engine = FakeEngine()
    tts = TTSManager(ctx.settings, tmp_path, engine=engine, player=FakePlayer())
    audio = audio if audio is not None else np.concatenate([noise(0.3), tone(0.8), noise(2.0)])
    mic = Microphone(sd=FakeSoundDevice(audio))
    controller = AssistantController(
        ctx, ai=AIManager.from_settings(ctx.settings, transport=fake.transport()), tts=tts,
        microphone=mic, stt=FakeSTT(stt_text),
    )
    controller.pipeline.settings = ctx.settings.model_copy(update={"silence_timeout": 0.4, "speech_start_timeout": 1.0})
    return controller, fake, engine


def record(controller: AssistantController) -> dict[str, list]:
    events: dict[str, list] = {"states": [], "user": [], "deltas": [], "finished": [], "tools": [], "confirm": [], "notice": []}
    controller.state_changed.connect(events["states"].append)
    controller.user_message.connect(events["user"].append)
    controller.assistant_delta.connect(events["deltas"].append)
    controller.assistant_finished.connect(events["finished"].append)
    controller.tool_event.connect(events["tools"].append)
    controller.confirmation_requested.connect(events["confirm"].append)
    controller.notice.connect(events["notice"].append)
    return events


def call(name: str, **args) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


# ------------------------------------------------------------------ widgets


def test_orb_renders_every_state_and_sleeps_when_hidden(qapp) -> None:
    orb = OrbWidget()
    orb.resize(300, 300)
    for state in ("IDLE", "LISTENING", "THINKING", "EXECUTING", "SPEAKING", "ERROR", "nonsense"):
        orb.set_state(state)
        orb.set_level(-20)
        assert not orb.grab().isNull()
    assert orb.state == "IDLE"  # unknown states fall back safely
    orb.show()
    qapp.processEvents()
    assert orb.animating
    orb.hide()
    assert not orb.animating  # no CPU use while hidden


def test_chat_escapes_everything(qapp) -> None:
    chat = ChatView("Jarvis")
    chat.add_message("user", '<script>alert(1)</script><a href="http://evil">x</a>')
    chat.begin_stream()
    chat.append_stream("<b>not bold</b> ")
    chat.append_stream("done")
    chat.end_stream()
    html = chat.toHtml()
    assert "<script>" not in html and 'href="http://evil"' not in html and "<b>not bold</b>" not in html
    assert chat.messages[-1] == ("assistant", "<b>not bold</b> done")
    assert "&lt;script&gt;" in html or "<script>" in chat.toPlainText()


def test_confirm_dialog_defaults_to_cancel_and_times_out(qapp) -> None:
    request = ConfirmationRequest("power_control", RiskLevel.DANGEROUS, "Restart the computer now?")
    dialog = ConfirmDialog(request, heard="restart my computer", timeout=1)
    assert dialog.cancel_button.isDefault() and not dialog.confirm_button.isDefault()
    assert dialog.confirm_button.objectName() == "danger"
    dialog.show()
    assert wait_until(qapp, lambda: not dialog.isVisible(), 3.0)
    assert dialog.result() == ConfirmDialog.DialogCode.Rejected
    approve = ConfirmDialog(request, timeout=30)
    approve.show()
    approve.confirm_button.click()
    assert approve.result() == ConfirmDialog.DialogCode.Accepted


def test_hotkey_parsing() -> None:
    assert parse_hotkey("ctrl+shift+j") == (MOD_CONTROL | MOD_SHIFT, ord("J"))
    assert parse_hotkey("Ctrl + Space") == (MOD_CONTROL, 0x20)
    assert parse_hotkey("alt+f4")[1] == 0x73
    assert is_global_combo("ctrl+space") and not is_global_combo("escape")  # Esc is never grabbed globally


def test_tray_icon(qapp) -> None:
    assert not make_icon("THINKING").isNull()


# --------------------------------------------------------------- controller


def test_text_conversation_streams_and_ends_idle(qapp, tmp_path: Path) -> None:
    controller, fake, engine = make_controller(tmp_path, replies=[{"role": "assistant", "content": "Hello there."}])
    events = record(controller)
    try:
        assert controller.send_text("hi")
        assert wait_until(qapp, lambda: events["finished"])
        assert events["user"] == ["hi"] and "".join(events["deltas"]) == "Hello there."
        assert events["finished"][0].ok and controller.state == "IDLE"
        assert "THINKING" in events["states"]
        assert engine.spoken == []  # typed replies are not spoken by default
    finally:
        controller.shutdown()


def test_tool_call_shows_executing_and_tool_line(qapp, tmp_path: Path) -> None:
    controller, fake, _ = make_controller(tmp_path, replies=[call("get_datetime"), {"role": "assistant", "content": "It's noon."}])
    events = record(controller)
    try:
        controller.send_text("how is it going?")
        assert wait_until(qapp, lambda: events["finished"])
        assert "EXECUTING" in events["states"] and events["tools"][0].startswith("[tool] get_datetime")
    finally:
        controller.shutdown()


def test_confirmation_round_trip_and_decline(qapp, tmp_path: Path) -> None:
    controller, fake, _ = make_controller(
        tmp_path, replies=[call("power_control", action="restart"), {"role": "assistant", "content": "Okay, cancelled."}]
    )
    events = record(controller)
    try:
        controller.send_text("I think you should restart the computer for me")  # the model decides: it asks
        assert wait_until(qapp, lambda: events["confirm"])
        pending = events["confirm"][0]
        assert pending.request.summary.startswith("Restart the computer")
        controller.resolve_confirmation(pending, False)
        assert wait_until(qapp, lambda: events["finished"])
        tool_result = [b for _, p, b in fake.requests if p == "/api/chat"][-1]["messages"][-1]["content"]
        assert '"error": "declined"' in tool_result
    finally:
        controller.shutdown()


def test_escape_cancels_a_pending_confirmation(qapp, tmp_path: Path) -> None:
    controller, fake, _ = make_controller(
        tmp_path, replies=[call("forget_everything"), {"role": "assistant", "content": "Nothing was deleted."}]
    )
    events = record(controller)
    try:
        controller.send_text("forget everything")
        assert wait_until(qapp, lambda: events["confirm"])
        controller.cancel()
        assert wait_until(qapp, lambda: events["finished"])
        assert events["confirm"][0].future.result() is False
    finally:
        controller.shutdown()


def test_voice_turn_listens_transcribes_answers_and_speaks(qapp, tmp_path: Path) -> None:
    controller, fake, engine = make_controller(tmp_path, replies=[{"role": "assistant", "content": "It is noon."}])
    events = record(controller)
    mic_states: list[bool] = []
    controller.microphone.on_active_change = mic_states.append
    try:
        controller.start_listening()
        assert controller.state == "LISTENING"
        assert wait_until(qapp, lambda: events["finished"], 8)
        assert events["user"] == ["how is it going"]
        assert wait_until(qapp, lambda: engine.spoken == ["It is noon."])
        assert mic_states == [True, False]  # microphone released after the utterance
        assert wait_until(qapp, lambda: controller.state == "IDLE")
    finally:
        controller.shutdown()


def test_busy_controller_refuses_a_second_request(qapp, tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path, replies=[{"role": "assistant", "content": "One."}])
    events = record(controller)
    try:
        assert controller.send_text("first")
        assert not controller.send_text("second")
        assert "Esc to cancel" in events["notice"][0]
        wait_until(qapp, lambda: events["finished"])
    finally:
        controller.shutdown()


# -------------------------------------------------------------- main window


def test_main_window_send_and_close_to_tray(qapp, tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path, replies=[{"role": "assistant", "content": "Hi, Labib."}])
    window = MainWindow(controller)
    try:
        window.show()
        window.input.setText("hello")
        window.send_button.click()
        assert window.input.text() == ""
        assert wait_until(qapp, lambda: any(r == "assistant" and t == "Hi, Labib." for r, t in window.chat.messages))
        assert "%" in window.cpu_label.text() and "GB" in window.ram_label.text()
        window.close()  # the X button hides to the tray...
        assert not window.isVisible()
        window.allow_close = True  # ...only "Exit" really closes
        window.close()
    finally:
        controller.shutdown()


def test_tray_menu_entries_all_work(qapp, tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller)
    opened, quit_called = [], []
    tray = TrayController(window, controller, lambda: opened.append(1), lambda: quit_called.append(1))
    try:
        assert set(tray.actions) == {"open", "hands_free", "listen", "stop_listen", "mute", "new_chat", "settings", "memory", "automations", "logs", "exit"}
        tray.actions["logs"].trigger()
        tray.actions["exit"].trigger()
        tray.actions["mute"].trigger()
        assert opened == [1] and quit_called == [1] and controller.tts.muted
        tray.actions["open"].trigger()
        assert window.isVisible()
        tray.actions["new_chat"].trigger()
        assert window.chat.messages[-1] == ("notice", "Started a new conversation.")
    finally:
        window.allow_close = True
        window.close()
        controller.shutdown()


def test_no_empty_assistant_label_around_tool_lines(qapp) -> None:
    chat = ChatView("Jarvis")
    chat.begin_stream()
    chat.append_stream("")  # nothing yet
    chat.add_message("tool", "[tool] get_datetime(): ok")
    chat.append_stream("It is noon.")
    chat.finish_reply("It is noon.")
    assert chat.messages == [("tool", "[tool] get_datetime(): ok"), ("assistant", "It is noon.")]
    assert chat.toPlainText().count("JARVIS ›") == 1


def test_reply_that_was_never_streamed_is_still_shown(qapp) -> None:
    chat = ChatView("Jarvis")
    chat.begin_stream()
    chat.finish_reply("Ollama is not running. Start Ollama and try again.")
    assert chat.messages == [("assistant", "Ollama is not running. Start Ollama and try again.")]


def test_screen_capture_indicator_and_wiring(qapp, tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller)
    tray = TrayController(window, controller, lambda: None, lambda: None)
    window.SCREEN_INDICATOR_MIN_MS = 50
    try:
        assert controller.executor.context.ai is controller.ai
        assert not window.screen_indicator.isVisibleTo(window)
        controller.executor.context.on_screen_capture(True)
        qapp.processEvents()
        assert window.screen_indicator.isVisibleTo(window) and "SCREEN CAPTURE ACTIVE" in tray.tray.toolTip()
        controller.executor.context.on_screen_capture(False)
        qapp.processEvents()
        assert window.screen_indicator.isVisibleTo(window)  # stays up briefly so it's noticed
        assert wait_until(qapp, lambda: not window.screen_indicator.isVisibleTo(window), 2.0)
        assert "SCREEN CAPTURE" not in tray.tray.toolTip()
        enabled = controller.ctx.settings.model_copy(update={"screen_capture_enabled": True})
        controller.apply_settings(enabled, {"screen_capture_enabled"})
        assert controller.executor.context.settings.screen_capture_enabled
    finally:
        window.allow_close = True
        window.close()
        controller.shutdown()


def test_run_gui_starts_and_quits_cleanly(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QMessageBox

    from app.ui import app as gui

    ctx = bootstrap(tmp_path, environ={"OLLAMA_HOST": "http://127.0.0.1:9", "TTS_ENABLED": "false", "MINI_ORB": "true"})
    windows = []
    real_window = gui.MainWindow

    def capture_window(*args, **kwargs):
        windows.append(real_window(*args, **kwargs))
        return windows[-1]

    monkeypatch.setattr(gui, "MainWindow", capture_window)
    monkeypatch.setattr(gui, "instance_lock_path", lambda: tmp_path / "instance.lock")  # not the real app's lock
    quitter = QTimer()  # quit() is ignored until the event loop runs, so keep asking
    quitter.timeout.connect(qapp.quit)
    quitter.start(600)
    assert gui.run_gui(ctx, ["jarvis"]) == 0
    quitter.stop()
    window = windows[0]
    assert any(role == "notice" and text.startswith("Hello.") for role, text in window.chat.messages)

    notices = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: notices.append(a[-1]))
    held = QLockFile(str(tmp_path / "instance.lock"))
    assert held.tryLock(100)
    try:
        assert gui.run_gui(ctx, ["jarvis"]) == 0 and "already running" in notices[0]  # single instance
    finally:
        held.unlock()
    window.allow_close = True
    window.close()


def test_hands_free_in_the_app(qapp, tmp_path: Path) -> None:
    """Always listening: the wake phrase starts a turn without pressing anything; the toggle is saved."""
    ctx = bootstrap(tmp_path, environ={"HANDS_FREE": "true", "WAKE_WORD_ENGINE": "whisper", "SILENCE_TIMEOUT": "0.5",
                                     "VAD_ENGINE": "energy"})
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=[{"role": "assistant", "content": "It's noon."}])
    engine = FakeEngine()
    tts = TTSManager(ctx.settings, tmp_path, engine=engine, player=FakePlayer())
    audio = np.concatenate([noise(1.0), tone(0.8), noise(30.0)])
    controller = AssistantController(
        ctx, ai=AIManager.from_settings(ctx.settings, transport=fake.transport()), tts=tts,
        microphone=Microphone(sd=FakeSoundDevice(audio)), stt=FakeSTT("Hey Jarvis, how is it going?"),
    )
    window = MainWindow(controller)
    events = record(controller)
    states: list[str] = []
    controller.hands_free_state.connect(states.append)
    try:
        assert controller.start_hands_free()
        assert wait_until(qapp, lambda: events["finished"], 10)
        assert events["user"] == ["how is it going?"] and events["finished"][0].text == "It's noon."
        assert "waiting" in states and window.hands_free_indicator.isVisibleTo(window)
        assert wait_until(qapp, lambda: any("noon" in t for t in engine.spoken), 5)  # the answer is spoken
        controller.set_hands_free(False)  # the tray/menu toggle
        assert controller.hands_free is None and "HANDS_FREE=false" in ctx.paths.env_file.read_text()
        assert not window.hands_free_action.isChecked()
    finally:
        window.allow_close = True
        window.close()
        controller.shutdown()


def test_reconnects_when_ollama_starts_later(qapp, tmp_path: Path, monkeypatch) -> None:
    """At sign-in Jarvis often starts before Ollama: it keeps checking and says when it's connected."""
    import httpx

    from app.utils import ollama_service

    started = []
    monkeypatch.setattr(ollama_service, "start_ollama", lambda *a, **kw: started.append(1) or True)
    controller, fake, _ = make_controller(tmp_path)
    monkeypatch.setattr(controller, "STATUS_RETRY_START", 0.05)
    controller._status_retry = 0.05
    events = record(controller)
    statuses = []
    controller.status_changed.connect(statuses.append)
    fake.raise_on["/api/version"] = httpx.ConnectError("refused")
    try:
        controller.refresh_status()
        assert wait_until(qapp, lambda: len(statuses) >= 2, 5)  # it retried by itself
        assert not statuses[0].reachable and started == [1]  # and tried to start Ollama, once
        assert any("Starting Ollama" in n for n in events["notice"])
        del fake.raise_on["/api/version"]
        assert wait_until(qapp, lambda: statuses[-1].reachable, 5)
        assert wait_until(qapp, lambda: any("Connected to the AI model" in n for n in events["notice"]), 3)
        assert started == [1]
    finally:
        controller.shutdown()


def test_repeated_problem_is_shown_once(qapp, tmp_path: Path) -> None:
    from app.ai.manager import AIStatus

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller)
    try:
        down = AIStatus(provider="ollama", reachable=False, message="Ollama is not running.")
        for _ in range(3):
            window._on_status(down)
        errors = [t for r, t in window.chat.messages if r == "error"]
        assert errors == ["Ollama is not running. I'll keep trying."]
    finally:
        window.allow_close = True
        window.close()
        controller.shutdown()



def test_one_lock_for_every_way_of_starting(tmp_path: Path) -> None:
    """The installed app and `python run.py` keep data in different folders but share the instance lock."""
    from app.ui import app as gui

    assert gui.instance_lock_path().parent == Path(__import__("tempfile").gettempdir())
    assert "JarvisAssistant" in gui.instance_lock_path().name


def test_startup_tidy_minimises_ollama_and_brings_jarvis_forward(qapp) -> None:
    from app.tools.winapi import WindowInfo
    from app.ui.startup import StartupTidy

    windows = [WindowInfo(1, 10, "Ollama", "ollama app.exe"), WindowInfo(2, 11, "Inbox - Google Chrome", "chrome.exe"),
               WindowInfo(3, 12, "ollama - Notepad", "notepad.exe")]
    minimised, forward = [], []
    tidy = StartupTidy(lambda: forward.append(1), windows=lambda: windows, minimize=minimised.append)
    tidy.check()
    assert minimised == [1] and forward == [1]  # only Ollama's own window
    tidy.check()
    assert minimised == [1]  # each window once: if you open it again yourself it stays
    late = StartupTidy(lambda: None, seconds=-1, windows=lambda: windows, minimize=minimised.append)
    late.check()
    assert minimised == [1] and not late.timer.isActive()  # after the start-up period: hands off


def test_command_center_layout(qapp, tmp_path: Path) -> None:
    """The dashboard: tabs switch pages, the event bus and timing panel show what happened."""
    from app.ui.main_window import MainWindow

    controller = make_controller(tmp_path)[0]
    window = MainWindow(controller)
    try:
        assert window.pages.currentIndex() == 0 and window.orb is not None
        window.tabs[1].click()
        assert window.pages.currentIndex() == 1
        window.tabs[0].click()
        assert window.pages.currentIndex() == 0
        window._on_tool_event("[tool] open_application(name='chrome'): ok - Opened Google Chrome.")
        assert window.events.lines[-1][1] == "tool" and "open_application" in window.live_box.text()
        window._on_timing({"heard": 0.4, "ai": 0.9, "voice": 0.5})
        assert window.timing_big.text() == "1.8 s"
        window._refresh_dashboard(counts=True)
        assert window.modules["hands_free"].text() == "OFF" and window.overview["actions"].text() == "PROTECTED"
        assert window.live_voice_button.text() == "LIVE VOICE: OFF"
    finally:
        window.allow_close = True
        window.close()
