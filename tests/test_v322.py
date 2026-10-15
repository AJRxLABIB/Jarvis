"""3.2.2: songs play in the same YouTube tab (no pile of tabs, no jumping between profiles), other sites open in a
new tab of the same browser/profile, and "volume / brightness set at N" works in any word order."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agent.shortcuts import level_command, match_shortcut
from app.tools import applications, browser_tabs, winapi
from app.tools.browser_profiles import choose_profile, load_profiles, pin_profile, pinned_profile
from tests.test_browser_profiles import write_local_state
from tests.test_windows_tools import FakeDesktop, W, ctx, run_tool, start_menu  # noqa: F401 (fixture)

CHROME = r"C:\Apps\chrome.exe"
SONG = "https://www.youtube.com/watch?v=abcDEF12345"


class Browser:
    """Chrome windows with tabs; keys typed into the window in front are recorded."""

    def __init__(self, monkeypatch, pc: FakeDesktop, windows: dict[int, list[str]], front: int = 0) -> None:
        self.pc, self.tabs, self.front = pc, windows, front
        self.active = {hwnd: 0 for hwnd in windows}
        self.keys: list[tuple[int, str]] = []
        self.selected: list[str] = []
        self.focus_works = True
        pc.windows = [W(h, 100, self.title(h), "chrome.exe") for h in windows] + [W(9, 300, "Jarvis", "jarvis.exe")]
        monkeypatch.setattr(winapi, "force_foreground", self._focus)
        monkeypatch.setattr(winapi, "foreground_window", lambda: self.front)
        monkeypatch.setattr(winapi, "window_title", self.title)
        monkeypatch.setattr(winapi, "visible_windows", self._windows)
        monkeypatch.setattr(winapi, "press_combo", lambda combo: self.keys.append((self.front, combo)))
        monkeypatch.setattr(winapi, "type_unicode", lambda text: self.keys.append((self.front, "type:" + text)))
        monkeypatch.setattr(winapi, "foreground_is_ours", lambda: self.front == 9)
        monkeypatch.setattr(browser_tabs, "tab_items", lambda hwnd: [(t, (hwnd, i)) for i, t in enumerate(self.tabs[hwnd])])
        monkeypatch.setattr(browser_tabs, "select_tab", self._select)
        monkeypatch.setattr(browser_tabs.time, "sleep", lambda s: None)

    def title(self, hwnd: int) -> str:
        return f"{self.tabs[hwnd][self.active[hwnd]]} - Google Chrome" if hwnd in self.tabs else "Jarvis"

    def _windows(self):
        return [W(h, 100, self.title(h), "chrome.exe") for h in self.tabs] + [W(9, 300, "Jarvis", "jarvis.exe")]

    def _focus(self, hwnd: int) -> bool:
        if self.focus_works:
            self.front = hwnd
        return self.focus_works

    def _select(self, element) -> bool:
        hwnd, index = element
        self.active[hwnd] = index
        self.selected.append(self.tabs[hwnd][index])
        return True


@pytest.fixture()
def pc(tmp_path: Path, monkeypatch, start_menu) -> FakeDesktop:
    monkeypatch.setattr(applications, "_process_running", lambda names: True)
    desktop = FakeDesktop(monkeypatch)
    desktop.default_browser = "ChromeHTML"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_local_state(tmp_path)
    return desktop


def _typed(browser: Browser) -> list[str]:
    return [k for _, k in browser.keys]


# ------------------------------------------------------------------ same tab for songs


def test_the_youtube_tab_in_front_is_reused(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Old song - YouTube", "Inbox - Gmail"]}, front=9)
    assert applications.open_url(SONG, ctx(), same_tab="youtube") == "Google Chrome (same tab)"
    assert pc.programs == []  # no new tab, no new window, no other profile
    assert _typed(tabs) == ["esc", "ctrl+l", "type:" + SONG, "enter"]
    assert all(window == 1 for window, _ in tabs.keys)  # every key went to Chrome, none to Jarvis


def test_a_youtube_tab_behind_other_tabs_is_selected_first(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Facebook", "Song - YouTube", "Gmail"], 2: ["Docs"]}, front=2)
    applications.open_url(SONG, ctx(), same_tab="youtube")
    assert tabs.selected == ["Song - YouTube"] and pc.programs == []
    assert (1, "type:" + SONG) in tabs.keys and tabs.front == 1


def test_no_youtube_tab_opens_one_new_tab(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Facebook", "Gmail"]})
    where = applications.open_url(SONG, ctx(), same_tab="youtube")
    assert tabs.keys == [] and pc.programs == [(CHROME, ["--profile-directory=Profile 5", SONG])]
    assert where == "Google Chrome (Your Chrome profile)"


def test_no_keys_are_sent_when_chrome_cannot_be_brought_in_front(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Song - YouTube"]}, front=3)
    tabs.focus_works = False
    applications.open_url(SONG, ctx(), same_tab="youtube")
    assert tabs.keys == [] and len(pc.programs) == 1  # a new tab instead of typing into the wrong app


def test_jarvis_steps_aside_before_the_keys(pc, monkeypatch) -> None:
    Browser(monkeypatch, pc, {1: ["Song - YouTube"]}, front=9)
    events = []
    context = ctx()
    context.step_aside = lambda: events.append("aside")
    applications.open_url(SONG, context, same_tab="youtube")
    assert events == ["aside"]


def test_play_music_twice_uses_one_tab(pc, monkeypatch) -> None:
    from app.tools import assistant
    from app.tools.assistant import PlayMusicArgs, play_music

    tabs = Browser(monkeypatch, pc, {1: ["Gmail"]})
    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, text='"videoId":"abcDEF12345"')))
    first = asyncio.run(play_music(PlayMusicArgs(query="Imran Hashmi"), ctx()))
    assert len(pc.programs) == 1 and "Your Chrome profile" in first.message
    tabs.tabs[1].append("Imran Hashmi song - YouTube")  # Chrome opened it
    second = asyncio.run(play_music(PlayMusicArgs(query="Arijit Singh"), ctx()))
    assert len(pc.programs) == 1 and "same tab" in second.message
    assert tabs.selected == ["Imran Hashmi song - YouTube"]


def test_youtube_search_reuses_the_tab_google_search_does_not(pc, monkeypatch) -> None:
    Browser(monkeypatch, pc, {1: ["Song - YouTube"]})
    assert "same tab" in run_tool("search_in_browser", query="arijit", engine="youtube").result.message
    assert pc.programs == []
    run_tool("search_in_browser", query="weather", engine="google")
    assert len(pc.programs) == 1  # google results: a new tab


def test_other_sites_open_in_a_new_tab_of_the_same_profile(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Song - YouTube"]})
    result = run_tool("open_website", site="facebook", browser="chrome").result
    assert result.ok and pc.programs == [(CHROME, ["--profile-directory=Profile 5", "https://www.facebook.com"])]
    assert tabs.keys == []  # the song keeps playing


def test_open_youtube_shows_the_playing_tab_without_reloading(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Facebook", "Song - YouTube"]})
    result = run_tool("open_website", site="youtube", browser="chrome").result
    assert result.ok and pc.programs == [] and tabs.selected == ["Song - YouTube"]
    assert not any(k.startswith("type:") for k in _typed(tabs))


def test_reuse_can_be_switched_off(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Song - YouTube"]})
    context = ctx()
    context.settings = context.settings.model_copy(update={"reuse_tabs": False})
    applications.open_url(SONG, context, same_tab="youtube")
    assert tabs.keys == [] and len(pc.programs) == 1


def test_without_ui_automation_the_tab_search_is_used(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Facebook", "Song - YouTube"]})
    monkeypatch.setattr(browser_tabs, "tab_items", lambda hwnd: None)

    def keys(combo):
        tabs.keys.append((tabs.front, combo))
        if combo == "enter" and ("type:YouTube" in _typed(tabs)) and tabs.active[1] == 0:
            tabs.active[1] = 1  # Chrome's "Search tabs" switched to it

    monkeypatch.setattr(winapi, "press_combo", keys)
    applications.open_url(SONG, ctx(), same_tab="youtube")
    assert _typed(tabs)[:3] == ["ctrl+shift+a", "type:YouTube", "enter"] and pc.programs == []
    assert "type:" + SONG in _typed(tabs)


def test_failed_tab_search_is_closed_and_a_new_tab_opens(pc, monkeypatch) -> None:
    tabs = Browser(monkeypatch, pc, {1: ["Facebook"]})
    monkeypatch.setattr(browser_tabs, "tab_items", lambda hwnd: None)
    applications.open_url(SONG, ctx(), same_tab="youtube")
    assert _typed(tabs) == ["ctrl+shift+a", "type:YouTube", "enter", "esc"] and len(pc.programs) == 1


@pytest.mark.parametrize("title, yes", [
    ("Imran Hashmi songs - YouTube - Google Chrome", True), ("YouTube", True), ("(3) Song – YouTube", True),
    ("How to use YouTube Studio - Google Search", False), ("Facebook", False), ("", False),
])
def test_which_titles_are_youtube_tabs(title, yes) -> None:
    assert browser_tabs.has_mark(title, "YouTube") is yes


# ------------------------------------------------------------------ one profile


def test_the_profile_jarvis_used_is_kept(tmp_path: Path) -> None:
    profiles = load_profiles(write_local_state(tmp_path))
    assert choose_profile(profiles, "last", pinned="Profile 2").name == "Night"  # not Chrome's last-used one
    assert choose_profile(profiles, "last", pinned="Profile 99").name == "Your Chrome"  # deleted: ignore it
    kept = choose_profile(profiles, "Labib Hossain", pinned="Profile 1")  # several match: the one in use wins
    assert kept.directory == "Profile 1" and kept.note == ""
    assert choose_profile(profiles, "Night", pinned="Profile 1").directory == "Profile 2"  # you named another


def test_the_pin_survives_a_restart(tmp_path: Path) -> None:
    from app.tools import browser_profiles

    pin_profile(CHROME, "Profile 2", tmp_path)
    assert json.loads((tmp_path / "browser_profile.json").read_text()) == {"chrome.exe": "Profile 2"}
    browser_profiles._pins.clear()
    browser_profiles._loaded_from.clear()
    assert pinned_profile(CHROME, tmp_path) == "Profile 2"
    pin_profile(CHROME, "../evil", tmp_path)  # only Chrome's own folder names
    assert pinned_profile(CHROME, tmp_path) == "Profile 2"


def test_naming_a_profile_once_moves_everything_there(pc, tmp_path, monkeypatch) -> None:
    context = ctx()
    context.paths = SimpleNamespace(data_dir=tmp_path / "data")
    run_tool("open_application", name="chrome", profile="Night")  # uses its own ctx: pin is per process
    applications.open_url("https://www.facebook.com", context)
    assert pc.programs[-1][1][0] == "--profile-directory=Profile 2"


# ------------------------------------------------------------------ volume / brightness to a number


@pytest.mark.parametrize("said, tool, args", [
    ("volume set at 50", "volume_control", {"action": "set", "level": 50}),
    ("Volume set at 50.", "volume_control", {"action": "set", "level": 50}),
    ("set the volume to 35%", "volume_control", {"action": "set", "level": 35}),
    ("make the volume forty", "volume_control", {"action": "set", "level": 40}),
    ("put the sound at sixty five percent", "volume_control", {"action": "set", "level": 65}),
    ("increase volume to 70", "volume_control", {"action": "set", "level": 70}),
    ("reduce the volume to 10", "volume_control", {"action": "set", "level": 10}),
    ("volume up by 20", "volume_control", {"action": "up", "step": 20}),
    ("volume should be 50 please", "volume_control", {"action": "set", "level": 50}),
    ("max volume", "volume_control", {"action": "set", "level": 100}),
    ("ভলিউম ৫০ করো", "volume_control", {"action": "set", "level": 50}),
    ("ভলিউম ৫০% এ সেট করো", "volume_control", {"action": "set", "level": 50}),
    ("ভলিউম পঞ্চাশ করো", "volume_control", {"action": "set", "level": 50}),
    ("ভলিউম কমাও", "volume_control", {"action": "down"}),
    ("brightness set at 70", "brightness_control", {"action": "set", "level": 70}),
    ("set the screen brightness to 45 percent", "brightness_control", {"action": "set", "level": 45}),
    ("brightness 30", "brightness_control", {"action": "set", "level": 30}),
    ("lower brightness to 20", "brightness_control", {"action": "set", "level": 20}),
    ("brightness down by 15", "brightness_control", {"action": "down", "step": 15}),
    ("dim the screen", None, None),
    ("increase the brightness", "brightness_control", {"action": "up"}),
    ("full brightness", "brightness_control", {"action": "set", "level": 100}),
    ("what's the brightness", "brightness_control", {"action": "get"}),
    ("ব্রাইটনেস ৮০ করো", "brightness_control", {"action": "set", "level": 80}),
    ("উজ্জ্বলতা বাড়াও", "brightness_control", {"action": "up"}),
])
def test_level_commands(said, tool, args) -> None:
    call = match_shortcut(said, lambda n: None)
    if tool is None:
        assert call is None or call.name != "brightness_control"
        return
    assert call is not None and call.name == tool and dict(call.arguments) == args


@pytest.mark.parametrize("said", [
    "set volume to 50 and open chrome", "sound of music", "brightness 150", "volume 20 30",
    "turn the volume up and down", "what is the brightness at 50",
])
def test_not_level_commands(said) -> None:
    assert level_command(said) is None


class Screen:
    def __init__(self, monkeypatch, level: int | None = 40, screens: int = 1, steps: int = 1) -> None:
        self.level, self.screens, self.steps = level, screens, steps
        monkeypatch.setattr(winapi, "is_windows", lambda: True)
        monkeypatch.setattr(winapi, "require_windows", lambda: None)
        monkeypatch.setattr(winapi, "get_brightness", lambda: self.level)
        monkeypatch.setattr(winapi, "set_brightness", self._set)
        from app.tools import windows

        monkeypatch.setattr(windows.time, "sleep", lambda s: None)

    def _set(self, percent: int) -> int:
        if self.screens:
            self.level = round(percent / self.steps) * self.steps
        return self.screens


def test_brightness_is_set_to_the_number(monkeypatch) -> None:
    screen = Screen(monkeypatch)
    result = run_tool("brightness_control", action="set", level=70).result
    assert result.ok and result.message == "Brightness set to 70%." and screen.level == 70
    assert run_tool("brightness_control", action="up", step=20).result.message == "Brightness set to 90%."
    assert run_tool("brightness_control", action="up").result.message == "Brightness set to 100%."
    assert run_tool("brightness_control", action="get").result.message == "Brightness is 100%."


def test_brightness_steps_count_as_done(monkeypatch) -> None:
    Screen(monkeypatch, steps=5)
    assert run_tool("brightness_control", action="set", level=52).result.ok  # the screen went to 50


def test_screens_without_brightness_control_say_so(monkeypatch) -> None:
    Screen(monkeypatch, level=None, screens=0)
    result = run_tool("brightness_control", action="set", level=50).result
    assert not result.ok and "DDC/CI" in result.message
    assert not run_tool("brightness_control", action="down").result.ok


def test_brightness_that_did_not_change_is_reported(monkeypatch) -> None:
    screen = Screen(monkeypatch)
    monkeypatch.setattr(winapi, "set_brightness", lambda p: 1)  # "done", but nothing changed
    result = run_tool("brightness_control", action="set", level=90).result
    assert not result.ok and "it is 40%" in result.message and screen.level == 40


def test_brightness_goes_to_the_windows_tools() -> None:
    from app.agent.tool_router import categories_for

    assert "windows" in categories_for("brightness set at 70")
