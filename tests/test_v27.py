"""2.7: Bangla speech model, model comparison, health report, step checklist."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import eval_bangla, health, model_compare
from app.agent.conversation import step_checklist
from app.agent.shortcuts import match_shortcut
from app.agent.tool_manager import ToolExecution
from app.ai.base import ToolCall
from app.tools.base import ToolResult
from app.voice.speech_to_text import FasterWhisperSTT


def speech(n: int = 16000):
    return (0.1 * np.sin(np.arange(n) / 3)).astype(np.float32)


def seg(text: str, logprob: float = -0.1, no_speech: float = 0.01):
    return SimpleNamespace(text=text, avg_logprob=logprob, no_speech_prob=no_speech)


class Whisper:
    def __init__(self, name: str, text: str, language: str, fail: bool = False) -> None:
        self.name, self.text, self.language, self.fail = name, text, language, fail
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("broken model")
        return iter([seg(self.text)]), SimpleNamespace(language=kwargs.get("language") or self.language)


def make_stt(tmp_path: Path, main_language: str, bangla_fails: bool = False):
    models: dict[str, Whisper] = {}

    def factory(name, **kw):
        if name == "bangla-ct2":
            models[name] = Whisper(name, "ক্রোম খোলো", "bn", fail=bangla_fails)
        else:
            models[name] = Whisper(name, "ক্রম খল" if main_language == "bn" else "open chrome", main_language)
        return models[name]

    stt = FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, model_factory=factory, bangla_model="bangla-ct2",
                           background_upgrade=False)
    return stt, models


# ------------------------------------------------------------------ Bangla speech model

def test_bangla_model_is_used_for_bangla(tmp_path: Path) -> None:
    stt, models = make_stt(tmp_path, "bn")
    result = stt.transcribe(speech(), "bn")
    assert result.text == "ক্রোম খোলো"
    assert models["bangla-ct2"].calls[0]["language"] == "bn"


def test_bangla_model_is_not_used_for_english(tmp_path: Path) -> None:
    stt, models = make_stt(tmp_path, "en")
    assert stt.transcribe(speech(), "en").text == "open chrome"
    assert "bangla-ct2" not in models


def test_broken_bangla_model_is_turned_off_and_the_normal_model_keeps_working(tmp_path: Path) -> None:
    stt, models = make_stt(tmp_path, "bn", bangla_fails=True)
    assert stt.transcribe(speech(), "bn").text == "ক্রম খল"
    assert stt.transcribe(speech(), "bn").text == "ক্রম খল"
    assert len(models["bangla-ct2"].calls) == 1  # not retried every sentence


def test_no_bangla_model_by_default(tmp_path: Path) -> None:
    stt = FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, background_upgrade=False,
                           model_factory=lambda name, **kw: Whisper(name, "ক্রম খল", "bn"))
    assert stt.bangla_model == "" and stt.transcribe(speech(), "bn").text == "ক্রম খল"


# ------------------------------------------------------------------ model comparison

def test_pick_winner_prefers_the_faster_of_close_scores() -> None:
    a = model_compare.ModelScore("big", 90.0, 14, 15, 4000)
    b = model_compare.ModelScore("small", 88.0, 13, 15, 900)
    c = model_compare.ModelScore("tiny", 60.0, 5, 15, 300)
    assert model_compare.pick_winner([a, b, c]).model == "small"
    assert model_compare.pick_winner([a, c]).model == "big"
    assert model_compare.pick_winner([]) is None


def test_compare_scores_each_model_with_real_answers() -> None:
    questions = eval_bangla.QUESTIONS[:3]
    answers = {"good": {q.text: " ".join(q.contains_any[:1]) + " উত্তর" for q in questions},
               "bad": {q.text: "I don't know" for q in questions}}

    def ask_for(model):
        async def ask(question):
            return answers[model][question]
        return ask

    lines: list[str] = []
    results = asyncio.run(model_compare.compare(["good", "bad"], ask_for, lines.append, questions))
    by_name = {r.model: r for r in results}
    assert by_name["good"].score > by_name["bad"].score
    assert by_name["good"].fully_right == 3 and by_name["bad"].fully_right == 0
    assert "Best for you: good" in [line.strip() for line in lines]


def test_compare_is_limited_to_four_models() -> None:
    def ask_for(model):
        async def ask(question):
            return "ঢাকা"
        return ask

    results = asyncio.run(model_compare.compare([f"m{i}" for i in range(6)], ask_for, lambda _: None,
                                                eval_bangla.QUESTIONS[:1]))
    assert len(results) == model_compare.MAX_MODELS


# ------------------------------------------------------------------ health report

def test_health_compare_says_better_or_worse() -> None:
    lines = health.compare({"bangla_score": 80.0, "thinking_s": 1.5, "not_found": 3},
                           {"bangla_score": 70.0, "thinking_s": 1.0, "not_found": 3})
    assert "Bangla answer score: 80%  (better than 70%)" in lines
    assert "Typical thinking time: 1.5 s  (worse than 1 s)" in lines
    assert "Things it couldn't find (7 days): 3" in lines  # unchanged: no comparison


def test_health_save_keeps_history(tmp_path: Path) -> None:
    import time

    first, summary1 = health.save(tmp_path, {"selftest_passed": 20, "selftest_failed": 2}, {"Self-test": ["- ok"]})
    assert first.is_file() and not any("(" in line for line in summary1)
    time.sleep(1.1)  # a report per second at most: the file name has the time
    _, summary2 = health.save(tmp_path, {"selftest_passed": 22, "selftest_failed": 0}, {})
    assert "Self-test failed: 0  (better than 2)" in summary2
    rows = [json.loads(line) for line in (tmp_path / "reports" / "health.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 2 and rows[-1]["selftest_passed"] == 22
    assert "## Self-test" in first.read_text("utf-8")


def test_health_previous_ignores_a_broken_file(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "health.jsonl").write_text("not json\n", encoding="utf-8")
    assert health.previous(tmp_path) is None


# ------------------------------------------------------------------ voice commands

@pytest.mark.parametrize("said, action", [
    ("health report", "health_report"),
    ("make a health report", "health_report"),
    ("run the health check", "health_report"),
    ("compare models", "compare_models"),
    ("compare my AI models", "compare_models"),
])
def test_voice_commands(said, action) -> None:
    call = match_shortcut(said, lambda name: False)
    assert call is not None and call.name == "jarvis_control" and call.arguments["action"] == action


def test_controller_has_the_actions() -> None:
    from app.ui.controller import AssistantController

    assert callable(AssistantController.health_report) and callable(AssistantController.compare_models)


# ------------------------------------------------------------------ step checklist

def run(name: str, result: ToolResult) -> ToolExecution:
    return ToolExecution(ToolCall(name, {}), result)


def test_checklist_reports_the_real_results() -> None:
    text = step_checklist([
        run("web_search", ToolResult.success("Found 5 results")),
        run("write_document", ToolResult.failure("permission", "The folder is read-only")),
        run("open_file", ToolResult.success("")),
    ])
    lines = text.splitlines()
    assert lines[0] == "Steps (2 of 3 worked):"
    assert lines[1] == "1. ✓ web_search - Found 5 results"
    assert lines[2] == "2. ✗ write_document - permission: The folder is read-only"
    assert lines[3] == "3. ✓ open_file"


def test_no_checklist_for_a_single_step() -> None:
    assert step_checklist([run("get_time", ToolResult.success("10:00"))]) == ""
    assert step_checklist([]) == ""


def test_task_prompt_asks_the_model_to_check_results() -> None:
    from datetime import datetime

    from app.ai.prompts import build_system_prompt

    prompt = build_system_prompt(assistant_name="Jarvis", now=datetime.now(), tools=[SimpleNamespace(name="web_search")], task=True)
    assert "never claim a step worked" in prompt
