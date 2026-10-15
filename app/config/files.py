"""JSON configuration files in ``config/``: applications, permissions, hotkeys.

These files are written by the user (later also by the Settings window) and
never by the language model. They are validated strictly: a broken file stops
startup with a precise message rather than silently falling back to values the
user did not choose. A *missing* file is recreated from the built-in defaults.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config.settings import ConfigError, format_validation_error

APPS_FILE = "apps.json"
PERMISSIONS_FILE = "permissions.json"
SHORTCUTS_FILE = "shortcuts.json"

_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:")
_ABS_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# Characters that have special meaning to cmd.exe / PowerShell. Launch values are
# never passed through a shell, but rejecting them keeps configs unambiguous.
_FORBIDDEN_CHARS = frozenset('&|;<>^"`\r\n')

PATH_TOKENS: tuple[str, ...] = (
    "{HOME}", "{DESKTOP}", "{DOCUMENTS}", "{DOWNLOADS}", "{PICTURES}", "{MUSIC}", "{VIDEOS}",
)
MODIFIERS: tuple[str, ...] = ("ctrl", "alt", "shift", "win")
_MODIFIER_ALIASES = {"control": "ctrl", "windows": "win", "super": "win", "cmd": "win"}
NAMED_KEYS = frozenset(
    {
        "space", "enter", "escape", "tab", "backspace", "delete", "insert", "home", "end",
        "pageup", "pagedown", "up", "down", "left", "right", "printscreen", "pause",
        *(f"f{i}" for i in range(1, 25)),
    }
)
REQUIRED_HOTKEY_ACTIONS: tuple[str, ...] = ("activate_assistant", "toggle_listening", "cancel")


def _reject_forbidden(value: str, what: str) -> str:
    bad = sorted(set(value) & _FORBIDDEN_CHARS)
    if bad:
        raise ValueError(f"{what} contains forbidden characters: {' '.join(repr(c) for c in bad)}")
    return value


def _normalize_name(value: str) -> str:
    return " ".join(value.lower().split())


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- apps


class LaunchSpec(_Strict):
    """How to start an application.

    * ``app_paths``  - executable registered under Windows "App Paths" (then PATH)
    * ``executable`` - executable name on PATH, or an absolute path
    * ``uri``        - protocol handler, e.g. ``spotify:`` or ``ms-settings:``
    ``candidates`` are fallback locations; environment variables like
    ``%LOCALAPPDATA%`` are expanded at launch time.
    """

    type: Literal["app_paths", "executable", "uri"]
    value: str = Field(min_length=1, max_length=260)
    candidates: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("value")
    @classmethod
    def _check_value(cls, value: str) -> str:
        return _reject_forbidden(value, "launch value")

    @field_validator("candidates")
    @classmethod
    def _check_candidates(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            value = _reject_forbidden(value.strip(), "launch candidate")
            if not value.lower().endswith(".exe"):
                raise ValueError(f"launch candidate must point to an .exe file: {value}")
            cleaned.append(value)
        return cleaned

    @model_validator(mode="after")
    def _check_type(self) -> "LaunchSpec":
        if self.type == "uri" and not _URI_SCHEME_RE.match(self.value):
            raise ValueError(f"uri launch value must start with a scheme such as 'spotify:' (got {self.value!r})")
        if self.type == "app_paths":
            if not self.value.lower().endswith(".exe") or any(sep in self.value for sep in "\\/"):
                raise ValueError("app_paths launch value must be a bare file name ending in .exe")
        return self


class AppEntry(_Strict):
    display_name: str = Field(min_length=1, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    launch: LaunchSpec
    process_names: list[str] = Field(default_factory=list, max_length=10)
    enabled: bool = True

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            alias = _normalize_name(value)
            if not 1 <= len(alias) <= 40:
                raise ValueError(f"alias must be 1-40 characters: {value!r}")
            if alias not in out:
                out.append(alias)
        return out

    @field_validator("process_names")
    @classmethod
    def _check_processes(cls, values: list[str]) -> list[str]:
        out = []
        for value in values:
            name = value.strip().lower()
            if not name.endswith(".exe") or any(sep in name for sep in "\\/"):
                raise ValueError(f"process name must be a file name ending in .exe: {value!r}")
            out.append(name)
        return out


class AppsConfig(_Strict):
    version: Literal[1] = 1
    applications: dict[str, AppEntry]

    @field_validator("applications")
    @classmethod
    def _check_ids(cls, apps: dict[str, AppEntry]) -> dict[str, AppEntry]:
        for app_id in apps:
            if not _APP_ID_RE.match(app_id):
                raise ValueError(f"application id {app_id!r} must be lowercase letters, digits or '_'")
        return apps

    @model_validator(mode="after")
    def _check_unique_names(self) -> "AppsConfig":
        owner: dict[str, str] = {}
        for app_id, entry in self.applications.items():
            for name in self._names_for(app_id, entry):
                if owner.setdefault(name, app_id) != app_id:
                    raise ValueError(f"name {name!r} is used by both '{owner[name]}' and '{app_id}'")
        return self

    @staticmethod
    def _names_for(app_id: str, entry: AppEntry) -> list[str]:
        names = [app_id, app_id.replace("_", " "), _normalize_name(entry.display_name), *entry.aliases]
        return list(dict.fromkeys(names))

    def alias_index(self) -> dict[str, str]:
        """Spoken/typed name -> application id, for enabled applications."""
        index: dict[str, str] = {}
        for app_id, entry in self.applications.items():
            if entry.enabled:
                for name in self._names_for(app_id, entry):
                    index[name] = app_id
        return index

    def resolve(self, name: str) -> str | None:
        """Exact name or alias first; then, only if exactly one app fits:
        every spoken word is in its name ("visual code" -> Visual Studio Code), or a close spelling."""
        wanted = _normalize_name(name)
        index = self.alias_index()
        if wanted in index:
            return index[wanted]
        words = set(wanted.split())
        if words and all(len(w) >= 3 for w in words):
            subset = {app_id for alias, app_id in index.items() if words <= set(alias.split())}
            if len(subset) == 1:
                return subset.pop()
        from difflib import get_close_matches

        close = {index[a] for a in get_close_matches(wanted, list(index), n=3, cutoff=0.86)}
        if len(close) == 1:
            return close.pop()
        if close:
            return None
        # misheard names: "bone link" -> Phone Link, "spot if I" -> Spotify
        from app.utils.phonetic import best_sounding

        heard = best_sounding(wanted, list(index), threshold=0.8)
        return index[heard] if heard is not None else None


# -------------------------------------------------------------------- permissions


class DevCommand(_Strict):
    """An allow-listed development command (argv prefix, never a shell string)."""

    argv: list[str] = Field(min_length=1, max_length=12)
    level: int = Field(default=0, ge=0, le=2)
    description: str = Field(default="", max_length=120)

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, values: list[str]) -> list[str]:
        for token in values:
            if not token.strip():
                raise ValueError("argv tokens must not be empty")
            _reject_forbidden(token, "argv token")
        return values


class PermissionsConfig(_Strict):
    version: Literal[1] = 1
    confirm_moderate: bool = True  # Level 1 actions ask for confirmation
    block_critical: bool = True  # Level 3 actions are refused
    tool_levels: dict[str, int] = Field(default_factory=dict)  # may only RAISE a tool's built-in level
    protected_processes: list[str] = Field(default_factory=list)
    safe_roots: list[str] = Field(default_factory=list)
    allowed_dev_commands: list[DevCommand] = Field(default_factory=list)

    @field_validator("tool_levels")
    @classmethod
    def _check_levels(cls, levels: dict[str, int]) -> dict[str, int]:
        for name, level in levels.items():
            if not _TOOL_NAME_RE.match(name):
                raise ValueError(f"invalid tool name {name!r}")
            if not 0 <= level <= 3:
                raise ValueError(f"level for {name!r} must be 0-3")
        return levels

    @field_validator("protected_processes")
    @classmethod
    def _lower_processes(cls, values: list[str]) -> list[str]:
        return sorted({v.strip().lower() for v in values if v.strip()})

    @field_validator("safe_roots")
    @classmethod
    def _check_roots(cls, values: list[str]) -> list[str]:
        for value in values:
            if not (value.startswith(PATH_TOKENS) or _ABS_WIN_PATH_RE.match(value)):
                raise ValueError(
                    f"safe root {value!r} must start with one of {', '.join(PATH_TOKENS)} or be an absolute path like D:\\Work"
                )
            _reject_forbidden(value, "safe root")
            if ".." in re.split(r"[\\/]", value):
                raise ValueError(f"safe root must not contain '..': {value!r}")
        return values


# ---------------------------------------------------------------------- hotkeys


def normalize_hotkey(combo: str) -> str:
    """'Ctrl + Shift + J' -> 'ctrl+shift+j'. Raises ValueError if invalid."""
    parts = [p.strip().lower() for p in combo.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"invalid hotkey {combo!r}")
    parts = [_MODIFIER_ALIASES.get(p, p) for p in parts]
    *mods, key = parts
    if key in MODIFIERS:
        raise ValueError(f"hotkey {combo!r} must end with a non-modifier key")
    if not (key in NAMED_KEYS or re.fullmatch(r"[a-z0-9]", key)):
        raise ValueError(f"unknown key {key!r} in hotkey {combo!r}")
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r} in hotkey {combo!r}")
    if len(set(mods)) != len(mods):
        raise ValueError(f"duplicate modifier in hotkey {combo!r}")
    ordered = [m for m in MODIFIERS if m in mods]
    return "+".join([*ordered, key])


class ShortcutsConfig(_Strict):
    version: Literal[1] = 1
    hotkeys: dict[str, str]

    @field_validator("hotkeys")
    @classmethod
    def _check_hotkeys(cls, hotkeys: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        seen: dict[str, str] = {}
        for action, combo in hotkeys.items():
            if not _ACTION_RE.match(action):
                raise ValueError(f"invalid hotkey action name {action!r}")
            value = normalize_hotkey(combo)
            if value in seen:
                raise ValueError(f"hotkey {value!r} is assigned to both '{seen[value]}' and '{action}'")
            seen[value] = action
            normalized[action] = value
        missing = [a for a in REQUIRED_HOTKEY_ACTIONS if a not in normalized]
        if missing:
            raise ValueError(f"missing hotkey action(s): {', '.join(missing)}")
        return normalized


# --------------------------------------------------------------------- defaults


def default_apps() -> AppsConfig:
    def app(name: str, launch: dict[str, Any], aliases: list[str], processes: list[str]) -> dict[str, Any]:
        return {"display_name": name, "aliases": aliases, "launch": launch, "process_names": processes}

    return AppsConfig.model_validate(
        {
            "applications": {
                "chrome": app(
                    "Google Chrome",
                    {"type": "app_paths", "value": "chrome.exe", "candidates": [
                        "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe",
                        "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",
                        "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe",
                    ]},
                    ["browser", "chrome browser"],
                    ["chrome.exe"],
                ),
                "edge": app(
                    "Microsoft Edge",
                    {"type": "app_paths", "value": "msedge.exe", "candidates": [
                        "%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe",
                        "%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe",
                    ]},
                    ["edge browser"],
                    ["msedge.exe"],
                ),
                "firefox": app(
                    "Mozilla Firefox",
                    {"type": "app_paths", "value": "firefox.exe", "candidates": [
                        "%ProgramFiles%\\Mozilla Firefox\\firefox.exe",
                    ]},
                    [],
                    ["firefox.exe"],
                ),
                "vscode": app(
                    "Visual Studio Code",
                    {"type": "app_paths", "value": "Code.exe", "candidates": [
                        "%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe",
                        "%ProgramFiles%\\Microsoft VS Code\\Code.exe",
                    ]},
                    ["code", "vs code", "vscode", "visual code", "editor", "code editor"],
                    ["code.exe"],
                ),
                "notepad": app("Notepad", {"type": "executable", "value": "notepad.exe"}, ["text editor"], ["notepad.exe"]),
                "calculator": app("Calculator", {"type": "uri", "value": "calculator:"}, ["calc"], ["calculatorapp.exe"]),
                "explorer": app(
                    "File Explorer", {"type": "executable", "value": "explorer.exe"}, ["files", "file manager"], []
                ),
                "terminal": app(
                    "Windows Terminal", {"type": "executable", "value": "wt.exe"}, ["windows terminal"], ["windowsterminal.exe"]
                ),
                "powershell": app("PowerShell", {"type": "executable", "value": "powershell.exe"}, [], ["powershell.exe"]),
                "cmd": app("Command Prompt", {"type": "executable", "value": "cmd.exe"}, ["command prompt"], ["cmd.exe"]),
                "task_manager": app("Task Manager", {"type": "executable", "value": "taskmgr.exe"}, [], ["taskmgr.exe"]),
                "settings": app("Windows Settings", {"type": "uri", "value": "ms-settings:"}, ["system settings"], []),
                "spotify": app("Spotify", {"type": "uri", "value": "spotify:"}, ["music"], ["spotify.exe"]),
                "discord": app("Discord", {"type": "uri", "value": "discord:"}, [], ["discord.exe"]),
            }
        }
    )


def default_permissions() -> PermissionsConfig:
    return PermissionsConfig.model_validate(
        {
            "protected_processes": [
                "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "memory compression",
                "msmpeng.exe", "securityhealthservice.exe", "spoolsv.exe", "audiodg.exe", "sihost.exe",
                "ctfmon.exe", "explorer.exe",
            ],
            "safe_roots": ["{DESKTOP}", "{DOCUMENTS}", "{DOWNLOADS}", "{PICTURES}", "{MUSIC}", "{VIDEOS}"],
            "allowed_dev_commands": [
                {"argv": ["git", "status"], "level": 0, "description": "Show repository status"},
                {"argv": ["git", "diff"], "level": 0, "description": "Show changes"},
                {"argv": ["git", "log", "--oneline"], "level": 0, "description": "Show recent commits"},
                {"argv": ["git", "branch"], "level": 0, "description": "List branches"},
                {"argv": ["git", "fetch"], "level": 0, "description": "Fetch from remotes"},
                {"argv": ["git", "switch", "-c"], "level": 1, "description": "Create and switch to a branch"},
                {"argv": ["git", "pull"], "level": 1, "description": "Pull from the tracked remote"},
                {"argv": ["npm", "test"], "level": 0, "description": "Run npm tests"},
                {"argv": ["npm", "run", "dev"], "level": 0, "description": "Start dev server"},
                {"argv": ["npm", "run", "build"], "level": 0, "description": "Build project"},
                {"argv": ["npm", "install"], "level": 1, "description": "Install npm dependencies"},
                {"argv": ["python", "-m", "pytest"], "level": 0, "description": "Run Python tests"},
                {"argv": ["python", "-m", "pip", "install", "-r", "requirements.txt"], "level": 1,
                 "description": "Install Python dependencies"},
                {"argv": ["python", "-m", "ruff", "check"], "level": 0, "description": "Lint Python code (read-only)"},
                {"argv": ["python", "-m", "flake8"], "level": 0, "description": "Lint Python code (read-only)"},
                {"argv": ["python", "-m", "mypy"], "level": 0, "description": "Type-check Python code"},
                {"argv": ["npm", "run", "lint"], "level": 0, "description": "Run the project's linter"},
                {"argv": ["npm", "run", "typecheck"], "level": 0, "description": "Type-check the project"},
            ],
        }
    )


def default_shortcuts() -> ShortcutsConfig:
    return ShortcutsConfig.model_validate(
        {"hotkeys": {"activate_assistant": "ctrl+space", "toggle_listening": "ctrl+shift+j", "cancel": "escape",
                     "emergency_stop": "ctrl+shift+f12"}}
    )


# ---------------------------------------------------------------------- loading

M = TypeVar("M", bound=BaseModel)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json_config(path: Path, model: type[M], default: M) -> M:
    if not path.exists():
        write_json_atomic(path, default.model_dump(mode="json"))
        return default
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate the BOM Notepad may add
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{format_validation_error(exc)}") from None


@dataclass(frozen=True)
class ConfigBundle:
    apps: AppsConfig
    permissions: PermissionsConfig
    shortcuts: ShortcutsConfig


def load_config_bundle(config_dir: Path) -> ConfigBundle:
    return ConfigBundle(
        apps=load_json_config(config_dir / APPS_FILE, AppsConfig, default_apps()),
        permissions=load_json_config(config_dir / PERMISSIONS_FILE, PermissionsConfig, default_permissions()),
        shortcuts=load_json_config(config_dir / SHORTCUTS_FILE, ShortcutsConfig, default_shortcuts()),
    )
