"""The command-center view: its ten states, timeline controls, device honesty, security overview, automation gating,
contrast of both themes, and a real (offscreen) build of the whole view with an accessibility audit."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import a11y
from app.memory.database import Database
from app.ui.command_center import approval, automations, devices, security, states, style, timeline


# ------------------------------------------------------------------ states


def test_ten_distinct_states_that_do_not_rely_on_colour():
    assert len(states.STATES) == 10
    glyphs = [s.glyph for s in states.STATES.values()]
    labels = [s.label for s in states.STATES.values()]
    assert len(set(glyphs)) == 10 and len(set(labels)) == 10                       # shape AND word differ
    assert all(s.hint and s.accessible.startswith("Status: ") for s in states.STATES.values())


def test_state_priority():
    r = states.resolve
    assert r("IDLE").key == "idle" and r("LISTENING").key == "listening" and r("THINKING").key == "thinking"
    assert r("EXECUTING").key == "executing" and r("SPEAKING").key == "executing"
    assert r("WAITING_FOR_PERMISSION").key == "waiting" and r("ERROR").key == "failed"
    assert r("EXECUTING", verifying=True).key == "verifying"
    assert r("IDLE", ai_online=False).key == "offline" and r("THINKING", ai_online=False).key == "thinking"
    assert r("IDLE", outcome="completed").key == "completed" and r("IDLE", outcome="failed").key == "failed"
    assert r("EXECUTING", emergency=True, ai_online=False).key == "emergency"          # nothing outranks it
    assert r("WAITING_FOR_PERMISSION", verifying=True).key == "waiting"


def test_verifying_flag_is_real():
    from app.core import activity

    assert not activity.is_verifying()
    with activity.verifying():
        assert activity.is_verifying()
    assert not activity.is_verifying()


# ------------------------------------------------------------------ contrast


@pytest.mark.parametrize("theme", ["dark", "light", "forest"])
def test_both_themes_meet_contrast(theme):
    p = style.palette(theme)
    bad = [(a, b, round(a11y.contrast(p[a], p[b]), 2)) for a, b in style.CONTRAST_PAIRS if a11y.contrast(p[a], p[b]) < 4.5]
    assert not bad, bad


def test_text_scale_is_clamped():
    assert style.scaled(10, 5) == 16.0 and style.scaled(10, 0.1) == 8.0
    assert "font-size:12.5pt" in style.stylesheet("dark", 1.25)


# ------------------------------------------------------------------ timeline


def _step(n, state, tool="open_application", inverse=None, result="", checkpoint=False, **arguments):
    from app.core.missions import Step

    return Step(n, f"step {n}", tool, dict(arguments), state, result=result, inverse=inverse, checkpoint=checkpoint)


def _mission(state, steps):
    from app.core.missions import Mission

    return Mission("m1", "Prepare the demo", state, steps)


def test_timeline_rows_targets_and_errors():
    m = _mission("paused", [_step(1, "done", inverse={"tool": "x"}), _step(2, "failed", tool="phone_control", phone="Pixel",
                                                                          result="the phone said no\nmore"),
                            _step(3, "pending", checkpoint=True)])
    tl = timeline.build(m)
    assert [r.label for r in tl.rows] == ["Done", "Failed", "Waiting"]
    assert tl.rows[1].target == "phone Pixel" and tl.rows[0].target == "this PC"
    assert tl.rows[1].detail == "the phone said no" and "✖" in tl.rows[1].text and "go-ahead" in tl.rows[2].text
    assert tl.current == 2 and tl.progress == "1/3"


def test_controls_only_when_supported():
    running = timeline.build(_mission("running", [_step(1, "done", inverse={"tool": "x"}), _step(2, "running")]))
    assert running.controls.cancel and running.controls.pause and not running.controls.retry and not running.controls.undo
    paused = timeline.build(_mission("paused", [_step(1, "done", inverse={"tool": "x"}), _step(2, "pending")]))
    assert paused.controls.retry and paused.controls.cancel and paused.controls.undo and paused.controls.review
    done = timeline.build(_mission("completed", [_step(1, "done")]))
    assert not (done.controls.cancel or done.controls.retry or done.controls.undo or done.controls.pause)   # no undo info
    undoable = timeline.build(_mission("completed", [_step(1, "done", inverse={"tool": "x"})]))
    assert undoable.controls.undo and not undoable.controls.cancel
    assert not timeline.build(_mission("rolled_back", [_step(1, "done", inverse={"tool": "x"})])).controls.undo


def test_current_timeline_from_the_database(tmp_path: Path):
    from app.core.missions import MissionStore

    db = Database(tmp_path / "a.db")
    db.initialize()
    assert timeline.current_timeline(db) is None and timeline.current_timeline(None) is None
    store = MissionStore(db)
    store.create("Tidy up", [_step(1, "pending"), _step(2, "pending")])
    tl = timeline.current_timeline(db)
    assert tl.title == "Tidy up" and tl.controls.cancel and not tl.controls.retry


# ------------------------------------------------------------------ approval


def test_approval_shows_action_target_and_risk():
    from app.security.confirmations import ConfirmationRequest
    from app.security.permissions import RiskLevel

    view = approval.describe(ConfirmationRequest("phone_control", RiskLevel.DANGEROUS, "Call 01700 from your phone?",
                                                 {"phone": "Pixel", "number": "01700"}))
    text = "\n".join(view.lines())
    assert "Action: Call 01700" in text and "Target: phone Pixel" in text and "Risk: Sensitive" in text
    pc = approval.describe(ConfirmationRequest("delete_item", RiskLevel.MODERATE, "Delete report.pdf?", {"path": "C:/x/report.pdf"}))
    assert pc.target == "this PC" and ("path", "C:/x/report.pdf") in pc.details


# ------------------------------------------------------------------ devices


def _phone(trust="trusted", permissions=("read", "chat", "control")):
    return SimpleNamespace(id="p1", name="Pixel", platform="android", trust=trust, permissions=list(permissions),
                           last_seen=None, app_version="1.4.0")


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_phone_that_never_checked_in_shows_unknowns_not_zeros():
    card = devices.phone_card(_phone(), {}, now=NOW)
    assert card.connection == "offline" and card.battery is None and "never" in card.connection_text
    control = next(c for c in card.capabilities if c.name.startswith("Control"))
    assert control.status == "unknown"
    assert "battery" not in card.headline


def test_phone_capabilities_need_both_sides():
    seen = (NOW - timedelta(seconds=20)).isoformat()
    state = {"seen": seen, "battery": 61, "charging": True, "control": True, "comms": False, "lock": False,
             "notifications": True, "app_version": "1.4.0"}
    card = devices.phone_card(_phone(), state, now=NOW)
    by = {c.name: c for c in card.capabilities}
    assert card.connection == "connected" and card.battery == 61 and "charging" in card.headline
    assert by["Status and battery"].status == "available"
    assert by["Control (flashlight, ring, open apps, lock)"].status == "available"
    assert by["Calls and messages"].status == "needs action" and "Allow calls" in by["Calls and messages"].why
    assert by["Lock the screen"].status == "needs action" and "device admin" in by["Lock the screen"].why
    assert by["Screen view"].status == "needs action"                     # no 'screen' permission on the PC
    off = devices.phone_card(_phone(permissions=("read",)), state, now=NOW)
    assert "Calls and messages" not in {c.name for c in off.capabilities}          # not offered at all
    assert any("Restricted" in w for w in off.warnings)


def test_connection_ageing_and_pending():
    old = {"seen": (NOW - timedelta(minutes=10)).isoformat()}
    assert devices.phone_card(_phone(), old, now=NOW).connection == "recent"
    assert devices.phone_card(_phone(), {"seen": (NOW - timedelta(hours=5)).isoformat()}, now=NOW).connection == "offline"
    pending = devices.phone_card(_phone("pending"), {}, now=NOW)
    assert pending.connection == "pending" and pending.capabilities == []


# ------------------------------------------------------------------ security


def test_security_overview_has_the_required_sections(tmp_path: Path):
    from app.config.settings import Settings

    db = Database(tmp_path / "a.db")
    db.initialize()
    o = security.collect(db, Settings(), {"mic_muted": False, "hands_free": True, "screen_capture": False})
    titles = [s.title.lower() for s in o.sections]
    for part in ("right now", "your data", "activity log", "guards", "standing", "blocked", "phones", "backups", "sensitive"):
        assert any(t.startswith(part) for t in titles), part
    assert "microphone" in " ".join(i.text for i in o.section("right now").items)
    assert o.headline and not o.emergency
    assert security.collect(None, Settings()).warnings == 1


def test_emergency_switch_changes_the_headline(tmp_path: Path):
    from app.config.settings import Settings
    from app.core import emergency

    db = Database(tmp_path / "a.db")
    db.initialize()
    before = dict(emergency.STATE.flags)
    try:
        emergency.STATE.flags["locked"] = True
        assert "ACTIVE" in security.collect(db, Settings()).headline
    finally:
        emergency.STATE.flags.update(before)


# ------------------------------------------------------------------ automations


@pytest.fixture()
def engine(tmp_path: Path):
    from app.automation.tasks import AutomationStore
    from app.security.permissions import PermissionEngine, PermissionsConfig
    from app.config.settings import Settings
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry

    db = Database(tmp_path / "a.db")
    db.initialize()
    registry = build_default_registry()
    ctx = ToolContext(Settings(), database=db)
    return SimpleNamespace(store=AutomationStore(db), registry=registry, ctx=ctx,
                           permissions=PermissionEngine(PermissionsConfig()))


def _save(engine, name, steps, trigger=None):
    from app.automation.tasks import parse_steps
    from app.automation.triggers import ManualTrigger

    trigger = trigger or ManualTrigger()
    task, _ = engine.store.save(name, trigger, parse_steps(steps), None)
    return task


def test_inspection_lists_trigger_conditions_actions_approvals_and_failure(engine):
    task = _save(engine, "morning", [{"tool": "get_datetime"}, {"tool": "delete_item", "arguments": {"path": "x.txt"},
                                                                  "when": "weekday", "continue_on_error": True}])
    info = automations.inspect(task, engine.registry, engine.ctx, engine.permissions, now=datetime(2026, 10, 26, 9, 0).astimezone())
    assert info.trigger and info.kind == "manual" and len(info.actions) == 2
    assert any("only on weekdays" in c for c in info.conditions)
    assert info.actions[1].changes_state and info.actions[1].on_error == "keeps going" and info.actions[0].on_error == "stops here"
    assert any("asks you" in a for a in info.approvals)
    assert any("changes things" in w for w in info.warnings) and "keep going" in info.failure


def test_private_data_and_event_warnings(engine):
    from app.automation.triggers import EventTrigger

    task = _save(engine, "mail check", [{"tool": "read_emails"}], EventTrigger(event="wifi", value="Home"))
    info = automations.inspect(task, engine.registry, engine.ctx, engine.permissions)
    assert any("private data" in w for w in info.warnings) and any("starts on an event" in w for w in info.warnings)


def test_enable_is_refused_with_problems_and_needs_a_tick_for_warnings(engine):
    safe = _save(engine, "just time", [{"tool": "get_datetime"}])
    info = automations.inspect(safe, engine.registry, engine.ctx, engine.permissions)
    assert not info.warnings and info.can_enable
    assert automations.enable(engine.store, safe, info, acknowledged=False)[0]
    risky = _save(engine, "cleanup", [{"tool": "delete_item", "arguments": {"path": "x.txt"}}])
    engine.store.set_enabled(risky.id, False, None)
    info = automations.inspect(engine.store.get(risky.id), engine.registry, engine.ctx, engine.permissions)
    ok, message = automations.enable(engine.store, risky, info, acknowledged=False)
    assert not ok and "tick" in message and not engine.store.get(risky.id).enabled
    assert automations.enable(engine.store, risky, info, acknowledged=True)[0] and engine.store.get(risky.id).enabled
    broken = automations.Inspection("x", False, "t", "manual", problems=["step 1: there is no tool named 'gone'"])
    assert not automations.enable(engine.store, risky, broken, acknowledged=True)[0] and not broken.can_enable
    assert "off" in automations.disable(engine.store, risky) and not engine.store.get(risky.id).enabled


def test_test_text_never_runs_anything(engine):
    task = _save(engine, "show", [{"tool": "get_datetime"}, {"type": "notify", "message": "hello"}])
    text = automations.test_text(task, engine.registry, engine.ctx, engine.permissions)
    assert text.startswith("Test (nothing was run)") and "get_datetime" in text and "hello" in text
    assert "SIMULATED" in automations.simulate_text(task, "tool_fails", engine.registry)


# ------------------------------------------------------------------ the whole view (offscreen)


@pytest.fixture()
def view(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from app.agent.tool_manager import ToolExecutor
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine, PermissionsConfig
    from app.tools.base import ToolContext
    from app.tools.catalog import build_default_registry
    from app.ui.command_center.view import CommandCenterView

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "a.db")
    db.initialize()

    class Ctrl(QObject):
        user_message = Signal(str)
        assistant_started = Signal()
        assistant_delta = Signal(str)
        assistant_finished = Signal(object)
        tool_event = Signal(str)
        notice = Signal(str)
        state_changed = Signal(str)
        status_changed = Signal(object)
        confirmation_requested = Signal(object)
        confirmation_answered = Signal(object, bool)
        mic_muted_changed = Signal(bool)
        hands_free_state = Signal(str)
        state, mic_muted, hands_free = "IDLE", False, None

        def __init__(self):
            super().__init__()
            self.ctx = SimpleNamespace(settings=Settings(), database=db, paths=None)
            self.executor = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()),
                                         ToolContext(Settings(), database=db))
            self.sent, self.settings_applied, self.stopped = [], [], 0

        def spoken_wave(self): return None
        def toggle_listening(self): pass
        def cancel(self): self.stopped += 1
        def send_text(self, text, **kw):
            self.sent.append(text)
            return True
        def mobile_registry(self): return None
        def emergency_stop(self): return "Stopped: speaking."
        def live_privacy_state(self): return {}
        def _apply_setting(self, name, value): self.settings_applied.append((name, value))

    controller = Ctrl()
    v = CommandCenterView(controller, None)
    v.resize(1400, 860)
    v.show()
    v.controller = controller
    yield v
    v.close()


def test_every_section_has_a_page_and_the_rail_is_complete(view):
    from app.ui.command_center.widgets import SECTIONS

    keys = [k for k, _, _ in SECTIONS]
    for required in ("chat", "dashboard", "tasks", "devices", "memory", "knowledge", "automations", "developer", "settings"):
        assert required in keys
    for key in keys:
        view.show_section(key)
        assert view.pages.currentIndex() == view.page_index[key] and view.rail.current() == key
        assert view.rail.buttons[key].toolTip() and view.rail.buttons[key].accessibleName()


def test_command_bar_sends_and_status_shows_state_as_shape_and_word(view):
    view.command.input.setText("open chrome")
    view.command.send.click()
    assert view.controller.sent == ["open chrome"] and view.command.input.text() == ""
    view.controller.state = "EXECUTING"
    view.tick()
    assert "▶" in view.status.pill.text() and "Executing" in view.status.pill.text()
    assert view.command.stop.isEnabled() and "Executing" in view.command.status.text()
    view.controller.state = "IDLE"
    view._ai_ok = False
    view.tick()
    assert "Offline" in view.status.pill.text() and not view.command.stop.isEnabled()


def test_unmeasured_metrics_show_a_dash_not_zero(view):
    view.on_stats(None, 41.0, None)
    assert view.status.labels["cpu"].text() == "CPU -" and view.status.labels["ram"].text() == "RAM 41%"
    assert view.status.labels["gpu"].text() == "GPU -"
    view.on_stats(12.0, 41.0, "30% · 2.1/6 GB")
    assert view.status.labels["gpu"].text().startswith("GPU 30%")


def test_emergency_stop_button_and_state(view):
    from app.core import emergency

    view.show_section("security")
    view.security.stop_button.click()
    assert any("Stopped: speaking." in text for _, text in view.chat.messages)
    before = dict(emergency.STATE.flags)
    try:
        emergency.STATE.flags["locked"] = True
        view.tick()
        assert "Emergency" in view.status.pill.text()
        view.security.refresh()
        assert "ACTIVE" in view.security.headline.text()
    finally:
        emergency.STATE.flags.update(before)


def test_settings_page_applies_theme_text_size_and_motion(view):
    page = view.settings_page
    page.theme.setCurrentText("light")
    page.size.setCurrentIndex(3)
    page.motion.setChecked(True)
    assert ("ui_theme", "light") in view.controller.settings_applied
    assert ("ui_text_scale", "1.5") in view.controller.settings_applied and ("reduced_motion", "true") in view.controller.settings_applied
    from app.config.settings import Settings

    view.controller.ctx.settings = Settings(ui_theme="light", ui_text_scale=1.5)
    view.apply_style()
    assert style.palette("light")["bg"] in view.styleSheet() and "font-size:15.0pt" in view.styleSheet()


def test_view_is_accessible_and_keyboard_reachable(view):
    from app.ui.command_center.widgets import SECTIONS

    findings = []
    for key, _, _ in SECTIONS:
        view.show_section(key)
        findings += [f for f in a11y.audit_widgets(view) if f.level == "fail"]
    assert not findings, a11y.report(findings)
    assert view.command.input.accessibleName() and view.status.accessibleName() and view.context.accessibleName()


def test_automation_page_end_to_end(view, engine=None):
    from app.automation.tasks import AutomationStore, parse_steps
    from app.automation.triggers import ManualTrigger

    AutomationStore(view.controller.ctx.database).save("demo", ManualTrigger(), parse_steps([{"tool": "get_datetime"}]), None)
    view.show_section("automations")
    page = view.automations
    assert page.listing.count() == 1 and "TRIGGER" in page.view.toPlainText() and "ACTIONS" in page.view.toPlainText()
    assert "nothing was run" in page.test()
    assert "SIMULATED" in page.simulate()
    assert page.enable()[0]


def test_task_panel_shows_timeline_and_routes_controls(view):
    from app.core.missions import MissionStore

    MissionStore(view.controller.ctx.database).create("Tidy up", [_step(1, "pending"), _step(2, "pending")])
    view.tick()
    task = view.context.task
    assert task.steps.count() == 2 and "Tidy up" in task.title.text()
    assert not task.buttons["cancel"].isHidden() and task.buttons["retry"].isHidden() and task.buttons["undo"].isHidden()
    task.buttons["cancel"].click()
    assert view.controller.sent[-1] == "cancel the mission"


def test_main_window_opens_the_command_center_beside_the_hud(tmp_path: Path):
    """The real window: Ctrl+Shift+C view is built on first use, replays the chat, hides the HUD's bottom bar, and the
    core view comes back untouched."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from tests.test_gui import QApplication, MainWindow, make_controller, wait_until

    qapp = QApplication.instance() or QApplication([])
    controller, _, _ = make_controller(tmp_path, replies=[{"role": "assistant", "content": "Hi, Labib."}])
    window = MainWindow(controller)
    try:
        window.show()
        assert window.command_center is None and window.pages.count() == 2         # nothing built until asked for
        window.chat.add_message("notice", "earlier line")
        window.show_command_center("devices")
        cc = window.command_center
        assert cc is not None and window.pages.currentWidget() is cc and cc.rail.current() == "devices"
        assert any(text == "earlier line" for _, text in cc.chat.messages)         # the conversation was replayed
        assert not window.input.isVisibleTo(window) and cc.command.input.isVisibleTo(window)
        cc.show_section("chat")
        cc.command.input.setText("hello")
        cc.command.send.click()
        assert wait_until(qapp, lambda: any(t == "Hi, Labib." for r, t in cc.chat.messages))
        assert any(t == "Hi, Labib." for r, t in window.chat.messages)             # both views show the same conversation
        window.show_page(0)
        assert window.pages.currentIndex() == 0 and window.input.isVisibleTo(window)       # the core view is back
        window.show_command_center()
        assert window.command_center is cc                                         # reused, not rebuilt
    finally:
        controller.shutdown()


