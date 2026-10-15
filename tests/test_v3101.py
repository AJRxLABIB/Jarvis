"""3.10.1: the things Jarvis refused although it could do them - unknown app / site names, "restart X",
"minimize yourself", looking at the screen, "yes, install it", brightness on monitors without DDC/CI."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.conversation import ConversationManager as Conversation, strip_emoji
from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.tools import applications, winapi, windows
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer
from tests.test_v322 import Browser, Screen, pc  # noqa: F401 (fixture)
from tests.test_windows_tools import desktop, run_tool, start_menu  # noqa: F401 (fixtures)


def shortcut(text: str, site: bool = False) -> ToolCall | None:
    return match_shortcut(text, lambda name: None, known_site=lambda name: site)


@pytest.mark.parametrize(("text", "tool", "arguments"), [
    ("Open Cloud for me", "open_application", {"name": "Cloud"}),
    ("Restart Cloud", "restart_application", {"name": "Cloud"}),
    ("relaunch spotify please", "restart_application", {"name": "spotify"}),
    ("Open Netmirror in Google Chrome", "open_website", {"site": "Netmirror", "browser": "google chrome"}),
    ("Minimize Yourself, I need to see my screen", "jarvis_control", {"action": "hide_window"}),
    ("Show me my screen", "jarvis_control", {"action": "hide_window"}),
    ("Visualized my desktop", "describe_screen", {"target": "screen"}),
    ("what's on my screen?", "describe_screen", {"target": "screen"}),
])
def test_requests_the_model_refused_go_to_a_tool(text, tool, arguments) -> None:
    call = shortcut(text)
    assert call is not None and call.name == tool and call.arguments == arguments


@pytest.mark.parametrize("text", ["open a new tab", "open the report", "open my last file", "restart explorer",
                                  "restart my pc", "open the file in chrome", "start the timer"])
def test_the_last_resort_leaves_other_requests_alone(text) -> None:
    call = shortcut(text)
    assert call is None or call.name not in ("open_application", "restart_application", "open_website")


def test_an_unknown_app_offers_similar_names_or_a_search(desktop, start_menu) -> None:
    result = run_tool("open_application", name="Zzqx").result
    assert not result.ok and "search the web" in result.message
    assert result.data["suggestion"] == {"tool": "search_in_browser", "arguments": {"query": "Zzqx"}}
    (start_menu.roots()[0] / "iCloud Drive.lnk").write_bytes(b"lnk")
    (start_menu.roots()[0] / "Nextcloud.lnk").write_bytes(b"lnk")
    start_menu._entries = []
    result = run_tool("open_application", name="Cloud").result
    assert not result.ok and "iCloud Drive" in result.message and "Nextcloud" in result.message


def test_an_unknown_site_is_searched_for_in_the_browser_asked_for(pc, monkeypatch) -> None:
    Browser(monkeypatch, pc, {1: ["Inbox"]})
    result = run_tool("open_website", site="Netmirror", browser="google chrome").result
    assert result.ok and result.data["searched"] and "google.com/search?q=Netmirror" in result.data["url"]
    assert pc.programs and "google.com/search?q=Netmirror" in pc.programs[-1][1][-1]


def test_restart_closes_and_opens_again(desktop, start_menu, monkeypatch) -> None:
    monkeypatch.setattr(applications.time, "sleep", lambda s: None)
    (start_menu.roots()[0] / "Notepad.lnk").write_bytes(b"lnk")
    start_menu._entries = []
    result = run_tool("restart_application", name="notepad").result
    assert result.ok and result.message == "Restarted Notepad." and 3 in desktop.closed
    assert desktop.started and "notepad" in desktop.started[-1].lower()  # Start Menu or System32 (on Windows)
    assert "Program Manager" not in str(desktop.closed)


def test_file_explorer_is_not_restarted(desktop, start_menu) -> None:
    (start_menu.roots()[0] / "File Explorer.lnk").write_bytes(b"lnk")
    start_menu._entries = []
    assert run_tool("restart_application", name="file explorer").result.error == "not_allowed"


def _dimmable(monkeypatch, level=None, screens=0):
    screen = Screen(monkeypatch, level=level, screens=screens)
    veils: list[int] = []
    context = ToolContext(Settings(), dim_screen=lambda value: veils.append(value) or True)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), context,
                      confirmer=ScriptedConfirmer(True))
    return screen, veils, lambda **a: asyncio.run(ex.execute(ToolCall("brightness_control", a))).result


def test_monitor_without_ddc_is_dimmed_in_software(monkeypatch) -> None:
    _, veils, brightness = _dimmable(monkeypatch)
    result = brightness(action="set", level=20)
    assert result.ok and result.data["software"] and veils == [20] and "in software" in result.message
    assert brightness(action="get").data["brightness"] == 20
    assert brightness(action="up", step=30).data["brightness"] == 50 and veils[-1] == 50
    assert brightness(action="set", level=100).ok and windows.SOFT["level"] == 100


def test_real_brightness_lifts_the_software_veil(monkeypatch) -> None:
    _, veils, brightness = _dimmable(monkeypatch, level=60, screens=1)
    windows.SOFT["level"] = 30
    result = brightness(action="set", level=70)
    assert result.ok and veils == [100] and windows.SOFT["level"] == 100 and "software" not in result.data


def test_without_a_window_to_dim_it_still_says_so(monkeypatch) -> None:
    Screen(monkeypatch, level=None, screens=0)
    result = run_tool("brightness_control", action="set", level=20).result
    assert not result.ok and "DDC/CI" in result.message


def test_windows_update_list_offers_install_and_install_updates_defender(desktop, monkeypatch) -> None:
    monkeypatch.setattr(winapi, "pending_updates", lambda: ["Security Intelligence Update for Microsoft Defender"])
    listed = run_tool("windows_update", action="list").result
    assert listed.ok and listed.data["suggestion"] == {"tool": "windows_update", "arguments": {"action": "install"}}
    ran = []
    monkeypatch.setattr(winapi, "update_defender_signatures", lambda: ran.append(1) or (True, "up to date"))
    monkeypatch.setattr(winapi, "start_windows_update", lambda: None)
    installed = run_tool("windows_update", action="install").result
    assert installed.ok and ran and installed.data["defender_updated"] and "Defender" in installed.message


@pytest.mark.parametrize("text", ["Yes, install it", "yes install them", "install it", "yes", "turn it on",
                                  "search for it", "হ্যাঁ"])
def test_yes_variants(text) -> None:
    assert Conversation._YES.match(text)


def test_update_commands_are_not_run_as_dev_commands() -> None:
    result = run_tool("run_command", command="wusa KB2267602 /quiet /norestart").result
    assert not result.ok and result.error == "wrong_tool" and "windows_update" in result.message


def test_emoji_are_removed_from_replies() -> None:
    assert strip_emoji("Opened it for you! \U0001F60A") == "Opened it for you!"
    assert strip_emoji("✓ tests pass, ⚠ one warning") == "✓ tests pass, ⚠ one warning"


def test_screen_capture_off_offers_to_turn_it_on() -> None:
    result = run_tool("describe_screen", target="screen").result
    assert result.error == "disabled"
    assert result.data["suggestion"]["tool"] == "change_setting"


def test_dimmer_opacity_never_blacks_the_screen_out() -> None:
    from app.ui.dimmer import MAX_OPACITY, opacity_for

    assert opacity_for(100) == 0 and opacity_for(0) == MAX_OPACITY < 1 and 0 < opacity_for(20) < MAX_OPACITY
    assert opacity_for(150) == 0 and opacity_for(-5) == MAX_OPACITY
