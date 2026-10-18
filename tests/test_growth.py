"""The learning loop: every request recorded, outcome decided by what happens next, skills + lessons learned and
scored, unhelpful ones retired, a personal test set, and self-improvement that only keeps a measured gain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import self_improve
from app.agent.growth import Growth
from app.memory.database import Database


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)


def ex(name, args=None, ok=True, error=None):
    return SimpleNamespace(call=SimpleNamespace(name=name, arguments=args or {}),
                           result=SimpleNamespace(ok=ok, error=error))


@pytest.fixture()
def growth(tmp_path: Path):
    db = Database(tmp_path / "x.db")
    db.initialize()
    clock = Clock()
    return Growth(db, clock=clock), clock


def outcomes(g: Growth) -> list[tuple[str, str]]:
    with g.db.connect() as conn:
        return [(r["outcome"], r["reason"]) for r in conn.execute("SELECT outcome, reason FROM learning_turns ORDER BY id")]


def test_outcomes_come_from_what_happens_next(growth) -> None:
    g, clock = growth
    g.record("open my work editor", [ex("open_application", {"name": "Notepad"})], "jarvis")
    clock.tick(10)
    g.record("no, I meant visual studio code", [ex("open_application", {"name": "Visual Studio Code"})], "jarvis")
    clock.tick(200)
    g.record("what's the weather", [ex("get_weather")], "jarvis")          # moved on: the correction was good
    g.record("delete the old logs", [ex("delete_item", ok=False, error="declined")], "jarvis")
    assert outcomes(g)[:2] == [("bad", "you corrected it"), ("good", "it worked and you moved on")]
    assert outcomes(g)[3] == ("bad", "you said no")                          # a failure decides at once
    assert outcomes(g)[2][0] == "good"                                       # you moved on to something else
    clock.tick(500)
    assert g.settle_quiet() == 0
    report = g.report()
    assert report["lessons"] == 1 and report["skills"] >= 2 and report["turns"] == 4


def test_lessons_and_skills_are_offered_for_similar_requests_and_scored(growth) -> None:
    g, clock = growth
    g.record("open my work editor", [ex("open_application", {"name": "Notepad"})], "jarvis")
    clock.tick(5)
    g.record("no, I meant visual studio code", [ex("open_application", {"name": "Visual Studio Code"})], "jarvis")
    clock.tick(300)
    g.settle_quiet()
    items = g.relevant("open the work editor please")
    assert items and items[0]["kind"] == "lesson" and "Visual Studio Code" in items[0]["content"]
    prompt = Growth.prompt(items)
    assert "Lesson:" in prompt and "current request wins" in prompt
    lesson_id = items[0]["id"]
    for _ in range(4):                                                       # shown 4 times, never helped
        clock.tick(300)
        g.record("open the work editor", [ex("open_application", ok=False, error="not_found")], "jarvis",
                 used=[lesson_id])
    assert all(i["id"] != lesson_id for i in g.relevant("open the work editor"))          # retired
    assert g.report()["retired"] >= 1


def test_direct_commands_and_secrets_are_not_learned_as_skills(growth) -> None:
    g, clock = growth
    g.record("open chrome", [ex("open_application", {"name": "Chrome"})], "direct")
    g.record("my password is hunter2-Secret!99 save it", [ex("remember")], "jarvis")
    clock.tick(500)
    g.settle_quiet()
    with g.db.connect() as conn:
        requests = [r[0] for r in conn.execute("SELECT request FROM growth_items")]
        stored = " ".join(r[0] for r in conn.execute("SELECT request FROM learning_turns"))
    assert "open chrome" not in requests and "hunter2-Secret!99" not in stored


def test_personal_test_cases_and_training_export(growth, tmp_path: Path) -> None:
    g, clock = growth
    for text, tool in [("volume up", "volume_control"), ("weather today", "get_weather"),
                       ("open spotify", "open_application")]:
        g.record(text, [ex(tool)], "jarvis")
        clock.tick(300)
    g.settle_quiet()
    cases = g.cases()
    assert {c["expect_tool"] for c in cases} == {"volume_control", "get_weather", "open_application"}
    count = self_improve.export_training(g.db, tmp_path / "exports" / "training.jsonl")
    assert count == 3 and '"tool_calls"' in (tmp_path / "exports" / "training.jsonl").read_text()


def test_self_improve_keeps_a_change_only_when_it_measures_better(tmp_path: Path) -> None:
    cases = [{"request": f"r{i}", "expect_tool": "t"} for i in range(10)]
    quality = {"base-a": 0.5, "base-b": 0.9, "base-c": 0.5}

    async def ask(model, temperature, text):
        i = int(text[1:])
        return ("t" if i < quality[model] * 10 else "other"), False

    applied = []

    async def apply(candidate):
        applied.append(candidate.base)

    options = [self_improve.Candidate("base-a", 0.3, "current"), self_improve.Candidate("base-b", 0.3, "other"),
               self_improve.Candidate("base-c", 0.3, "other")]
    outcome = asyncio.run(self_improve.run(ask, apply, cases, options, tmp_path))
    assert outcome.changed and applied == ["base-b"] and "90% vs 50%" in outcome.message
    applied.clear()
    small_gain = asyncio.run(self_improve.run(ask, apply, cases, [options[0], options[2]], tmp_path))
    assert not small_gain.changed and applied == []                          # 50% vs 50%: no reason to switch
    too_few = asyncio.run(self_improve.run(ask, apply, cases[:3], options, tmp_path))
    assert not too_few.changed and "need 8" in too_few.message
    assert len(self_improve.history(tmp_path)) == 2                         # (too few cases: nothing to record)


def test_broken_tool_calls_block_a_switch() -> None:
    cur = self_improve.Score(self_improve.Candidate("a", 0.3), cases=10, right=5, malformed=0, seconds=10)
    new = self_improve.Score(self_improve.Candidate("b", 0.3), cases=10, right=9, malformed=2, seconds=10)
    assert self_improve.better(new, cur) == (False, "it made more broken tool calls")


def test_learning_tool_report_and_forget(growth) -> None:
    from app.tools.growth_tools import LearningArgs, learning

    g, clock = growth
    ctx = SimpleNamespace(database=g.db)
    assert "haven't finished learning" in learning(LearningArgs(), ctx).message
    g.record("weather", [ex("get_weather")], "jarvis")
    clock.tick(300)
    g.settle_quiet()
    assert "went right" in learning(LearningArgs(), ctx).message
    assert "Forgot" in learning(LearningArgs(action="forget"), ctx).message and g.report()["turns"] == 0


def test_quiet_settling(growth) -> None:
    g, clock = growth
    g.record("weather", [ex("get_weather")], "jarvis")
    clock.tick(30)
    assert g.settle_quiet() == 0                                              # too soon to say
    clock.tick(200)
    assert g.settle_quiet() == 1 and outcomes(g) == [("good", "it worked and you moved on")]


def test_real_conversation_turns_are_recorded_and_used(tmp_path: Path) -> None:
    from app.agent.conversation import ConversationManager
    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.ai.manager import AIManager
    from app.config.files import PermissionsConfig
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine, RiskLevel
    from app.tools.base import NoArgs, Tool, ToolContext, ToolResult
    from tests.fake_ollama import FakeOllama, model_entry

    db = Database(tmp_path / "x.db")
    db.initialize()
    registry = ToolRegistry()
    registry.register(Tool("lights_on", "Turn on the lights", lambda a, c: ToolResult.success("Lights on."), NoArgs,
                           RiskLevel.SAFE, "home"))
    replies = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lights_on", "arguments": {}}}]},
               {"role": "assistant", "content": "Done."}]
    fake = FakeOllama(models=[model_entry("llama3.2:3b")], replies=replies)
    settings = Settings(data_dir=tmp_path)
    ex = ToolExecutor(registry, PermissionEngine(PermissionsConfig()), ToolContext(settings, database=db))
    conv = ConversationManager(AIManager.from_settings(settings, transport=fake.transport()), settings, tools=ex)
    reply = asyncio.run(conv.send("could you switch the lights on in here"))
    assert reply.ok
    with db.connect() as conn:
        row = conn.execute("SELECT request, tools, outcome FROM learning_turns").fetchone()
    assert "lights on" in row["request"] and '"lights_on"' in row["tools"] and row["outcome"] == "pending"
    with db.transaction() as conn:                                            # pretend two quiet minutes passed
        conn.execute("UPDATE learning_turns SET ts = '2000-01-01T00:00:00+00:00'")
    Growth(db).settle_quiet()
    assert Growth(db).relevant("switch the lights on please")                # learned as a skill
    settings_off = Settings(data_dir=tmp_path, learn_from_use=False)
    ex.context.settings = settings_off
    asyncio.run(conv.send("hello there"))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM learning_turns").fetchone()[0] == 1   # switched off: not recorded
