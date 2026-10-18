from __future__ import annotations

from pathlib import Path

import pytest

from app.core import a11y

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_contrast_arithmetic_matches_the_wcag_reference_values() -> None:
    assert a11y.contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert a11y.contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.02)      # the classic just-fails example
    assert a11y.contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


def test_every_theme_colour_pair_meets_wcag() -> None:
    assert a11y.audit_theme() == [], a11y.report(a11y.audit_theme())


def test_a_bad_colour_pair_is_reported(monkeypatch) -> None:
    from app.ui.styles import theme

    monkeypatch.setattr(theme, "DIM", "#2a3a4a")
    findings = a11y.audit_theme()
    assert findings and "secondary (dim) text" in findings[0].where and "below" in findings[0].problem


def test_the_checker_finds_unnamed_and_unreachable_controls_and_leaves_good_ones_alone(qapp) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout

    root = QWidget()
    layout = QVBoxLayout(root)
    good = QPushButton("&Save")
    icon_only = QPushButton("")
    named_icon = QPushButton("")
    named_icon.setAccessibleName("Close panel")
    skipped = QPushButton("Skip me")
    skipped.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    field = QLineEdit()
    for w in (good, icon_only, named_icon, skipped, field):
        layout.addWidget(w)
    root.show()
    findings = [f for f in a11y.audit_widgets(root) if f.level == "fail"]
    where = " | ".join(f"{f.where}:{f.problem}" for f in findings)
    assert "no name" in where and "cannot be reached with the keyboard" in where
    assert not any("Save" in f.where for f in findings) and not any("Close panel" in f.where for f in findings)
    assert sum(1 for f in findings if "no name" in f.problem) == 2             # the empty button and the empty field
    assert "Not checked by this tool" in a11y.report(findings)


def test_a_label_buddy_names_a_field(qapp) -> None:
    from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout

    root = QWidget()
    layout = QVBoxLayout(root)
    label, field = QLabel("Name:"), QLineEdit()
    label.setBuddy(field)
    layout.addWidget(label)
    layout.addWidget(field)
    root.show()
    assert a11y.audit_widgets(root) == []


def test_real_dialogs_have_no_unnamed_or_unreachable_controls(qapp, tmp_path: Path) -> None:
    from app.config.settings import load_settings
    from app.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(load_settings(None, environ={}), tmp_path / ".env", models=["qwen3:8b"])
    dialog.show()
    problems = [f for f in a11y.audit_widgets(dialog) if f.level == "fail"]
    assert problems == [], a11y.report(problems)


def test_main_window_has_no_unnamed_or_unreachable_controls(qapp, tmp_path: Path) -> None:
    from app.ui.main_window import MainWindow
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    window = MainWindow(controller)
    try:
        window.show()
        problems = [f for f in a11y.audit_widgets(window) if f.level == "fail"]
        assert problems == [], a11y.report(problems)
    finally:
        window.allow_close = True
        window.close()


def test_workflow_studio_and_memory_dialog_are_clean(qapp, tmp_path: Path) -> None:
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.memory.database import Database
    from app.security.permissions import PermissionEngine
    from app.tools.base import ToolContext
    from app.ui.workflow_studio import WorkflowStudioDialog
    from tests.test_automation import recording_registry

    db = Database(tmp_path / "a.db")
    db.initialize()
    ctx = ToolContext(Settings(), database=db)
    studio = WorkflowStudioDialog(ctx, None, registry=recording_registry([]), permissions=PermissionEngine(PermissionsConfig()))
    studio.show()
    problems = [f for f in a11y.audit_widgets(studio) if f.level == "fail"]
    assert problems == [], a11y.report(problems)
