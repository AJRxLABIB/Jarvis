"""Saving settings changed in the app back to .env.

Comments, blank lines, ordering and unknown lines are kept. Only the given keys
change (added at the end if missing). The new file is validated with the same
loader the app uses BEFORE it replaces the old one, and is written atomically.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.config.settings import ConfigError, Settings, load_settings

_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_PLAIN = re.compile(r"^[A-Za-z0-9_.:/+@%-]*$")


def format_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ConfigError("Settings can't contain line breaks.")
    if _PLAIN.match(value):
        return value
    if "'" not in value:
        return f"'{value}'"  # single quotes: no escape processing, safe for Windows paths
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_env(text: str, updates: Mapping[str, str]) -> str:
    lines = text.splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        match = _KEY_LINE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={format_env_value(remaining.pop(key))}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Changed in the app -----------------------------------------------------")
        out.extend(f"{key}={format_env_value(value)}" for key, value in remaining.items())
    return "\n".join(out) + "\n"


def protect_secrets(updates: Mapping[str, str]) -> dict[str, str]:
    """Secret settings (tokens, keys) are written encrypted for your Windows account ("dpapi:...")."""
    from app.config.settings import ENV_NAMES, SECRET_FIELDS
    from app.security.protect import available, protect_text

    secret_envs = {ENV_NAMES[f] for f in SECRET_FIELDS}
    result = dict(updates)
    if available():
        for key, value in result.items():
            if key in secret_envs and value:
                result[key] = protect_text(value)
    return result


def encrypt_plain_secrets(path: Path) -> list[str]:
    """Secrets typed into .env by hand are encrypted at the next start (Windows). Returns the keys changed."""
    from app.config.settings import ENV_NAMES, SECRET_FIELDS, _read_env_file
    from app.security.protect import PREFIX, available

    path = Path(path)
    if not available() or not path.is_file():
        return []
    values = _read_env_file(path)
    plain = {ENV_NAMES[f]: values[ENV_NAMES[f]] for f in SECRET_FIELDS
             if values.get(ENV_NAMES[f]) and not values[ENV_NAMES[f]].startswith(PREFIX)}
    if plain:
        update_env_file(path, plain)
    return sorted(plain)


def update_env_file(path: Path, updates: Mapping[str, str]) -> Settings:
    """Apply ``updates`` (ENV_NAME -> text) to .env. Returns the validated settings.
    Raises ConfigError (and leaves the file untouched) if the result is invalid."""
    path = Path(path)
    updates = protect_secrets(updates)
    current = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    new_text = render_env(current, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        settings = load_settings(Path(tmp_name), environ={})  # raises ConfigError if invalid
        os.replace(tmp_name, path)
        return settings
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def overridden_by_environment(keys: list[str]) -> list[str]:
    """Keys that a real environment variable overrides (so .env changes won't show)."""
    return [key for key in keys if key in os.environ]
