"""2.8: fixes found in the real usage logs - tool budget, context overflow, argument repair,
Chrome profiles, "did you mean", Bangla second look, best model, weekly check, update restarts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app import health
from app.agent.arg_repair import repair
from app.agent.shortcuts import match_shortcut
from app.agent.tool_router import select_specs, spec_tokens
from app.ai.base import ChatMessage, ToolCall
from app.tools.base import ToolResult
from app.tools.browser_profiles import BrowserProfile, choose_profile
from app.tools.catalog import build_default_registry
from app.tools.filesystem import ListArgs
from app.tools.windows import PowerArgs
from tests.fake_ollama import FakeOllama
from tests.test_agent_loop import build, call, chat_bodies, run, say


# ------------------------------------------------------------------ tools that fit the context

def test_unmatched_request_no_longer_sends_every_tool() -> None:
    registry = build_default_registry()
    everything = sum(spec_tokens(s) for s in registry.specs())
    for text in ("thanks", "how are you", "stop."):
        offered = select_specs(registry, text)
        assert sum(spec_tokens(s) for s in offered) < everything / 4
        assert {"open_application", "get_datetime"} <= {s.name for s in offered}


def test_budget_keeps_the_named_group_and_drops_the_rest() -> None:
    registry = build_default_registry()
    offered = select_specs(registry, "open chrome", ["list_folder", "take_screenshot"], max_tokens=2000)
    names = {s.name for s in offered}
    assert "open_application" in names  # what the request names always goes in
    assert sum(spec_tokens(s) for s in offered) <= 2400
    unlimited = {s.name for s in select_specs(registry, "open chrome", ["list_folder"])}
    assert "list_folder" in unlimited  # without a budget the recent group stays


def test_long_history_is_trimmed_to_fit_the_window() -> None:
    conv, fake, _ = build([say("Sure.")])
    conv.ai.provider.context_window = 2048
    for i in range(30):
        conv.context.add_turn([ChatMessage.user(f"question {i} " + "word " * 60),
                               ChatMessage.assistant("answer " * 60)])
    reply, _ = run(conv, "and now?")
    assert reply.ok
    sent = chat_bodies(fake)[0]["messages"]
    assert sent[0]["role"] == "system" and sent[-1]["content"] == "and now?"
    stored = len(conv.context.messages)
    assert len(sent) - 2 < stored - 2 and sent[1]["role"] != "tool"  # history was trimmed for this request only


class OverflowOnce(FakeOllama):
    def handle(self, request):
        import httpx

        if request.url.path == "/api/chat" and not getattr(self, "_overflowed", False):
            self._overflowed = True
            self.requests.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(400, json={"error": "request (12402 tokens) exceeds the available context size "
                                                      "(6144 tokens), try increasing it"})
        return super().handle(request)


def test_context_overflow_is_retried_smaller() -> None:
    from app.agent.conversation import ConversationManager
    from app.ai.manager import AIManager
    from tests.fake_ollama import model_entry

    conv, _, _ = build([])
    fake = OverflowOnce(models=[model_entry("llama3.2:3b")], replies=[say("You're welcome.")])
    conv = ConversationManager(AIManager.from_settings(conv.settings, transport=fake.transport()), conv.settings,
                               clock=conv._clock, tools=conv.tools)
    reply, _ = run(conv, "thanks a lot")
    assert reply.ok and reply.text == "You're welcome."
    first, second = chat_bodies(fake)[:2]
    assert len(second.get("tools") or []) < len(first.get("tools") or [])


# ------------------------------------------------------------------ argument repair

def errors(model, arguments):
    try:
        model.model_validate(arguments)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


def test_bad_optional_choice_falls_back_to_default() -> None:
    args = {"path": "Downloads", "sort_by": "extension"}  # from the user's log
    fixed, changes = repair(ListArgs, args, errors(ListArgs, args))
    assert "sort_by" not in fixed and ListArgs.model_validate(fixed).sort_by == "modified" and changes


def test_near_miss_choice_is_corrected() -> None:
    args = {"sort_by": "Modifed"}
    fixed, _ = repair(ListArgs, args, errors(ListArgs, args))
    assert fixed["sort_by"] == "modified"


def test_extra_field_is_dropped() -> None:
    args = {"path": "Desktop", "recursive": True}
    fixed, _ = repair(ListArgs, args, errors(ListArgs, args))
    assert fixed == {"path": "Desktop"}


def test_dangerous_tools_are_never_repaired() -> None:
    conv, _, executions = build([call("power_control", action="restat"), say("Sorry.")])
    reply, _ = run(conv, "the machine feels slow, sort it out for me")
    assert reply.ok
    power = [e for e in executions if e.call.name == "power_control"]
    assert power and power[0].result.error == "invalid_arguments"  # "restat" is not guessed into "restart"


def test_safe_tool_runs_with_repaired_arguments() -> None:
    from typing import Literal

    from pydantic import Field

    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine, RiskLevel
    from app.tools.base import Tool, ToolArgs, ToolContext

    class SortArgs(ToolArgs):
        order: Literal["name", "modified", "size"] = Field(default="modified", description="order")

    seen = []
    registry = ToolRegistry([Tool("sort_things", "Sort a list of things for the user.", lambda a, c: seen.append(a.order)
                                  or ToolResult.success("sorted"), SortArgs, RiskLevel.SAFE, "basic")])
    executor = ToolExecutor(registry, PermissionEngine(PermissionsConfig()), ToolContext(Settings()))
    done = asyncio.run(executor.execute(ToolCall("sort_things", {"order": "extension"})))
    assert done.result.ok and seen == ["modified"] and "order" not in done.call.arguments
    done = asyncio.run(executor.execute(ToolCall("sort_things", {"order": "sise"})))
    assert done.result.ok and seen[-1] == "size"


def test_power_args_are_not_repaired_by_the_repair_function_either() -> None:
    args = {"action": "lock", "delay_seconds": "five"}
    fixed, _ = repair(PowerArgs, args, errors(PowerArgs, args))  # the function itself works ...
    assert fixed["delay_seconds"] == 5  # ... the executor only uses it for safe tools (test above)


# ------------------------------------------------------------------ Chrome profiles

def test_several_profiles_with_one_name_open_the_most_recent() -> None:
    profiles = [BrowserProfile("Default", "Labib", "", False, 100.0),
                BrowserProfile("Profile 1", "LABIB", "", False, 300.0),
                BrowserProfile("Profile 3", "labib", "", False, 200.0),
                BrowserProfile("Profile 4", "Night", "", True, 400.0)]
    chosen = choose_profile(profiles, "labib", "Google Chrome")
    assert chosen.directory == "Profile 1" and "3 profiles are called 'labib'" in chosen.note
    assert choose_profile(profiles, "profile 3").directory == "Profile 3"  # by folder: exact
    assert choose_profile(profiles, "Night").note == ""


def test_last_used_wins_among_same_names() -> None:
    profiles = [BrowserProfile("Default", "Labib", "", True, 1.0), BrowserProfile("Profile 1", "LABIB", "", False, 9.0)]
    assert choose_profile(profiles, "labib").directory == "Default"


# ------------------------------------------------------------------ "did you mean ...? say yes"

def test_closest_name() -> None:
    from app.tools.applications import closest_name

    assert closest_name("WordShop", ["Photoshop", "Spotify", "Discord"]) == "Photoshop"
    assert closest_name("Double Up", ["Photoshop", "Spotify"]) is None


def suggestion_result(heard="WordShop", name="Photoshop"):
    from app.tools.applications import _suggestion

    return ToolResult.failure("not_found", f"I couldn't find {heard}. Did you mean {name}? Say yes to open it.",
                              **_suggestion("open_application", heard, name))


def test_yes_runs_the_suggestion_and_learns_the_name(tmp_path: Path, monkeypatch) -> None:
    from app.agent import aliases
    from app.agent.tool_manager import ToolExecution

    conv, _, _ = build([])
    ran: list[ToolCall] = []

    async def fake_execute(c, user_command=False):
        ran.append(c)
        return ToolExecution(c, ToolResult.success("Opened Photoshop.", app="Photoshop"))

    monkeypatch.setattr(conv.tools, "execute", fake_execute)
    monkeypatch.setattr(conv.tools.context, "paths", SimpleNamespace(data_dir=tmp_path))
    conv._remember_suggestion([ToolExecution(ToolCall("open_application", {"name": "WordShop"}), suggestion_result())])
    reply, _ = run(conv, "yes")
    assert ran == [ToolCall("open_application", {"name": "Photoshop"})]
    assert reply.ok and "remember that WordShop means Photoshop" in reply.text
    assert aliases.AliasStore(tmp_path).get("wordshop") == {"kind": "app", "target": "Photoshop"}


def test_no_or_something_else_drops_the_suggestion(monkeypatch) -> None:
    from app.agent.tool_manager import ToolExecution

    conv, _, _ = build([say("Hello!")])
    execution = ToolExecution(ToolCall("open_application", {"name": "WordShop"}), suggestion_result())
    conv._remember_suggestion([execution])
    reply, _ = run(conv, "no")
    assert reply.text == "Okay, I won't." and conv._suggestion is None
    conv, _, _ = build([say("Hello!")])
    conv._remember_suggestion([execution])
    reply, _ = run(conv, "what's the weather like")
    assert conv._suggestion is None


def test_only_app_tools_can_be_suggested() -> None:
    from app.agent.tool_manager import ToolExecution

    conv, _, _ = build([])
    bad = ToolResult.failure("x", "?", suggestion={"tool": "power_control", "arguments": {"action": "shutdown"}})
    conv._remember_suggestion([ToolExecution(ToolCall("open_application", {}), bad)])
    assert conv._suggestion is None


# ------------------------------------------------------------------ Bangla second look

class TwoLanguages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        lang = kwargs.get("language")
        if lang == "bn":
            return iter([SimpleNamespace(text="গুগল ক্রোম খোলো", avg_logprob=-0.2, no_speech_prob=0.01)]), \
                SimpleNamespace(language="bn")
        return iter([SimpleNamespace(text="Google Chrome Club", avg_logprob=-1.2, no_speech_prob=0.01)]), \
            SimpleNamespace(language="en", all_language_probs=[("en", 0.6), ("bn", 0.3), ("hi", 0.1)])


def make_stt(tmp_path, device="cuda"):
    from app.voice.speech_to_text import FasterWhisperSTT

    model = TwoLanguages()
    stt = FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, model_factory=lambda name, **kw: model,
                           allowed_languages=("en", "bn"), background_upgrade=False, second_pass=False)
    stt.device = device
    return stt, model


def audio():
    return (0.1 * np.sin(np.arange(16000) / 3)).astype(np.float32)


def test_unsure_english_is_checked_as_bangla(tmp_path: Path, monkeypatch) -> None:
    stt, model = make_stt(tmp_path)
    monkeypatch.setattr(stt, "_load_locked", lambda: setattr(stt, "_model", model))
    result = stt.transcribe(audio(), None)
    assert result.text == "গুগল ক্রোম খোলো" and result.language == "bn"
    assert [c.get("language") for c in model.calls] == [None, "bn"]


def test_no_second_look_when_a_language_is_forced(tmp_path: Path, monkeypatch) -> None:
    stt, model = make_stt(tmp_path)
    monkeypatch.setattr(stt, "_load_locked", lambda: setattr(stt, "_model", model))
    assert stt.transcribe(audio(), "en").text == "Google Chrome Club"
    assert len(model.calls) == 1


def test_other_language_rules(tmp_path: Path) -> None:
    stt, _ = make_stt(tmp_path)
    info = SimpleNamespace(language="en", all_language_probs=[("en", 0.9), ("bn", 0.05)])
    assert stt._other_language(info, None, 0.3) is None  # Bangla too unlikely
    info = SimpleNamespace(language="en", all_language_probs=[("en", 0.6), ("bn", 0.3)])
    assert stt._other_language(info, None, 0.8) is None  # sure of the English
    assert stt._other_language(info, None, 0.3) == "bn"
    stt.device = "cpu"
    assert stt._other_language(info, None, 0.3) is None  # too slow on the CPU


# ------------------------------------------------------------------ best model, weekly check

@pytest.mark.parametrize("said", ["use the best model", "switch to the best model", "use the winner",
                                  "use the best AI model for me"])
def test_use_best_model_by_voice(said) -> None:
    found = match_shortcut(said, lambda name: None)
    assert found is not None and found.arguments["action"] == "use_best_model"


def test_model_choice_notice() -> None:
    from app.ui.controller import model_choice_notice

    def m(name):
        return SimpleNamespace(name=name)

    assert model_choice_notice(SimpleNamespace(selected=m("jarvis:latest"), models=(m("jarvis:latest"),))) == ""
    assert "use the best model" in model_choice_notice(
        SimpleNamespace(selected=m("qwen3:4b-instruct"), models=(m("qwen3:4b-instruct"), m("jarvis:latest"))))
    assert "compare models" in model_choice_notice(SimpleNamespace(selected=m("qwen3:4b-instruct"), models=()))


def test_weekly_health_check_is_due(tmp_path: Path) -> None:
    assert health.due(tmp_path)  # never made one
    health.save(tmp_path, {"selftest_passed": 1}, {})
    assert not health.due(tmp_path)
    assert health.due(tmp_path, now=datetime.now() + timedelta(days=8))
    assert health.worse(["a: 1  (worse than 0)", "b: 2  (better than 1)", "c: 3"]) == ["a: 1  (worse than 0)"]


def test_weekly_health_check_setting_by_voice() -> None:
    from app.tools.voice_control import FRIENDLY_SETTINGS

    assert FRIENDLY_SETTINGS["weekly health check"][0] == "weekly_health_check"


# ------------------------------------------------------------------ restarts by the installer are not crashes

def test_update_restart_is_not_a_crash(tmp_path: Path) -> None:
    from app.utils import crash

    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path / "data"), log_file=str(logs / "jarvis.log"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / crash.MARKER).write_text("2026-09-24T13:21:18 2.6.0", encoding="utf-8")
    try:
        assert crash.start(ctx) is None  # an older version was closed by the installer
        assert (tmp_path / "data" / crash.MARKER).read_text(encoding="utf-8").endswith(" " + __import__("app").__version__)
    finally:
        crash.stop(ctx)
    (logs / "crash.log").write_text("--- started x\nFatal Python error: Segmentation fault\n", encoding="utf-8")
    (tmp_path / "data" / crash.MARKER).write_text("2026-09-24T13:21:18 2.6.0", encoding="utf-8")
    try:
        assert crash._fault_logged(logs / "crash.log")
    finally:
        crash.stop(ctx)


def test_same_version_without_clean_exit_is_still_reported(tmp_path: Path) -> None:
    from app import __version__
    from app.utils import crash

    assert asyncio  # (keeps the import list honest)
    (tmp_path / "logs").mkdir()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path / "data"), log_file=str(tmp_path / "logs" / "j.log"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / crash.MARKER).write_text(f"2026-09-24T13:21:18 {__version__}", encoding="utf-8")
    called = []
    original = crash._write_report
    crash._write_report = lambda c, started: called.append(started) or tmp_path / "r.zip"
    try:
        crash.start(ctx)
    finally:
        crash._write_report = original
        crash.stop(ctx)
    assert called == ["2026-09-24T13:21:18"]


@pytest.mark.parametrize("said, name, profile", [
    ("open chrome labib", "chrome", "labib"),
    ("open chrome with labib profile", "chrome", "labib"),
    ("open google chrome with the Night profile", "google chrome", "Night"),
    ("open chrome profile 3", "chrome", "profile 3"),
    ("open edge profile 12", "edge", "profile 12"),
])
def test_browser_profile_commands(said, name, profile) -> None:
    found = match_shortcut(said, lambda n: None)
    assert found is not None and found.name == "open_application"
    assert found.arguments == {"name": name, "profile": profile}


@pytest.mark.parametrize("said", ["open chrome and youtube", "open chrome new window", "open chrome settings",
                                  "open chrome incognito"])
def test_not_browser_profiles(said) -> None:
    found = match_shortcut(said, lambda n: None)
    assert found is None or "profile" not in found.arguments


def test_out_of_range_numbers_are_not_replaced() -> None:
    from app.tools.windows import VolumeArgs

    args = {"action": "set", "level": 150}
    assert repair(VolumeArgs, args, errors(VolumeArgs, args)) is None
