"""Automatic model switching, driven by evidence you collected - never by guesses.

Off by default (AI_AUTO_SWITCH=true). When on, the model chosen for a request may be replaced by another INSTALLED
model, but only in two situations, and only by a model whose own benchmark record (`python run.py --eval-quality --model <name>`) supports it:

* the conversation contains untrusted text (web page, email, document, screen) and the chosen model has a recorded
  safety failure (it obeyed an instruction hidden in text): use the best installed model with a clean safety record;
* the chosen model's tool calls have been malformed too often in this session (health record): use the best installed
  model with a clean safety record and a better score.

A model with no benchmark record is never a switch target, a model with no record is never switched away from, and when
nothing qualifies the original model is kept (and the reason is logged). Every switch is reported in "which model are
you using". Pure functions: no network, no model calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_TOOL_CALLS = 5
MIN_TOOL_OK_RATE = 0.8


@dataclass(frozen=True)
class Evidence:
    model: str
    score: float
    safety_failures: int


@dataclass(frozen=True)
class Switch:
    model: str
    reason: str


def load_evidence(data_dir: Path | None) -> dict[str, Evidence]:
    """The newest benchmark record per model, read from data\\benchmark\\quality-history.jsonl."""
    if data_dir is None:
        return {}
    from app.quality_eval import HISTORY, folder

    try:
        lines = (folder(data_dir) / HISTORY).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    found: dict[str, Evidence] = {}
    for line in lines:
        try:
            row: dict[str, Any] = json.loads(line)
            found[str(row["model"])] = Evidence(str(row["model"]), float(row["score"]),
                                                len(row.get("safety_failures") or []))
        except (ValueError, KeyError, TypeError):
            continue
    return found


def _same(a: str, b: str) -> bool:
    norm = lambda n: n.lower().removesuffix(":latest")  # noqa: E731
    return norm(a) == norm(b)


def _evidence_for(name: str, evidence: dict[str, Evidence]) -> Evidence | None:
    return next((e for k, e in evidence.items() if _same(k, name)), None)


def _best_safe(current: str, installed: list[str], evidence: dict[str, Evidence], min_score: float = 0.0) -> str | None:
    options = []
    for name in installed:
        e = _evidence_for(name, evidence)
        if e is not None and e.safety_failures == 0 and e.score > min_score and not _same(name, current):
            options.append((e.score, name))
    return max(options)[1] if options else None


def decide(current: str, installed: list[str], evidence: dict[str, Evidence], health: dict[str, dict], *,
           untrusted: bool, tools: bool) -> Switch | None:
    """A Switch when the evidence says another installed model is safer/better here, else None (keep ``current``)."""
    mine = _evidence_for(current, evidence)
    if untrusted and tools and mine is not None and mine.safety_failures > 0:
        target = _best_safe(current, installed, evidence)
        if target:
            return Switch(target, f"{current} obeyed hidden instructions in its benchmark ({mine.safety_failures} safety "
                                  f"failure(s)) and this conversation contains untrusted text")
    stats = next((v for k, v in health.items() if _same(k, current)), {})
    rate, calls = stats.get("tool_call_ok_rate"), stats.get("tool_calls", 0)
    if tools and rate is not None and calls >= MIN_TOOL_CALLS and rate < MIN_TOOL_OK_RATE:
        target = _best_safe(current, installed, evidence, min_score=mine.score if mine else 0.0)
        if target:
            return Switch(target, f"{current} made well-formed tool calls only {rate:.0%} of the time "
                                  f"({calls} calls) this session")
    return None
