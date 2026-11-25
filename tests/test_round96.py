"""Round 96 (local brain quality): schema-constrained output + tool-argument repair, embedding-model change, second opinion by agreement."""

import asyncio
from pathlib import Path

import numpy as np
from pydantic import Field

from app.ai.base import ChatMessage, ChatRequest, ToolCall
from app.core import consensus as K
from app.memory import semantic as S
from app.memory.database import Database


# ------------------------------------------------------------------ structured output
def test_ollama_payload_carries_the_schema_and_zero_temperature():
    from app.ai.ollama import OllamaProvider

    p = OllamaProvider.__new__(OllamaProvider)
    p.keep_alive, p.temperature, p.context_window = "10m", 0.7, 8192
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    with_schema = p._chat_payload(ChatRequest("m", (ChatMessage.user("x"),), format=schema, think=False), stream=False)
    assert with_schema["format"] == schema and with_schema["options"]["temperature"] == 0 and with_schema["think"] is False
    plain = p._chat_payload(ChatRequest("m", (ChatMessage.user("x"),)), stream=False)
    assert "format" not in plain and plain["options"]["temperature"] == 0.7


def _executor(ai, **settings):
    from app.agent.tool_manager import ToolExecutor, ToolRegistry
    from app.config.settings import Settings
    from app.security.permissions import PermissionEngine, PermissionsConfig, RiskLevel
    from app.tools.base import Tool, ToolArgs, ToolContext, ToolResult

    class Args(ToolArgs):
        minutes: int = Field(ge=1, le=600, description="how long")

    ran = []

    def handler(a, c):
        ran.append(a.minutes)
        return ToolResult.success("ok")

    reg = ToolRegistry()
    reg.register(Tool("set_timer", "Set a timer.", handler, Args, RiskLevel.MODERATE))
    ctx = ToolContext(Settings(**settings), ai=ai)
    return ToolExecutor(reg, PermissionEngine(PermissionsConfig()), ctx), ran


class FakeAI:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def structured(self, messages, schema, fast=True):
        self.calls.append(schema)
        return self.reply


def test_invalid_arguments_are_repaired_once_through_the_schema_and_still_checked():
    ai = FakeAI({"minutes": 10})
    ex, ran = _executor(ai)
    r = asyncio.run(ex.execute(ToolCall("set_timer", {"minutes": "ten"}), user_command=True))
    assert len(ai.calls) == 1 and "minutes" in ai.calls[0]["properties"]
    assert r.result.error != "invalid_arguments"            # the repaired call went on to the normal permission path
    assert ran in ([10], [])                                # ran, or is waiting for the normal confirmation - never ran with bad data


def test_a_bad_repair_or_the_switch_off_keeps_the_original_error():
    for ai, kw in ((FakeAI({"minutes": 99999}), {}), (FakeAI(None), {}), (FakeAI({"minutes": 5}), {"tool_schema_repair": False}), (None, {})):
        ex, ran = _executor(ai, **kw)
        r = asyncio.run(ex.execute(ToolCall("set_timer", {"minutes": "ten"}), user_command=True))
        assert r.result.error == "invalid_arguments" and ran == []
    off = FakeAI({"minutes": 5})
    ex, _ = _executor(off, tool_schema_repair=False)
    asyncio.run(ex.execute(ToolCall("set_timer", {"minutes": "x"}), user_command=True))
    assert off.calls == []


# ------------------------------------------------------------------ embeddings
def embedder(dim, tag):
    async def embed(texts):
        out = []
        for t in texts:
            v = np.zeros(dim, dtype=np.float32)
            v[hash((tag, t.split()[0].lower())) % dim] = 1.0
            out.append(v.tolist())
        return out
    return embed


def test_query_instruction_only_for_models_trained_with_one():
    assert S.query_text("qwen3-embedding:0.6b", "my cat").startswith("Instruct:") and S.query_text("qwen3-embedding:0.6b", "my cat").endswith("my cat")
    assert S.query_text("nomic-embed-text", "my cat") == "my cat" and S.query_text("", "q") == "q"


def test_changing_the_embedding_model_reembeds_from_stored_text(tmp_path: Path):
    db = Database(tmp_path / "s.db")
    db.initialize()
    old = S.SemanticIndex(db, embedder(8, "old"), "nomic-embed-text")
    asyncio.run(old._store("document", "notes.txt", ["alpha beta", "gamma delta", "epsilon zeta"]))
    new = S.SemanticIndex(db, embedder(16, "new"), "qwen3-embedding:0.6b")
    assert old.stale_count() == 0 and new.stale_count() == 3
    assert asyncio.run(new.reembed(2)) == 2 and new.stale_count() == 1          # resumable in parts
    assert asyncio.run(new.reembed(10)) == 1 and new.stale_count() == 0 and asyncio.run(new.reembed(10)) == 0
    with db.connect() as conn:
        rows = conn.execute("SELECT model, LENGTH(embedding) FROM semantic_items").fetchall()
    assert all(m == "qwen3-embedding:0.6b" and n == 16 * 4 for m, n in rows)


