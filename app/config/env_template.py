"""A fresh ``.env`` made from the settings themselves: every setting, at its default, grouped by what it is for.

There is no separate ``.env.example`` to keep in step any more - the defaults live in one place (``Settings``) and this writes them out.
Secrets are always written empty. ``write_default_env`` never overwrites an existing file.
"""

from __future__ import annotations

from pathlib import Path

from app.config.env_writer import format_env_value
from app.config.groups import GROUPS
from app.config.settings import ENV_NAMES, SECRET_FIELDS, Settings


def _value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return format_env_value(str(value))


def default_env_text() -> str:
    defaults = Settings()
    lines = ["# Jarvis settings. Every setting is listed at its default; change a value and restart Jarvis.",
             "# Secrets (tokens, passwords) typed here are encrypted on the next start (Windows)."]
    seen: set[str] = set()
    for group, fields in GROUPS.items():
        lines += ["", f"# --- {group.replace('_', ' ')} " + "-" * max(4, 60 - len(group))]
        for field in fields:
            if field in ENV_NAMES and field not in seen:
                seen.add(field)
                lines.append(f"{ENV_NAMES[field]}=" + ("" if field in SECRET_FIELDS else _value(getattr(defaults, field))))
    rest = [f for f in ENV_NAMES if f not in seen]
    if rest:
        lines += ["", "# --- other " + "-" * 55]
        lines += [f"{ENV_NAMES[f]}=" + ("" if f in SECRET_FIELDS else _value(getattr(defaults, f))) for f in rest]
    return "\n".join(lines) + "\n"


def write_default_env(path: Path) -> bool:
    """Creates ``path`` with every setting at its default. False (and nothing written) when it already exists."""
    path = Path(path)
    if path.exists():
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(default_env_text(), encoding="utf-8")
    tmp.replace(path)       # atomic: either the whole file exists or nothing does
    return True
