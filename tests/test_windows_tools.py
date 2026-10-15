from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ToolCall
from app.config.files import PermissionsConfig, default_apps
from app.config.settings import Settings
from app.security.permissions import PermissionEngine, RiskLevel
from app.tools import applications, system, winapi
from app.tools.applications import AppsFolderIndex, StartMenuIndex, resolve_application, website_url
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from tests.test_tools import ScriptedConfirmer

W = winapi.WindowInfo


class FakeTimer:
    """Stands in for threading.Timer: records the countdown and fires immediately."""

    started: list[tuple[float, object]] = []

    def __init__(self, delay: float, function) -> None:
        self.delay, self.function, self.daemon, self.cancelled = delay, function, False, False

    def start(self) -> None:
        FakeTimer.started.append((self.delay, self))

    def fire(self) -> None:
        if not self.cancelled:
            self.function()

    def cancel(self) -> None:
        self.cancelled = True


class FakeDesktop:
    """Stands in for app.tools.winapi on any OS."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started: list[str] = []
        self.programs: list[tuple[str, list[str]]] = []
        self.default_browser: str | None = None
        self.closed: list[int] = []
        self.shown: list[tuple[int, int]] = []
        self.focused: list[int] = []
        self.shutdowns: list[tuple[str, int]] = []
        self.locked = 0
        self.volume, self.muted = 40, False
        self.windows = [W(1, 100, "Inbox - Google Chrome", "chrome.exe"), W(2, 100, "YouTube - Google Chrome", "chrome.exe"),
                        W(3, 200, "notes.txt - Notepad", "notepad.exe")]
        self.closes_ok = True
        for name, value in {
            "is_windows": lambda: True, "require_windows": lambda: None, "start_file": self.started.append,
            "start_program": lambda exe, arguments: self.programs.append((exe, list(arguments))),
            "default_browser_progid": lambda: self.default_browser,
            "top_level_windows": lambda: list(self.windows),
            "visible_windows": lambda: [w for w in self.windows if w.switchable], "close_window": self._close,
            "window_exists": lambda h: any(w.hwnd == h for w in self.windows),
            "show_window": lambda h, c: self.shown.append((h, c)), "focus_window": lambda h: self.focused.append(h) or True,
            "minimize_all": lambda: self.shown.append((0, 0)), "lock_workstation": self._lock,
            "run_shutdown": lambda kind, delay=0: self.shutdowns.append((kind, delay)),
            "get_volume": lambda: (self.volume, self.muted), "set_volume": self._set_volume,
            "set_mute": self._set_mute, "app_paths_lookup": lambda exe: rf"C:\Apps\{exe}" if exe == "chrome.exe" else None,
            # the checks after an action ("is it really in front / minimized now?")
            "foreground_window": lambda: self.focused[-1] if self.focused else 0,
            "window_state": self._window_state,
        }.items():
            monkeypatch.setattr(winapi, name, value)
        # restart/shutdown countdowns fire at once in tests
        from app.tools import windows as windows_tools

        monkeypatch.setattr(windows_tools, "_timer_factory", FakeTimer)
        self.timers = FakeTimer.started
        FakeTimer.started.clear()
        # never read the real browser profiles of the PC running the tests
        monkeypatch.setenv("LOCALAPPDATA", str(Path(__file__).parent / "_no_local_app_data_"))

    def _close(self, hwnd: int) -> None:
        self.closed.append(hwnd)
        if self.closes_ok:
            self.windows = [w for w in self.windows if w.hwnd != hwnd]

    def _window_state(self, hwnd: int) -> str:
        if not any(w.hwnd == hwnd for w in self.windows):
            return "gone"
        last = [c for h, c in self.shown if h == hwnd]
        return {winapi.SW_MINIMIZE: "minimized", winapi.SW_MAXIMIZE: "maximized"}.get(last[-1], "normal") if last \
            else "normal"

    def _lock(self) -> None:
        self.locked += 1

    def _set_volume(self, v: int) -> None:
        self.volume = v

    def _set_mute(self, m: bool) -> None:
        self.muted = m


@pytest.fixture()
def desktop(monkeypatch: pytest.MonkeyPatch) -> FakeDesktop:
    monkeypatch.setattr(applications, "_process_running", lambda names: True)
    return FakeDesktop(monkeypatch)


@pytest.fixture()
def start_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StartMenuIndex:
    root = tmp_path / "Programs"
    for rel in ("Discord Inc/Discord.lnk", "Adobe Photoshop 2025.lnk", "Visual Studio Code/Visual Studio Code.lnk",
                "Google Chrome.lnk", "Chrome Remote Desktop.lnk", "Discord Inc/Uninstall Discord.lnk", "Obsidian.lnk"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"lnk")
    index = StartMenuIndex([root])
    monkeypatch.setattr(applications, "START_MENU", index)
    monkeypatch.setattr(applications, "APPS_FOLDER", AppsFolderIndex(lambda: STORE_APPS))
    return index


STORE_APPS = [
    ("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
    ("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    ("Get Help", "Microsoft.GetHelp_8wekyb3d8bbwe!App"),
    ("Evil", 'x" & calc'),
]


def ctx(**settings) -> ToolContext:
    return ToolContext(Settings(**settings))


def run_tool(tool: str, confirm: bool = True, **arguments):
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx(),
                      confirmer=ScriptedConfirmer(confirm))
    return asyncio.run(ex.execute(ToolCall(tool, arguments)))


# ------------------------------------------------------------------ open / close


def test_open_configured_app_uses_app_paths(desktop: FakeDesktop, start_menu) -> None:
    result = run_tool("open_application", name="browser").result
    assert result.ok and result.message == "Opened Google Chrome." and desktop.started == [r"C:\Apps\chrome.exe"]


def test_open_unconfigured_app_from_start_menu(desktop: FakeDesktop, start_menu) -> None:
    result = run_tool("open_application", name="obsidian").result
    assert result.ok and desktop.started[-1].endswith("Obsidian.lnk")
    run_tool("open_application", name="photoshop")
    assert desktop.started[-1].endswith("Adobe Photoshop 2025.lnk")


def test_install_folder_candidates_are_found(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(winapi, "app_paths_lookup", lambda name: None)
    monkeypatch.setattr(applications.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # exactly the path the configured candidate expands to (works on Windows and Linux)
    exe = Path(applications.expand_windows_vars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    target = resolve_application("vs code", default_apps(), StartMenuIndex([]))
    assert target.source == "config" and Path(target.target) == exe


def test_open_store_app_from_apps_folder(desktop: FakeDesktop, start_menu) -> None:
    result = run_tool("open_application", name="WhatsApp").result
    assert result.ok and result.message == "Opened WhatsApp."
    assert desktop.started[-1] == "shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"


@pytest.mark.parametrize("spoken", ["WhatsApp", "whats_app", "whats app", "Whats-App", "the whatsapp app", "whatsap"])
def test_store_app_name_variants(spoken: str) -> None:
    assert AppsFolderIndex(lambda: STORE_APPS).find(spoken)[0] == "WhatsApp"


def test_apps_folder_filters_and_failures() -> None:
    index = AppsFolderIndex(lambda: STORE_APPS)
    assert index.find("whats app") is None or index.find("whatsapp")[0] == "WhatsApp"
    assert index.find("get help") is None  # help/support entries are skipped
    assert index.find("evil") is None  # an app id with shell characters is never used

    def broken():
        raise RuntimeError("COM failure")

    assert AppsFolderIndex(broken).find("whatsapp") is None  # failure = not found, not a crash


def test_start_menu_matching(start_menu: StartMenuIndex) -> None:
    assert start_menu.find("chrome")[1].name == "Google Chrome.lnk"  # shortest match, not Remote Desktop
    assert start_menu.find("the discord app")[1].name == "Discord.lnk"
    assert start_menu.find("uninstall discord") is None  # uninstallers are never indexed
    assert start_menu.find("obsidan")[1].name == "Obsidian.lnk"  # small typo
    assert start_menu.find("blender") is None


def test_open_missing_app_is_friendly(desktop: FakeDesktop, start_menu) -> None:
    result = run_tool("open_application", name="blender").result
    assert not result.ok and result.message.startswith("There's no app called blender on this computer.")
    assert desktop.started == []


def test_config_app_not_installed_falls_back_to_start_menu(start_menu, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(winapi, "app_paths_lookup", lambda exe: None)
    monkeypatch.setattr(applications.shutil, "which", lambda exe: None)
    # Hide the real install folders: on a machine with VS Code installed the
    # %LOCALAPPDATA% candidate would (correctly) be found first.
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "empty"))
    target = resolve_application("vs code", default_apps(), start_menu)
    assert target.source == "start_menu" and target.target.endswith("Visual Studio Code.lnk")


def test_unverified_launch_is_reported_honestly(desktop: FakeDesktop, start_menu, monkeypatch) -> None:
    monkeypatch.setattr(applications, "_process_running", lambda names: False)
    monkeypatch.setattr(applications.time, "sleep", lambda s: None)
    clock = iter(range(0, 100))
    monkeypatch.setattr(applications.time, "monotonic", lambda: next(clock))
    result = run_tool("open_application", name="chrome").result
    assert result.ok and result.data["verified"] is False and "could not confirm" in result.message


def test_close_application(desktop: FakeDesktop) -> None:
    result = run_tool("close_application", name="chrome").result
    assert result.ok and sorted(desktop.closed) == [1, 2] and result.data["windows_closed"] == 2
    assert run_tool("close_application", name="chrome").result.error == "no_window"  # process still alive


def test_close_reports_save_prompt(desktop: FakeDesktop, monkeypatch) -> None:
    desktop.closes_ok = False
    monkeypatch.setattr(applications.time, "sleep", lambda s: None)
    clock = iter(range(0, 100))
    monkeypatch.setattr(applications.time, "monotonic", lambda: next(clock))
    result = run_tool("close_application", name="notepad").result
    assert not result.ok and result.error == "still_open" and "still open" in result.message
    assert result.data["still_open"] == ["notes.txt - Notepad"]


def test_close_chrome_profile_picker_that_is_not_an_alt_tab_window(desktop: FakeDesktop) -> None:
    # Chrome's "Who's using Chrome?" picker: visible, titled, but flagged as a tool window
    desktop.windows = [W(9, 300, "Google Chrome", "chrome.exe", tool_window=True),
                       W(10, 300, "", "chrome.exe"), W(11, 400, "Hidden", "chrome.exe", cloaked=True)]
    result = run_tool("close_application", name="google chrome").result
    assert result.ok and desktop.closed == [9]  # untitled and cloaked windows are never touched


def test_close_app_running_only_in_background(desktop: FakeDesktop, monkeypatch) -> None:
    desktop.windows = []
    result = run_tool("close_application", name="chrome").result
    assert result.error == "no_window" and "force-quit" in result.message
    monkeypatch.setattr(applications, "_process_running", lambda names: False)
    assert run_tool("close_application", name="chrome").result.error == "not_running"


def test_window_control(desktop: FakeDesktop) -> None:
    assert run_tool("window_control", action="focus", name="notepad").result.ok and desktop.focused == [3]
    assert run_tool("window_control", action="minimize", name="chrome").result.ok
    assert [h for h, _ in desktop.shown] == [1, 2]
    assert run_tool("window_control", action="maximize", name="youtube").result.ok  # title match
    assert run_tool("window_control", action="minimize_all").result.ok
    assert run_tool("window_control", action="focus").result.error == "missing_target"


# ---------------------------------------------------------------------- web


@pytest.mark.parametrize(
    ("site", "url"),
    [("YouTube", "https://www.youtube.com"), ("github.com", "https://github.com"),
     ("https://docs.python.org/3/", "https://docs.python.org/3/"), ("the gmail", "https://mail.google.com")],
)
def test_website_urls(site: str, url: str) -> None:
    assert website_url(site) == url


@pytest.mark.parametrize("site", ["javascript:alert(1)", "file:///C:/Windows/System32", "ftp://x.com", "hello world", "C:\\secret.txt"])
def test_dangerous_or_invalid_urls_rejected(site: str) -> None:
    with pytest.raises(ToolError):
        website_url(site)


def test_open_website_and_search(monkeypatch, desktop, start_menu) -> None:
    opened: list[str] = []
    monkeypatch.setattr(applications.webbrowser, "open", lambda url, new=0: opened.append(url) or True)
    assert run_tool("open_website", site="youtube").result.ok
    result = run_tool("search_in_browser", query="python tutorials & tips", engine="youtube").result
    assert opened == ["https://www.youtube.com", "https://www.youtube.com/results?search_query=python+tutorials+%26+tips"]
    assert "haven't read the results" in result.message


def test_web_tools_refused_offline(monkeypatch) -> None:
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx(offline_mode=True))
    assert asyncio.run(ex.execute(ToolCall("open_website", {"site": "youtube"}))).result.error == "offline"


# ------------------------------------------------------------------ volume / power


def test_volume(desktop: FakeDesktop) -> None:
    assert run_tool("volume_control", action="get").result.message == "Volume is 40%."
    assert run_tool("volume_control", action="up").result.data["volume"] == 50
    assert run_tool("volume_control", action="down", step=50).result.data["volume"] == 0
    assert run_tool("volume_control", action="set", level=35).result.ok and desktop.volume == 35
    run_tool("volume_control", action="mute")
    assert desktop.muted
    run_tool("volume_control", action="set", level=60)
    assert not desktop.muted  # raising the volume unmutes
    assert run_tool("volume_control", action="set", level=150).result.error == "invalid_arguments"
    assert run_tool("volume_control", action="set").result.error == "missing_level"


def test_lock_runs_without_confirmation(desktop: FakeDesktop) -> None:
    assert run_tool("power_control", confirm=False, action="lock").result.ok and desktop.locked == 1


@pytest.mark.parametrize("action", ["restart", "shutdown", "sign_out"])
def test_restart_shutdown_signout_need_confirmation(desktop: FakeDesktop, action: str) -> None:
    execution = run_tool("power_control", confirm=False, action=action)
    assert execution.result.error == "declined" and desktop.shutdowns == [] and execution.level == RiskLevel.DANGEROUS
    assert run_tool("power_control", confirm=True, action=action).result.ok
    for _, timer in desktop.timers:
        timer.fire()
    assert desktop.shutdowns[0] == (action, 0)  # after the countdown: /t 0, never the forcing /t N


def test_restart_question_and_cancel(desktop: FakeDesktop) -> None:
    confirmer = ScriptedConfirmer(True)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx(), confirmer=confirmer)
    asyncio.run(ex.execute(ToolCall("power_control", {"action": "restart"})))
    assert confirmer.requests[0].summary == "Restart the computer in 10 seconds? Open work will be saved first."
    assert desktop.timers and desktop.timers[0][0] == 10
    result = run_tool("power_control", confirm=False, action="cancel_shutdown").result
    assert result.ok and "Cancelled" in result.message
    desktop.timers[0][1].fire()
    assert desktop.shutdowns == []  # the countdown was cancelled: nothing happened
    assert run_tool("power_control", action="cancel_shutdown").result.ok  # nothing pending: asks Windows
    assert desktop.shutdowns[-1] == ("cancel", 0)


def test_shutdown_argv_is_fixed(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    winapi.run_shutdown("restart", 9999)
    argv, kw = calls[0]
    assert argv[0].lower().endswith(os.path.join("system32", "shutdown.exe")) and argv[1:] == ["/r", "/t", "3600"]
    assert kw["shell"] is False


# ------------------------------------------------------------------ system / processes


def test_system_status_real_readings() -> None:
    result = run_tool("system_status", aspect="all").result
    assert result.ok and 0 <= result.data["cpu_percent"] <= 100 and result.data["ram_total_gb"] > 0
    assert "CPU" in result.message and "RAM" in result.message
    assert run_tool("system_status", aspect="battery").result.ok


def test_list_processes_real() -> None:
    result = run_tool("list_processes", sort_by="memory", limit=3).result
    rows = result.data["processes"]
    assert result.ok and 1 <= len(rows) <= 3 and rows == sorted(rows, key=lambda r: r["memory_mb"], reverse=True)


def test_list_running_apps_uses_windows(desktop: FakeDesktop, monkeypatch) -> None:
    monkeypatch.setattr(system, "_process_table", lambda sort: [
        {"name": "chrome.exe", "processes": 12, "cpu_percent": 3.0, "memory_mb": 900},
        {"name": "svchost.exe", "processes": 80, "cpu_percent": 1.0, "memory_mb": 500},
        {"name": "notepad.exe", "processes": 1, "cpu_percent": 0.0, "memory_mb": 20},
    ])
    rows = run_tool("list_processes", apps_only=True).result.data["processes"]
    assert [r["name"] for r in rows] == ["chrome.exe", "notepad.exe"] and rows[0]["windows"][0] == "Inbox - Google Chrome"


def test_end_process_protected_is_blocked_before_asking() -> None:
    confirmer = ScriptedConfirmer(True)
    ex = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx(), confirmer=confirmer)
    for target in ({"name": "lsass"}, {"name": "csrss.exe"}, {"pid": os.getpid()}):
        execution = asyncio.run(ex.execute(ToolCall("end_process", target)))
        assert execution.result.error == "blocked", target
    assert confirmer.requests == []


def test_end_process_needs_confirmation_and_works() -> None:
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert run_tool("end_process", confirm=False, pid=child.pid).result.error == "declined"
        assert child.poll() is None
        result = run_tool("end_process", confirm=True, pid=child.pid).result
        assert result.ok and child.wait(5) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_end_process_argument_rules() -> None:
    assert run_tool("end_process").result.error == "invalid_arguments"
    assert run_tool("end_process", name="a", pid=5).result.error == "invalid_arguments"
    assert run_tool("end_process", name="definitely-not-running-xyz").result.error in ("not_running", "declined")


def test_non_windows_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(winapi.sys, "platform", "linux")
    assert run_tool("volume_control", action="get").result.error == "unsupported_os"


def test_close_store_app_with_unguessable_process_name(desktop: FakeDesktop) -> None:
    desktop.windows = [W(20, 900, "WhatsApp", "whatsapp.root.exe", minimized=True)]
    result = run_tool("close_application", name="whatsapp").result
    assert result.ok and desktop.closed == [20]


def test_close_store_app_reported_as_cloaked(desktop: FakeDesktop) -> None:
    desktop.windows = [W(21, 900, "WhatsApp", "whatsapp.root.exe", cloaked=True),
                       W(22, 901, "Settings", "applicationframehost.exe", cloaked=True)]
    result = run_tool("close_application", name="WhatsApp").result
    assert result.ok and desktop.closed == [21]  # never an unrelated cloaked window


def test_prefix_process_check(monkeypatch) -> None:
    fake = [SimpleNamespace(info={"name": "WhatsApp.Root.exe"})]
    monkeypatch.setattr(applications.psutil, "process_iter", lambda attrs=None: iter(fake))
    assert applications._process_running(("whatsapp",))
    assert not applications._process_running(("whatsapp.exe",))
