"""'open YouTube / Instagram / WhatsApp': the installed app if there is one, else the site in Chrome's profile.
And 'lock my PC'."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.tools import applications
from app.tools.applications import AppsFolderIndex, open_url, site_key
from app.tools.base import ToolContext, ToolError
from app.config.settings import Settings
from tests.test_browser_profiles import write_local_state
from tests.test_windows_tools import FakeDesktop, run_tool, start_menu  # noqa: F401 (fixture)

CHROME = r"C:\Apps\chrome.exe"
PROFILE = "--profile-directory=Profile 5"


@pytest.fixture()
def pc(tmp_path: Path, monkeypatch, start_menu) -> FakeDesktop:
    monkeypatch.setattr(applications, "_process_running", lambda names: True)
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_local_state(tmp_path)
    monkeypatch.setattr(applications, "APPS_FOLDER", AppsFolderIndex(lambda: [
        ("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"), ("Xbox", "Microsoft.GamingApp_8wekyb3d8bbwe!App"),
        ("Spotify - Music and Podcasts", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")]))
    return desktop


def test_youtube_opens_in_chromes_profile(pc: FakeDesktop) -> None:
    pc.default_browser = "ChromeHTML"
    result = run_tool("open_website", site="youtube").result
    assert result.ok and result.message == "Opened www.youtube.com in Google Chrome (Your Chrome profile)."
    assert pc.programs == [(CHROME, [PROFILE, "https://www.youtube.com"])] and pc.started == []


def test_installed_app_wins(pc: FakeDesktop) -> None:
    result = run_tool("open_website", site="WhatsApp").result
    assert result.ok and "WhatsApp" in result.message and pc.programs == []
    assert pc.started and "WhatsAppDesktop" in pc.started[-1]
    assert "Spotify" in run_tool("open_website", site="spotify").result.message  # the Store app


def test_x_is_not_xbox(pc: FakeDesktop) -> None:
    pc.default_browser = "ChromeHTML"
    result = run_tool("open_website", site="x").result
    assert result.ok and pc.programs[-1][1] == [PROFILE, "https://x.com"]


def test_asking_for_a_browser_skips_the_app(pc: FakeDesktop) -> None:
    result = run_tool("open_website", site="whatsapp", browser="Google Chrome").result
    assert result.ok and pc.programs[-1] == (CHROME, [PROFILE, "https://web.whatsapp.com"])


def test_open_application_falls_back_to_the_website(pc: FakeDesktop) -> None:
    pc.default_browser = "ChromeHTML"
    result = run_tool("open_application", name="Instagram").result
    assert result.ok and "isn't installed here" in result.message
    assert pc.programs[-1][1] == [PROFILE, "https://www.instagram.com"]
    assert run_tool("open_application", name="blender").result.error == "not_found"  # not a website either


def test_site_in_the_profile_field_is_understood(pc: FakeDesktop) -> None:
    """The model once sent open_application(name='chrome', profile='YouTube')."""
    result = run_tool("open_application", name="chrome", profile="YouTube").result
    assert result.ok and pc.programs[-1] == (CHROME, [PROFILE, "https://www.youtube.com"])


def test_web_browser_setting(pc: FakeDesktop, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(applications.webbrowser, "open", lambda url, new=0: opened.append(url) or True)
    ctx = ToolContext(Settings(web_browser="chrome"))
    assert open_url("https://github.com", ctx) == "Google Chrome (Your Chrome profile)"
    pc.default_browser = "MSEdgeHTM"  # Edge is the default but isn't installed on this fake PC
    real_resolve = applications.resolve_application
    monkeypatch.setattr(applications, "resolve_application",  # the real PC may well have Edge installed
                        lambda name, apps, *a: None if name == "edge" else real_resolve(name, apps, *a))
    assert open_url("https://github.com", ToolContext(Settings())) == "your browser" and opened
    with pytest.raises(ToolError, match="doesn't seem to be installed"):
        open_url("https://github.com", ToolContext(Settings()), browser="edge")


def test_search_opens_in_the_profile_too(pc: FakeDesktop) -> None:
    pc.default_browser = "ChromeHTML"
    result = run_tool("search_in_browser", query="lofi music", engine="youtube").result
    assert result.ok and pc.programs[-1][1][0] == PROFILE and "search_query=lofi+music" in pc.programs[-1][1][1]


@pytest.mark.parametrize("url", ["https://x.com/ --remote-debugging-port=9222", "file:///C:/x", "javascript:alert(1)",
                                 'https://a.com/"evil'])
def test_unsafe_urls_never_reach_the_browser(pc: FakeDesktop, url: str) -> None:
    with pytest.raises(ToolError):
        open_url(url, ToolContext(Settings(web_browser="chrome")))
    assert pc.programs == []


def test_site_names() -> None:
    assert site_key("YouTube") == "youtube" and site_key("www.facebook.com") == "facebook"
    assert site_key("https://instagram.com/") == "instagram" and site_key("example.com") == "example"
    assert site_key("youtube.com/watch?v=1") is None and site_key("notepad") is None


# --------------------------------------------------------------------- lock


def test_lock_needs_no_confirmation(pc: FakeDesktop) -> None:
    result = run_tool("power_control", confirm=False, action="lock").result
    assert result.ok and pc.locked == 1


@pytest.mark.parametrize("text", ["Lock my PC.", "lock my laptop", "Lock the computer", "lock screen", "Block my PC.",
                                  "please lock my pc", "Lock my window.", "lock"])
def test_lock_phrases_are_direct_commands(text: str) -> None:
    call = match_shortcut(text, lambda n: None, None, known_site=lambda n: site_key(n) is not None)
    assert call is not None and (call.name, dict(call.arguments)) == ("power_control", {"action": "lock"})


@pytest.mark.parametrize(("text", "expected"), [
    ("Open YouTube.", ("open_website", {"site": "YouTube"})),
    ("open youtube in google chrome", ("open_website", {"site": "youtube", "browser": "google chrome"})),
    ("Open Facebook", ("open_website", {"site": "Facebook"})),
    ("open instagram app", ("open_website", {"site": "instagram"})),
    ("open github.com", ("open_website", {"site": "github.com"})),
])
def test_site_phrases_are_direct_commands(text: str, expected) -> None:
    call = match_shortcut(text, lambda n: None, None, known_site=lambda n: site_key(n) is not None)
    assert call is not None and (call.name, dict(call.arguments)) == expected


def test_other_phrases_are_left_to_the_model() -> None:
    known = lambda n: site_key(n) is not None  # noqa: E731
    assert match_shortcut("lock my car", lambda n: None, None, known_site=known) is None
    assert match_shortcut("open my report.docx", lambda n: None, None, known_site=known) is None
    call = match_shortcut("open chrome", lambda n: "Google Chrome" if n == "chrome" else None, None, known_site=known)
    assert call.name == "open_application"
