"""Start-up safety net for the OpenGL core.

Some GPU drivers crash natively (access violation, no Python frame) right after the 3D window first appears, e.g.
when the GPU is also busy loading the local AI and speech models. That crash can't be caught, so it is remembered:
data/gl_guard.json records whether the last run got past "window shown" to "GL stable". If a run crashed in between,
the next start steps down - safe start (no intro/fades) + balanced quality, then the flat (painter) core - and says
so. A run that reaches "stable" at full settings clears the strikes. ``python run.py --reset-gl-guard`` (or deleting
the file) restores everything; CORE_RENDERER=gl in .env bypasses the step-down to the painter core.
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
    """Settings for THIS run only (nothing is written to .env)."""
    if strikes <= 0:
        return {}
    out: dict = {"intro_animation": False}
    if getattr(settings, "visual_quality", "auto") in ("auto", "high"):
        out["visual_quality"] = "balanced"
    if strikes >= 2 and getattr(settings, "core_renderer", "auto") == "auto":
        out["core_renderer"] = "painter"
    return out


def notice(strikes: int, changed: dict) -> str:
    if not changed:
        return ""
    parts = ["the start-up intro and fades are off", "quality is balanced" if "visual_quality" in changed else ""]
    if changed.get("core_renderer") == "painter":
        parts.append("the flat core is used instead of the 3D one")
    done = ", ".join(p for p in parts if p)
    return (f"The graphics driver crashed while the 3D core started ({strikes}x). To keep Jarvis running, {done} for "
            "now. Update the GPU driver, then run 'python run.py --reset-gl-guard' to restore everything.")


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
