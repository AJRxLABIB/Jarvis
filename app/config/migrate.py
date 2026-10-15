"""Settings upgrades: SETTINGS_VERSION in .env, and forward-only migrations.

When a new version changes what a setting should be (a renamed value, a better default
that old files still pin), a migration updates the user's .env once, keeps every other
line and comment, and bumps SETTINGS_VERSION. A copy of the old file is kept (.env.bak).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from app.config.env_writer import render_env
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("settings_migrate")
Migration = tuple[int, str, Callable[[Mapping[str, str]], dict[str, str]]]


def _thinking_model(env: Mapping[str, str]) -> dict[str, str]:
    # "qwen3:4b" became a thinking-only model that reasons for a minute before every answer
    return {"AI_MODEL": "qwen3:4b-instruct"} if env.get("AI_MODEL", "").strip() == "qwen3:4b" else {}


def _old_speech_defaults(env: Mapping[str, str]) -> dict[str, str]:
    old = (env.get("STT_MODEL"), env.get("STT_DEVICE"), env.get("STT_COMPUTE_TYPE"))
    return {"STT_MODEL": "auto", "STT_DEVICE": "auto", "STT_COMPUTE_TYPE": "auto"} if old == ("base.en", "cpu", "int8") else {}


def _yes_no_offline(env: Mapping[str, str]) -> dict[str, str]:
    value = env.get("OFFLINE_MODE", "").strip().lower()
    return {"OFFLINE_MODE": {"true": "on", "false": "auto", "1": "on", "0": "auto"}[value]} if value in (
        "true", "false", "1", "0") else {}


MIGRATIONS: tuple[Migration, ...] = (
    (1, "offline mode yes/no -> auto/on", _yes_no_offline),
    (2, "old speech defaults -> automatic model choice", _old_speech_defaults),
    (3, "thinking model -> instruct model", _thinking_model),
)
CURRENT_VERSION = MIGRATIONS[-1][0]


def read_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def migrate_env_file(path: Path) -> list[str]:
    """Apply pending migrations to ``path``. Returns the names of the migrations applied."""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    env = read_env(text)
    try:
        version = int(env.get("SETTINGS_VERSION", "0") or 0)
    except ValueError:
        version = 0
    if version >= CURRENT_VERSION:
        return []
    updates: dict[str, str] = {}
    applied: list[str] = []
    for number, name, migration in MIGRATIONS:
        if number <= version:
            continue
        changes = migration({**env, **updates})
        if changes:
            updates.update(changes)
            applied.append(name)
    updates["SETTINGS_VERSION"] = str(CURRENT_VERSION)
    path.with_name(path.name + ".bak").write_text(text, encoding="utf-8")
    path.write_text(render_env(text, updates), encoding="utf-8")
    log_event(_log, "settings_migrated", from_version=version, to_version=CURRENT_VERSION, applied=applied)
    return applied