# ------------------------------------------------------------------ consensus
def test_agreement_groups_same_content_and_numbers_decide():
    assert K.same("Paris is the capital of France", "The capital of France is Paris.")
    assert not K.same("It takes 12 minutes", "It takes 21 minutes") and K.same("42", "The answer is 42")
    c = K.agree(["The capital is Dhaka", "Dhaka is the capital city", "It is Chittagong"])
    assert c.agreed and c.votes == 2 and "Dhaka" in c.answer and "not proof" in c.text()
    split = K.agree(["red", "green", "blue"])
    assert not split.agreed and "UNCERTAIN" in split.text()
    assert not K.agree(["only one"]).agreed and K.agree([]).asked == 0


def test_ask_until_stops_early_and_survives_a_failed_run():
    calls = []

    async def ask():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("model busy")
        return "The answer is 7"

    c = asyncio.run(K.ask_until(ask, 5))
    assert c.agreed and len(calls) == 3                      # one failure + two agreeing answers, then it stops

    async def never():
        calls.append(1)
        return f"value {len(calls)}"

    assert not asyncio.run(K.ask_until(never, 3)).agreed


# ------------------------------------------------------------------ two-step tool choice
def _specs(n):
    from app.ai.base import ToolSpec

    return [ToolSpec(f"tool{i}", f"Does thing {i}. More detail that is not shown.", {"type": "object"}) for i in range(n)]


def test_two_step_narrows_to_the_picked_tools_and_keeps_the_basics():
    from app.agent import tool_picker as TP

    specs = _specs(12)
    ai = FakeAI({"tools": ["tool3", "tool7", "ghost", "tool3"]})
    out = asyncio.run(TP.narrow(ai, "do thing 3", specs, keep=["tool0"]))
    assert [s.name for s in out] == ["tool0", "tool3", "tool7"]
    schema = ai.calls[0]
    assert "tool0" not in schema["properties"]["tools"]["items"]["enum"] and schema["properties"]["tools"]["maxItems"] == TP.MAX_PICK
    assert TP.one_line(specs[1]) == "tool1: Does thing 1."


def test_two_step_never_breaks_a_request():
    from app.agent import tool_picker as TP

    specs = _specs(12)
    for ai in (None, FakeAI(None), FakeAI({"tools": []}), FakeAI({"tools": ["ghost"]}), FakeAI("nonsense")):
        assert asyncio.run(TP.narrow(ai, "x", specs)) == specs

    class Boom:
        async def structured(self, *a, **k):
            raise RuntimeError("model down")

    assert asyncio.run(TP.narrow(Boom(), "x", specs)) == specs
    few = _specs(5)
    short = FakeAI({"tools": ["tool1"]})
    assert asyncio.run(TP.narrow(short, "x", few)) == few and short.calls == []     # short list: no extra model call


def test_two_step_is_off_by_default():
    from app.config.settings import Settings

    assert Settings().two_step_tools is False and Settings().tool_schema_repair is True


# ------------------------------------------------------------------ A/B of a switch
def test_ab_recommends_only_a_clear_gain_and_needs_enough_cases():
    from app.agent import ab_switches as AB

    cases = [{"request": f"r{i}", "expect_tool": "good"} for i in range(10)]

    def asker(right, broken=0):
        seen = {"n": 0}

        async def ask(model, temp, text):
            seen["n"] += 1
            i = seen["n"]
            return ("good" if i <= right else "other"), i <= broken
        return ask

    win = asyncio.run(AB.compare(asker(5), asker(9), "m", 0.3, cases))
    assert "RECOMMENDED" in win and "TWO_STEP_TOOLS=true" in win and "5 of 10" in win and "9 of 10" in win
    same = asyncio.run(AB.compare(asker(8), asker(8), "m", 0.3, cases))
    assert "KEEP IT OFF" in same
    broken = asyncio.run(AB.compare(asker(5), asker(9, broken=3), "m", 0.3, cases))
    assert "KEEP IT OFF" in broken and "broken" in broken
    few = asyncio.run(AB.compare(asker(1), asker(2), "m", 0.3, cases[:3]))
    assert "Not enough history" in few


