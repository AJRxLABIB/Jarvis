"""Workspaces hub: every screen reachable, grouped, searchable; voice shortcuts for it and the phone screen."""

from types import SimpleNamespace

from app.agent.shortcuts import match_shortcut
from app.ui import workspaces


def test_every_workspace_entry_is_a_real_main_window_screen():
    from app.ui.main_window import MainWindow

    for _group, items in workspaces.WORKSPACES:
        for _title, _note, method in items:
            assert callable(getattr(MainWindow, method, None)), method


def test_search_matches_words_anywhere():
    assert workspaces.matches("phone tap", "Phone screen", "see and tap your phone", "Control")
    assert workspaces.matches("", "x", "y", "z")
    assert not workspaces.matches("budget", "Phone screen", "see and tap your phone", "Control")


def test_dialog_filters_and_opens():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    opened = []
    names = {m for _g, items in workspaces.WORKSPACES for _t, _n, m in items}
    window = SimpleNamespace(**{m: (lambda m=m: opened.append(m)) for m in names})
    dialog = workspaces.WorkspacesDialog(window)
    assert len(dialog.visible()) == len(names)
    dialog.search.setText("security")
    assert dialog.visible() == ["open_security_center"]
    dialog.open_first()
    assert opened == ["open_security_center"]


def test_voice_shortcuts():
    for text, action in (("show my phone screen", "phone_screen"), ("control my phone", "phone_screen"),
                         ("show my workspaces", "workspaces")):
        call = match_shortcut(text, lambda name: None)
        assert call is not None and call.name == "jarvis_control" and call.arguments["action"] == action, text
    assert match_shortcut("show me my screen", lambda name: None).arguments["action"] == "window_overview"     # unchanged
