"""3.2.1: Jarvis gets out of the way - after opening or searching it fades into the mini orb so the app is
visible, and keys / scrolls / clicks go to your app, not to Jarvis. "Search X on YouTube" is direct."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut


@pytest.mark.parametrize("said, query, engine", [
    ("search imran hashmi in youtube", "imran hashmi", "youtube"),
    ("search Imran Hashmi on YouTube", "Imran Hashmi", "youtube"),
    ("search youtube for arijit singh", "arijit singh", "youtube"),
    ("open youtube and search imran hashmi", "imran hashmi", "youtube"),
    ("search for cats on google", "cats", "google"),
    ("ইউটিউবে ইমরান হাশমি সার্চ করো", "ইমরান হাশমি", "youtube"),
])
def test_search_commands_are_direct(said, query, engine) -> None:
    call = match_shortcut(said, lambda n: None)
    assert call is not None and call.name == "search_in_browser"
    assert dict(call.arguments) == {"query": query, "engine": engine}


def test_input_goes_to_your_app_not_to_jarvis(monkeypatch) -> None:
    from app.tools import screen_agent, winapi

    events = []
    monkeypatch.setattr(winapi, "foreground_is_ours", lambda: True)
    monkeypatch.setattr(winapi, "target_window", lambda: SimpleNamespace(hwnd=42))
    monkeypatch.setattr(winapi, "force_foreground", lambda hwnd: events.append(("focus", hwnd)))
    ctx = SimpleNamespace(step_aside=lambda: events.append(("aside", None)))
    screen_agent.hand_over(ctx)
    assert events == [("aside", None), ("focus", 42)]
    events.clear()
    monkeypatch.setattr(winapi, "foreground_is_ours", lambda: False)
    screen_agent.hand_over(ctx)
    assert events == []  # your app is already in front: nothing to do


def test_every_input_tool_hands_over_first(monkeypatch) -> None:
    import inspect

    from app.tools import screen_agent, voice_control

    for fn in (screen_agent.type_text, screen_agent.press_keys, screen_agent.scroll_screen, screen_agent.click_text,
               voice_control.app_command, voice_control.mouse_control):
        assert "hand_over(" in inspect.getsource(fn), fn.__name__


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    return QApplication.instance() or QApplication([])


def execution(tool: str, ok: bool = True, **arguments):
    from app.agent.tool_manager import ToolExecution
    from app.ai.base import ToolCall
    from app.tools.base import ToolResult

    return ToolExecution(ToolCall(tool, arguments), ToolResult(ok, "done"))


def test_opening_something_asks_the_window_to_step_aside(qapp, tmp_path) -> None:
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    asked = []
    controller.step_aside_requested.connect(asked.append)
    controller._tool_done(execution("open_website", site="YouTube"))
    controller._tool_done(execution("search_in_browser", query="x"))
    controller._tool_done(execution("open_website", ok=False, site="nope"))  # failed: stay
    controller._tool_done(execution("get_datetime"))  # nothing shown: stay
    controller._tool_done(execution("window_control", action="minimize", name="Chrome"))
    assert asked == [None, None]
    controller.ctx.settings = controller.ctx.settings.model_copy(update={"always_on_top": True})
    controller._tool_done(execution("open_website", site="YouTube"))
    assert len(asked) == 2  # "stay on top" means stay


def test_window_fades_into_the_orb_and_comes_back(qapp, tmp_path) -> None:
    from app.ui.main_window import MainWindow
    from tests.test_gui import make_controller, wait_until

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller, 3.0)
    window.show_start(False, fade=False)
    done = threading.Event()
    window.step_aside(done)
    assert wait_until(qapp, done.is_set, 3)
    assert window.isMinimized() and window.windowOpacity() == 1.0
    window.bring_back()
    assert wait_until(qapp, lambda: not window.isMinimized() and window.windowOpacity() > 0.99, 3)
    window.close()


def test_step_aside_blocking_waits_for_the_window(qapp, tmp_path) -> None:
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    controller.step_aside_requested.connect(lambda done: threading.Timer(0.2, done.set).start())
    started = time.monotonic()
    controller.step_aside_blocking()
    assert 0.15 <= time.monotonic() - started < 1.0
