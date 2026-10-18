from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.ai import switching
from app.ai.base import ChatMessage, ToolSpec
from app.ai.manager import AIManager
from app.config.settings import Settings
from tests.test_v3190 import Failing, models

E = switching.Evidence
EVIDENCE = {"small": E("small", 85.0, 1), "big": E("big", 96.0, 0), "other": E("other", 90.0, 0)}


def test_untrusted_text_moves_a_model_with_a_safety_failure_to_the_best_clean_one() -> None:
    pick = switching.decide("small", ["small", "big", "other", "unbenchmarked"], EVIDENCE, {}, untrusted=True, tools=True)
    assert pick and pick.model == "big" and "untrusted text" in pick.reason


def test_no_switch_without_untrusted_text_tools_or_evidence() -> None:
    assert switching.decide("small", ["small", "big"], EVIDENCE, {}, untrusted=False, tools=True) is None
    assert switching.decide("small", ["small", "big"], EVIDENCE, {}, untrusted=True, tools=False) is None
    assert switching.decide("mystery", ["mystery", "big"], EVIDENCE, {}, untrusted=True, tools=True) is None   # no record
    assert switching.decide("small", ["small", "unbenchmarked"], EVIDENCE, {}, untrusted=True, tools=True) is None  # never guess


def test_a_model_that_keeps_malformed_tool_calls_is_replaced_by_a_better_clean_one() -> None:
    health = {"other": {"tool_calls": 10, "tool_call_ok_rate": 0.5}}
    pick = switching.decide("other", ["other", "big", "small"], EVIDENCE, health, untrusted=False, tools=True)
    assert pick and pick.model == "big" and "50%" in pick.reason
    few = {"other": {"tool_calls": 3, "tool_call_ok_rate": 0.0}}
    assert switching.decide("other", ["other", "big"], EVIDENCE, few, untrusted=False, tools=True) is None
    best = {"big": {"tool_calls": 10, "tool_call_ok_rate": 0.2}}
    assert switching.decide("big", ["big", "other"], EVIDENCE, best, untrusted=False, tools=True) is None  # nothing better


def test_evidence_is_the_newest_record_per_model_and_survives_bad_lines(tmp_path: Path) -> None:
    folder = tmp_path / "benchmark"
    folder.mkdir()
    rows = [{"model": "m", "score": 50, "safety_failures": ["x"]}, {"model": "m", "score": 90, "safety_failures": []}]
    (folder / "quality-history.jsonl").write_text("\n".join([json.dumps(r) for r in rows] + ["garbage", "{}"]), encoding="utf-8")
    got = switching.load_evidence(tmp_path)
    assert got["m"].score == 90 and got["m"].safety_failures == 0 and len(got) == 1
    assert switching.load_evidence(None) == {} and switching.load_evidence(tmp_path / "nope") == {}


def _history(tmp: Path) -> None:
    folder = tmp / "benchmark"
    folder.mkdir()
    rows = [{"model": "qwen3:4b-instruct", "score": 87, "safety_failures": ["Ignore this"]},
            {"model": "qwen3:8b", "score": 95.7, "safety_failures": []}]
    (folder / "quality-history.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


TOOL = ToolSpec(name="t", description="d", parameters={"type": "object", "properties": {}})


def test_the_manager_switches_only_when_enabled_and_untrusted(tmp_path: Path) -> None:
    _history(tmp_path)
    fake = Failing(models=models("qwen3:4b-instruct", "qwen3:8b"))
    off = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", data_dir=tmp_path), transport=fake.transport())
    assert asyncio.run(off.chat([ChatMessage.user("hi")], [TOOL], untrusted=True)).model == "qwen3:4b-instruct"
    on = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", ai_auto_switch=True, data_dir=tmp_path),
                                 transport=fake.transport())
    assert asyncio.run(on.chat([ChatMessage.user("hi")], [TOOL], untrusted=False)).model == "qwen3:4b-instruct"
    assert asyncio.run(on.chat([ChatMessage.user("hi")], [TOOL], untrusted=True)).model == "qwen3:8b"
    assert on.last_route["role"] == "auto-switch" and "untrusted" in on.last_route["switched"]
