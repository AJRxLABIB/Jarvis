"""3.7.0 Phase 4: real Wi-Fi / Bluetooth / dark-mode switches, file tools, pressing controls by name (UI
Automation), screen recording through Game Bar - each verified."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools import device_toggles, file_tools, ui_elements, winapi
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_filesystem import home, policy_for  # noqa: F401 (fixture)
from tests.test_tools import ScriptedConfirmer


def run(home: Path, tool_name: str, confirm: bool = True, database=None, **arguments):
    ctx = ToolContext(Settings(), path_policy=policy_for(home), database=database)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx,
                      confirmer=ScriptedConfirmer(confirm))
    return asyncio.run(ex.execute(ToolCall(tool_name, arguments))).result


# ------------------------------------------------------------------ switches


class Switches:
    def __init__(self, monkeypatch, wifi=True, dark=False, allowed=True) -> None:
        self.state = {"wifi": wifi, "bluetooth": False, "dark_mode": dark}
        self.allowed = allowed
        monkeypatch.setattr(winapi, "require_windows", lambda: None)

        async def get(kind):
            if not self.allowed:
                raise ToolError("Windows doesn't let apps switch radios", "denied")
            return self.state[kind]

        async def put(kind, on):
            self.state[kind] = on

        monkeypatch.setattr(device_toggles, "radio_state", get)
        monkeypatch.setattr(device_toggles, "set_radio", put)
        monkeypatch.setattr(device_toggles, "dark_mode", lambda: self.state["dark_mode"])
        monkeypatch.setattr(device_toggles, "set_dark_mode", lambda on: self.state.__setitem__("dark_mode", on))
        opened = []
        monkeypatch.setattr(winapi, "start_file", opened.append)
        self.opened = opened


def test_switches_are_verified_and_undoable(home, monkeypatch, tmp_path) -> None:
    from app.memory.database import Database

    database = Database(tmp_path / "j.db")
    database.initialize()
    switches = Switches(monkeypatch)
    result = run(home, "device_toggle", setting="dark_mode", state="on", database=database)
    assert result.ok and result.message == "Dark mode is on." and switches.state["dark_mode"]
    assert run(home, "device_toggle", setting="dark_mode", state="on").message == "Dark mode is already on."
    assert run(home, "device_toggle", setting="wifi", state="status").message == "Wi-Fi is on."
    assert run(home, "undo_last_action", database=database).ok and not switches.state["dark_mode"]


def test_wifi_off_asks_first_unless_you_said_it(home, monkeypatch) -> None:
    switches = Switches(monkeypatch)
    ctx = ToolContext(Settings(), path_policy=policy_for(home))
    confirmer = ScriptedConfirmer(False)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx, confirmer=confirmer)
    assert asyncio.run(ex.execute(ToolCall("device_toggle", {"setting": "wifi", "state": "off"}))).result.error \
        == "declined" and switches.state["wifi"]
    assert asyncio.run(ex.execute(ToolCall("device_toggle", {"setting": "wifi", "state": "off"}),
                                  user_command=True)).result.ok and not switches.state["wifi"]


def test_a_switch_that_doesnt_change_is_reported(home, monkeypatch) -> None:
    switches = Switches(monkeypatch)

    async def stuck(kind, on):
        pass

    monkeypatch.setattr(device_toggles, "set_radio", stuck)
    result = run(home, "device_toggle", setting="bluetooth", state="on")
    assert not result.ok and "still off" in result.message and not switches.state["bluetooth"]


def test_windows_refusing_radios_opens_the_page(home, monkeypatch) -> None:
    switches = Switches(monkeypatch, allowed=False)
    result = run(home, "device_toggle", setting="wifi", state="off")
    assert not result.ok and "doesn't let apps" in result.message and switches.opened == ["ms-settings:network-wifi"]


# ------------------------------------------------------------------ file tools


def test_zip_and_unzip_next_to_the_originals(home) -> None:
    zipped = run(home, "zip_items", paths=["Documents\\Projects", "notes.txt"], name="backup")
    assert zipped.ok and zipped.data["verified"] and (home / "Documents" / "backup.zip").exists()
    names = zipfile.ZipFile(home / "Documents" / "backup.zip").namelist()
    assert sorted(names) == ["Projects/plan.docx", "notes.txt"]
    again = run(home, "zip_items", paths=["notes.txt"], name="backup")
    assert again.data["path"].endswith("backup (2).zip")  # never overwritten
    unzipped = run(home, "extract_zip", path="Documents\\backup.zip")
    assert unzipped.ok and (home / "Documents" / "backup" / "Projects" / "plan.docx").exists()


def test_zip_slip_and_bombs_are_refused(tmp_path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../../outside.txt", "x")
    with zipfile.ZipFile(evil) as archive, pytest.raises(ToolError, match="outside its folder"):
        file_tools.safe_members(archive, tmp_path / "out")
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w") as archive:
        archive.writestr("a.txt", "x")
    with zipfile.ZipFile(big) as archive:
        archive.infolist()[0].file_size = file_tools.MAX_UNZIP_BYTES + 1
        with pytest.raises(ToolError, match="2 GB"):
            file_tools.safe_members(archive, tmp_path / "out")


def test_merge_pdfs(home) -> None:
    from pypdf import PdfWriter

    for name, pages in (("a.pdf", 1), ("b.pdf", 2)):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=200, height=200)
        with open(home / "Documents" / name, "wb") as handle:
            writer.write(handle)
    result = run(home, "merge_pdfs", paths=["Documents\\a.pdf", "Documents\\b.pdf"], name="both")
    assert result.ok and result.data["pages"] == 3 and (home / "Documents" / "both.pdf").exists()
    assert not run(home, "merge_pdfs", paths=["notes.txt", "Documents\\a.pdf"]).ok


def test_shrink_and_convert_images(home) -> None:
    from PIL import Image

    Image.new("RGB", (4000, 3000), "red").save(home / "Pictures" / "big.png")
    shrunk = run(home, "shrink_images", paths=["Pictures\\big.png"], max_side=1000)
    assert shrunk.ok and Image.open(home / "Pictures" / "big-small.jpg").size == (1000, 750)
    assert (home / "Pictures" / "big.png").exists()  # the original is untouched
    converted = run(home, "convert_image", path="Pictures\\big.png", to="webp")
    assert converted.ok and (home / "Pictures" / "big.webp").exists()


def test_protected_folders_stay_protected(home) -> None:
    (home.parent / "Windows" / "System32" / "x.txt").write_text("x")
    result = run(home, "zip_items", paths=[str(home.parent / "Windows" / "System32" / "x.txt")])
    assert not result.ok


# ------------------------------------------------------------------ pressing controls by name


@pytest.mark.parametrize("wanted, name, points", [("save", "Save", 1.0), ("save", "Save as", 0.9),
                                                  ("ok", "OK", 1.0), ("sign in", "Sign in to continue", 0.9),
                                                  ("save", "Unsaved changes", 0.0), ("in", "Sign in", 0.9),
                                                  ("save", "Don't Save", 0.0), ("don't save", "Don't Save", 1.0),
                                                  ("ok", "Not OK", 0.0)])
def test_name_scores(wanted, name, points) -> None:
    assert ui_elements.score(wanted, name) == points


class Element:
    def __init__(self, name):
        self.CurrentName, self.CurrentIsOffscreen, self.CurrentControlType = name, False, 50000


def controls(monkeypatch, names, react=True):
    pressed = []
    window = SimpleNamespace(hwnd=5, title="Notepad")
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "target_window", lambda exclude_pid=None: window)
    monkeypatch.setattr(winapi, "foreground_is_ours", lambda: False)
    monkeypatch.setattr(ui_elements, "find_controls", lambda hwnd, wanted: sorted([
        ui_elements.Found(n, "button", Element(n), ui_elements.score(wanted, n)) for n in names
        if ui_elements.score(wanted, n)], key=lambda f: -f.score))
    monkeypatch.setattr(ui_elements, "press", lambda element: pressed.append(element.CurrentName) or "invoke")
    monkeypatch.setattr(ui_elements, "_toggle_state", lambda e: None)
    monkeypatch.setattr(ui_elements, "_snapshot", lambda: (set(), {}))
    monkeypatch.setattr(ui_elements, "reacted", lambda *a, **k: "a window closed" if react else None)
    return pressed


def test_press_a_control_by_name(home, monkeypatch) -> None:
    pressed = controls(monkeypatch, ["Don't Save", "Save", "Cancel"])
    only_negative = controls(monkeypatch, ["Don't Save"])
    assert not run(home, "press_control", name="save").ok and only_negative == []  # never the opposite button
    pressed = controls(monkeypatch, ["Don't Save", "Save", "Cancel"])
    result = run(home, "press_control", name="save")
    assert result.ok and pressed == ["Save"] and "(a window closed)" in result.message and result.data["verified"]


def test_no_change_is_said_honestly(home, monkeypatch) -> None:
    controls(monkeypatch, ["OK"], react=False)
    result = run(home, "press_control", name="ok")
    assert result.ok and "can't see a change" in result.message and result.data["verified"] is None


def test_missing_control_points_to_ocr(home, monkeypatch) -> None:
    controls(monkeypatch, ["Cancel"])
    result = run(home, "press_control", name="submit")
    assert not result.ok and "click the text submit" in result.message


# ------------------------------------------------------------------ screen recording


def test_recording_checks_that_a_video_appeared(home, monkeypatch, tmp_path) -> None:
    captures = tmp_path / "Videos" / "Captures"
    captures.mkdir(parents=True)
    keys = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "known_folder", lambda key: tmp_path / "Videos")
    monkeypatch.setattr(winapi, "foreground_is_ours", lambda: False)

    def press(combo):
        keys.append(combo)
        if len(keys) == 2:
            (captures / "Notepad 2026-09-26.mp4").write_bytes(b"mp4")

    monkeypatch.setattr(winapi, "press_combo", press)
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    device_toggles._RECORDING.clear()
    assert "Recording the app in front" in run(home, "screen_recording", action="start").message
    stopped = run(home, "screen_recording", action="stop")
    assert stopped.ok and "Notepad 2026-09-26.mp4" in stopped.message and keys == ["win+alt+r", "win+alt+r"]
    device_toggles._RECORDING.clear()
    run(home, "screen_recording", action="start")
    failed = run(home, "screen_recording", action="stop")
    assert not failed.ok and "Game Bar may be off" in failed.message


@pytest.mark.parametrize("said, tool, arguments", [
    ("turn off wifi", "device_toggle", {"setting": "wifi", "state": "off"}),
    ("bluetooth on", "device_toggle", {"setting": "bluetooth", "state": "on"}),
    ("turn on dark mode", "device_toggle", {"setting": "dark_mode", "state": "on"}),
    ("switch to light mode", "device_toggle", {"setting": "dark_mode", "state": "off"}),
    ("is wifi on", "device_toggle", {"setting": "wifi", "state": "status"}),
    ("ওয়াইফাই বন্ধ করো", "device_toggle", {"setting": "wifi", "state": "off"}),
    ("press save", "press_control", {"name": "save"}),
    ("click the Settings tab", "press_control", {"name": "Settings"}),
    ("tick remember me", "press_control", {"name": "remember me"}),
    ("zip my projects folder", "zip_items", {"paths": ["projects"]}),
    ("unzip report.zip", "extract_zip", {"path": "report.zip"}),
    ("record my screen", "screen_recording", {"action": "start"}),
    ("stop recording", "screen_recording", {"action": "stop"}),
])
def test_commands(said, tool, arguments) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)


@pytest.mark.parametrize("said", ["press enter", "press ctrl+s", "open bluetooth settings", "check the weather"])
def test_not_these(said) -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut(said, lambda n: None)
    assert call is None or call.name not in ("press_control", "device_toggle")
