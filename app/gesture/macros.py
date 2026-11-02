"""Gesture macros: a trigger gesture bound to ONE registered JARVIS tool (often ``run_workflow`` with a saved workflow name),
scoped to a profile, kept across restarts.

What makes it safe: the trigger must be a gesture that does not already drive the mouse (see MappingRegistry.TRIGGERS); a hold
time for FIST so relaxing a hand cannot fire it; a cooldown; tools above DANGEROUS are refused; shell-like tools are forbidden;
and the call goes through the normal ToolExecutor (permissions, confirmation, audit) as a NON-direct command, so a gesture can
never skip an approval. ``preview`` says exactly what a macro would do, with its risk, before anything is bound.
Recording a sequence is the existing teach-by-showing feature: save it as a workflow, then bind a gesture to ``run_workflow``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.gesture.safety import MappingRegistry

_LEVEL_WORDS = {0: "safe", 1: "asks first (moderate)", 2: "always asks you (dangerous)", 3: "critical"}


def preview(trigger: str, tool: str, args: dict, *, level: int, profile: str, hold_s: float, cooldown_s: float) -> str:
    when = f"{trigger.replace('_', ' ').lower()}" + (f" held {hold_s:.1f}s" if hold_s else "")
    where = "in every profile" if profile == "*" else f"only in the {profile} profile"
    what = tool + (f" {json.dumps(args, ensure_ascii=False)}" if args else "")
    return f"When you {when} ({where}), JARVIS runs {what}; {_LEVEL_WORDS.get(level, 'unknown risk')}; at most once every {cooldown_s:g}s."


class MacroStore:
    """JSON file in the data folder: the active profile and the macros. A corrupt or unknown entry is skipped, never fatal."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, mapping: MappingRegistry) -> str:
        """Fill ``mapping``; returns the saved profile name ("" when none)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        for trigger, m in (data.get("macros") or {}).items():
            try:
                mapping.add(trigger, m["tool"], m.get("args") or {}, cooldown_s=float(m.get("cooldown_s", 2.0)),
                            confirm=bool(m.get("confirm", True)), enabled=bool(m.get("enabled", True)),
                            profile=str(m.get("profile", "*")), hold_s=float(m.get("hold_s", 0.0)), note=str(m.get("note", "")),
                            level=int(m.get("level", 0)))
                mapping.items[trigger]["level"] = int(m.get("level", 0))
            except (KeyError, TypeError, ValueError):
                continue
        return str(data.get("profile", ""))

    def save(self, mapping: MappingRegistry, profile: str) -> None:
        body: dict[str, Any] = {"version": 1, "profile": profile, "macros": mapping.items}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
