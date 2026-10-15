"""Speed report: where the time of your requests goes (hearing, thinking, speaking), with tips.

Only timings and the model name are kept (data\\speed.json, the last 200 requests) - never what
was said.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

FILE = "speed.json"
KEEP = 200
STAGES = (("heard", "Hearing (speech recognition)"), ("ai", "Thinking (until the first words)"),
          ("voice", "Speaking (until you hear it)"))


class SpeedLog:
    def __init__(self, data_dir: Path | None) -> None:
        self.path = Path(data_dir) / FILE if data_dir is not None else None
        self.entries: list[dict[str, Any]] = []
        if self.path is not None and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = [e for e in data if isinstance(e, dict)][-KEEP:] if isinstance(data, list) else []
            except (OSError, ValueError):
                self.entries = []

    def add(self, timing: dict[str, Any]) -> dict[str, Any]:
        entry = {"at": datetime.now().isoformat(timespec="seconds")}
        entry.update({k: round(float(v), 3) for k, v in timing.items() if k in ("heard", "ai", "voice") and v is not None})
        self.entries.append(entry)
        del self.entries[:-KEEP]
        return entry

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries, indent=0), encoding="utf-8")
        except OSError:
            pass

    def report(self, last: int = 50) -> str:
        entries = self.entries[-last:]
        if not entries:
            return "No requests timed yet. Ask me something, then look again."
        lines = [f"Speed of the last {len(entries)} request(s):"]
        medians: dict[str, float] = {}
        for key, label in STAGES:
            values = sorted(e[key] for e in entries if key in e)
            if not values:
                continue
            p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
            medians[key] = median(values)
            lines.append(f"  {label}: typical {medians[key]:.1f} s, slow ones {p90:.1f} s")
        models: dict[str, int] = {}
        for e in entries:
            if e.get("model"):
                models[e["model"]] = models.get(e["model"], 0) + 1
        if models:
            lines.append("  Answered by: " + ", ".join(f"{m} ({n})" for m, n in sorted(models.items(), key=lambda x: -x[1])))
        lines += [f"  Tip: {tip}" for tip in tips(medians, models)]
        return "\n".join(lines)


def tips(medians: dict[str, float], models: dict[str, int]) -> list[str]:
    found = []
    if medians.get("heard", 0) > 1.5:
        found.append("hearing is slow: the speech model runs on the CPU or is large. With an NVIDIA GPU, set "
                     "STT_DEVICE=auto; otherwise try STT_MODEL=small.")
    if medians.get("ai", 0) > 3.0:
        found.append("thinking is slow: set AI_FAST_MODEL to a small model (e.g. llama3.2:1b) for quick requests, "
                     "and keep OLLAMA_KEEP_ALIVE long so the model stays loaded.")
    if medians.get("voice", 0) > 1.5:
        found.append("the voice starts late: the neural voice needs the internet; offline, TTS_ENGINE=piper is quicker.")
    direct = models.get("direct", 0)
    if models and direct / max(1, sum(models.values())) < 0.2:
        found.append("everyday commands (open, close, volume, time) are answered instantly without the AI - keep "
                     "DIRECT_COMMANDS on.")
    return found
