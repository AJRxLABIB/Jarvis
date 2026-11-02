"""Configuration profiles: named sets of everyday settings you can switch between ("meeting", "night", "performance").

A profile holds ONLY the fields in ``PROFILE_FIELDS`` - how loud, how talkative, how animated, how proactive. It can never
contain a secret, a path, a device, a URL, or anything that guards your PC (confirmations, signature checks, direct
commands, remote access, the screen blocklist): those are deliberately outside the allowlist, so importing or applying a
profile cannot weaken security. Switching shows exactly what will change first and remembers what it replaced, so
"go back" restores your previous settings in one step.

Profiles are stored in the database (settings_kv), not in .env; applying one writes the individual settings through the
same path as "change setting" (validated, saved to .env, applied live).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config.settings import Settings

# Everyday behaviour only. Fields that widen what Jarvis can SEE are listed separately so the confirmation can name them.
PROFILE_FIELDS: tuple[str, ...] = (
    "tts_enabled", "tts_speed", "tts_volume", "speak_typed_replies", "wake_sound", "follow_up_seconds", "barge_in",
    "hands_free", "voice_interrupt",
    "proactive_alerts", "proactive_suggestions", "proactive_level", "proactive_max_per_hour", "quiet_hours",
    "pc_watcher", "weekly_health_check", "auto_sort_downloads", "low_battery_percent",
    "animations", "intro_animation", "always_on_top", "start_fullscreen", "start_minimized", "mini_orb", "monitor_interval",
    "metrics_history",
    "screen_capture_enabled", "presence_detection", "context_window_titles", "process_history", "offline_mode",
)
WIDENS_PRIVACY = frozenset({"screen_capture_enabled", "presence_detection", "gesture_control", "context_window_titles", "process_history", "cyber_guardian"})

BUILTIN: dict[str, dict[str, str]] = {
    "meeting": {"tts_enabled": "false", "wake_sound": "false", "proactive_level": "minimal", "hands_free": "false",
                "always_on_top": "false", "start_fullscreen": "false", "speak_typed_replies": "false"},
    "night": {"tts_volume": "0.4", "wake_sound": "false", "proactive_level": "minimal", "animations": "false",
              "quiet_hours": "20:00-08:00"},
    "performance": {"animations": "false", "intro_animation": "false", "monitor_interval": "10.0", "metrics_history": "false",
                    "mini_orb": "false", "proactive_level": "minimal"},
}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,29}$")
PREVIOUS = "_previous"
_PREFIX = "profile."
_PREVIOUS_KEY = "profile_previous_values"
_NAMES_KEY = "profile_names"


class ProfileError(Exception):
    pass


def check_name(name: str) -> str:
    name = " ".join(name.split()).lower()
    if not _NAME.match(name):
        raise ProfileError("A profile name starts with a letter and has up to 30 letters, digits, spaces, - or _.")
    return re.sub(r"[ -]+", "_", name)


def render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def capture(settings: Settings, fields: tuple[str, ...] = PROFILE_FIELDS) -> dict[str, str]:
    return {f: render(getattr(settings, f)) for f in fields if hasattr(settings, f)}


def clean(values: dict[str, Any]) -> dict[str, str]:
    """Keep only allowed fields, as text. Anything else (a secret, a path, a security setting) is dropped, never applied."""
    if not isinstance(values, dict):
        raise ProfileError("A profile is a set of field names and values.")
    return {f: render(v)[:80] for f, v in values.items() if f in PROFILE_FIELDS}


def validate(values: dict[str, str]) -> None:
    """Every value must be accepted by the real settings model (nothing is saved here)."""
    try:
        Settings.model_validate(values)
    except Exception as exc:
        raise ProfileError(f"That profile has a value the settings don't accept: {str(exc)[:160]}") from None


def diff(settings: Settings, profile: dict[str, str]) -> dict[str, tuple[str, str]]:
    """{field: (current, new)} for what applying ``profile`` would actually change."""
    now = capture(settings)
    return {f: (now.get(f, ""), v) for f, v in profile.items() if f in PROFILE_FIELDS and now.get(f) != v}


def describe_diff(changes: dict[str, tuple[str, str]]) -> str:
    if not changes:
        return "Nothing would change; those settings already match."
    lines = [f"{f.replace('_', ' ')}: {old} -> {new}" for f, (old, new) in changes.items()]
    wider = sorted(f.replace("_", " ") for f in changes if f in WIDENS_PRIVACY and changes[f][1] in ("true", "on"))
    if wider:
        lines.append("This lets Jarvis see more: " + ", ".join(wider) + ".")
    return "; ".join(lines)


class ProfileStore:
    def __init__(self, settings_store) -> None:
        self._kv = settings_store

    def names(self) -> list[str]:
        saved = self._kv.get(_NAMES_KEY, []) or []
        return sorted(set(BUILTIN) | {n for n in saved if isinstance(n, str)})

    def get(self, name: str) -> dict[str, str]:
        name = check_name(name) if name != PREVIOUS else name
        if name == PREVIOUS:
            data = self._kv.get(_PREVIOUS_KEY)
        else:
            data = self._kv.get(_PREFIX + name)
            if data is None:
                data = BUILTIN.get(name)
        if not data:
            raise ProfileError(f"There's no profile called '{name}'. Profiles: {', '.join(self.names()) or 'none'}.")
        return clean(data)

    def save(self, name: str, values: dict[str, Any]) -> str:
        name = check_name(name)
        values = clean(values)
        if not values:
            raise ProfileError("Nothing to save.")
        validate(values)
        self._kv.set(_PREFIX + name, values)
        names = set(self._kv.get(_NAMES_KEY, []) or [])
        names.add(name)
        self._kv.set(_NAMES_KEY, sorted(names))
        return name

    def remember_previous(self, values: dict[str, str]) -> None:
        self._kv.set(_PREVIOUS_KEY, values)

    def delete(self, name: str) -> None:
        name = check_name(name)
        if name in BUILTIN and self._kv.get(_PREFIX + name) is None:
            raise ProfileError(f"'{name}' is built in and can't be deleted (you can save your own version with that name).")
        self._kv.delete(_PREFIX + name)
        names = set(self._kv.get(_NAMES_KEY, []) or [])
        names.discard(name)
        self._kv.set(_NAMES_KEY, sorted(names))

    def export(self, name: str) -> str:
        return json.dumps({"profile": check_name(name), "settings": self.get(name)}, indent=2)

    def import_text(self, text: str, name: str | None = None) -> str:
        """Import an exported profile. Disallowed fields are dropped silently (and reported), values are validated."""
        try:
            data = json.loads(text)
            values, label = data["settings"], data.get("profile", "imported")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProfileError("That isn't an exported Jarvis profile.") from None
        return self.save(name or str(label), values)
