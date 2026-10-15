"""Opening Chrome/Edge straight into a profile (no "Who's using Chrome?")."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.tools import applications
from app.tools.base import ToolError
from app.tools.browser_profiles import BrowserProfile, choose_profile, load_profiles, profile_arguments
from tests.test_windows_tools import FakeDesktop, run_tool

# Like the profiles on the user's PC: several belong to the same Google account.
PROFILES = {
    "Default": {"name": "Labib", "gaia_name": "Labib Hossain", "user_name": "someone@example.com"},
    "Profile 1": {"name": "LABIB", "gaia_name": "LABIB HOSSAIN"},
    "Profile 2": {"name": "Night", "gaia_name": "Night Fury"},
    "Profile 5": {"name": "Your Chrome", "gaia_name": "Labib Hossain"},
    "Guest Profile": {"name": "Guest"},
    "Profile 9\" --remote-debugging-port=9222": {"name": "Evil"},  # never passed on
}


def write_local_state(root: Path, last_used: str | None = "Profile 5", browser: str = r"Google\Chrome\User Data") -> Path:
    folder = root.joinpath(*browser.split("\\"))
    folder.mkdir(parents=True)
    profile = {"info_cache": PROFILES}
    if last_used:
        profile["last_used"] = last_used
    (folder / "Local State").write_text(json.dumps({"profile": profile}), encoding="utf-8")
    return folder


def test_profiles_are_read_from_local_state(tmp_path: Path) -> None:
    profiles = load_profiles(write_local_state(tmp_path))
    assert [p.directory for p in profiles] == ["Default", "Profile 1", "Profile 2", "Profile 5"]
    assert [p.name for p in profiles if p.last_used] == ["Your Chrome"]
    assert load_profiles(tmp_path / "missing") == []


def test_choosing(tmp_path: Path) -> None:
    profiles = load_profiles(write_local_state(tmp_path))
    assert choose_profile(profiles, "last").name == "Your Chrome"
    assert choose_profile(profiles, "Night").directory == "Profile 2"
    assert choose_profile(profiles, "night fury").directory == "Profile 2"  # by account name
    assert choose_profile(profiles, "your chrome").directory == "Profile 5"
    assert choose_profile(profiles, "Profile 1").name == "LABIB"
    assert choose_profile(profiles, "none") is None  # show Chrome's picker
    both = choose_profile(profiles, "Labib Hossain")  # three profiles use that account: the one used last
    assert both.directory == "Profile 5" and "3 profiles are called" in both.note
    with pytest.raises(ToolError, match="There is no"):
        choose_profile(profiles, "Work")


def test_last_used_missing_falls_back_to_first(tmp_path: Path) -> None:
    profiles = load_profiles(write_local_state(tmp_path, last_used="Profile 77"))
    assert choose_profile(profiles, "last").directory == "Default"


def test_only_real_profile_folder_names_reach_the_command_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_local_state(tmp_path)
    with pytest.raises(ToolError, match="no Google Chrome profile called 'Evil'"):  # the injected entry was dropped
        profile_arguments(r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Evil")
    arguments, profile = profile_arguments(r"C:\Apps\chrome.exe", "last")
    assert arguments == ["--profile-directory=Profile 5"] and profile.name == "Your Chrome"
    assert profile_arguments(r"C:\Apps\notepad.exe", "last") == ([], None)


def test_evil_name_is_just_not_found(tmp_path: Path) -> None:
    profiles = load_profiles(write_local_state(tmp_path))
    assert "Evil" not in [p.name for p in profiles]


# ---------------------------------------------------------------- the tool


@pytest.fixture()
def chrome(tmp_path: Path, monkeypatch) -> FakeDesktop:
    monkeypatch.setattr(applications, "_process_running", lambda names: True)
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_local_state(tmp_path)
    return desktop


def test_open_chrome_opens_the_last_used_profile(chrome: FakeDesktop) -> None:
    result = run_tool("open_application", name="chrome").result
    assert result.ok and result.message == "Opened Google Chrome (Your Chrome profile)."
    assert chrome.programs == [(r"C:\Apps\chrome.exe", ["--profile-directory=Profile 5"])]
    assert chrome.started == []  # not through the shell, which would show the picker


def test_open_chrome_with_a_named_profile(chrome: FakeDesktop) -> None:
    result = run_tool("open_application", name="google chrome", profile="Night").result
    assert result.ok and "Night profile" in result.message
    assert chrome.programs[-1][1] == ["--profile-directory=Profile 2"]


def test_unknown_profile_is_reported(chrome: FakeDesktop) -> None:
    result = run_tool("open_application", name="chrome", profile="Work").result
    assert result.error == "profile_not_found" and "Night" in result.message and chrome.programs == []


def test_profile_setting_none_shows_the_picker(chrome: FakeDesktop, monkeypatch) -> None:
    from tests import test_windows_tools

    monkeypatch.setattr(test_windows_tools, "ctx", lambda **s: test_windows_tools.ToolContext(
        test_windows_tools.Settings(chrome_profile="none")))
    result = run_tool("open_application", name="chrome").result
    assert result.ok and result.message == "Opened Google Chrome." and chrome.started == [r"C:\Apps\chrome.exe"]


def test_empty_profile_placeholder_means_default(chrome: FakeDesktop) -> None:
    result = run_tool("open_application", name="chrome", profile="none").result  # small models send "none"
    assert result.ok and chrome.programs[-1][1] == ["--profile-directory=Profile 5"]


def test_cli_lists_profiles(tmp_path: Path, monkeypatch) -> None:
    from app.cli import run_browser_profiles
    from app.main import bootstrap

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    write_local_state(tmp_path / "lad")
    out = io.StringIO()
    assert run_browser_profiles(bootstrap(tmp_path / "app", environ={}), out) == 0
    text = out.getvalue()
    assert "opens 'Your Chrome' (Profile 5)" in text and "* Profile 5" in text and "example.com" not in text


def test_describe() -> None:
    assert BrowserProfile("Profile 5", "Your Chrome", "Labib Hossain").describe() == "Your Chrome (Labib Hossain)"
