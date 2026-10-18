"""The window / tab overview: layout, motion, keyboard and mouse, live-picture lifecycle and the voice commands."""

from __future__ import annotations

import time

import pytest

from app.agent.shortcuts import match_shortcut
from app.core.overview import OverviewItem, ease_out_cubic, fallback_source, layout, lerp_rect, neighbour


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_layout_fits_everything_without_overlap_and_keeps_aspect() -> None:
    for n in (1, 2, 3, 5, 8, 13):
        aspects = [16 / 10, 4 / 3, 1.0, 21 / 9][:1] * n
        rects = layout(aspects, 1600, 800)
        assert len(rects) == n
        for x, y, w, h in rects:
            assert x >= 0 and y >= 0 and x + w <= 1600 + 1e-6 and y + h + 30 <= 800 + 1e-6
            assert abs(w / h - aspects[0]) < 1e-6
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap_x = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                overlap_y = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                assert overlap_x <= 0 or overlap_y <= 0
    single = layout([1.6], 1600, 800)[0]
    assert single[3] <= 800 * 0.62 + 1e-6                     # one window is not blown up to fill the screen
    assert layout([], 100, 100) == [] and layout([1.0], 0, 0) == []


def test_motion_helpers_and_keyboard_neighbours() -> None:
    assert ease_out_cubic(0) == 0 and ease_out_cubic(1) == 1 and ease_out_cubic(0.5) > 0.5
    assert lerp_rect((0, 0, 10, 10), (100, 50, 20, 20), 0.5) == (50, 25, 15, 15)
    src = fallback_source((100, 100, 400, 250), 1920, 1080)
    assert src[1] > 900 and src[2] == 100                      # from the taskbar, small
    grid = [(0, 0, 10, 10), (20, 0, 10, 10), (0, 20, 10, 10), (20, 20, 10, 10)]
    assert neighbour(grid, 0, "right") == 1 and neighbour(grid, 0, "down") == 2
    assert neighbour(grid, 3, "left") == 2 and neighbour(grid, 3, "up") == 1 and neighbour(grid, 0, "left") == 0


class FakeThumb:
    made: list = []

    def __init__(self, dest: int, source: int) -> None:
        self.dest, self.source, self.places, self.released = dest, source, [], False
        self.ok = source != 99                                 # window 99 refuses a live picture
        FakeThumb.made.append(self)

    def place(self, x, y, w, h, opacity=1.0, visible=True):
        self.places.append((x, y, w, h, opacity))

    def release(self):
        self.released = True


def _wait(qapp, condition, seconds=2.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not condition():
        qapp.processEvents()
    return condition()


def _items():
    return [OverviewItem("1", "Inbox - Gmail", "Google Chrome", hwnd=11, source=(100, 100, 800, 500)),
            OverviewItem("2", "notes.txt", "Notepad", hwnd=12, source=(300, 200, 600, 400)),
            OverviewItem("3", "Minimised", "Spotify", hwnd=99, minimized=True)]


def test_overview_flies_in_picks_a_window_and_releases_the_live_pictures(qapp) -> None:
    from app.ui.window_overview import WindowOverview

    FakeThumb.made = []
    overview = WindowOverview(_items(), thumbnail_factory=FakeThumb)
    overview.resize(1600, 900)
    picked, closed = [], []
    overview.chosen.connect(picked.append)
    overview.closed.connect(lambda: closed.append(1))
    overview.open()
    first = overview.card_rect(0)
    assert _wait(qapp, lambda: overview._phase == "open")
    landed = overview.card_rect(0)
    assert (first.x(), first.width()) != (landed.x(), landed.width())          # it moved from its real place
    assert len(overview._thumbs) == 2                                          # live pictures for 11 and 12, not 99
    assert all(t.places for t in FakeThumb.made if t.ok)                        # and they follow the cards
    overview.pick(1)
    assert _wait(qapp, lambda: picked)
    assert picked[0].title == "notes.txt" and closed == [1]
    assert all(t.released for t in FakeThumb.made if t.ok)


def test_escape_and_background_click_close_without_picking(qapp) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent

    from app.ui.window_overview import WindowOverview

    overview = WindowOverview(_items(), mode="tabs")
    overview.resize(1600, 900)
    picked, closed = [], []
    overview.chosen.connect(picked.append)
    overview.closed.connect(lambda: closed.append(1))
    overview.open()
    assert _wait(qapp, lambda: overview._phase == "open")
    overview.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert overview.selected == 1
    overview.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(3, 3), QPointF(3, 3),
                                           Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert _wait(qapp, lambda: closed) and picked == []


@pytest.mark.parametrize("text, action", [
    ("show me my screen", "window_overview"), ("show my windows", "window_overview"), ("mission control", "window_overview"),
    ("show all my apps", "window_overview"), ("show my tabs", "tab_overview"), ("show me my open chrome tabs", "tab_overview"),
])
def test_voice_commands_open_the_overview(text, action) -> None:
    call = match_shortcut(text, lambda n: None)
    assert call is not None and call.name == "jarvis_control" and call.arguments == {"action": action}


@pytest.mark.parametrize("text, tool, target", [
    ("read this", "read_screen", "window"), ("can you see my screen", "describe_screen", "window"),
    ("what am I looking at", "describe_screen", "window"), ("look at my screen", "describe_screen", "screen"),
])
def test_screen_reading_phrases(text, tool, target) -> None:
    call = match_shortcut(text, lambda n: None)
    assert call is not None and call.name == tool and call.arguments["target"] == target


def test_show_me_my_desktop_still_steps_aside() -> None:
    assert match_shortcut("show me my desktop", lambda n: None).arguments == {"action": "hide_window"}


def test_window_opened_from_the_tray_comes_back_full_screen(tmp_path) -> None:
    """Started in the tray (START_MINIMIZED) and opened later: full screen like its own OS, no title bar."""
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from tests.test_gui import make_controller

    QApplication.instance() or QApplication([])
    controller, *_ = make_controller(Path(tmp_path))
    window = MainWindow(controller)
    assert window.wants_full_screen is True                   # START_FULLSCREEN defaults to on
    shown = []
    window.showFullScreen = lambda: shown.append("full")      # type: ignore[method-assign]
    window.showNormal = lambda: shown.append("normal")        # type: ignore[method-assign]
    window.bring_back()
    assert shown == ["full"]
    window.allow_close = True
    window.close()
