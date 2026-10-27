"""Reusable skills: workflows with named, validated inputs; the Experience Compiler proposes them from repeated routines."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.automation.tasks import Step, bind, check_parameters, clean_parameters, placeholders
from app.automation.triggers import ManualTrigger
from app.tools.base import ToolError
from tests.test_anomaly_experience import NOW, _audit
from tests.test_automation import Clock
from tests.test_automation import NOW as ANOW
from tests.test_automation import db, scheduler_for  # noqa: F401  (pytest fixture + helper)

PARAMS = {"who": {"default": "Ada", "description": "a name"}, "city": {"default": "Dhaka"}}


def steps_with(**args):
    return [Step(type="tool", tool="step_one", arguments=args)]


# ------------------------------------------------------------------ binding


def test_bind_substitutes_defaults_and_overrides_in_arguments_only():
    s = [Step(type="tool", tool="step_one", arguments={"name": "hello {{ who }} in {{city}}", "nested": {"x": ["{{who}}"]}}),
         Step(type="notify", message="done {{who}}")]
    out = bind(s, PARAMS, {"who": "Grace"})
    assert out[0].arguments == {"name": "hello Grace in Dhaka", "nested": {"x": ["Grace"]}}
    assert out[0].tool == "step_one" and out[1].message == "done {{who}}"            # tool names / messages are never templated
    assert bind(s, PARAMS)[0].arguments["name"] == "hello Ada in Dhaka"
    assert s[0].arguments["name"] == "hello {{ who }} in {{city}}"                      # the saved steps are not changed


def test_bind_rejects_unknown_missing_and_unsafe_values():
    s = steps_with(name="{{who}}")
    with pytest.raises(ToolError, match="no input called"):
        bind(s, PARAMS, {"nope": "x"})
    with pytest.raises(ToolError, match="Tell me the who"):
        bind(s, {"who": {"default": None, "description": "a name"}}, {})
    for bad in ("a\x00b", "line\nbreak", "{{who}}", "x" * 301):
        with pytest.raises(ToolError, match="isn't allowed|aren't allowed|too long"):
            bind(s, PARAMS, {"who": bad})
    with pytest.raises(ToolError, match="no input"):
        bind(s, {}, {"who": "x"})
    assert bind(s, {}, {}) is s                                                          # no inputs: untouched


def test_parameters_are_validated_when_declared():
    assert clean_parameters(None) == {} and clean_parameters({"a": "x"})["a"]["default"] == "x"
    for bad in ({"Bad Name": "x"}, {"1x": "x"}, {f"p{i}": "x" for i in range(6)}):
        with pytest.raises(ToolError):
            clean_parameters(bad)
    with pytest.raises(ToolError):
        clean_parameters({"a": "x\x00y"})
    s = steps_with(name="{{who}}")
    assert placeholders(s) == {"who"}
    with pytest.raises(ToolError, match="never declared"):
        check_parameters(s, {})
    with pytest.raises(ToolError, match="no step uses"):
        check_parameters(s, {"who": {"default": "a"}, "extra": {"default": "b"}})


# ------------------------------------------------------------------ storage and running


def test_store_keeps_parameters_through_replacement_and_deletes_them(db):
    from app.automation.tasks import AutomationStore

    store = AutomationStore(db)
    task, _ = store.save("greet", ManualTrigger(), steps_with(name="{{who}}"), None, {"who": {"default": "Ada", "description": ""}})
    assert store.get(task.id).parameters["who"]["default"] == "Ada" and "inputs: who" in store.find("greet").describe()
    again, replaced = store.save("greet", ManualTrigger(), steps_with(name="Hi {{who}}"), None)            # None = keep if still used
    assert replaced and again.parameters["who"]["default"] == "Ada"
    gone, _ = store.save("greet", ManualTrigger(), steps_with(name="plain"), None)                         # no longer used: dropped
    assert gone.parameters == {}
    with pytest.raises(ToolError):
        store.save("bad", ManualTrigger(), steps_with(name="{{who}}"), None, {})                           # undeclared use is refused
    assert store.find("greet") and not any(t.name == "bad" for t in store.all())
    store.save("greet2", ManualTrigger(), steps_with(name="{{who}}"), None, {"who": {"default": "Z"}})
    t2 = store.find("greet2")
    store.delete(t2.id)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_parameters WHERE task_id = ?", (t2.id,)).fetchone()[0] == 0


def test_running_a_workflow_with_inputs_uses_the_bound_values_and_refuses_bad_ones(db):
    calls, clock = [], Clock(ANOW)
    scheduler, _, _ = scheduler_for(db, calls, clock)
    task, _ = scheduler.store.save("greet", ManualTrigger(), steps_with(name="hi {{who}}"), None,
                                   {"who": {"default": "Ada", "description": "a name"}})
    assert asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True, params={"who": "Grace"})).ok
    assert asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True)).ok
    assert calls == [("step_one", "hi Grace"), ("step_one", "hi Ada")]
    calls.clear()
    bad = asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True, params={"who": "a\x00b"}))
    unknown = asyncio.run(scheduler.run(scheduler.store.get(task.id), manual=True, params={"zzz": "x"}))
    assert not bad.ok and not unknown.ok and calls == []                                                  # refused before any step ran


# ------------------------------------------------------------------ the compiler proposes inputs


def _site_routine(db, day, site, level=30):
    base = NOW - timedelta(days=day)
    _audit(db, base, "open_application", ms=800, arguments={"name": "chrome"})
    _audit(db, base + timedelta(seconds=20), "open_website", ms=600, arguments={"site": site})
    _audit(db, base + timedelta(seconds=40), "volume_control", ms=100, arguments={"action": "set", "level": level})


def test_changing_text_becomes_an_input_numbers_and_secrets_stay_fixed(db):
    from app.core import experience as X

    for day, site, level in ((1, "github.com", 30), (2, "docs.python.org", 40), (4, "stackoverflow.com", 50)):
        _site_routine(db, day, site, level)
    p = X.propose(db, NOW)[0]
    assert p.parameters.keys() == {"site"} and p.parameters["site"]["default"] == "github.com"
    assert "github.com" in p.parameters["site"]["description"] and "docs.python.org" in p.parameters["site"]["description"]
    assert p.steps[1]["arguments"] == {"site": "{{site}}"} and p.steps[0]["arguments"] == {"name": "chrome"}
    assert p.steps[2]["arguments"]["level"] == 30 and any("level changed" in v for v in p.varied)        # numbers stay fixed, reported
    assert "Inputs: site" in p.describe()


def test_secret_looking_changing_text_is_never_made_an_input(db):
    from app.core import experience as X

    for day, site in ((1, "https://a.example/?k=aaaaaaaaaaaa"), (2, "https://u:pw12345@x.example/"), (3, "https://c.example")):
        _site_routine(db, day, site)
    assert X.propose(db, NOW) == [] or all("site" not in p.parameters for p in X.propose(db, NOW))


def test_saving_a_parameterised_proposal_and_running_it_with_a_value(db):
    from app.ai.base import ToolCall
    from tests.test_anomaly_experience import _executor

    for day, site in ((1, "github.com"), (2, "docs.python.org"), (3, "bbc.com")):
        base = NOW - timedelta(days=day)
        _audit(db, base, "open_application", arguments={"name": "chrome"})
        _audit(db, base + timedelta(seconds=20), "open_website", arguments={"site": site})
    ex, _ = _executor(db)
    from datetime import datetime, timezone

    # the audit rows are dated relative to NOW (2026-10-27); the tool looks back from the real clock, so re-date them
    shift = datetime.now(timezone.utc) - NOW
    with db.transaction() as conn:
        for rid, ts in conn.execute("SELECT id, ts FROM audit_log").fetchall():
            conn.execute("UPDATE audit_log SET ts = ? WHERE id = ?",
                         ((datetime.fromisoformat(ts) + shift).isoformat(timespec="seconds"), rid))

    def go(tool, **a):
        return asyncio.run(ex.execute(ToolCall(tool, a))).result

    proposal = go("experience", action="propose")
    assert proposal.ok and proposal.data["proposals"][0]["parameters"].keys() == {"site"}, proposal.message
    saved = go("experience", action="save", id="1", name="Open a site")
    assert saved.ok, saved.message
    listed = go("list_automations")
    row = next(a for a in listed.data["automations"] if a["name"] == "Open a site")
    assert row["parameters"].keys() == {"site"} and "{{site}}" in " ".join(row["steps"])
    bad = go("run_workflow", name="Open a site", params={"nope": "x"})
    assert not bad.ok and "no input called" in bad.message


def test_run_workflow_tool_exposes_params_with_a_description():
    from app.tools.automation import RunWorkflowArgs

    assert "named inputs" in RunWorkflowArgs.model_fields["params"].description
