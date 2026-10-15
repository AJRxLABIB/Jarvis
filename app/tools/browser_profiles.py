"""Chromium browser profiles (Chrome, Edge, Brave).

Opening Chrome with several profiles shows "Who's using Chrome?". Passing
``--profile-directory`` opens a profile directly. Profiles are read from the
browser's own ``Local State`` file. The directory passed on the command line
always comes from that file (never from what the model or the user typed),
and it is validated.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath

from app.tools.base import ToolError

# exe name -> (display name, User Data folder under %LOCALAPPDATA%)
CHROMIUM_BROWSERS: dict[str, tuple[str, str]] = {
    "chrome.exe": ("Google Chrome", r"Google\Chrome\User Data"),
    "msedge.exe": ("Microsoft Edge", r"Microsoft\Edge\User Data"),
    "brave.exe": ("Brave", r"BraveSoftware\Brave-Browser\User Data"),
}
LAST_USED = frozenset({"last", "last used", "default", "auto"})
NO_PROFILE = frozenset({"", "none", "picker", "ask"})
_DIRECTORY = re.compile(r"^(Default|Profile \d{1,4})$")


@dataclass(frozen=True)
class BrowserProfile:
    directory: str  # "Default", "Profile 3"
    name: str  # the name on the profile card, e.g. "Your Chrome", "Night"
    account: str  # the signed-in Google/Microsoft account name, e.g. "Labib Hossain" ("" if none)
    last_used: bool = False
    active_time: float = 0.0  # when the browser last used it (seconds since 1970; 0 = unknown)
    note: str = field(default="", compare=False)  # set when several profiles matched and one was picked

    def describe(self) -> str:
        return f"{self.name} ({self.account})" if self.account and self.account != self.name else self.name


def user_data_dir(exe_name: str, local_app_data: str | None = None) -> Path | None:
    browser = CHROMIUM_BROWSERS.get(exe_name.lower())
    base = local_app_data or os.environ.get("LOCALAPPDATA")
    if browser is None or not base:
        return None
    return Path(base).joinpath(*browser[1].split("\\"))


def load_profiles(user_data: Path) -> list[BrowserProfile]:
    """Profiles from the browser's Local State file ([] if it has none or can't be read)."""
    try:
        data = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
        profile = data.get("profile", {})
        cache = profile.get("info_cache", {})
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(cache, dict):
        return []
    last = profile.get("last_used") if isinstance(profile.get("last_used"), str) else "Default"
    found = []
    for directory, info in cache.items():
        if not isinstance(directory, str) or not _DIRECTORY.match(directory) or not isinstance(info, dict):
            continue  # guest/system profiles and anything unexpected are skipped
        name = str(info.get("name") or directory).strip()[:80]
        account = str(info.get("gaia_name") or info.get("gaia_given_name") or "").strip()[:80]
        try:
            active = float(info.get("active_time") or 0.0)
        except (TypeError, ValueError):
            active = 0.0
        found.append(BrowserProfile(directory, name, account, directory == last, active))
    found.sort(key=lambda p: (p.directory != "Default", int(p.directory.split()[-1]) if p.directory != "Default" else 0))
    if found and not any(p.last_used for p in found):  # last_used points at a deleted profile
        found = [replace(p, last_used=p.directory == found[0].directory) for p in found]
    return found


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", text.lower()).split())


def choose_profile(profiles: list[BrowserProfile], wanted: str | None, browser: str = "the browser",
                   pinned: str | None = None) -> BrowserProfile | None:
    """The profile to open: None = let the browser decide (it may show its profile picker).

    ``pinned`` is the profile directory Jarvis used last time. When you don't name a profile (or the
    name fits several), Jarvis keeps using that one, so songs and websites never jump between profiles.
    """
    key = _norm(wanted or "")
    if key in NO_PROFILE or not profiles:
        if key not in NO_PROFILE and key not in LAST_USED:
            raise ToolError(f"{browser} has no profiles to choose from.", "profile_not_found")
        return None
    kept = next((p for p in profiles if p.directory == pinned), None) if pinned else None
    if key in LAST_USED:
        return kept or next((p for p in profiles if p.last_used), profiles[0])
    for attribute in ("directory", "name", "account"):
        matches = [p for p in profiles if _norm(getattr(p, attribute)) == key]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return kept if kept in matches else _most_recent(matches, wanted or "", browser)
    partial = [p for p in profiles if key in _norm(p.name) or key in _norm(p.account)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        return kept if kept in partial else _most_recent(partial, wanted or "", browser)
    names = ", ".join(f"'{p.name}'" for p in profiles)
    raise ToolError(f"There is no {browser} profile called '{wanted}'. Profiles: {names}.", "profile_not_found")


def _most_recent(matches: list[BrowserProfile], wanted: str, browser: str) -> BrowserProfile:
    """Several profiles fit ("labib" = 'LABIB', 'Labib', ...): open the one used last instead of asking again."""
    chosen = next((p for p in matches if p.last_used), None) or max(matches, key=lambda p: p.active_time)
    return replace(chosen, note=f"{len(matches)} profiles are called '{wanted}', so I opened the one you used "
                                f"last ({chosen.directory}).")


def profile_arguments(exe_path: str, wanted: str | None,
                      pinned: str | None = None) -> tuple[list[str], BrowserProfile | None]:
    """Command-line arguments that open ``wanted`` in this browser ([] for other programs)."""
    exe_name = PureWindowsPath(exe_path).name.lower()
    if exe_name not in CHROMIUM_BROWSERS:
        return [], None
    user_data = user_data_dir(exe_name)
    if user_data is None:
        return [], None
    profile = choose_profile(load_profiles(user_data), wanted, CHROMIUM_BROWSERS[exe_name][0], pinned)
    if profile is None:
        return [], None
    if not _DIRECTORY.match(profile.directory):  # belt and braces: only Chrome's own directory names
        raise ToolError("That profile folder name is not valid.", "invalid_profile")
    return [f"--profile-directory={profile.directory}"], profile


# ------------------------------------------------------------------ one profile for everything Jarvis opens

PIN_FILE = "browser_profile.json"
_pins: dict[str, str] = {}
_loaded_from: set[str] = set()


def pinned_profile(exe_path: str, data_dir: Path | None) -> str | None:
    """The profile directory Jarvis last opened in this browser (remembered across restarts)."""
    exe = PureWindowsPath(exe_path).name.lower()
    if data_dir is not None and str(data_dir) not in _loaded_from:
        _loaded_from.add(str(data_dir))
        try:
            saved = json.loads((Path(data_dir) / PIN_FILE).read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if isinstance(key, str) and isinstance(value, str) and _DIRECTORY.match(value):
                        _pins.setdefault(key, value)
        except (OSError, ValueError):
            pass
    return _pins.get(exe)


def pin_profile(exe_path: str, directory: str, data_dir: Path | None) -> None:
    exe = PureWindowsPath(exe_path).name.lower()
    if not _DIRECTORY.match(directory) or _pins.get(exe) == directory:
        return
    _pins[exe] = directory
    if data_dir is None:
        return
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        (Path(data_dir) / PIN_FILE).write_text(json.dumps(_pins), encoding="utf-8")
    except OSError:
        pass