# ------------------------------------------------------------------ knowledge-graph browser


def _graph():
    from app.core.knowledge_graph import Graph, Node

    g = Graph()
    for n in (Node("project:tms", "project", "TMS", {"folder": "D:/tms"}, "a project you told me about"),
              Node("tech:laravel", "technology", "Laravel", {}, "the project's files"),
              Node("tech:mysql", "technology", "MySQL"),
              Node("memory:1", "decision", "We chose MySQL because of hosting")):
        g.add(n)
    g.link("project:tms", "uses", "tech:laravel", "composer.json")
    g.link("project:tms", "uses", "tech:mysql", "env")
    g.link("memory:1", "about", "project:tms", "memory #1")
    return g


def test_graph_view_lists_filters_and_explains_connections():
    from app.ui.command_center import graph_view as gv

    g = _graph()
    rows, total = gv.rows(g)
    assert total == 4 and rows[0].name == "TMS" and rows[0].links == 3        # most connected first
    only, _ = gv.rows(g, kind="technology", query="lara")
    assert [r.name for r in only] == ["Laravel"]
    assert ("technology", "Technologies", 2) in gv.kinds(g)
    text = gv.details(g, "project:tms")
    assert "Connections (3)" in text and "uses technology: Laravel" in text and "[composer.json]" in text
    assert "<- about decision" in text and "Source: a project you told me about" in text
    assert gv.details(g, "nope").startswith("Select an item")


