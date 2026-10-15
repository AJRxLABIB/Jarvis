"""3.1.2: animations never pause - no slow work on the window's thread, a clock that survives a
broken widget, even frame timing."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest


def test_stats_are_measured_off_the_window_thread() -> None:
    from app.ui.stats_sampler import busiest_apps, take

    sample = take(os.path.abspath(os.sep), with_processes=True)
    assert sample.memory is not None and sample.cores >= 1 and sample.processes is not None
    assert all(len(item) == 3 for item in sample.processes)
    assert take(os.path.abspath(os.sep), with_processes=False).processes is None
    assert isinstance(busiest_apps(1), list)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    return QApplication.instance() or QApplication([])


def test_one_broken_animation_does_not_stop_the_others(qapp) -> None:
    from app.ui.motion import Clock

    clock = Clock()
    ticks = []
    clock._subscribers = [lambda now: 1 / 0, lambda now: ticks.append(now) or True]
    clock._tick()
    assert len(ticks) == 1 and len(clock._subscribers) == 1  # the broken one is dropped, the rest go on


def test_animation_timers_are_precise(qapp) -> None:
    from PySide6.QtCore import Qt

    from app.ui import motion
    from app.ui.visualizer import OrbWidget

    orb = OrbWidget()
    assert orb._timer.timerType() == Qt.TimerType.PreciseTimer
    motion.CLOCK.join(lambda now: False)
    assert motion.CLOCK._timer.timerType() == Qt.TimerType.PreciseTimer


def test_core_gauge_can_tick_before_its_first_value(qapp) -> None:
    from app.ui.dashboard import CoreGauge

    gauge = CoreGauge()
    gauge._tick(time.monotonic())  # used to raise: no value yet


def test_unchanged_rows_are_not_restyled(qapp) -> None:
    from app.ui.dashboard import StatusRow

    row = StatusRow("CPU")
    styled = []
    original = row.value.setStyleSheet
    row.value.setStyleSheet = lambda css: styled.append(css) or original(css)
    row.set("12%", "ok")
    row.set("12%", "ok")
    row.set("13%", "ok")
    assert len(styled) == 2


def test_window_applies_a_sample_from_a_worker(qapp, tmp_path) -> None:
    from app.ui.main_window import MainWindow
    from app.ui.stats_sampler import take
    from tests.test_gui import make_controller, wait_until

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller, 3.0)
    got = []
    window.stats_ready.connect(lambda s: got.append(threading.current_thread() is threading.main_thread()))
    window._stats_worker = None
    window._tick_stats()
    assert wait_until(qapp, lambda: got, 10)
    assert got[0] is True  # applied on the window's thread, measured on another
    window._apply_stats(take(os.path.abspath(os.sep), with_processes=True))
    assert window.cpu_label.text()
    window.close()


def test_a_slow_measurement_is_never_waited_for(qapp, tmp_path) -> None:
    from app.ui.main_window import MainWindow
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller, 3.0)
    busy = threading.Event()
    window._stats_worker = threading.Thread(target=busy.wait, daemon=True)
    window._stats_worker.start()
    started = time.perf_counter()
    window._tick_stats()  # the last one is still running: this tick is skipped, not queued
    assert time.perf_counter() - started < 0.2
    busy.set()
    window.close()
    assert SimpleNamespace  # (keeps the import list honest)
