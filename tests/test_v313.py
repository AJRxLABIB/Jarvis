"""3.1.3: "PROBLEM" clears itself when the AI connects (it stayed red after Ollama started late)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    return QApplication.instance() or QApplication([])


def statuses():
    from app.ai.base import ModelInfo
    from app.ai.manager import AIStatus

    down = AIStatus("ollama", reachable=False, message="Ollama is not running. Start Ollama and try again.")
    model = ModelInfo(name="qwen3:4b-instruct")
    up = AIStatus("ollama", reachable=True, models=(model,), selected=model)
    return down, up


def test_problem_shows_while_the_ai_is_away_and_clears_when_it_answers(qapp, tmp_path) -> None:
    from app.ui.main_window import MainWindow
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller, 3.0)
    controller._schedule = lambda *a, **kw: None  # no real retries in the test
    controller._try_start_ollama = lambda: None
    down, up = statuses()
    for status in (down, down):  # Jarvis started before Ollama, and retried
        controller.status_changed.emit(status)
    assert controller._state == "ERROR" and window.orb.state == "ERROR"
    assert window.state_label.text() == "PROBLEM"
    controller.status_changed.emit(up)  # Ollama is up now
    assert controller._state == "IDLE" and window.orb.state == "IDLE"
    assert window.state_label.text() != "PROBLEM"
    window.close()


def test_first_check_already_fine_after_a_problem(qapp, tmp_path) -> None:
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    controller._schedule = lambda *a, **kw: None
    controller._try_start_ollama = lambda: None
    down, up = statuses()
    controller._after_status(down)
    controller._ai_was_ok = None  # e.g. the first check ever came back fine only now
    controller._after_status(up)
    assert controller._state == "IDLE"


def test_problem_does_not_interrupt_a_reply(qapp, tmp_path) -> None:
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    controller._schedule = lambda *a, **kw: None
    controller._try_start_ollama = lambda: None
    controller._busy = True
    controller._state = "THINKING"
    controller._after_status(statuses()[0])
    assert controller._state == "THINKING"