def test_graph_view_caps_the_list():
    from app.core.knowledge_graph import Graph, Node
    from app.ui.command_center import graph_view as gv

    g = Graph()
    for i in range(gv.LIMIT + 50):
        g.add(Node(f"n{i}", "file", f"f{i}"))
    rows, total = gv.rows(g)
    assert len(rows) == gv.LIMIT and total == gv.LIMIT + 50


def test_knowledge_page_builds_in_the_background_and_shows_details(qtbot=None):
    from PySide6.QtWidgets import QApplication

    from app.ui.command_center.pages import KnowledgePage

    app = QApplication.instance() or QApplication([])
    page = KnowledgePage(_graph, lambda: "docs\n\n  pdf: 2 file(s)/item(s)\n  note: 1", lambda q: "answer: " + q)
    page.refresh()
    for _ in range(200):
        app.processEvents()
        if page.graph is not None:
            break
        import time

        time.sleep(0.01)
    assert page.graph is not None and page.listing.count() == 4
    assert page.kind.count() == 4                       # All + project/technology/decision
    page.listing.setCurrentRow(0)
    assert "Connections (3)" in page.detail.toPlainText()
    page.search.setText("mysql")
    assert page.listing.count() == 2
    page.ask_box.setText("which files are related to TMS")
    page.ask_box.returnPressed.emit()
    app.processEvents()
    assert "answer: which files" in page.answer.toPlainText()
    assert a11y.audit_widgets(page) == []