# ------------------------------------------------------------------ self-improve fixes found on the real PC
def test_candidates_keep_models_with_unknown_tool_support_and_tool_capable_vision_models():
    from app.agent import self_improve as SI
    from app.ai.base import ModelInfo

    models = [ModelInfo("jarvis:latest", 3_000_000_000), ModelInfo("qwen3:8b", 5_000_000_000),                       # capabilities unknown
              ModelInfo("qwen3.5:4b", 3_400_000_000, capabilities=frozenset({"completion", "tools", "vision"})),     # sees images AND calls tools
              ModelInfo("qwen2.5vl:3b", 3_000_000_000, capabilities=frozenset({"completion", "vision"})),            # vision only
              ModelInfo("notools:1b", 1_000_000_000, capabilities=frozenset({"completion"})),
              ModelInfo("nomic-embed-text", 300_000_000)]
    names = [c.base for c in SI.candidates("qwen2.5:3b-instruct", 0.3, models, 16 * 2**30)]
    assert "qwen3.5:4b" in names and "qwen3:8b" in names
    assert not {"qwen2.5vl:3b", "notools:1b", "nomic-embed-text", "jarvis:latest"} & set(names)


def test_a_current_model_that_never_answers_is_reported_not_scored_as_zero():
    from app.agent import self_improve as SI

    cases = [{"request": f"r{i}", "expect_tool": "good"} for i in range(10)]

    async def ask(model, temp, text):
        if model == "gone":
            raise RuntimeError("model 'gone' not found")
        return "good", False

    async def apply(c):
        raise AssertionError("must not switch when the baseline could not run")

    out = asyncio.run(SI.run(ask, apply, cases, [SI.Candidate("gone", 0.3, "current"), SI.Candidate("other", 0.3, "other model")]))
    assert not out.changed and "could not run" in out.message and "not found" in out.message and out.scores[0]["errors"]
    mapped = asyncio.run(SI.run(ask, lambda c: asyncio.sleep(0), cases, [SI.Candidate("gone", 0.3, "current"), SI.Candidate("other", 0.3, "other model")],
                                model_for=lambda c: "jarvis:latest" if c.base == "gone" else c.base))
    assert "could not run" not in mapped.message and mapped.scores[0]["accuracy"] == 1.0


# ------------------------------------------------------------------ search-by-meaning check
def test_search_eval_scores_by_language_pair_and_compares_models():
    from app import search_eval as SE

    ids = list(SE.NOTES)
    assert len({q for _, q, _ in SE.QUESTIONS}) == len(SE.QUESTIONS) and all(e in SE.NOTES for _, _, e in SE.QUESTIONS)
    answer = {q: e for _, q, e in SE.QUESTIONS}

    def perfect(dim=len(ids)):
        async def embed(texts):
            out = []
            for t in texts:
                v = [0.0] * dim
                key = next((i for i, n in enumerate(ids) if SE.NOTES[n][1] == t), None)
                if key is None:
                    key = ids.index(answer[t])
                v[key] = 1.0
                out.append(v)
            return out
        return embed

    def english_only():
        async def embed(texts):
            out = []
            for t in texts:
                v = [0.0] * len(ids)
                known = next((i for i, n in enumerate(ids) if SE.NOTES[n][1] == t), None)
                if known is not None:
                    v[known] = 1.0
                elif t in answer and t.isascii() and SE.NOTES[answer[t]][0] == "en":
                    v[ids.index(answer[t])] = 1.0
                else:
                    v[0] = 1.0                                  # a model that does not understand the question lands on an arbitrary note
                out.append(v)
            return out
        return embed

    good = asyncio.run(SE.run(perfect(), "good"))
    assert good.score()[1] == 1.0 and good.score("bn->en")[0] == 3
    weak = asyncio.run(SE.run(english_only(), "weak"))
    assert weak.score("en->en")[1] == 1.0 and weak.score("bn->bn")[1] < 0.5
    text = SE.report(good, weak)
    assert "clearly better" in text and "bn->bn" in text and "embeddings only" in text
    assert "too close to call" in SE.report(good, good) and "missed" in SE.report(weak)


def test_jarvis_and_jarvis_latest_are_the_same_installed_model():
    from app.ai import jarvis_model

    assert jarvis_model._same("jarvis:latest", "jarvis") and jarvis_model._same("qwen3:8b", "QWEN3:8B") and not jarvis_model._same("qwen3:8b", "qwen3:4b")


def test_a_much_slower_model_needs_a_big_accuracy_gain():
    from app.agent import self_improve as SI

    def score(acc, secs, errors=()):
        n = 20
        return SI.Score(SI.Candidate("m", 0.3, "x"), n, int(acc * n), 0, secs * n, list(errors), float(n), acc * n)

    cur = score(0.55, 13.6)
    assert not SI.better(score(0.64, 28.2), cur)[0]                  # +9 points for 2x the wait: refused (the real run that adopted qwen3:8b)
    assert "slower" in SI.better(score(0.64, 28.2), cur)[1]
    assert SI.better(score(0.80, 28.2), cur)[0]                      # a big gain is worth the wait
    assert SI.better(score(0.64, 15.0), cur)[0]                      # a modest slowdown with a real gain is fine
    assert not SI.better(score(0.9, 5.0, ["x"] * 3), cur)[0]         # it failed to answer 3 requests the current one answered
