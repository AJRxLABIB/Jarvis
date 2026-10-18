"""Full control by voice: nicknames, the window you're in, app shortcuts, the mouse, dictation,
Jarvis's own screens and settings, memory suggestions, WhatsApp, tidy-ups and file search.

Nothing here needs a click. Actions that change or send something still ask first - by voice
("yes" / "হ্যাঁ") - unless it is your own direct command and harmless.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.voice_control")


def _data_dir(ctx: ToolContext) -> Path:
    return ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"


# ------------------------------------------------------------------ nicknames

def _resolve_target(target: str, ctx: ToolContext) -> tuple[str, str]:
    """(kind, target) for what a nickname should open."""
    from app.tools.applications import known_application, site_key
    from app.tools.windows_settings import find_page

    text = target.strip()
    page = find_page(text, fuzzy=False)
    if page is not None and page[1] != "Settings":
        return "page", text
    store = ctx.get_automation_store() if hasattr(ctx, "get_automation_store") else None
    if store is not None:
        for task in store.all():
            if task.is_workflow and task.name.lower() == text.lower():
                return "workflow", task.name
    if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", text) or site_key(text):
        return "site", text
    app = known_application(text, ctx)
    if app is not None:
        return "app", text
    if page is not None:
        return "page", text
    raise ToolError(f"I don't know what '{target}' is: say an app, a website, a Settings page or a workflow.",
                    "unknown_target")


class NicknameArgs(ToolArgs):
    nickname: str = Field(min_length=1, max_length=40, description="The new name, e.g. 'code', 'my university'")
    target: str = Field(min_length=1, max_length=200,
                        description="What it opens: an app, website/URL, Settings page or workflow")


def set_nickname(args: NicknameArgs, ctx: ToolContext) -> ToolResult:
    from app.agent.aliases import AliasStore

    kind, target = _resolve_target(args.target, ctx)
    key = AliasStore(_data_dir(ctx)).set(args.nickname, kind, target)
    return ToolResult.success(f"Done: '{key}' now opens {target}.", nickname=key, kind=kind, target=target)


class NicknameNameArgs(ToolArgs):
    nickname: str = Field(min_length=1, max_length=40, description="The nickname to remove")


def remove_nickname(args: NicknameNameArgs, ctx: ToolContext) -> ToolResult:
    from app.agent.aliases import AliasStore

    if not AliasStore(_data_dir(ctx)).remove(args.nickname):
        raise ToolError(f"There is no nickname '{args.nickname}'.", "unknown")
    return ToolResult.success(f"Removed the nickname '{args.nickname}'.")


def list_nicknames(_: NoArgs, ctx: ToolContext) -> ToolResult:
    from app.agent.aliases import AliasStore

    items = AliasStore(_data_dir(ctx)).items()
    if not items:
        return ToolResult.success("You have no nicknames yet. Say for example: call Visual Studio Code 'code'.")
    text = "; ".join(f"{k} -> {v['target']}" for k, v in sorted(items.items()))
    return ToolResult.success(f"{len(items)} nickname(s): {text}.", nicknames=items)


# ------------------------------------------------------------------ learning from mistakes

FIXES_FILE = "pending_fixes.json"


def _candidates(ctx: ToolContext) -> dict[str, str]:
    from app.tools.applications import KNOWN_SITES, _apps
    from app.tools.windows_settings import PAGES, TOOLS_APPS

    found: dict[str, str] = {}
    try:
        for entry in _apps(ctx).applications.values():
            if entry.enabled:
                found[entry.display_name] = "app"
    except Exception:
        pass
    if winapi.is_windows():
        try:
            for name, _ in winapi.apps_folder_items():
                found.setdefault(name, "app")
        except Exception:
            pass
    for name in KNOWN_SITES:
        found.setdefault(name, "site")
    for name in list(PAGES) + list(TOOLS_APPS):
        found.setdefault(name, "page")
    return found


def review_mistakes(_: NoArgs, ctx: ToolContext) -> ToolResult:
    from app.agent import learning
    from app.agent.aliases import AliasStore

    if ctx.database is None:
        raise ToolError("The review needs the database.", "no_database")
    fixes = learning.review(ctx.database, _candidates(ctx), known=set(AliasStore(_data_dir(ctx)).items()))
    counts = learning.summary(ctx.database)
    (_data_dir(ctx) / FIXES_FILE).write_text(json.dumps([f.__dict__ for f in fixes], ensure_ascii=False),
                                              encoding="utf-8")
    head = (f"This week: {counts.get('not_found', 0)} thing(s) I couldn't find, {counts.get('cancelled', 0)} "
            f"cancelled, {counts.get('corrected', 0)} corrected, {counts.get('bad_answer', 0)} bad answer(s).")
    if not fixes:
        return ToolResult.success(head + " No fixes to suggest.", fixes=[])
    lines = [f"{i}. {f.describe()}" for i, f in enumerate(fixes, 1)]
    return ToolResult.success(head + " Suggested fixes: " + " ".join(lines) + " Say 'accept fix 1' (or 'accept all "
                              "fixes').", fixes=[f.__dict__ for f in fixes])


class FixArgs(ToolArgs):
    which: str = Field(min_length=1, max_length=10, description="The fix number from the review, or 'all'")


def accept_fix(args: FixArgs, ctx: ToolContext) -> ToolResult:
    from app.agent.aliases import AliasStore

    try:
        fixes = json.loads((_data_dir(ctx) / FIXES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("There are no suggested fixes: say 'review your mistakes' first.", "no_fixes") from None
    chosen = fixes if args.which.strip().lower() in ("all", "every", "each") else []
    if not chosen:
        digits = re.findall(r"\d+", args.which)
        chosen = [fixes[int(d) - 1] for d in digits if 0 < int(d) <= len(fixes)]
    if not chosen:
        raise ToolError(f"There is no fix '{args.which}'. There are {len(fixes)}.", "unknown")
    store = AliasStore(_data_dir(ctx))
    for fix in chosen:
        store.set(fix["heard"], fix["kind"], fix["target"])
    names = ", ".join(f"'{f['heard']}' -> {f['target']}" for f in chosen)
    return ToolResult.success(f"Learned: {names}.")


# ------------------------------------------------------------------ the window you're in

class ThisWindowArgs(ToolArgs):
    action: Literal["close", "minimize", "maximize", "restore", "which"] = Field(
        description="What to do with the window you're working in ('which' = say what it is)")


def this_window(args: ThisWindowArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    window = winapi.target_window()
    if window is None:
        raise ToolError("I can't see a window to use.", "no_window")
    name = window.title[:60] or window.process_name
    if args.action == "which":
        return ToolResult.success(f"You're in {name} ({window.process_name}).", title=window.title,
                                  process=window.process_name)
    if args.action == "close":
        winapi.close_window(window.hwnd)
        return ToolResult.success(f"Closed {name}.")
    command = {"minimize": winapi.SW_MINIMIZE, "maximize": winapi.SW_MAXIMIZE, "restore": winapi.SW_RESTORE}
    winapi.show_window(window.hwnd, command[args.action])
    return ToolResult.success(f"{args.action.capitalize()}d {name}.")


# ------------------------------------------------------------------ app shortcuts

BROWSERS = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe")
APP_COMMANDS: dict[str, dict[str, str]] = {
    "browser": {"new tab": "ctrl+t", "close tab": "ctrl+w", "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab",
                "reopen tab": "ctrl+shift+t", "refresh": "f5", "reload": "f5", "go back": "alt+left", "back": "alt+left",
                "go forward": "alt+right", "forward": "alt+right", "zoom in": "ctrl+plus", "zoom out": "ctrl+minus",
                "reset zoom": "ctrl+0", "new window": "ctrl+n", "incognito": "ctrl+shift+n", "history": "ctrl+h",
                "downloads": "ctrl+j", "bookmark": "ctrl+d", "find": "ctrl+f", "address bar": "ctrl+l",
                "full screen": "f11", "home page": "alt+home"},
    "youtube": {"full screen": "f", "pause": "k", "play": "k", "skip": "l", "forward": "l", "rewind": "j",
                "back": "j", "mute": "m", "captions": "c", "subtitles": "c", "next video": "shift+n",
                "previous video": "shift+p", "theater mode": "t", "speed up": "shift+period",
                "slow down": "shift+comma"},
    "word": {"save": "ctrl+s", "save as pdf": "f12", "save as": "f12", "bold": "ctrl+b", "italic": "ctrl+i",
             "underline": "ctrl+u", "new document": "ctrl+n", "print": "ctrl+p", "center": "ctrl+e",
             "align left": "ctrl+l", "align right": "ctrl+r", "undo": "ctrl+z", "redo": "ctrl+y"},
    "code": {"command palette": "ctrl+shift+p", "terminal": "ctrl+backtick", "save": "ctrl+s", "save all": "ctrl+k",
             "find file": "ctrl+p", "comment": "ctrl+slash", "format": "shift+alt+f", "sidebar": "ctrl+b"},
    "explorer": {"new folder": "ctrl+shift+n", "back": "alt+left", "folder up": "alt+up", "refresh": "f5",
                 "rename": "f2", "search": "ctrl+e", "address bar": "ctrl+l"},
    "any": {"copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x", "undo": "ctrl+z", "redo": "ctrl+y",
            "select all": "ctrl+a", "save": "ctrl+s", "find": "ctrl+f", "print": "ctrl+p", "close window": "alt+f4",
            "switch window": "alt+tab", "enter": "enter", "escape": "escape", "delete": "delete", "tab": "tab",
            "new window": "ctrl+n", "open file": "ctrl+o", "zoom in": "ctrl+plus", "zoom out": "ctrl+minus",
            "page down": "pagedown", "page up": "pageup", "go to top": "ctrl+home", "go to bottom": "ctrl+end"},
}


def app_family(window) -> str:
    process = (window.process_name or "").lower()
    title = (window.title or "").lower()
    if process in BROWSERS:
        return "youtube" if "youtube" in title else "browser"
    if process == "winword.exe":
        return "word"
    if process == "code.exe":
        return "code"
    if process == "explorer.exe":
        return "explorer"
    return "any"


def keys_for(command: str, family: str) -> str | None:
    command = " ".join(command.lower().replace("-", " ").split())
    for group in ((family,) if family == "any" else (family, "browser" if family == "youtube" else "", "any")):
        if group and command in APP_COMMANDS.get(group, {}):
            return APP_COMMANDS[group][command]
    return None


class AppCommandArgs(ToolArgs):
    command: str = Field(min_length=2, max_length=40,
                         description="e.g. 'new tab', 'next tab', 'full screen', 'save as pdf', 'copy', 'undo'")


def app_command(args: AppCommandArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    from app.tools.screen_agent import hand_over

    hand_over(ctx)
    window = winapi.target_window()
    if window is None:
        raise ToolError("I can't see a window to use.", "no_window")
    from app.tools.screen_agent import _BLOCKED_COMBOS
    from app.vision import capture as cap

    cap.check_not_blocked([window], cap.blocked_patterns(ctx.settings.screen_blocked_windows))
    family = app_family(window)
    combo = keys_for(args.command, family)
    if combo is None:
        raise ToolError(f"I don't know '{args.command}' for {window.process_name or 'this window'}.", "unknown")
    if combo in _BLOCKED_COMBOS:
        raise ToolError("That key combination isn't available here.", "blocked")
    if winapi.foreground_window() != window.hwnd:
        winapi.focus_window(window.hwnd)
        time.sleep(0.15)
    winapi.press_combo(combo)
    log_event(_log, "app_command", family=family)
    return ToolResult.success(f"{args.command.capitalize()} ({window.title[:40] or window.process_name}).",
                              keys=combo, app=family)


# ------------------------------------------------------------------ mouse

class MouseArgs(ToolArgs):
    action: Literal["click", "double_click", "right_click", "move", "center"] = Field(
        description="click / double_click / right_click where the pointer is, move it, or put it in the middle")
    direction: Literal["up", "down", "left", "right"] | None = Field(default=None, description="For 'move'")
    pixels: int = Field(default=150, ge=5, le=2000, description="How far to move")


def mouse_control(args: MouseArgs, ctx: ToolContext) -> ToolResult:
    from app.tools.screen_agent import hand_over

    if args.action in ("click", "double_click", "right_click"):
        hand_over(ctx)
    return _mouse_control(args, ctx)


def _mouse_control(args: MouseArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    if args.action == "center":
        width, height = winapi.screen_size()
        winapi.move_cursor(width // 2, height // 2)
        return ToolResult.success("The pointer is in the middle of the screen.")
    if args.action == "move":
        if args.direction is None:
            raise ToolError("Which way? up, down, left or right.", "missing_direction")
        x, y = winapi.cursor_pos()
        dx = {"left": -args.pixels, "right": args.pixels}.get(args.direction, 0)
        dy = {"up": -args.pixels, "down": args.pixels}.get(args.direction, 0)
        winapi.move_cursor(x + dx, y + dy)
        return ToolResult.success(f"Moved {args.direction}.")
    button = "right" if args.action == "right_click" else "left"
    winapi.click_here(button, args.action == "double_click")
    return ToolResult.success({"click": "Clicked.", "double_click": "Double-clicked.",
                               "right_click": "Right-clicked."}[args.action])


# ------------------------------------------------------------------ Jarvis itself by voice

class JarvisArgs(ToolArgs):
    action: Literal["open_settings", "open_memory", "open_privacy", "open_logs", "open_automations", "mission_control",
                    "open_notifications", "emergency_controls", "evolution_center", "task_center", "my_day", "workflow_studio",
                    "security_center", "show_system",
                    "show_dashboard", "enroll_voice", "teach_wake_word", "train_voice", "make_jarvis_model",
                    "self_test", "speed_report", "check_updates", "phone_remote", "new_conversation", "hide_window",
                    "show_window", "setup_wizard", "hands_free_off", "mute_microphone", "close_dialog", "health_report", "use_best_model", "full_screen", "exit_full_screen", "stay_on_top", "not_on_top",
                    "compare_models", "restart_jarvis", "window_overview", "tab_overview"] = Field(
                        description="What Jarvis should do")


def jarvis_control(args: JarvisArgs, ctx: ToolContext) -> ToolResult:
    control = getattr(ctx, "jarvis_control", None)
    if control is None:
        raise ToolError("That works in the Jarvis window.", "unavailable")
    return ToolResult.success(control(args.action))


# setting name you say -> (Settings field, kind, needs asking first)
FRIENDLY_SETTINGS: dict[str, tuple[str, str, bool]] = {
    "screen capture": ("screen_capture_enabled", "bool", True), "screen control": ("screen_capture_enabled", "bool", True),
    "browser control": ("browser_enabled", "bool", True), "phone remote": ("remote_enabled", "bool", True),
    "webcam presence": ("presence_detection", "bool", True), "presence detection": ("presence_detection", "bool", True),
    "speak replies": ("tts_enabled", "bool", False), "voice replies": ("tts_enabled", "bool", False),
    "speak typed replies": ("speak_typed_replies", "bool", False), "wake sound": ("wake_sound", "bool", False),
    "tone awareness": ("tone_aware", "bool", False), "memory suggestions": ("memory_suggestions", "bool", False),
    "conversation memory": ("conversation_memory", "bool", False), "sort downloads": ("auto_sort_downloads", "bool", False),
    "only my voice": ("speaker_verification", "bool", False), "speaker verification": ("speaker_verification", "bool", False),
    "direct commands": ("direct_commands", "bool", False), "proactive alerts": ("proactive_alerts", "bool", False),
    "weekly health check": ("weekly_health_check", "bool", False), "animations": ("animations", "bool", False),
    "suggestions": ("proactive_suggestions", "bool", False), "mini orb": ("mini_orb", "bool", False),
    "talk over": ("voice_interrupt", "bool", False), "voice confirm": ("voice_confirm", "bool", False),
    "voice speed": ("tts_speed", "float", False), "speaking speed": ("tts_speed", "float", False),
    "voice volume": ("tts_volume", "float", False), "follow up time": ("follow_up_seconds", "float", False),
    "conversation time": ("follow_up_seconds", "float", False), "home city": ("home_location", "text", False),
    "language": ("voice_language", "choice:auto,en,bn", False), "call me": ("user_address", "text", False),
    "start with windows": ("start_with_windows", "bool", False), "pc watcher": ("pc_watcher", "bool", False),
    "voice engine": ("tts_engine", "choice:jarvis", False),
    "jarvis voice": ("tts_engine", "choice:jarvis", False),
}
_ON = {"on", "true", "yes", "enable", "enabled", "start", "চালু"}
_OFF = {"off", "false", "no", "disable", "disabled", "stop", "বন্ধ"}


def _setting(name: str) -> tuple[str, str, bool]:
    from app.utils.phonetic import best_sounding

    key = " ".join(name.lower().replace("_", " ").split()).removesuffix(" setting").removesuffix(" mode")
    if key in FRIENDLY_SETTINGS:
        return FRIENDLY_SETTINGS[key]
    guess = best_sounding(key, list(FRIENDLY_SETTINGS), threshold=0.78)
    if guess is None:
        raise ToolError(f"I can't change '{name}' by voice. I can change: {', '.join(sorted(FRIENDLY_SETTINGS))}.",
                        "unknown_setting")
    return FRIENDLY_SETTINGS[guess]


def _value(kind: str, value: str) -> str:
    text = value.strip().lower()
    if kind == "bool":
        if text in _ON:
            return "true"
        if text in _OFF:
            return "false"
        raise ToolError("Say on or off.", "bad_value")
    if kind == "float":
        number = re.search(r"\d+(?:\.\d+)?", text)
        if not number:
            raise ToolError("Say a number.", "bad_value")
        return number.group(0)
    if kind.startswith("choice:"):
        options = kind.split(":", 1)[1].split(",")
        aliases = {"bangla": "bn", "bengali": "bn", "english": "en", "both": "auto", "automatic": "auto"}
        text = aliases.get(text, text)
        if text not in options:
            raise ToolError(f"Choose one of: {', '.join(options)}.", "bad_value")
        return text
    return value.strip()[:80]


class SettingArgs(ToolArgs):
    setting: str = Field(min_length=2, max_length=40, description="e.g. 'screen capture', 'voice speed', 'language'")
    value: str = Field(min_length=1, max_length=80, description="on/off, a number, or text")


def change_setting(args: SettingArgs, ctx: ToolContext) -> ToolResult:
    field, kind, _ = _setting(args.setting)
    value = _value(kind, args.value)
    apply = getattr(ctx, "apply_setting", None)
    if apply is None:
        raise ToolError("Settings can be changed by voice in the Jarvis window.", "unavailable")
    apply(field, value)
    shown = {"true": "on", "false": "off"}.get(value, value)
    return ToolResult.success(f"{args.setting.capitalize()} is now {shown}.", setting=field, value=value)


def _setting_needs_asking(args: SettingArgs, _: ToolContext) -> bool:
    try:
        return _setting(args.setting)[2]
    except ToolError:
        return False


# ------------------------------------------------------------------ memory suggestions by voice

class SuggestionArgs(ToolArgs):
    action: Literal["accept", "reject", "list"] = Field(description="accept / reject the oldest suggestion, or list")


def memory_suggestion(args: SuggestionArgs, ctx: ToolContext) -> ToolResult:
    from app.memory.manager import MemoryManager
    from app.memory.suggestions import SuggestionStore

    if ctx.database is None:
        raise ToolError("Memory needs the database.", "no_database")
    store = SuggestionStore(ctx.database)
    pending = store.pending()
    if not pending:
        return ToolResult.success("There are no memory suggestions waiting.")
    if args.action == "list":
        return ToolResult.success(f"{len(pending)} suggestion(s): " + "; ".join(t for _, t in pending[:5]) + ".")
    suggestion_id, text = pending[0]
    if args.action == "accept":
        store.accept(suggestion_id, MemoryManager(ctx.database))
        return ToolResult.success(f"Remembered: {text}.")
    store.reject(suggestion_id)
    return ToolResult.success(f"Forgot the suggestion: {text}.")


# ------------------------------------------------------------------ dictation

class DictationArgs(ToolArgs):
    action: Literal["start", "stop"] = Field(description="start: everything you say is typed; stop: back to commands")


def dictation(args: DictationArgs, ctx: ToolContext) -> ToolResult:
    control = getattr(ctx, "dictation", None)
    if control is None:
        raise ToolError("Dictation works in the Jarvis window.", "unavailable")
    return ToolResult.success(control(args.action))


# ------------------------------------------------------------------ WhatsApp

class WhatsAppArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=60, description="A saved contact")
    message: str = Field(min_length=1, max_length=1000, description="The message")


def whatsapp_message(args: WhatsAppArgs, ctx: ToolContext) -> ToolResult:
    """Opens the chat in WhatsApp Desktop with the message typed, then presses Enter (you said yes already)."""
    winapi.require_windows()
    from app.tools.phone import find_contact
    from app.utils.redaction import redact

    if redact(args.message) != args.message:
        raise ToolError("That message contains something that looks like a password or card number.", "sensitive")
    name, number = find_contact(ctx, args.name)
    digits = re.sub(r"\D", "", number)
    if digits.startswith("0") and len(digits) == 11:  # Bangladesh mobile without country code
        digits = "88" + digits
    winapi.start_file(f"whatsapp://send?phone={digits}&text={quote(args.message)}")
    deadline = time.monotonic() + 12
    window = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        window = next((w for w in winapi.visible_windows()
                       if "whatsapp" in (w.process_name or "").lower() or "whatsapp" in (w.title or "").lower()), None)
        if window is not None and winapi.foreground_window() == window.hwnd:
            break
    if window is None:
        raise ToolError("WhatsApp didn't open. Is WhatsApp Desktop installed and signed in?", "not_found")
    time.sleep(1.2)  # the chat and the typed message need a moment
    winapi.focus_window(window.hwnd)
    winapi.press_combo("enter")
    return ToolResult.success(f"Sent to {name} on WhatsApp.", contact=name)


# ------------------------------------------------------------------ clean temporary files

def _temp_dir() -> Path:
    return Path(os.environ.get("TEMP") or os.environ.get("TMP") or Path.home() / "AppData" / "Local" / "Temp")


def clean_temp_files(_: NoArgs, ctx: ToolContext, folder: Path | None = None, now: float | None = None) -> ToolResult:
    """Deletes files in your Temp folder that are more than 2 days old (files in use are skipped)."""
    folder = folder or _temp_dir()
    if not folder.is_dir() or folder.name.lower() not in ("temp", "tmp"):
        raise ToolError("I couldn't find your Temp folder.", "not_found")
    cutoff = (now or time.time()) - 2 * 24 * 3600
    freed = removed = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                size = path.stat().st_size
                path.unlink()
                freed += size
                removed += 1
        except OSError:
            continue  # in use
    return ToolResult.success(f"Removed {removed} old temporary file(s), freed {freed / 1024**2:.0f} MB.",
                              removed=removed, freed_bytes=freed)


# ------------------------------------------------------------------ find files

TYPES = {"pdf": (".pdf",), "word": (".doc", ".docx"), "document": (".pdf", ".doc", ".docx", ".txt", ".md", ".odt"),
         "excel": (".xls", ".xlsx", ".csv"), "powerpoint": (".ppt", ".pptx"), "image": (".jpg", ".jpeg", ".png", ".gif"),
         "photo": (".jpg", ".jpeg", ".png", ".heic"), "video": (".mp4", ".mkv", ".mov", ".avi"),
         "music": (".mp3", ".wav", ".m4a"), "zip": (".zip", ".rar", ".7z"), "any": ()}
MAX_SCAN = 20000


def _search_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return [home / name for name in ("Desktop", "Documents", "Downloads") if (home / name).is_dir()]


class FindArgs(ToolArgs):
    about: str = Field(default="", max_length=120, description="Words in the name or content, e.g. 'thesis draft'")
    kind: Literal["pdf", "word", "document", "excel", "powerpoint", "image", "photo", "video", "music", "zip",
                  "any"] = Field(default="any", description="Type of file")
    within_days: int | None = Field(default=None, ge=1, le=3650, description="Changed in the last N days")
    newest: bool = Field(default=True, description="Newest first")
    open_first: bool = Field(default=False, description="Open the best match")


def find_my_files(args: FindArgs, ctx: ToolContext, roots: list[Path] | None = None) -> ToolResult:
    """Desktop, Documents and Downloads: by type, date and words in the name (and in the text of documents
    that are in the search index)."""
    from app.utils.phonetic import similarity

    words = [w for w in re.findall(r"[\wঀ-৿]+", args.about.lower()) if len(w) > 1]
    extensions = TYPES[args.kind]
    cutoff = (datetime.now() - timedelta(days=args.within_days)).timestamp() if args.within_days else None
    found: list[tuple[float, float, Path]] = []
    scanned = 0
    for root in roots or _search_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$")) and d.lower() not in
                           ("node_modules", "appdata", "__pycache__", ".git")]
            for filename in filenames:
                scanned += 1
                if scanned > MAX_SCAN:
                    break
                path = Path(dirpath) / filename
                if extensions and path.suffix.lower() not in extensions:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if cutoff and mtime < cutoff:
                    continue
                stem = path.stem.lower().replace("_", " ").replace("-", " ")
                score = 1.0 if not words else sum(1.0 if w in stem else 0.6 * similarity(w, stem) for w in words) / len(words)
                if words and score < 0.5:
                    continue
                found.append((score, mtime, path))
    semantic = []
    if words and ctx.database is not None:
        try:
            with ctx.database.connect() as conn:
                sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM semantic_items WHERE kind='document' "
                                                       "AND lower(text) LIKE ?", (f"%{' '.join(words)}%",)).fetchall()]
            semantic = [Path(s) for s in sources if Path(s).is_file()]
        except Exception:
            semantic = []
    found.sort(key=lambda item: (-round(item[0], 1), -item[1] if args.newest else item[1]))
    paths = list(dict.fromkeys([p for _, _, p in found] + semantic))[:8]
    if not paths:
        return ToolResult.success("I didn't find a matching file in Desktop, Documents or Downloads.", files=[])
    if args.open_first:
        winapi.start_file(str(paths[0]))
    listed = "; ".join(f"{p.name} ({datetime.fromtimestamp(p.stat().st_mtime):%d %b})" for p in paths[:5])
    opened = f" Opened {paths[0].name}." if args.open_first else ""
    return ToolResult.success(f"Found {len(paths)}: {listed}.{opened}", files=[str(p) for p in paths])


TOOLS: tuple[Tool, ...] = (
    Tool("set_nickname", "Give something a nickname the user can say: an app, website, Settings page or workflow "
         "(\"call Visual Studio Code 'code'\").", set_nickname, NicknameArgs, RiskLevel.MODERATE, category="voice",
         confirmation=lambda a, _: f"Make '{a.nickname}' open {a.target}?", direct_without_confirmation=lambda a, _: True),
    Tool("remove_nickname", "Remove one of the user's nicknames.", remove_nickname, NicknameNameArgs, RiskLevel.MODERATE, category="voice",
         confirmation=lambda a, _: f"Remove the nickname '{a.nickname}'?", direct_without_confirmation=lambda a, _: True),
    Tool("list_nicknames", "The user's nicknames for apps, sites and pages.", list_nicknames, NoArgs, RiskLevel.SAFE,
         category="voice"),
    Tool("review_mistakes", "Review this week's misunderstood, cancelled and corrected requests and suggest fixes.",
         review_mistakes, NoArgs, RiskLevel.SAFE, category="voice", timeout=30),
    Tool("accept_fix", "Accept a fix from the mistakes review (by number, or 'all').", accept_fix, FixArgs,
         RiskLevel.MODERATE, category="voice", confirmation=lambda a, _: f"Accept fix {a.which}?",
         direct_without_confirmation=lambda a, _: True),
    Tool("this_window", "Close, minimize, maximize or name the window the user is working in ('close this').",
         this_window, ThisWindowArgs, RiskLevel.MODERATE, category="apps",
         confirmation=lambda a, _: f"{a.action.capitalize()} the window you're in?",
         direct_without_confirmation=lambda a, _: True),
    Tool("app_command", "A command in the app the user is in, sent as its keyboard shortcut: new tab, next tab, "
         "full screen, save as pdf, copy, paste, undo, zoom in, go back ...", app_command, AppCommandArgs,
         RiskLevel.MODERATE, category="apps", confirmation=lambda a, _: f"Do '{a.command}' in the current app?",
         direct_without_confirmation=lambda a, _: True),
    Tool("mouse_control", "Click, double-click or right-click where the pointer is, move it, or center it.",
         mouse_control, MouseArgs, RiskLevel.MODERATE, category="screen",
         confirmation=lambda a, _: f"{a.action.replace('_', ' ').capitalize()} the mouse?",
         direct_without_confirmation=lambda a, _: True),
    Tool("jarvis_control", "Jarvis's own screens and features: settings, memory, privacy, logs, automations, the "
         "system page, voice enrollment, wake word, voice training, the Jarvis model, self-test, speed report, "
         "updates, phone remote, new conversation, hide/show the window.", jarvis_control, JarvisArgs,
         RiskLevel.SAFE, category="voice", timeout=20),
    Tool("change_setting", "Change one of Jarvis's settings by voice (screen capture, voice speed, language, "
         "hands-free follow-up time, speak replies, only my voice ...).", change_setting, SettingArgs,
         RiskLevel.MODERATE, category="voice", confirmation=lambda a, _: f"Set {a.setting} to {a.value}?",
         direct_without_confirmation=lambda a, ctx: not _setting_needs_asking(a, ctx)),
    Tool("memory_suggestion", "Accept or reject the waiting memory suggestion, or list them.", memory_suggestion,
         SuggestionArgs, RiskLevel.MODERATE, category="memory", confirmation=lambda a, _: f"{a.action} the suggestion?",
         direct_without_confirmation=lambda a, _: True),
    Tool("dictation", "Start or stop dictation: while on, everything the user says is typed where the cursor is.",
         dictation, DictationArgs, RiskLevel.MODERATE, category="voice",
         confirmation=lambda a, _: "Start typing what you say?" if a.action == "start" else "Stop dictation?",
         direct_without_confirmation=lambda a, _: True),
    Tool("whatsapp_message", "Send a WhatsApp message to a saved contact through WhatsApp Desktop.", whatsapp_message,
         WhatsAppArgs, RiskLevel.MODERATE, category="phone", timeout=40,
         confirmation=lambda a, _: f"Send to {a.name} on WhatsApp: \"{a.message[:200]}\"?"),
    Tool("clean_temp_files", "Delete temporary files older than 2 days to free disk space.", clean_temp_files, NoArgs,
         RiskLevel.MODERATE, category="system", timeout=120,
         confirmation=lambda a, _: "Delete temporary files older than 2 days?"),
    Tool("find_my_files", "Find the user's files in Desktop, Documents and Downloads by what they are about, type "
         "and date ('the PDF about machine learning I downloaded last week'), optionally open the best one.",
         find_my_files, FindArgs, RiskLevel.SAFE, category="files", timeout=60),
)
