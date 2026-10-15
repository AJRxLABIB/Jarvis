"""3.9.0 Phase 6: event bus, notification center, modes, emergency controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.core import emergency, events, modes, notifications
from app.memory.database import Database
from app.memory.settings_store import SettingsStore
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "j.db")
    database.initialize()
    return database


def test_events_are_logged_redacted_and_isolated(db) -> None:
    events.BUS.database = db
    seen = []
    events.BUS.subscribe("*", lambda e: 1 / 0)  # a broken subscriber
    events.BUS.subscribe(events.REMINDER, seen.append)
    events.BUS.publish(events.REMINDER, title="Call", message="password is hunter2 x")
    assert seen and seen[0].data["title"] == "Call"
    stored = events.BUS.recent(1)[0]
    assert stored["name"] == "Reminder" and "hunter2" not in stored["data"]
    with pytest.raises(ValueError):
        events.BUS.publish("Typo")


def test_notifications_come_from_events(db) -> None:
    center = notifications.NotificationCenter(db)
    news = []
    events.BUS.database = db
    notifications.connect(events.BUS, center, on_new=lambda *a: news.append(a))
    events.BUS.publish(events.MISSION_PAUSED, title="prepare", note="Paused at step 2")
    events.BUS.publish(events.RUN_FINISHED, outcome="completed")  # not everything becomes a notification
    events.BUS.publish(events.BATTERY_LOW, message="Battery 5%", critical=True)
    items = center.items()
    assert [n.title for n in items] == ["Battery low", "Mission paused"]  # urgent first
    assert items[0].priority == "urgent" and items[1].action == "continue my mission" and len(news) == 2
    assert center.unread_count() == 2
    center.mark_read([items[0].id])
    assert center.unread_count() == 1
    center.archive()
    assert center.items() == [] and len(center.items(include_archived=True)) == 2


def test_modes_only_make_jarvis_quieter(db) -> None:
    state = modes.ModeState(SettingsStore(db))
    state.set(modes.resolve("gaming"))
    assert state.speaks("urgent") and not state.speaks("high")
    assert modes.ModeState(SettingsStore(db)).current.name == "gaming"  # survives a restart
    assert modes.resolve("incognito").name == "private" and modes.resolve("nonsense") is None
    presentation = modes.MODES["presentation"]
    assert not presentation.speak and not presentation.capture


def executor(db):
    ctx = ToolContext(Settings(), database=db)
    return ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx,
                        confirmer=ScriptedConfirmer(True))


def go(ex, tool_name, user_command=False, **arguments):
    return asyncio.run(ex.execute(ToolCall(tool_name, arguments), user_command=user_command)).result


def test_private_and_presentation_modes_refuse_screen_capture(db) -> None:
    ex = executor(db)
    modes.STATE.set(modes.MODES["private"])
    result = go(ex, "read_screen")
    assert not result.ok and "Private mode: no screen capture" in result.message


def test_lock_allows_only_read_only_tools(db) -> None:
    ex = executor(db)
    emergency.STATE.set("locked", True, by_voice=True)
    assert go(ex, "get_datetime").ok
    blocked = go(ex, "save_contact", name="Rafi", number="01712345678", user_command=True)
    assert not blocked.ok and "locked" in blocked.message


def test_network_switch_blocks_egress(db) -> None:
    ex = executor(db)
    emergency.STATE.set("network_disabled", True, by_voice=True)
    result = go(ex, "open_website", site="github.com", user_command=True)
    assert not result.ok and "Network actions are switched off" in result.message


def test_voice_can_switch_off_but_never_back_on(db) -> None:
    state = emergency.Emergency()
    state.load(SettingsStore(db))
    assert state.set("remote_disabled", True, by_voice=True)
    assert not state.set("remote_disabled", False, by_voice=True) and state.flags["remote_disabled"]
    assert state.set("remote_disabled", False, by_voice=False) and not state.flags["remote_disabled"]
    again = emergency.Emergency()
    state.set("automation_disabled", True, by_voice=True)
    again.load(SettingsStore(db))
    assert again.flags["automation_disabled"]  # survives a restart


def test_automations_stop_when_switched_off(db) -> None:
    from app.automation.scheduler import Scheduler
    from app.automation.tasks import AutomationStore

    scheduler = Scheduler(AutomationStore(db), executor(db), notify=lambda *a: None)
    emergency.STATE.set("automation_disabled", True, by_voice=True)
    assert asyncio.run(scheduler.tick()) == []


def test_remote_refuses_everything_when_switched_off() -> None:
    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer

    pairing = PairingStore(None, encrypt=False)
    device, key = pairing.pair(pairing.new_code(), "p")
    server = RemoteServer(RemoteHub(lambda t: None, lambda a, b: True, lambda: None, lambda: {}), pairing, 0)
    request = SimpleNamespace(remote="192.168.1.5", headers={"X-Device": device, "X-Key": key})
    assert server._allowed(request) == (True, 200)
    emergency.STATE.set("remote_disabled", True, by_voice=True)
    assert server._allowed(request) == (False, 503)


def test_controller_wiring(tmp_path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    assert controller.notifications is not None
    assert "Gaming mode" in controller._mode_control("gaming") and modes.STATE.current.name == "gaming"
    spoken = []
    controller.tts.speak = spoken.append
    controller.speak_up("Disk almost full", "normal", events.PC_PROBLEM, fix="clean temp files")
    assert spoken == [] and controller.notifications.items()[0].action == "clean temp files"  # gaming: not spoken
    message = controller.emergency_stop()
    assert "Emergency stop" in message and emergency.STATE.flags["automation_disabled"]
    assert "Only the window" in controller._emergency_control("lock") and emergency.STATE.flags["locked"]
    controller.set_emergency_switch("locked", False)
    assert not emergency.STATE.flags["locked"]
    titles = [n.title for n in controller.notifications.items()]
    assert "Emergency controls" in titles


def test_notification_window_runs_only_jarvis_actions(db) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.notification_dialog import EmergencyDialog, NotificationDialog

    center = notifications.NotificationCenter(db)
    center.add("PC watcher", "Disk almost full", source="PcProblem", category="alert", action="clean temp files")
    ran = []
    dialog = NotificationDialog(center, run_action=ran.append)
    dialog.list.setCurrentRow(0)
    assert dialog.do_action() == "clean temp files" and ran == ["clean temp files"]
    switched = []
    EmergencyDialog(lambda: None, lambda s, on: switched.append((s, on))).boxes["locked"].setChecked(True)
    assert switched == [("locked", True)]


@pytest.mark.parametrize("said, tool, arguments", [
    ("emergency stop", "emergency_control", {"action": "stop"}),
    ("stop everything", "emergency_control", {"action": "stop"}),
    ("lock yourself", "emergency_control", {"action": "lock"}),
    ("disable network actions", "emergency_control", {"action": "disable_network"}),
    ("any notifications", "notifications", {"action": "list"}),
    ("what did I miss", "notifications", {"action": "list"}),
    ("clear my notifications", "notifications", {"action": "clear"}),
    ("gaming mode", "set_mode", {"mode": "gaming"}),
    ("switch to private mode", "set_mode", {"mode": "private"}),
    ("what mode are you in", "set_mode", {"mode": "status"}),
])
def test_commands(said, tool, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)
