from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDoubleSpinBox
except (ImportError, OSError) as exc:  # pragma: no cover
    pytest.skip(f"Qt is not available here: {exc}", allow_module_level=True)

from app.config.settings import load_settings
from app.memory.database import Database
from app.memory.manager import MemoryManager
from app.ui.log_viewer import LogViewer, filter_records, read_log
from app.ui.memory_dialog import MemoryDialog
from app.ui.settings_dialog import TABS, SettingsDialog


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


# -------------------------------------------------------------------- settings


def test_every_setting_field_has_a_widget(qapp, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    dialog = SettingsDialog(load_settings(None, environ={}), env)
    names = {spec.name for _, specs in TABS for spec in specs}
    assert names == set(dialog.widgets)
    assert dialog.changes() == {}  # opening and saving without edits changes nothing


def test_settings_save_writes_only_changes_and_calls_back(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    notices: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: notices.append(text))
    env = tmp_path / ".env"
    env.write_text("# my notes\nAI_MODEL=\nTTS_SPEED=1.0\n", encoding="utf-8")
    applied: list = []
    restarts: list = []
    dialog = SettingsDialog(load_settings(env, environ={}), env, models=["llama3.2:3b", "qwen3:8b"],
                            microphones=["Microphone Array (Realtek(R) Audio)"],
                            on_saved=lambda s, changed: applied.append((s, changed)),
                            on_restart=lambda names: restarts.append(names))
    model = dialog.widgets["ai_model"]
    assert isinstance(model, QComboBox) and model.findText("qwen3:8b") >= 0
    model.setCurrentText("qwen3:8b")
    speed = dialog.widgets["tts_speed"]
    assert isinstance(speed, QDoubleSpinBox)
    speed.setValue(1.3)
    dialog.widgets["microphone_device"].setCurrentText("Microphone Array (Realtek(R) Audio)")
    dialog.widgets["speak_typed_replies"].setChecked(True)
    dialog.save()
    text = env.read_text()
    assert text.startswith("# my notes\n") and "AI_MODEL=qwen3:8b" in text and "TTS_SPEED=1.3" in text
    reloaded = load_settings(env, environ={})
    assert reloaded.microphone_device == "Microphone Array (Realtek(R) Audio)" and reloaded.speak_typed_replies
    settings, changed = applied[0]
    assert changed == {"ai_model", "tts_speed", "microphone_device", "speak_typed_replies"}
    assert notices == []                                            # no "restart the assistant" box any more
    assert restarts == [["MICROPHONE_DEVICE"]]                      # Jarvis restarts itself for that one; the rest is live


def test_remote_and_startup_settings_need_no_restart(qapp, tmp_path: Path) -> None:
    restarts: list = []
    env = tmp_path / ".env"
    env.write_text("REMOTE_ENABLED=false\nSTART_MINIMIZED=false\n", encoding="utf-8")
    dialog = SettingsDialog(load_settings(env, environ={}), env, on_saved=lambda s, c: None,
                            on_restart=lambda names: restarts.append(names))
    dialog.widgets["remote_enabled"].setChecked(True)
    dialog.widgets["start_minimized"].setChecked(True)
    dialog.save()
    assert restarts == [] and "REMOTE_ENABLED=true" in env.read_text()


def test_invalid_settings_are_not_saved(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    env = tmp_path / ".env"
    env.write_text("WAKE_WORD=hey jarvis\n", encoding="utf-8")
    dialog = SettingsDialog(load_settings(env, environ={}), env)
    dialog.widgets["wake_word"].setText("hey 123!!")
    dialog.save()
    assert warnings and "WAKE_WORD" in warnings[0]
    assert env.read_text() == "WAKE_WORD=hey jarvis\n" and dialog.saved is None


# ---------------------------------------------------------------------- memory


def test_memory_dialog_add_search_delete_clear(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    db = Database(tmp_path / "a.db")
    db.initialize()
    manager = MemoryManager(db)
    manager.remember("my project folder is D:\\Jarvis")
    dialog = MemoryDialog(manager, ask=lambda title, text: True)
    assert dialog.table.rowCount() == 1
    dialog.add("my favourite editor is VS Code")
    assert dialog.table.rowCount() == 2
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    dialog.add("my password is hunter2")
    assert warnings and "password manager" in warnings[0] and dialog.table.rowCount() == 2
    dialog.search.setText("editor")
    assert dialog.table.rowCount() == 1
    dialog.search.setText("")
    dialog.table.selectRow(0)
    dialog.delete_selected()
    assert dialog.table.rowCount() == 1
    dialog.clear_all()
    assert dialog.table.rowCount() == 0 and manager.all() == []


# ------------------------------------------------------------------------ logs


def test_log_reading_and_filtering(qapp, tmp_path: Path) -> None:
    log = tmp_path / "jarvis.log"
    lines = [
        {"ts": "2026-09-23T08:00:00.000+00:00", "level": "INFO", "logger": "jarvis.tools", "msg": "tool_call",
         "fields": {"tool": "open_application", "event": "tool_call"}},
        {"ts": "2026-09-23T08:00:01.000+00:00", "level": "ERROR", "logger": "jarvis.ai", "msg": "chat_failed",
         "fields": {"error": "timeout"}},
        {"ts": "2026-09-23T08:00:02.000+00:00", "level": "DEBUG", "logger": "jarvis.x", "msg": "noise"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in lines) + "\nnot json at all\n", encoding="utf-8")
    records = read_log(log)
    assert [r["msg"] for r in records] == ["noise", "chat_failed", "tool_call"]  # newest first, bad line skipped
    assert [r["msg"] for r in filter_records(records, "INFO")] == ["chat_failed", "tool_call"]
    assert [r["msg"] for r in filter_records(records, "DEBUG", "open_application")] == ["tool_call"]
    viewer = LogViewer(log)
    assert viewer.table.rowCount() == 2
    viewer.level.setCurrentText("ERROR")
    assert viewer.table.rowCount() == 1 and viewer.table.item(0, 3).text() == "chat_failed"
    assert read_log(tmp_path / "missing.log") == []


# ----------------------------------------------------------------- automations


def test_automation_dialog_pause_resume_delete(qapp, tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    from app.automation.tasks import AutomationStore, Step
    from app.automation.triggers import DailyTrigger
    from app.ui.automation_dialog import AutomationDialog

    database = Database(tmp_path / "a.db")
    database.initialize()
    store = AutomationStore(database)
    trigger = DailyTrigger(time="08:00", days=[0, 1, 2, 3, 4])
    store.save("work apps", trigger, [Step(type="notify", message="hi")], datetime.now().astimezone() + timedelta(hours=1))
    answers: list[bool] = [False, True]
    dialog = AutomationDialog(store, ask=lambda title, text: answers.pop(0))
    assert dialog.table.rowCount() == 1 and dialog.table.item(0, 0).text() == "work apps"
    dialog.table.selectRow(0)
    dialog.toggle_selected()
    assert not store.find("work apps").enabled and "(paused)" in dialog.table.item(0, 0).text()
    dialog.table.selectRow(0)
    dialog.toggle_selected()
    assert store.find("work apps").enabled and store.find("work apps").next_run_at
    dialog.table.selectRow(0)
    dialog.delete_selected()  # declined
    assert dialog.table.rowCount() == 1
    dialog.table.selectRow(0)
    dialog.delete_selected()
    assert dialog.table.rowCount() == 0 and store.all() == []
    dialog.run_selected()  # nothing selected, no controller: must not raise


def test_screen_settings_are_live(qapp, tmp_path: Path) -> None:
    from app.ui.settings_dialog import LIVE_FIELDS

    dialog = SettingsDialog(load_settings(None, environ={}), tmp_path / ".env", models=["qwen2.5vl:3b"])
    assert {"screen_capture_enabled", "vision_model", "ocr_engine"} <= LIVE_FIELDS
    assert isinstance(dialog.widgets["screen_capture_enabled"], QCheckBox)
    assert not dialog.widgets["screen_capture_enabled"].isChecked()  # off by default
    assert dialog.widgets["vision_model"].findText("qwen2.5vl:3b") >= 0
