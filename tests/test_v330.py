"""3.3.0: YouTube control and a song queue, window layouts, run my project / commit, unread WhatsApp chats,
your own voice commands from a form, and the browser self-check."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut
from app.tools import applications, browser_tabs, developer, layouts, messages, winapi, youtube
from app.tools.base import ToolContext, ToolError
from app.config.settings import Settings
from tests.test_developer import FakeExecutor, make
from tests.test_windows_tools import FakeDesktop, W, ctx, run_tool, start_menu  # noqa: F401 (fixture)

KNOWN = {"chrome": "Google Chrome", "vs code": "Visual Studio Code", "steam": "Steam", "whatsapp": "WhatsApp"}


def said(text: str):
    call = match_shortcut(text, lambda n: KNOWN.get(n.lower()))
    return (call.name, dict(call.arguments)) if call else None


# ------------------------------------------------------------------ what you say


@pytest.mark.parametrize("text, expected", [
    ("skip the ad", ("youtube_control", {"action": "skip_ad"})),
    ("like this song", ("youtube_control", {"action": "like"})),
    ("forward 30 seconds", ("youtube_control", {"action": "forward", "seconds": 30})),
    ("go back ten seconds", ("youtube_control", {"action": "back", "seconds": 10})),
    ("rewind one minute", ("youtube_control", {"action": "back", "seconds": 60})),
    ("start the song again", ("youtube_control", {"action": "restart"})),
    ("what song is this", ("youtube_control", {"action": "now_playing"})),
    ("after this play Arijit Singh", ("youtube_control", {"action": "queue", "query": "Arijit Singh"})),
    ("play tum hi ho next", ("youtube_control", {"action": "queue", "query": "tum hi ho"})),
    ("add Kesariya to the queue", ("youtube_control", {"action": "queue", "query": "Kesariya"})),
    ("what's in the queue", ("youtube_control", {"action": "show_queue"})),
    ("clear the queue", ("youtube_control", {"action": "clear_queue"})),
    ("play this again", ("youtube_control", {"action": "repeat"})),
    ("turn on captions", ("youtube_control", {"action": "captions"})),
    ("make the video full screen", ("youtube_control", {"action": "fullscreen"})),
    ("mute the video", ("youtube_control", {"action": "mute_video"})),
    ("put Chrome on the left", ("arrange_windows", {"action": "left", "app": "chrome"})),
    ("move this window to the right side", ("arrange_windows", {"action": "right"})),
    ("snap vs code top right", ("arrange_windows", {"action": "top_right", "app": "vs code"})),
    ("Chrome on the left and VS Code on the right",
     ("arrange_windows", {"action": "side_by_side", "app": "chrome", "second_app": "vs code"})),
    ("put chrome and vs code side by side",
     ("arrange_windows", {"action": "side_by_side", "app": "chrome", "second_app": "vs code"})),
    ("move Chrome to my second screen", ("arrange_windows", {"action": "next_screen", "app": "chrome"})),
    ("maximize everything", ("arrange_windows", {"action": "maximize_all"})),
    ("save this layout as work", ("arrange_windows", {"action": "save", "layout": "work"})),
    ("save this as my coding layout", ("arrange_windows", {"action": "save", "layout": "coding"})),
    ("work layout", ("arrange_windows", {"action": "restore", "layout": "work"})),
    ("restore my work layout", ("arrange_windows", {"action": "restore", "layout": "work"})),
    ("list my layouts", ("arrange_windows", {"action": "list"})),
    ("run my project", ("run_project", {})),
    ("commit my changes with message fix login", ("git_commit", {"message": "fix login", "push": False})),
    ("commit with message fix login and push", ("git_commit", {"message": "fix login", "push": True})),
    ("what's this error", ("describe_screen", {"question": "What does the error on the screen mean, and how do I "
                                                           "fix it?"})),
    ("any new WhatsApp messages", ("read_whatsapp", {})),
    ("summarize my unread emails", ("read_emails", {"unread_only": True})),
    ("next song", ("media_control", {"action": "next"})),
])
def test_commands(text, expected) -> None:
    assert said(text) == expected


@pytest.mark.parametrize("text", ["put the cup on the left", "move the mouse to the left", "commit and push",
                                  "what is next", "keyboard layout", "go back"])
def test_not_these_commands(text) -> None:
    result = said(text)
    assert result is None or result[0] not in ("arrange_windows", "youtube_control", "git_commit")


# ------------------------------------------------------------------ YouTube


@pytest.mark.parametrize("title, clean", [
    ("(3) Tum Hi Ho - Arijit Singh - YouTube - Google Chrome", "Tum Hi Ho - Arijit Singh"),
    ("Kesariya – YouTube", "Kesariya"), ("YouTube", "YouTube"),
])
def test_clean_title(title, clean) -> None:
    assert youtube.clean_title(title) == clean


def test_seek_keys() -> None:
    assert youtube._seek_keys(30, True) == ["l", "l", "l"]
    assert youtube._seek_keys(15, False) == ["j", "left"]


class Tab:
    """A YouTube tab in Chrome; keys and button presses are recorded."""

    def __init__(self, monkeypatch, title: str | None = "Song - YouTube", buttons=()) -> None:
        self.title, self.keys, self.pressed, self.front, self.loaded = title, [], [], 0, []
        self.buttons = list(buttons)
        monkeypatch.setattr(winapi, "is_windows", lambda: True)
        monkeypatch.setattr(winapi, "foreground_window", lambda: self.front)
        monkeypatch.setattr(winapi, "foreground_is_ours", lambda: False)
        monkeypatch.setattr(winapi, "force_foreground", lambda h: setattr(self, "front", h) or True)
        monkeypatch.setattr(winapi, "press_combo", self.keys.append)
        monkeypatch.setattr(winapi, "seconds_since_input", lambda: 10.0)
        monkeypatch.setattr(applications, "browser_target", lambda c, b=None: SimpleNamespace(
            target=r"C:\Apps\chrome.exe", display_name="Google Chrome"))
        monkeypatch.setattr(browser_tabs, "site_titles", lambda exe, site: [self.title] if self.title else [])
        monkeypatch.setattr(browser_tabs, "find_site_tab", lambda exe, site, prefer=None: 7 if self.title and (
            self.front == 7 or winapi.force_foreground(7)) else None)
        monkeypatch.setattr(browser_tabs, "focus_page", lambda h: True)
        monkeypatch.setattr(browser_tabs, "click_page_button", self._click)
        monkeypatch.setattr(browser_tabs, "reuse_tab", self._reuse)
        monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
        youtube.PLAYER.__init__()

    def _click(self, hwnd, pattern):
        import re

        name = next((b for b in self.buttons if re.search(pattern, b, re.I)), None)
        if name:
            self.pressed.append(name)
        return name

    def _reuse(self, exe, site, url, step_aside=None, prefer=None):
        if not self.title:
            return False
        self.loaded.append(url)
        self.title = f"{url[-4:]} - YouTube"
        return True


def act(**arguments):
    return asyncio.run(youtube.youtube_control(youtube.YouTubeArgs(**arguments), ctx()))


def test_skip_ad_and_like_press_the_real_buttons(monkeypatch) -> None:
    tab = Tab(monkeypatch, buttons=["Skip Ad", "like this video along with 1,234 other people"])
    assert act(action="skip_ad").message == "Skipped the ad."
    assert act(action="like").message == "Liked it."
    assert tab.pressed == ["Skip Ad", "like this video along with 1,234 other people"]


def test_no_skip_button_is_said_honestly(monkeypatch) -> None:
    Tab(monkeypatch, buttons=["Subscribe"])
    result = act(action="skip_ad")
    assert not result.ok and "Skip button" in result.message


def test_keys_go_to_the_tab(monkeypatch) -> None:
    tab = Tab(monkeypatch)
    act(action="forward", seconds=20)
    act(action="captions")
    assert tab.keys == ["l", "l", "c"]


def test_keys_stop_if_the_focus_moves(monkeypatch) -> None:
    tab = Tab(monkeypatch)
    monkeypatch.setattr(winapi, "force_foreground", lambda h: True)  # Windows refused: something else in front
    monkeypatch.setattr(browser_tabs, "find_site_tab", lambda exe, site, prefer=None: 7)
    with pytest.raises(ToolError, match="lost the focus"):
        asyncio.run(youtube.youtube_control(youtube.YouTubeArgs(action="restart"), ctx()))
    assert tab.keys == []


def test_now_playing_and_no_tab(monkeypatch) -> None:
    tab = Tab(monkeypatch, title="(2) Kesariya - Arijit Singh - YouTube")
    assert act(action="now_playing").message == "Now playing: Kesariya - Arijit Singh."
    tab.title = None
    assert not act(action="now_playing").ok


def test_queue_plays_after_the_song_changes(monkeypatch) -> None:
    tab = Tab(monkeypatch, title="First - YouTube")

    async def video(query):
        return "abcDEF12345"

    from app.tools import assistant

    monkeypatch.setattr(assistant, "first_youtube_video", video)
    monkeypatch.setattr(youtube.Player, "watch", lambda self: None)  # driven by hand below
    result = act(action="queue", query="Kesariya")
    assert result.ok and "after this song" in result.message
    assert act(action="show_queue").message == "Next up: Kesariya."
    player = youtube.PLAYER
    baseline = player.check_once(None)
    assert baseline == "First" and player.check_once(baseline) == "First" and tab.loaded == []
    tab.title = "Autoplayed - YouTube"  # the song ended and YouTube moved on
    assert player.check_once(baseline) is None
    assert tab.loaded == ["https://www.youtube.com/watch?v=abcDEF12345"] and player.queue == []


def test_queue_waits_while_you_type(monkeypatch) -> None:
    tab = Tab(monkeypatch, title="First - YouTube")
    monkeypatch.setattr(youtube.Player, "watch", lambda self: None)
    youtube.PLAYER.exe = "chrome.exe"
    youtube.PLAYER.add(youtube.Song("https://www.youtube.com/watch?v=zzzzzzzzzzz", "Next one"))
    monkeypatch.setattr(winapi, "seconds_since_input", lambda: 0.3)
    tab.title = "Other - YouTube"
    assert youtube.PLAYER.check_once("First") == "First" and tab.loaded == []


def test_closing_the_tab_forgets_the_queue(monkeypatch) -> None:
    tab = Tab(monkeypatch)
    monkeypatch.setattr(youtube.Player, "watch", lambda self: None)
    youtube.PLAYER.exe = "chrome.exe"
    youtube.PLAYER.add(youtube.Song("u", "x"))
    tab.title = None
    youtube.PLAYER.check_once("Song")
    assert youtube.PLAYER.queue == []


def test_next_song_takes_the_queue_first(monkeypatch) -> None:
    tab = Tab(monkeypatch)
    monkeypatch.setattr(youtube.Player, "watch", lambda self: None)
    youtube.PLAYER.exe = "chrome.exe"
    youtube.PLAYER.add(youtube.Song("https://www.youtube.com/watch?v=qqqqqqqqqqq", "Kesariya"))
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    pressed = []
    monkeypatch.setattr(winapi, "press_key", pressed.append)
    from app.tools.assistant import MediaArgs, media_control

    assert media_control(MediaArgs(action="next"), ctx()).message == "Playing Kesariya from your queue."
    assert pressed == [] and tab.loaded[-1].endswith("qqqqqqqqqqq")
    media_control(MediaArgs(action="next"), ctx())  # empty queue: the media key as before
    assert len(pressed) == 1


def test_repeat_only_what_jarvis_started(monkeypatch) -> None:
    tab = Tab(monkeypatch, title="Tum Hi Ho - YouTube")
    monkeypatch.setattr(youtube.Player, "watch", lambda self: None)
    youtube.PLAYER.started("https://www.youtube.com/watch?v=aaaaaaaaaaa", "tum hi ho", "chrome.exe")
    assert act(action="repeat").ok and youtube.PLAYER.queue[0].url.endswith("aaaaaaaaaaa")
    youtube.PLAYER.clear()
    tab.title = "Something else - YouTube"  # autoplay moved on
    assert not act(action="repeat").ok


# ------------------------------------------------------------------ layouts


AREA = winapi.Rect(0, 0, 1920, 1040)


def test_places() -> None:
    assert layouts.place(AREA, "left") == winapi.Rect(0, 0, 960, 1040)
    assert layouts.place(AREA, "bottom_right") == winapi.Rect(960, 520, 1920, 1040)
    center = layouts.place(AREA, "center")
    assert center.width == 1344 and center.left == 288


def test_moving_to_another_screen_keeps_the_relative_place() -> None:
    second = winapi.Rect(1920, 0, 3200, 1000)
    moved = layouts.moved_to(winapi.Rect(0, 0, 960, 1040), AREA, second)
    assert moved.left == 1920 and moved.width == 640


class Screens:
    def __init__(self, monkeypatch, pc: FakeDesktop, rects: dict[int, winapi.Rect]) -> None:
        self.rects, self.moves = dict(rects), []
        self.screens = [winapi.Monitor(AREA, True), winapi.Monitor(winapi.Rect(1920, 0, 3840, 1040), False)]
        monkeypatch.setattr(winapi, "monitors", lambda: self.screens)
        monkeypatch.setattr(winapi, "window_rect", lambda h: self.rects.get(h, winapi.Rect(100, 100, 900, 700)))
        monkeypatch.setattr(winapi, "move_window", self._move)
        monkeypatch.setattr(winapi, "process_path", lambda pid: {100: r"C:\Apps\chrome.exe",
                                                                 200: r"C:\Apps\notepad.exe"}.get(pid))
        monkeypatch.setattr(winapi, "target_window", lambda exclude_pid=None: pc.windows[0])

    def _move(self, hwnd, rect) -> None:
        self.moves.append((hwnd, rect))
        self.rects[hwnd] = rect


def test_put_and_side_by_side(pc, monkeypatch) -> None:
    screens = Screens(monkeypatch, pc, {})
    assert run_tool("arrange_windows", action="left", app="notepad").result.ok
    assert screens.moves[-1] == (3, winapi.Rect(0, 0, 960, 1040))
    result = run_tool("arrange_windows", action="side_by_side", app="chrome", second_app="notepad").result
    assert result.ok and screens.moves[-2:] == [(1, winapi.Rect(0, 0, 960, 1040)), (3, winapi.Rect(960, 0, 1920, 1040))]
    assert run_tool("arrange_windows", action="left").result.ok  # "this window"


def test_next_screen(pc, monkeypatch) -> None:
    screens = Screens(monkeypatch, pc, {3: winapi.Rect(0, 0, 960, 1040)})
    result = run_tool("arrange_windows", action="next_screen", app="notepad").result
    assert result.ok and "screen 2" in result.message and screens.moves[-1][1].left == 1920
    screens.screens = screens.screens[:1]
    assert not run_tool("arrange_windows", action="next_screen", app="notepad").result.ok


def test_save_and_restore_a_layout(pc, monkeypatch, tmp_path) -> None:
    screens = Screens(monkeypatch, pc, {1: winapi.Rect(0, 0, 960, 1040), 3: winapi.Rect(960, 0, 1920, 1040)})
    pc.windows = [w for w in pc.windows if w.hwnd != 2]
    context = ctx()
    context.paths = SimpleNamespace(data_dir=tmp_path)
    saved = layouts.arrange_windows(layouts.LayoutArgs(action="save", layout="Work"), context)
    assert saved.ok and "'work' layout with 2 window(s)" in saved.message
    data = json.loads((tmp_path / "layouts.json").read_text())
    assert [e["process"] for e in data["work"]["windows"]] == ["chrome.exe", "notepad.exe"]
    screens.rects = {}
    screens.moves.clear()
    restored = layouts.arrange_windows(layouts.LayoutArgs(action="restore", layout="work layout"), context)
    assert restored.ok and "2 window(s) in place" in restored.message
    assert dict(screens.moves) == {1: winapi.Rect(0, 0, 960, 1040), 3: winapi.Rect(960, 0, 1920, 1040)}
    assert layouts.arrange_windows(layouts.LayoutArgs(action="list"), context).message == "Your layouts: work."
    missing = layouts.arrange_windows(layouts.LayoutArgs(action="restore", layout="gaming"), context)
    assert not missing.ok and "Your layouts: work" in missing.message


def test_restore_starts_apps_that_are_closed(pc, monkeypatch) -> None:
    Screens(monkeypatch, pc, {})
    monkeypatch.setattr(layouts, "_can_start", lambda path: bool(path))
    pc.windows = []

    def start(exe, arguments):
        pc.programs.append((exe, list(arguments)))
        pc.windows.append(W(5, 500, "Untitled - Notepad", "notepad.exe"))

    monkeypatch.setattr(winapi, "start_program", start)
    entries = [{"process": "notepad.exe", "path": r"C:\Apps\notepad.exe", "title": "notes - Notepad",
                "rect": [0, 0, 960, 1040], "maximized": False},
               {"process": "gone.exe", "path": "", "title": "x", "rect": [0, 0, 10, 10]}]
    moved, started, missing = layouts.restore(entries, wait_seconds=1, sleep=lambda s: None)
    assert (moved, started, missing) == (1, ["notepad"], ["gone"])


def test_layout_names_are_checked() -> None:
    assert layouts.layout_key("My Work layout") == "my work"
    with pytest.raises(ToolError):
        layouts.layout_key("../../evil")


# ------------------------------------------------------------------ run my project / commit


@pytest.mark.parametrize("files, command", [
    ({"package.json": json.dumps({"scripts": {"build": "x", "dev": "vite"}})}, "npm run dev"),
    ({"package.json": json.dumps({"scripts": {"start": "node ."}})}, "npm run start"),
    ({"manage.py": ""}, "python manage.py runserver"),
    ({"artisan": ""}, "php artisan serve"),
    ({"main.py": "", "app.py": ""}, "python main.py"),
    ({"Cargo.toml": ""}, "cargo run"),
])
def test_how_a_project_starts(tmp_path, files, command) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert developer.project_command(tmp_path) == command


def test_unknown_project_says_what_it_looked_for(tmp_path) -> None:
    with pytest.raises(ToolError, match="package.json"):
        developer.project_command(tmp_path)


def test_xampp_projects_open_on_localhost(tmp_path) -> None:
    folder = tmp_path / "xampp" / "htdocs" / "tms"
    folder.mkdir(parents=True)
    (folder / "index.php").write_text("<?php")
    assert developer._xampp_url(folder) == "http://localhost/tms/"
    assert developer._xampp_url(tmp_path) is None


def test_run_project_uses_the_normal_command_pipeline(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(developer, "resolve_program", lambda name: name)
    ex, context, confirmer, project = make(tmp_path)
    (project / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    from app.ai.base import ToolCall

    result = asyncio.run(ex.execute(ToolCall("run_project", {}))).result
    assert result.ok and "npm run dev" in result.message
    assert context.command_executor.calls[-1][:2] == ("background", ("npm", "run", "dev"))


def test_commit_asks_and_uses_plain_arguments(tmp_path, monkeypatch) -> None:
    class Git(FakeExecutor):
        def run(self, argv, cwd, timeout=120):
            self.calls.append(("run", tuple(argv), Path(cwd)))
            out = {"status": " M a.py\n M b.py", "commit": "[main 1a2b3c4] fix login\n 2 files changed"}.get(argv[1], "")
            from app.security.command_executor import CommandOutput

            return CommandOutput(tuple(argv), 0, out)

    ex, context, confirmer, project = make(tmp_path, executor=Git())
    from app.ai.base import ToolCall

    result = asyncio.run(ex.execute(ToolCall("git_commit", {"message": "fix login; rm -rf /"}))).result
    assert result.ok and result.message == "Committed 2 file(s) as 1a2b3c4: fix login; rm -rf /."
    runs = [c[1] for c in context.command_executor.calls]
    assert ("git", "add", "-A") in runs and ("git", "commit", "-m", "fix login; rm -rf /") in runs  # one argument
    assert confirmer.requests and "2 changed file(s)" in str(vars(confirmer.requests[-1]))
    push_level = developer.TOOLS[[t.name for t in developer.TOOLS].index("git_commit")].level_for(
        developer.CommitArgs(message="x", push=True), context)
    assert int(push_level) == 2


# ------------------------------------------------------------------ WhatsApp


@pytest.mark.parametrize("label, parsed", [
    ("Rafi, 2 unread messages, on my way, 10:32", ("Rafi", 2, "on my way")),
    ("Ammu 1 unread message Call me, Yesterday", ("Ammu 1", 1, "Call me")),
    ("Office group, Tanvir: meeting at 3, 09:15", None),
])
def test_chat_labels(label, parsed) -> None:
    result = messages.parse_chat(label)
    assert (result[1] if result else None) == (parsed[1] if parsed else None)
    if parsed:
        assert result[2] == parsed[2]


def test_read_whatsapp(monkeypatch) -> None:
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "top_level_windows", lambda: [W(4, 40, "WhatsApp", "whatsapp.root.exe")])
    monkeypatch.setattr(messages, "chat_labels", lambda hwnd: [
        "Rafi, 2 unread messages, on my way, 10:32", "Office, meeting at 3, 09:15",
        "Ammu, 1 unread message, my password is hunter2abc!, 08:00"])
    result = messages.read_whatsapp(None, ctx())
    assert result.ok and result.message.startswith("2 chat(s), 3 unread message(s): Rafi (2): on my way")
    assert "untrusted" in json.dumps(result.data)


def test_whatsapp_closed(monkeypatch) -> None:
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "top_level_windows", lambda: [])
    with pytest.raises(ToolError, match="isn't open"):
        messages.read_whatsapp(None, ctx())


# ------------------------------------------------------------------ your own commands


def test_build_steps() -> None:
    from app.automation.builder import build_steps

    steps = build_steps([("close_app", "Chrome"), ("open_app", "Steam"), ("volume", "70%"), ("say", "Game on"),
                         ("", "ignored")])
    assert [s.describe() for s in steps] == ["close_application(name='Chrome')", "open_application(name='Steam')",
                                             "volume_control(action='set', level=70)", 'say "Game on"']
    with pytest.raises(ToolError, match="0 to 100"):
        build_steps([("volume", "loud")])
    with pytest.raises(ToolError, match="needs a value"):
        build_steps([("open_app", " ")])


def test_saved_command_runs_by_its_name(tmp_path) -> None:
    from app.automation.builder import save_command
    from app.automation.tasks import AutomationStore
    from app.memory.database import Database

    database = Database(tmp_path / "a.db")
    database.initialize()
    context = ToolContext(Settings(), database=database)
    task = save_command(context, "Gaming time", [("open_app", "Steam"), ("volume", "70")])
    assert task.is_workflow and [s.tool for s in task.steps] == ["open_application", "volume_control"]
    with pytest.raises(ToolError, match="already"):
        save_command(context, "gaming time", [("open_app", "Steam")])
    assert AutomationStore(database).all()[0].name == "Gaming time"


def test_builder_dialog(qtbot=None) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.command_builder import CommandBuilderDialog

    dialog = CommandBuilderDialog(ToolContext(Settings()))
    dialog.name.setText("x")
    dialog.save()
    assert "2 to 40" in dialog.error.text() and dialog.saved is None


# ------------------------------------------------------------------ self-check


def test_browser_that_does_not_start_falls_back(pc, monkeypatch) -> None:
    pc.windows = [w for w in pc.windows if w.process_name != "chrome.exe"]
    opened = []
    monkeypatch.setattr(applications, "_open_url", opened.append)
    where = applications.open_url("https://github.com", ctx(web_browser="chrome"))
    assert opened == ["https://github.com"] and "didn't start" in where


@pytest.fixture()
def pc(tmp_path: Path, monkeypatch, start_menu) -> FakeDesktop:
    monkeypatch.setattr(applications, "_process_running", lambda names: True)
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return desktop
