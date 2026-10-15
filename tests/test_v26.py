"""Jarvis 2.6: full voice control, learning from mistakes, nicknames, the window you're in, routines on
events, the PC watcher, file search, Kokoro, liked answers, WhatsApp, self-healing."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.memory.database import Database
from app.tools.base import NoArgs, ToolContext, ToolError


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


def known(name):
    return {"visual studio code": "Visual Studio Code", "chrome": "Google Chrome"}.get(name.lower())


def setting(name):
    from app.agent.conversation import _known_setting

    return _known_setting(name)


@pytest.mark.parametrize("said, tool, arguments", [
    ("call Visual Studio Code 'code'", "set_nickname", {"nickname": "code", "target": "Visual Studio Code"}),
    ("my university means diu.edu.bd", "set_nickname", {"nickname": "my university", "target": "diu.edu.bd"}),
    ("close this", "this_window", {"action": "close"}),
    ("minimize this window", "this_window", {"action": "minimize"}),
    ("click", "mouse_control", {"action": "click"}),
    ("double click", "mouse_control", {"action": "double_click"}),
    ("move mouse left 200", "mouse_control", {"action": "move", "direction": "left", "pixels": 200}),
    ("scroll down", "scroll_screen", {"direction": "down", "amount": 5}),
    ("start dictation", "dictation", {"action": "start"}),
    ("open your settings", "jarvis_control", {"action": "open_settings"}),
    ("open memory", "jarvis_control", {"action": "open_memory"}),
    ("close the dialog", "jarvis_control", {"action": "close_dialog"}),
    ("hide yourself", "jarvis_control", {"action": "hide_window"}),
    ("turn on screen capture", "change_setting", {"setting": "screen capture", "value": "on"}),
    ("set voice speed to 1.2", "change_setting", {"setting": "voice speed", "value": "1.2"}),
    ("accept the suggestion", "memory_suggestion", {"action": "accept"}),
    ("review your mistakes", "review_mistakes", {}),
    ("accept all fixes", "accept_fix", {"which": "all"}),
    ("send whatsapp to Rafi saying I'm on my way", "whatsapp_message", {"name": "Rafi", "message": "I'm on my way"}),
    ("clean temp files", "clean_temp_files", {}),
    ("new tab", "app_command", {"command": "new tab"}),
    ("save as pdf", "app_command", {"command": "save as pdf"}),
    ("copy", "app_command", {"command": "copy"}),
])
def test_voice_only_commands(said, tool, arguments) -> None:
    call = match_shortcut(said, known, known_setting=setting)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, arguments)


@pytest.mark.parametrize("said", ["the weather is nice today", "close it", "open", "turn off the fan",
                                  "what does this word mean"])
def test_not_voice_control(said) -> None:
    call = match_shortcut(said, known, known_setting=setting)
    assert call is None or call.name not in ("set_nickname", "this_window", "change_setting", "app_command")


# ------------------------------------------------------------------ nicknames


def test_nicknames(tmp_path: Path, monkeypatch) -> None:
    from app.agent.aliases import AliasStore, call_for
    from app.tools import voice_control

    monkeypatch.setattr("app.tools.applications.known_application", lambda name, ctx: known(name))
    ctx = ToolContext(Settings(), paths=SimpleNamespace(data_dir=tmp_path))
    assert "now opens" in voice_control.set_nickname(voice_control.NicknameArgs(nickname="code",
                                                                             target="visual studio code"), ctx).message
    voice_control.set_nickname(voice_control.NicknameArgs(nickname="My University", target="diu.edu.bd"), ctx)
    voice_control.set_nickname(voice_control.NicknameArgs(nickname="blue", target="bluetooth"), ctx)
    store = AliasStore(tmp_path)
    assert call_for(store.get("code")).arguments == {"name": "visual studio code"}
    assert call_for(store.get("my university")).name == "open_website"
    assert call_for(store.get("BLUE")).name == "open_settings_page"
    with pytest.raises(ToolError, match="don't know"):
        voice_control.set_nickname(voice_control.NicknameArgs(nickname="x", target="flux capacitor"), ctx)
    alias = lambda name: call_for(store.get(name)) if store.get(name) else None  # noqa: E731
    assert match_shortcut("code", lambda n: None, known_alias=alias).arguments == {"name": "visual studio code"}
    assert match_shortcut("open my university", lambda n: None, known_alias=alias).name == "open_website"
    assert voice_control.remove_nickname(voice_control.NicknameNameArgs(nickname="code"), ctx).ok


# ------------------------------------------------------------------ learning


def test_review_suggests_fixes_and_accepting_learns_them(tmp_path: Path) -> None:
    from app.agent import learning
    from app.agent.aliases import AliasStore
    from app.tools import voice_control

    database = db(tmp_path)
    for _ in range(3):
        learning.record(database, "not_found", "open visual code studio")
    learning.record(database, "cancelled", "restart my pc")
    fixes = learning.review(database, {"Visual Studio Code": "app", "YouTube": "site"})
    assert fixes and fixes[0].target == "Visual Studio Code" and fixes[0].times == 3
    ctx = ToolContext(Settings(), database=database, paths=SimpleNamespace(data_dir=tmp_path))
    ctx_candidates = voice_control._candidates
    try:
        voice_control._candidates = lambda _ctx: {"Visual Studio Code": "app"}
        result = voice_control.review_mistakes(NoArgs(), ctx)
    finally:
        voice_control._candidates = ctx_candidates
    assert "1 cancelled" in result.message and "accept fix 1" in result.message
    voice_control.accept_fix(voice_control.FixArgs(which="1"), ctx)
    assert AliasStore(tmp_path).get("visual code studio")["target"] == "Visual Studio Code"


def test_liked_answers_become_examples(tmp_path: Path) -> None:
    from app.agent import learning

    database = db(tmp_path)
    learning.save_example(database, "explain black holes simply", "A black hole is where gravity is so strong...")
    found = learning.similar_examples(database, "explain black holes to me")
    assert found and "gravity" in found[0][1]
    assert learning.similar_examples(database, "open chrome") == []
    assert "liked before" in learning.examples_prompt(found)


def test_good_answer_is_saved_and_bad_answer_noted(tmp_path: Path) -> None:
    import asyncio

    from app.agent import learning
    from app.agent.conversation import ConversationManager

    database = db(tmp_path)
    tools = SimpleNamespace(context=SimpleNamespace(database=database, paths=None), registry={})
    conv = ConversationManager(None, Settings(), None, tools=tools)
    conv._last_exchange = ("what is python", "Python is a programming language.")
    reply = asyncio.run(conv.send("good answer"))
    assert "answer like that" in reply.text and learning.similar_examples(database, "what is python")
    asyncio.run(conv.send("bad answer"))
    assert learning.summary(database).get("bad_answer") == 1


# ------------------------------------------------------------------ the window you're in, app shortcuts


def test_app_commands_follow_the_app() -> None:
    from app.tools.voice_control import app_family, keys_for

    chrome = SimpleNamespace(process_name="chrome.exe", title="Inbox - Gmail - Google Chrome")
    youtube = SimpleNamespace(process_name="msedge.exe", title="Lo-fi beats - YouTube - Edge")
    word = SimpleNamespace(process_name="WINWORD.EXE", title="Thesis.docx - Word")
    assert keys_for("new tab", app_family(chrome)) == "ctrl+t"
    assert keys_for("full screen", app_family(youtube)) == "f" and keys_for("new tab", app_family(youtube)) == "ctrl+t"
    assert keys_for("save as pdf", app_family(word)) == "f12"
    assert keys_for("copy", app_family(word)) == "ctrl+c"
    assert keys_for("launch missiles", "any") is None


def test_app_command_presses_keys_in_your_window(monkeypatch) -> None:
    from app.tools import voice_control, winapi

    pressed: list[str] = []
    window = SimpleNamespace(hwnd=5, pid=1, title="Docs - Google Chrome", process_name="chrome.exe")
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "target_window", lambda: window)
    monkeypatch.setattr(winapi, "foreground_window", lambda: 5)
    monkeypatch.setattr(winapi, "press_combo", pressed.append)
    result = voice_control.app_command(voice_control.AppCommandArgs(command="next tab"), ToolContext(Settings()))
    assert pressed == ["ctrl+tab"] and result.data["app"] == "browser"
    window.title = "KeePass - passwords"  # blocked windows are never touched
    with pytest.raises(ToolError):
        voice_control.app_command(voice_control.AppCommandArgs(command="copy"), ToolContext(Settings()))


# ------------------------------------------------------------------ settings, dictation


def test_settings_by_voice() -> None:
    from app.tools import voice_control

    changed: list[tuple[str, str]] = []
    ctx = ToolContext(Settings(), apply_setting=lambda field, value: changed.append((field, value)))
    voice_control.change_setting(voice_control.SettingArgs(setting="voice speed", value="1.3 please"), ctx)
    voice_control.change_setting(voice_control.SettingArgs(setting="language", value="Bangla"), ctx)
    voice_control.change_setting(voice_control.SettingArgs(setting="speak replies", value="off"), ctx)
    assert changed == [("tts_speed", "1.3"), ("voice_language", "bn"), ("tts_enabled", "false")]
    with pytest.raises(ToolError, match="can't change"):
        voice_control.change_setting(voice_control.SettingArgs(setting="the admin password", value="x"), ctx)
    tool = next(t for t in voice_control.TOOLS if t.name == "change_setting")
    assert not tool.direct_without_confirmation(voice_control.SettingArgs(setting="screen capture", value="on"), ctx)
    assert tool.direct_without_confirmation(voice_control.SettingArgs(setting="voice speed", value="1"), ctx)


def test_dictation_types_what_you_say(monkeypatch) -> None:
    from app.tools import winapi
    from app.ui.controller import AssistantController

    typed: list[str] = []
    monkeypatch.setattr(winapi, "target_window", lambda: None)
    monkeypatch.setattr(winapi, "type_unicode", typed.append)
    fake = SimpleNamespace(ctx=SimpleNamespace(settings=Settings()), dictating=True,
                           notice=SimpleNamespace(emit=lambda text: None), tts=SimpleNamespace(speak=lambda text: None))
    fake._DICTATION_STOP, fake._DICTATION_WORDS = AssistantController._DICTATION_STOP, AssistantController._DICTATION_WORDS
    dictate = AssistantController._dictate
    assert dictate(fake, "Dear Sir comma thank you full stop new line Regards")
    assert typed == ["Dear Sir, thank you.\nRegards "]
    assert dictate(fake, "my password is hunter2") and len(typed) == 1  # looks like a password: not typed
    assert not dictate(fake, "Stop dictation.") and fake.dictating is False


# ------------------------------------------------------------------ WhatsApp, temp files, finding files


def test_whatsapp_message(tmp_path: Path, monkeypatch) -> None:
    from app.tools import phone, voice_control, winapi

    started: list[str] = []
    pressed: list[str] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "start_file", started.append)
    window = SimpleNamespace(hwnd=9, process_name="WhatsApp.exe", title="WhatsApp")
    monkeypatch.setattr(winapi, "visible_windows", lambda: [window])
    monkeypatch.setattr(winapi, "foreground_window", lambda: 9)
    monkeypatch.setattr(winapi, "focus_window", lambda hwnd: True)
    monkeypatch.setattr(winapi, "press_combo", pressed.append)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ctx = ToolContext(Settings(), paths=SimpleNamespace(data_dir=tmp_path))
    phone.save_contact(phone.SaveContactArgs(name="Rafi", number="01812345678"), ctx)
    result = voice_control.whatsapp_message(voice_control.WhatsAppArgs(name="rafi", message="I'm on my way"), ctx)
    assert started == ["whatsapp://send?phone=8801812345678&text=I%27m%20on%20my%20way"] and pressed == ["enter"]
    assert result.ok
    tool = next(t for t in voice_control.TOOLS if t.name == "whatsapp_message")
    assert tool.direct_without_confirmation is None  # sending a message always asks (by voice)


def test_clean_temp_files(tmp_path: Path) -> None:
    from app.tools import voice_control

    temp = tmp_path / "Temp"
    temp.mkdir()
    (temp / "old.tmp").write_bytes(b"x" * 1000)
    (temp / "new.tmp").write_bytes(b"y")
    old = time.time() - 5 * 24 * 3600
    os.utime(temp / "old.tmp", (old, old))
    result = voice_control.clean_temp_files(NoArgs(), ToolContext(Settings()), folder=temp)
    assert result.data["removed"] == 1 and not (temp / "old.tmp").exists() and (temp / "new.tmp").exists()
    with pytest.raises(ToolError):
        voice_control.clean_temp_files(NoArgs(), ToolContext(Settings()), folder=tmp_path)  # not a Temp folder


def test_find_my_files(tmp_path: Path) -> None:
    from app.tools import voice_control

    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Downloads" / "Machine_Learning_Intro.pdf").write_bytes(b"%PDF")
    (tmp_path / "Documents" / "thesis draft v3.docx").write_bytes(b"x")
    (tmp_path / "Documents" / "old thesis.docx").write_bytes(b"x")
    old = time.time() - 40 * 24 * 3600
    os.utime(tmp_path / "Documents" / "old thesis.docx", (old, old))
    roots = [tmp_path / "Downloads", tmp_path / "Documents"]
    ctx = ToolContext(Settings())
    pdf = voice_control.find_my_files(voice_control.FindArgs(about="machine learning", kind="pdf", within_days=7), ctx,
                                      roots=roots)
    assert pdf.data["files"][0].endswith("Machine_Learning_Intro.pdf")
    thesis = voice_control.find_my_files(voice_control.FindArgs(about="thesis", kind="word"), ctx, roots=roots)
    assert thesis.data["files"][0].endswith("thesis draft v3.docx")  # newest first
    none = voice_control.find_my_files(voice_control.FindArgs(about="holiday photos", kind="photo"), ctx, roots=roots)
    assert none.data["files"] == []


# ------------------------------------------------------------------ routines on events, PC watcher


def test_event_triggers() -> None:
    from app.automation.scheduler import Scheduler
    from app.automation.triggers import EventTrigger

    home = EventTrigger(event="wifi", value="Home WiFi")
    assert Scheduler._event_due(home, {"wifi": ""}, {"wifi": "Home WiFi"})
    assert not Scheduler._event_due(home, {"wifi": "Home WiFi"}, {"wifi": "Home WiFi"})  # only when it joins
    assert not Scheduler._event_due(home, {"wifi": ""}, {"wifi": "Cafe"})
    spotify = EventTrigger(event="app_opened", value="spotify.exe")
    assert Scheduler._event_due(spotify, {"apps": {"chrome"}}, {"apps": {"chrome", "spotify"}})
    usb = EventTrigger(event="usb")
    assert Scheduler._event_due(usb, {"usb": set()}, {"usb": {"E:\\"}}) and "USB" in usb.describe()


def test_schedule_on_an_event() -> None:
    from app.tools.automation import ScheduleArgs, _trigger_for

    trigger = _trigger_for(ScheduleArgs(name="home", steps=[{"type": "notify", "message": "Welcome home"}],
                                        when_event="wifi", event_value="Home WiFi"), ToolContext(Settings()))
    assert trigger.type == "event" and trigger.value == "Home WiFi"


def test_pc_watcher(tmp_path: Path) -> None:
    from app.agent.watcher import Watcher

    clock = {"t": 1000.0}
    usage = {"chrome": 10.0}
    watcher = Watcher(downloads=tmp_path, clock=lambda: clock["t"], disk=lambda: 6.0, processes=lambda: dict(usage),
                      gpu_temperature=lambda: 60.0)
    first = watcher.check()
    assert len(first) == 1 and "almost full" in first[0]
    assert watcher.check() == []  # said once
    usage["miner"] = 95.0
    watcher.check()
    clock["t"] += 180
    assert any("miner" in m for m in watcher.check())
    big = tmp_path / "movie.mp4"
    with open(big, "wb") as handle:
        handle.truncate(60 * 1024 * 1024)
    assert any("movie.mp4" in m for m in watcher.check())
    watcher.gpu_temperature = lambda: 91.0
    assert any("hot" in m for m in watcher.check())


# ------------------------------------------------------------------ Kokoro


def test_kokoro_download_is_verified(tmp_path: Path) -> None:
    import httpx

    from app.tts import kokoro
    from app.tts.base import TTSError

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not the real model"))
    with pytest.raises(TTSError, match="checksum"):
        kokoro.download(tmp_path, transport=transport)
    assert not any(tmp_path.iterdir())  # the bad file was deleted
    assert not kokoro.is_installed(tmp_path)


def test_kokoro_is_used_offline_once_downloaded(tmp_path: Path, monkeypatch) -> None:
    from app.tts import kokoro, manager

    loaded: list[str] = []

    class Fake(kokoro.KokoroTTS):
        def load(self):
            loaded.append(self.voice)

    monkeypatch.setattr(kokoro, "KokoroTTS", Fake)
    monkeypatch.setattr(kokoro, "is_installed", lambda folder: True)
    engine, problem = manager.create_engine(Settings(tts_engine="kokoro", tts_kokoro_voice="am_michael"),
                                            tmp_path / "piper")
    assert problem is None and loaded == ["am_michael"] and "kokoro" in engine.name or "bilingual" in engine.name.lower()
