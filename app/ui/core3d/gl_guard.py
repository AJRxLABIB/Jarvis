"""Start-up safety net for the OpenGL core.

Some GPU drivers crash natively (access violation, no Python frame) right after the 3D window first appears, e.g.
when the GPU is also busy loading the local AI and speech models. That crash can't be caught, so it is remembered:
data/gl_guard.json records whether the last run got past "window shown" to "GL stable". If a run crashed in between,
the next start skips the start-up intro and fades - and says so. The 3D core and its quality are never stepped down
(the flat core is used only when CORE_RENDERER=painter is set by hand, or when OpenGL is not available at all).
A run that reaches "stable" at full settings clears the strikes. ``python run.py --reset-gl-guard`` (or deleting
the file) restores the intro.
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = "gl_guard.json"
SHOWING, STABLE = "showing", "stable"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def load(data_dir: Path) -> dict:
    try:
        state = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data_dir: Path, state: dict) -> None:
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        _path(data_dir).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def decide(data_dir: Path, previous_crashed: bool) -> int:
    """At start-up: how many crashed GL starts in a row (0 = all fine). Counts the last run if it crashed between
    showing the window and the core becoming stable."""
    state = load(data_dir)
    strikes = int(state.get("strikes", 0) or 0)
    if previous_crashed and state.get("phase") == SHOWING:
        strikes += 1
    state.update(strikes=strikes, phase="starting")
    _save(data_dir, state)
    return strikes


def overrides(strikes: int, settings) -> dict:
    """Settings for THIS run only (nothing is written to .env).

    The 3D core and its quality are NEVER changed here (the owner's decision: always the full 3D core at the chosen quality). After a crashed start
    only the start-up intro and fades are skipped - they are the part that differs between a start that crashed and one that ran. To use the flat
    core you have to choose it yourself with CORE_RENDERER=painter in .env."""
    if strikes <= 0:
        return {}
    return {"intro_animation": False}


def notice(strikes: int, changed: dict) -> str:
    if not changed:
        return ""
    more = (" It has now crashed several starts in a row: if it keeps happening, update the graphics driver, or set VISUAL_QUALITY=balanced "
            "(or CORE_RENDERER=painter for the flat core) in .env - Jarvis will not switch them for you.") if strikes >= 3 else ""
    return (f"Jarvis crashed while the 3D core started ({strikes}x). The 3D core and its quality are unchanged; only the start-up intro and fades "
            f"are skipped for now. 'python run.py --reset-gl-guard' brings the intro back.{more}")


def mark(data_dir: Path, phase: str, *, full_settings: bool = True) -> None:
    state = load(data_dir)
    state["phase"] = phase
    if phase == STABLE and full_settings:
        state["strikes"] = 0
    _save(data_dir, state)


def reset(data_dir: Path) -> bool:
    try:
        _path(data_dir).unlink()
        return True
    except OSError:
        return False
