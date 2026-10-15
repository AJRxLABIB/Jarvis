"""Application tools: open, close, window control, open website.

Lookup order for "open X":
  1. config/apps.json (aliases) -> App Paths registry / PATH / %ENV% candidates / URI
  2. Start Menu shortcuts (finds most installed apps without any configuration)
Nothing is ever run through a shell; targets go to the Windows shell's "open".
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import webbrowser
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath
from typing import Literal
from urllib.parse import quote_plus, urlparse

import psutil
from pydantic import Field

from app.config.files import AppEntry, AppsConfig, default_apps
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.apps")
_SKIP_SHORTCUT = re.compile(r"uninstall|readme|read me|help|documentation|license|release notes|website|manual|support", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = {"the", "app", "application", "program", "my"}


def normalize(name: str, *, keep_filler: bool = False) -> str:
    words = _WORD.findall(name.lower())
    return " ".join(w for w in words if keep_filler or w not in _FILLER)


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _apps(ctx: ToolContext) -> AppsConfig:
    return ctx.config.apps if ctx.config is not None else default_apps()


# ------------------------------------------------------------ Start Menu index


def best_match(entries: list[tuple[str, str]], name: str) -> tuple[str, str] | None:
    """entries: (normalized name, value). Tries, in order: exact (also ignoring spaces,
    so "whats_app" finds "WhatsApp"), all words contained (shortest wins), fuzzy.
    Each step is tried with and without filler words like "app"."""
    variants = [v for v in dict.fromkeys((normalize(name), normalize(name, keep_filler=True))) if v]
    if not variants:
        return None
    for wanted in variants:
        exact = [e for e in entries if e[0] == wanted or _compact(e[0]) == _compact(wanted)]
        if exact:
            return exact[0]
    for wanted in variants:
        words = set(wanted.split())
        contains = [e for e in entries if words <= set(e[0].split())]
        if contains:
            return min(contains, key=lambda e: len(e[0]))  # "chrome" -> "google chrome", not "chrome remote desktop"
    best_score, best = 0.0, None
    for wanted in variants:
        for entry in entries:
            score = SequenceMatcher(None, _compact(wanted), _compact(entry[0])).ratio()
            if score > best_score:
                best_score, best = score, entry
    if best_score >= 0.85:
        return best
    # speech recognition mishears names: "bone link" -> "phone link" (see app/utils/phonetic.py)
    from app.utils.phonetic import best_sounding

    by_key = {e[0]: e for e in entries}
    heard = best_sounding(variants[0], list(by_key), threshold=0.8)
    return by_key[heard] if heard is not None else None


class StartMenuIndex:
    """Shortcut (.lnk) names from the user's and all-users' Start Menu, cached."""

    TTL = 300.0

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots
        self._entries: list[tuple[str, Path]] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def roots(self) -> list[Path]:
        if self._roots is not None:
            return self._roots
        dirs = []
        for env in ("ProgramData", "APPDATA"):
            base = os.environ.get(env)
            if base:
                dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        return dirs

    def entries(self) -> list[tuple[str, Path]]:
        with self._lock:
            if not self._entries or time.monotonic() - self._loaded_at > self.TTL:
                found: dict[str, Path] = {}
                for root in self.roots():
                    if not root.is_dir():
                        continue
                    for path in root.rglob("*.lnk"):
                        if _SKIP_SHORTCUT.search(path.stem):
                            continue
                        found.setdefault(normalize(path.stem), path)
                self._entries = sorted(found.items())
                self._loaded_at = time.monotonic()
            return list(self._entries)

    def find(self, name: str) -> tuple[str, Path] | None:
        by_key = {key: path for key, path in self.entries()}
        match = best_match([(key, key) for key in by_key], name)
        return (match[0], by_key[match[0]]) if match else None


_SAFE_APP_ID = re.compile(r"^[^\x00-\x1f\"<>|]{1,400}$")


class AppsFolderIndex:
    """Apps listed in the Windows Apps folder (includes Microsoft Store apps), cached."""

    TTL = 300.0

    def __init__(self, loader=None) -> None:
        self._loader = loader
        self._entries: list[tuple[str, str, str]] = []  # (normalized, display name, app id)
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def entries(self) -> list[tuple[str, str, str]]:
        with self._lock:
            if not self._entries or time.monotonic() - self._loaded_at > self.TTL:
                try:
                    items = (self._loader or winapi.apps_folder_items)()
                except Exception:
                    _log.warning("could not read the Windows Apps folder", exc_info=True)
                    items = []
                found: dict[str, tuple[str, str, str]] = {}
                for display, app_id in items:
                    if _SKIP_SHORTCUT.search(display) or not _SAFE_APP_ID.match(app_id):
                        continue
                    key = normalize(display)
                    if key:
                        found.setdefault(key, (key, display, app_id))
                self._entries = sorted(found.values())
                self._loaded_at = time.monotonic()
            return list(self._entries)

    def find(self, name: str) -> tuple[str, str] | None:
        """(display name, app id) or None."""
        by_key = {key: (display, app_id) for key, display, app_id in self.entries()}
        match = best_match([(key, key) for key in by_key], name)
        return by_key[match[0]] if match else None


START_MENU = StartMenuIndex()
APPS_FOLDER = AppsFolderIndex()


# ------------------------------------------------------------------ resolving


@dataclass(frozen=True)
class LaunchTarget:
    display_name: str
    target: str  # executable path, .lnk path or URI
    source: str  # config | start_menu
    process_names: tuple[str, ...] = ()


_ENV_VAR = re.compile(r"%([^%]+)%")


def expand_windows_vars(text: str) -> str:
    """Expand %VAR% (case-insensitively, like Windows); unknown variables are left as-is."""
    env = {k.upper(): v for k, v in os.environ.items()}
    return _ENV_VAR.sub(lambda m: env.get(m.group(1).upper(), m.group(0)), text)


def _resolve_entry(entry: AppEntry) -> str | None:
    spec = entry.launch
    if spec.type == "uri":
        return spec.value
    if spec.type == "app_paths":
        found = winapi.app_paths_lookup(spec.value) or shutil.which(spec.value)
    else:
        found = spec.value if Path(spec.value).is_absolute() and Path(spec.value).is_file() else shutil.which(spec.value)
    if found:
        return found
    for candidate in spec.candidates:
        path = Path(expand_windows_vars(candidate))
        if "%" not in str(path) and path.is_file():
            return str(path)
    return None


def resolve_application(
    name: str, apps: AppsConfig, index: StartMenuIndex | None = None, apps_folder: AppsFolderIndex | None = None
) -> LaunchTarget | None:
    """config/apps.json -> Start Menu shortcuts -> Windows Apps folder (Store apps)."""
    index = index or START_MENU  # looked up at call time, not bound at import
    apps_folder = apps_folder or APPS_FOLDER
    app_id = apps.resolve(name)
    names = [name]
    process_names: tuple[str, ...] = ()
    display = None
    if app_id is not None:
        entry = apps.applications[app_id]
        target = _resolve_entry(entry)
        if target:
            return LaunchTarget(entry.display_name, target, "config", tuple(entry.process_names))
        names = [entry.display_name, name]
        process_names, display = tuple(entry.process_names), entry.display_name
    for candidate in names:
        shortcut = index.find(candidate)
        if shortcut:
            return LaunchTarget(display or shortcut[1].stem, str(shortcut[1]), "start_menu", process_names)
    for candidate in names:
        found = apps_folder.find(candidate)
        if found:
            return LaunchTarget(display or found[0], winapi.apps_folder_target(found[1]), "apps_folder", process_names)
    return None


def known_application(name: str, ctx: ToolContext) -> str | None:
    """Display name if ``name`` is an app the assistant can find, else None. Cheap: no launching."""
    apps = _apps(ctx)
    app_id = apps.resolve(name)
    if app_id is not None:
        return apps.applications[app_id].display_name
    if not winapi.is_windows():
        return None
    shortcut = START_MENU.find(name)
    if shortcut:
        return shortcut[1].stem
    found = APPS_FOLDER.find(name)
    return found[0] if found else None


def _process_running(names: tuple[str, ...]) -> bool:
    """True if a process has one of these names (a name without '.exe' matches as a prefix)."""
    wanted = {n.lower() for n in names}
    stems = {n for n in wanted if not n.endswith(".exe")}
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name in wanted or any(name.startswith(stem) for stem in stems):
            return True
    return False


# ------------------------------------------------------------------ open / close


class AppNameArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=64, description="Application name as the user said it, e.g. 'chrome', 'vs code', 'spotify'")


class OpenAppArgs(AppNameArgs):
    profile: str | None = Field(default=None, max_length=80,
                                description="Only for Chrome/Edge/Brave, and only if the user names a browser "
                                            "profile, e.g. 'Night'. Leave empty otherwise.")


def _browser_profile_setting(exe_path: str, ctx: ToolContext) -> str:
    exe = PureWindowsPath(exe_path).name.lower()
    if exe == "chrome.exe":
        return ctx.settings.chrome_profile
    if exe == "msedge.exe":
        return ctx.settings.edge_profile
    return "last"


def _launch(target: LaunchTarget, ctx: ToolContext, profile_name: str | None = None) -> ToolResult:
    shown = target.display_name
    extra = ""
    arguments: list[str] = []
    if target.target.lower().endswith(".exe"):
        wanted = profile_name or _browser_profile_setting(target.target, ctx)
        arguments, profile = browser_profile_arguments(target.target, wanted, ctx)
        if profile is not None:
            shown = f"{target.display_name} ({profile.name} profile)"
            extra = f" {profile.note}" if profile.note else ""
    elif profile_name:
        raise ToolError(f"I can only choose a profile for Chrome, Edge or Brave, not {target.display_name}.",
                        "profile_unsupported")
    log_event(_log, "launching", app=target.display_name, source=target.source, with_profile=bool(arguments))
    try:
        before = {w.hwnd for w in winapi.visible_windows()} if not target.process_names else set()
    except Exception:
        before = set()
    if arguments:
        winapi.start_program(target.target, arguments)  # opens that profile directly, no "Who's using Chrome?"
    else:
        winapi.start_file(target.target)
    if not target.process_names and before:  # no process name to look for: a new window is the proof
        if _wait_for(lambda: any(w.hwnd not in before for w in winapi.visible_windows()), 2.0, 0.25):
            return ToolResult.success(f"Opened {shown}.{extra}", app=target.display_name, verified=True)
    if target.process_names:
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if _process_running(target.process_names):
                return ToolResult.success(f"Opened {shown}.{extra}", app=target.display_name, verified=True)
            time.sleep(0.25)
        return ToolResult.success(
            f"Asked Windows to open {shown}, but I could not confirm it started yet.",
            app=target.display_name, verified=False,
        )
    return ToolResult.success(f"Opened {shown}.{extra}", app=target.display_name, verified=None)


def app_not_found(name: str, ctx: ToolContext, tool: str) -> ToolError:
    """The most useful "not found": a sounds-like app, apps with that word in their name, or a web search."""
    verb = "restart" if tool == "restart_application" else "open"
    similar = apps_named_like(name, ctx)
    if len(similar) > 1:
        return ToolError(f"I couldn't find an app called exactly {name}. I have: {', '.join(similar)}. Which one?",
                         "not_found", candidates=similar)
    guess = similar[0] if similar else suggest_application(name, ctx)
    if guess is not None:
        return ToolError(f"I couldn't find {name}. Did you mean {guess}? Say yes to {verb} it.", "not_found",
                         **_suggestion(tool, name, guess))
    return ToolError(f"There's no app called {name} on this computer. Say yes and I'll search the web for it.",
                     "not_found", suggestion={"tool": "search_in_browser", "arguments": {"query": name}})


def open_application(args: OpenAppArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    target = resolve_application(args.name, _apps(ctx))
    if target is None:
        if site_key(args.name) is not None:  # "open Instagram" with no app installed: open the website
            url = website_url(args.name)
            where = open_url(url, ctx)
            return ToolResult.success(f"{args.name} isn't installed here, so I opened {urlparse(url).hostname} "
                                      f"in {where}.", url=url)
        raise app_not_found(args.name, ctx, "open_application")
    browser = browser_id(target.display_name) or browser_id(args.name)
    if args.profile and browser and site_key(args.profile) is not None:
        # the model sometimes puts the site in the profile field: "open YouTube in Chrome"
        url = website_url(args.profile)
        where = open_url(url, ctx, browser)
        return ToolResult.success(f"Opened {urlparse(url).hostname} in {where}.", url=url)
    return _launch(target, ctx, args.profile)


def _process_matches(process: str, procs: set[str], stems: set[str]) -> bool:
    """Exact configured names, or a guessed stem as prefix: 'whatsapp' -> 'whatsapp.root.exe'."""
    return process in procs or any(stem and process.startswith(stem) for stem in stems)


def _target_windows(name: str, ctx: ToolContext) -> tuple[str, list[winapi.WindowInfo], set[str]]:
    """(display name, windows, process-name hints) for an app.

    Order: Alt+Tab windows of the app's processes; other visible titled windows
    of those processes (e.g. Chrome's profile picker); windows whose title names
    the app; finally titled windows Windows reports as cloaked (Store apps such
    as WhatsApp are sometimes reported that way while on screen).
    """
    apps = _apps(ctx)
    app_id = apps.resolve(name)
    every = winapi.top_level_windows()
    windows = [w for w in every if not w.cloaked]
    display, procs, stems = name, set(), set()
    wanted = normalize(name)
    if app_id is not None:
        entry = apps.applications[app_id]
        display, procs = entry.display_name, {p.lower() for p in entry.process_names}
    if not procs and wanted:
        stems = {_compact(wanted)}  # unknown app: guess from the name
    mine = [w for w in windows if _process_matches(w.process_name, procs, stems)]
    preferred = [w for w in mine if w.switchable]
    fallback = [w for w in mine if w.title and not w.owned]
    if preferred or fallback:
        return display, preferred or fallback, procs | {w.process_name for w in mine}
    if wanted:
        by_title = [w for w in windows if w.switchable and wanted in normalize(w.title)]
        if by_title:
            return display, by_title, procs | stems | {w.process_name for w in by_title}
    cloaked = [w for w in every if w.cloaked and w.title and not w.owned
               and (_process_matches(w.process_name, procs, stems) or (wanted and wanted == normalize(w.title)))]
    return display, cloaked, procs | stems


_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|]\s+")


def running_app_names(windows: list[winapi.WindowInfo]) -> dict[str, str]:
    """Spoken-name candidates for the open windows -> the name to look them up with.
    "main.py - Jarvis - Visual Studio Code" gives "Visual Studio Code" (and the other parts)."""
    names: dict[str, str] = {}
    for w in windows:
        if not w.switchable:
            continue
        parts = [p.strip() for p in _TITLE_SPLIT.split(w.title) if p.strip()]
        for part in ([parts[-1]] if parts else []) + parts[:-1] + [w.title]:
            if 2 <= len(part) <= 40:
                names.setdefault(part, part)
        stem = w.process_name.lower().removesuffix(".exe")
        if stem:
            names.setdefault(stem, stem)
    return names


SUGGEST_ABOVE = 0.6  # "did you mean ...?" - lower than the 0.8 used to correct a name without asking


def closest_name(heard: str, names) -> str | None:
    """The name that sounds most like ``heard`` (for a question, never acted on without a yes)."""
    from app.utils.phonetic import similarity

    scored = sorted(((similarity(heard, n), n) for n in dict.fromkeys(names) if n and normalize(n) != normalize(heard)),
                    reverse=True)
    return scored[0][1] if scored and scored[0][0] >= SUGGEST_ABOVE else None


def suggest_application(name: str, ctx: ToolContext) -> str | None:
    """An installed app whose name sounds like ``name`` ("WordShop" -> "Photoshop")."""
    names = [e.display_name for e in _apps(ctx).applications.values() if e.enabled]
    try:
        names += [path.stem for _, path in START_MENU.entries()]
        names += [display for _, display, _ in APPS_FOLDER.entries()]
    except Exception:
        _log.warning("could not list apps for a suggestion", exc_info=True)
    return closest_name(name, names)


def apps_named_like(name: str, ctx: ToolContext, limit: int = 5) -> list[str]:
    """Installed apps whose name contains what was said ("cloud" -> iCloud, Nextcloud), shortest first."""
    wanted = _compact(normalize(name))
    if len(wanted) < 3:
        return []
    names = [e.display_name for e in _apps(ctx).applications.values() if e.enabled]
    try:
        names += [path.stem for _, path in START_MENU.entries()]
        names += [display for _, display, _ in APPS_FOLDER.entries()]
    except Exception:
        return []
    found = {normalize(n): n for n in names if wanted in _compact(normalize(n))}
    return sorted(found.values(), key=len)[:limit]


def suggest_open_app(name: str, ctx: ToolContext) -> str | None:
    try:
        return closest_name(name, running_app_names(winapi.top_level_windows()))
    except Exception:
        return None


def _suggestion(tool: str, heard: str, name: str, **arguments) -> dict:
    return {"suggestion": {"tool": tool, "arguments": {**arguments, "name": name}, "heard": heard, "name": name}}


def _misheard_target(name: str, ctx: ToolContext):
    """No window for ``name``: maybe speech recognition misheard it ("bone link" for Phone Link).
    Returns (display, windows, procs) for the open app that sounds like it, or None."""
    from app.utils.phonetic import best_sounding

    candidates = running_app_names(winapi.top_level_windows())
    apps = _apps(ctx)
    for entry in apps.applications.values():  # configured names of apps that are open
        if entry.enabled and entry.process_names:
            candidates.setdefault(entry.display_name, entry.display_name)
    guess = best_sounding(name, list(candidates), threshold=0.72)
    if guess is None or normalize(guess) == normalize(name):
        return None
    found = _target_windows(candidates[guess], ctx)
    if not found[1]:
        return None
    log_event(_log, "misheard_app_corrected", heard_words=len(name.split()), app=found[0])
    return found


def close_application(args: AppNameArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    display, windows, procs = _target_windows(args.name, ctx)
    if not windows:
        guess = _misheard_target(args.name, ctx)
        if guess is not None:
            display, windows, procs = guess
    if not windows:
        if procs and _process_running(tuple(procs)):
            raise ToolError(
                f"{display} is running in the background without an open window. "
                f"To end it completely, ask me to force-quit {display}.", "no_window",
            )
        guess = suggest_open_app(args.name, ctx)
        if guess is not None:
            raise ToolError(f"{display} isn't open. Did you mean {guess}? Say yes to close it.", "not_running",
                            **_suggestion("close_application", args.name, guess))
        raise ToolError(f"{display} doesn't seem to be open.", "not_running")
    log_event(_log, "closing", app=display, windows=[w.title[:60] for w in windows])
    for window in windows:
        winapi.close_window(window.hwnd)
    deadline = time.monotonic() + 3.0
    remaining = windows
    while time.monotonic() < deadline:
        remaining = [w for w in remaining if winapi.window_exists(w.hwnd)]
        if not remaining:
            break
        time.sleep(0.2)
    if remaining:
        return ToolResult.failure(
            "still_open",
            f"Asked {display} to close, but it is still open (it may be asking to save changes).",
            app=display, windows_closed=len(windows) - len(remaining), still_open=[w.title[:60] for w in remaining],
        )
    return ToolResult.success(f"Closed {display}.", app=display, windows_closed=len(windows))


def restart_application(args: AppNameArgs, ctx: ToolContext) -> ToolResult:
    """Close it normally, wait until it's gone, open it again (verified by each step)."""
    winapi.require_windows()
    target = resolve_application(args.name, _apps(ctx))
    if target is None:
        raise app_not_found(args.name, ctx, "restart_application")
    if normalize(target.display_name) in ("explorer", "file explorer", "windows explorer"):
        raise ToolError("I won't restart File Explorer: closing it can take the taskbar and desktop with it.",
                        "not_allowed")
    display, windows, _procs = _target_windows(args.name, ctx)
    windows = [w for w in windows if w.title.strip().lower() not in _SHELL_TITLES]
    if windows:
        closed = close_application(args, ctx)
        if not closed.ok:
            return ToolResult.failure("still_open", f"I couldn't restart {display}: {closed.message}", app=display)
        time.sleep(1.0)  # let it finish exiting before it starts again
    opened = _launch(target, ctx)
    if not opened.ok:
        return opened
    verb = "Restarted" if windows else "It wasn't open, so I started"
    return ToolResult.success(f"{verb} {target.display_name}.", app=target.display_name, was_open=bool(windows))


class WindowArgs(ToolArgs):
    action: Literal["focus", "minimize", "maximize", "restore", "minimize_all"] = Field(
        description="focus = switch to it; minimize_all = show the desktop"
    )
    name: str | None = Field(default=None, max_length=64, description="Application name or part of the window title")


def _window_precheck(args: WindowArgs) -> None:
    if args.action == "minimize_all" and args.name:
        raise ToolError(
            f"minimize_all takes no application name, but '{args.name}' was given. The request was probably "
            "misunderstood; ask the user what they want.", "ambiguous")


def window_control(args: WindowArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    if args.action == "minimize_all":
        winapi.minimize_all()
        return ToolResult.success("Minimized all windows.")
    if not args.name:
        raise ToolError("Which application?", "missing_target")
    display, windows, _ = _target_windows(args.name, ctx)
    if not windows:
        guess = _misheard_target(args.name, ctx)
        if guess is not None:
            display, windows, _ = guess
    if not windows:
        guess = suggest_open_app(args.name, ctx)
        if guess is not None:
            raise ToolError(f"{display} isn't open. Did you mean {guess}? Say yes and I'll do it.", "not_running",
                            **_suggestion("window_control", args.name, guess, action=args.action))
        raise ToolError(f"{display} doesn't seem to be open.", "not_running")
    window = windows[0]
    if args.action == "focus":
        if not winapi.focus_window(window.hwnd):
            raise ToolError(f"Windows did not allow switching to {display}.", "focus_denied")
        if not _wait_for(lambda: winapi.foreground_window() == window.hwnd, 1.0):
            return ToolResult.failure("not_verified", f"I asked Windows to switch to {display}, but it is not in "
                                      "front yet (Windows sometimes blocks this; click it in the taskbar).",
                                      app=display, verified=False)
        return ToolResult.success(f"Switched to {display}.", app=display, window=window.title[:80], verified=True)
    command = {"minimize": winapi.SW_MINIMIZE, "maximize": winapi.SW_MAXIMIZE, "restore": winapi.SW_RESTORE}[args.action]
    targets = windows if args.action == "minimize" else [window]
    for w in targets:
        winapi.show_window(w.hwnd, command)
    verb = {"minimize": "Minimized", "maximize": "Maximized", "restore": "Restored"}[args.action]
    expected = {"minimize": "minimized", "maximize": "maximized", "restore": "normal"}[args.action]
    done = _wait_for(lambda: all(winapi.window_state(w.hwnd) in (expected, "gone") for w in targets), 1.0)
    if not done:
        return ToolResult.failure("not_verified", f"I asked Windows to {args.action} {display}, but it didn't change.",
                                  app=display, verified=False)
    return ToolResult.success(f"{verb} {display}.", app=display, windows=len(targets), verified=True)


def _wait_for(check, seconds: float, step: float = 0.1) -> bool:
    """Poll ``check()`` until it is true or ``seconds`` pass (actions take a moment to show)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if check():
                return True
        except Exception:
            return True  # can't check (e.g. the window closed): don't report a failure we can't see
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


# ---------------------------------------------------------------------- web

KNOWN_SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "facebook": "https://www.facebook.com", "linkedin": "https://www.linkedin.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com", "wikipedia": "https://www.wikipedia.org", "netflix": "https://www.netflix.com",
    "whatsapp": "https://web.whatsapp.com", "google drive": "https://drive.google.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "instagram": "https://www.instagram.com", "twitter": "https://x.com",
    "x": "https://x.com", "tiktok": "https://www.tiktok.com", "reddit": "https://www.reddit.com",
    "messenger": "https://www.messenger.com", "amazon": "https://www.amazon.com", "pinterest": "https://www.pinterest.com",
    "twitch": "https://www.twitch.tv", "outlook": "https://outlook.live.com", "telegram": "https://web.telegram.org",
    "google docs": "https://docs.google.com", "google sheets": "https://sheets.google.com",
    "google calendar": "https://calendar.google.com", "google photos": "https://photos.google.com",
    "google translate": "https://translate.google.com", "gemini": "https://gemini.google.com",
    "spotify": "https://open.spotify.com", "discord": "https://discord.com/app", "bing": "https://www.bing.com",
    "perplexity": "https://www.perplexity.ai", "copilot": "https://copilot.microsoft.com",
}
SEARCH_URLS = {
    "google": "https://www.google.com/search?q={}", "youtube": "https://www.youtube.com/results?search_query={}",
    "bing": "https://www.bing.com/search?q={}", "duckduckgo": "https://duckduckgo.com/?q={}",
}
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](-?[a-z0-9])*\.)+[a-z]{2,24}(/[^\s]*)?$", re.I)


def website_url(site: str) -> str:
    """Known site name, bare domain or http(s) URL -> URL. Anything else is refused."""
    text = site.strip()
    known = KNOWN_SITES.get(normalize(text)) or KNOWN_SITES.get(text.lower())
    if known:
        return known
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("Only http and https web addresses can be opened.", "invalid_url")
        return text
    local = re.fullmatch(r"(localhost|127\.0\.0\.1)(:\d{2,5})?(/[^\s]*)?", text, re.I)
    if local:
        return f"http://{text}"
    if _DOMAIN.match(text):
        return f"https://{text}"
    raise ToolError(f"'{site}' doesn't look like a website. Try a name like youtube or an address like example.com.", "invalid_url")


def _open_url(url: str) -> None:
    if not webbrowser.open(url, new=2):
        raise ToolError("No web browser could be opened.", "no_browser")


# Windows default-browser ProgId prefix -> app id in config/apps.json
_PROGID_BROWSERS = (("chromehtml", "chrome"), ("msedgehtm", "edge"), ("firefoxurl", "firefox"), ("firefoxhtml", "firefox"))
BROWSER_EXES = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
                          "iexplore.exe"})
_BROWSER_WORDS = {"chrome": "chrome", "google chrome": "chrome", "edge": "edge", "microsoft edge": "edge",
                  "firefox": "firefox", "mozilla firefox": "firefox", "default": "default", "browser": "default",
                  "my browser": "default", "default browser": "default"}


def browser_id(name: str | None) -> str | None:
    """'Google Chrome' -> 'chrome'; None if it isn't a browser name."""
    return _BROWSER_WORDS.get(normalize(name or "", keep_filler=True)) if name else None


def _check_openable(url: str) -> None:
    if not re.match(r"^https?://", url, re.I) or re.search(r"[\s\x00-\x1f\"<>]", url):
        raise ToolError("That web address can't be opened.", "invalid_url")


def _data_dir(ctx: ToolContext) -> Path | None:
    return ctx.paths.data_dir if ctx.paths is not None else None


def browser_profile_arguments(exe_path: str, wanted: str | None, ctx: ToolContext):
    """profile_arguments() that keeps using the profile Jarvis used last (and remembers a new choice)."""
    from app.tools.browser_profiles import pin_profile, pinned_profile, profile_arguments

    arguments, profile = profile_arguments(exe_path, wanted, pinned_profile(exe_path, _data_dir(ctx)))
    if profile is not None:
        pin_profile(exe_path, profile.directory, _data_dir(ctx))
    return arguments, profile


def browser_target(ctx: ToolContext, browser: str | None = None) -> LaunchTarget | None:
    """The installed browser (an .exe) web pages open in; None = hand the URL to Windows' default."""
    chosen = browser_id(browser) or ctx.settings.web_browser
    if chosen == "default":
        progid = (winapi.default_browser_progid() or "").lower() if winapi.is_windows() else ""
        chosen = next((app for prefix, app in _PROGID_BROWSERS if progid.startswith(prefix)), "default")
    if chosen != "default" and winapi.is_windows():
        target = resolve_application(chosen, _apps(ctx))
        if target is not None and target.target.lower().endswith(".exe"):
            return target
        if browser_id(browser) not in (None, "default"):
            raise ToolError(f"{browser} doesn't seem to be installed.", "not_found")
    return None


def open_url(url: str, ctx: ToolContext, browser: str | None = None, *, same_tab: str | None = None,
             load: bool = True) -> str:
    """Open an http(s) URL in the chosen browser (Chrome/Edge in the configured profile). Returns where.

    ``same_tab="youtube"``: if that site already has a tab, it is reused (``load=False`` only switches to
    it) instead of opening another tab. Everything else opens in a new tab of the same browser/profile.
    """
    _check_openable(url)
    target = browser_target(ctx, browser)
    if target is None:
        _open_url(url)
        return "your browser"
    exe = PureWindowsPath(target.target).name.lower()
    if same_tab and getattr(ctx.settings, "reuse_tabs", True):
        from app.tools.browser_tabs import reuse_tab

        if reuse_tab(exe, same_tab, url if load else None, ctx.step_aside):
            return f"{target.display_name} (same tab)"
    try:
        arguments, profile = browser_profile_arguments(target.target, _browser_profile_setting(target.target, ctx), ctx)
    except ToolError:  # a wrong profile setting shouldn't stop websites from opening
        arguments, profile = [], None
    running_before = _browser_running(exe)
    winapi.start_program(target.target, [*arguments, url])
    if not running_before and not _wait_for(lambda: _browser_running(exe), BROWSER_START_SECONDS, 0.25):
        # self-check: the browser didn't start at all -> hand the page to Windows' default browser instead
        log_event(_log, "browser_did_not_start", browser=exe)
        _open_url(url)
        return f"your default browser ({target.display_name} didn't start)"
    return target.display_name + (f" ({profile.name} profile)" if profile is not None else "")


BROWSER_START_SECONDS = 8.0


def _browser_running(exe: str) -> bool:
    try:
        return any(w.process_name == exe for w in winapi.top_level_windows())
    except Exception:
        return True  # can't check: don't open it twice


def site_key(site: str) -> str | None:
    """'YouTube', 'youtube.com', 'www.facebook.com' -> the site's plain name; None for URLs with a path etc."""
    text = site.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
    if not text or "/" in text or "?" in text:
        return None
    if normalize(text) in KNOWN_SITES:
        return normalize(text)
    match = re.fullmatch(r"([a-z0-9-]+)\.(com|org|net|tv|io|ai|app|me|co)", text)
    return match.group(1) if match else None


def installed_app_for_site(site: str, ctx: ToolContext) -> LaunchTarget | None:
    """The installed app for a site (WhatsApp, Instagram, Spotify...), never a web browser."""
    key = site_key(site)
    if key is None or not winapi.is_windows():
        return None
    target = resolve_application(key, _apps(ctx))
    if target is None:
        return None
    exe = PureWindowsPath(target.target).name.lower()
    names = {p.lower() for p in target.process_names}
    if exe in BROWSER_EXES or names & BROWSER_EXES or browser_id(target.display_name):
        return None
    if app_base_name(target.display_name) != re.sub(r"[^a-z0-9]", "", key.lower()):  # "Xbox" is not the app for "x"
        return None
    return target


_APP_NAME_EXTRAS = re.compile(r"\b(microsoft|desktop|app|for windows|beta|preview|new|\(.*?\))\b|\(.*?\)", re.I)


def app_base_name(display_name: str) -> str:
    """'Telegram Desktop' -> 'telegram', 'Outlook (new)' -> 'outlook', 'Microsoft Outlook' -> 'outlook'."""
    main = display_name.split(" - ")[0].split(" – ")[0]
    return re.sub(r"[^a-z0-9]", "", _APP_NAME_EXTRAS.sub(" ", main).lower())


class WebsiteArgs(ToolArgs):
    site: str = Field(min_length=1, max_length=300, description="Site name (youtube, facebook, gmail...), domain or URL")
    browser: str | None = Field(default=None, max_length=30,
                                description="Only if the user names a browser: 'chrome', 'edge' or 'firefox'")


def open_website(args: WebsiteArgs, ctx: ToolContext) -> ToolResult:
    if args.browser is None:
        app = installed_app_for_site(args.site, ctx)
        if app is not None:  # e.g. the WhatsApp or Instagram app is installed: use it
            return _launch(app, ctx)
    try:
        url = website_url(args.site)
    except ToolError as exc:
        if exc.code != "invalid_url" or "://" in args.site:
            raise
        # a name Jarvis doesn't know the address of ("Netmirror"): search for it rather than refuse
        url = SEARCH_URLS["google"].format(quote_plus(args.site))
        where = open_url(url, ctx, args.browser)
        return ToolResult.success(f"I don't know the address of {args.site}, so I searched for it in {where}. "
                                  "Tell me its address (like example.com) to open it directly next time.", url=url,
                                  searched=True)
    key = site_key(args.site)
    # "open YouTube" while a song plays: show that tab (the song keeps playing); other sites get a new tab
    where = open_url(url, ctx, args.browser, same_tab=key, load=False) if key == "youtube" else open_url(
        url, ctx, args.browser)
    return ToolResult.success(f"Opened {urlparse(url).hostname} in {where}.", url=url)


class SearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=200, description="What to search for")
    engine: Literal["google", "youtube", "bing", "duckduckgo"] = Field(default="google", description="Where to search")


def search_in_browser(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    url = SEARCH_URLS[args.engine].format(quote_plus(args.query))
    where = open_url(url, ctx, same_tab="youtube" if args.engine == "youtube" else None)
    return ToolResult.success(
        f"Opened a {args.engine} search for '{args.query}' in {where}. I haven't read the results.", url=url
    )


# Never closed by "close everything": Jarvis itself, and the parts of Windows you'd lose the desktop without.
KEEP_OPEN = frozenset({
    "explorer.exe:shell", "jarvis.exe", "jarvis-cli.exe", "textinputhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "lockapp.exe", "applicationframehost.exe:empty",
    "systemsettingsbroker.exe", "ollama.exe", "ollama app.exe", "nvidia overlay.exe", "nvcontainer.exe",
})
_SHELL_TITLES = {"program manager", ""}


class CloseAllArgs(ToolArgs):
    keep: str | None = Field(default=None, max_length=200,
                             description="Apps to leave open, comma-separated, only if the user names them "
                                         "('close everything except Chrome' -> 'Chrome')")


def _keeps(window: winapi.WindowInfo, keep: list[str]) -> bool:
    name = window.process_name.lower()
    if window.pid == os.getpid() or name in KEEP_OPEN:
        return True
    if name == "explorer.exe" and window.title.strip().lower() in _SHELL_TITLES:
        return True  # the desktop itself (File Explorer folders are closed)
    label = f"{window.title} {name}".lower()
    return any(k and (k in label or normalize(k) in normalize(label)) for k in keep)


def close_all_apps(args: CloseAllArgs, ctx: ToolContext) -> ToolResult:
    """Close every open app window normally (like clicking X): apps with unsaved work still ask first.
    Jarvis stays open, and so do the desktop, the taskbar and Windows' own parts. Background services
    and processes without a window are left alone - ending those can break Windows."""
    winapi.require_windows()
    keep = [k.strip().lower() for k in (args.keep or "").replace(" and ", ",").split(",") if k.strip()]
    targets = [w for w in winapi.top_level_windows() if w.switchable and not _keeps(w, keep)]
    if not targets:
        return ToolResult.success("There are no open apps to close.", closed=[], still_open=[])
    for window in targets:
        try:
            winapi.close_window(window.hwnd)
        except Exception:
            _log.warning("could not close a window", exc_info=True)
    handles = {w.hwnd for w in targets}
    _wait_for(lambda: not handles & {w.hwnd for w in winapi.top_level_windows()}, 5.0, 0.25)
    remaining = {w.hwnd for w in winapi.top_level_windows()} & handles

    def names(windows) -> list[str]:
        seen: list[str] = []
        for w in windows:
            label = app_label(w)
            if label not in seen:
                seen.append(label)
        return seen

    closed = names(w for w in targets if w.hwnd not in remaining)
    waiting = names(w for w in targets if w.hwnd in remaining)
    log_event(_log, "closed_all_apps", closed=len(closed), still_open=len(waiting))
    message = f"Closed {len(closed)} app{'s' if len(closed) != 1 else ''}"
    message += f": {', '.join(closed[:8])}." if closed else "."
    if waiting:
        message += (f" Still open (probably asking to save your work): {', '.join(waiting[:6])}. "
                    "Answer them, or say 'force quit' with the app's name.")
    return ToolResult.success(message, closed=closed, still_open=waiting)


def app_label(window: winapi.WindowInfo) -> str:
    """ "main.py - Jarvis - Visual Studio Code" -> "Visual Studio Code"; else the program name."""
    parts = [p.strip() for p in re.split(r" [-\u2013\u2014|] ", window.title) if p.strip()]
    if len(parts) > 1:
        return parts[-1][:40]
    stem = window.process_name.rsplit(".", 1)[0]
    if stem.lower() == "explorer":
        return "File Explorer"
    return (stem[:1].upper() + stem[1:])[:40] if stem else window.title[:40]


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="open_application",
        description="Open (launch/start) an installed application, e.g. Chrome, VS Code, Spotify, Notepad. "
                    "For websites (YouTube, Facebook, Instagram...) use open_website instead.",
        handler=open_application, args_model=OpenAppArgs, level=RiskLevel.SAFE, category="apps", timeout=20,
    ),
    Tool(
        name="close_application",
        description="Close an open application normally (like clicking X; it can still ask to save).",
        handler=close_application, args_model=AppNameArgs, level=RiskLevel.SAFE, category="apps", timeout=15,
    ),
    Tool(
        name="restart_application",
        description="Restart an application: close it normally and open it again ('restart Chrome'). Not the PC.",
        handler=restart_application, args_model=AppNameArgs, level=RiskLevel.SAFE, category="apps", timeout=30,
    ),
    Tool(
        name="close_all_apps",
        description="Close ALL open apps at once, normally (like clicking X on each; unsaved work still asks). "
                    "'close everything', 'close all apps'. Jarvis and Windows itself stay. Optionally keep some.",
        handler=close_all_apps, args_model=CloseAllArgs, level=RiskLevel.SAFE, category="apps", timeout=20,
    ),
    Tool(
        name="window_control",
        description="Switch to, minimize, maximize or restore an application's window; minimize_all shows the desktop. "
                    "NOT for closing apps (use close_application) and NOT for locking the PC (use power_control lock).",
        handler=window_control, args_model=WindowArgs, level=RiskLevel.SAFE, category="apps", timeout=10,
        precheck=lambda a, c: _window_precheck(a),
    ),
    Tool(
        name="open_website",
        description="Open a website or web app for the user: YouTube, Facebook, Instagram, Gmail, a domain or URL. "
                    "Uses the installed app if there is one (e.g. WhatsApp), otherwise the user's browser and profile. "
                    "Set browser only if the user names one ('open YouTube in Chrome'). Any site name works: unknown "
                    "names are searched for, so never refuse. You can NOT read the page "
                    "afterwards: to read, summarise or click on a page, use browser_open.",
        handler=open_website, args_model=WebsiteArgs, level=RiskLevel.SAFE, category="web", needs_network=True,
    ),
    Tool(
        name="search_in_browser",
        description="Open a Google or YouTube search page in the user's own browser for them to look at. "
                    "To find information and answer yourself, use web_search instead.",
        handler=search_in_browser, args_model=SearchArgs, level=RiskLevel.SAFE, category="web", needs_network=True,
    ),
)
