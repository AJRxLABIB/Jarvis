from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest

from app.automation import simulator as sim
from app.automation.simulator import Behaviour, Scenario
from app.core import emergency
from app.tools.catalog import build_default_registry

REG = build_default_registry()
STEPS = [{"tool": "create_note", "arguments": {"title": "report"}},
         {"tool": "set_reminder", "arguments": {"text": "review"}}]
# stand-ins: the simulator never calls handlers, it only needs the NAMES to exist in the real registry
REAL = [{"tool": "get_datetime"}, {"tool": "calculate", "arguments": {"expression": "1+1"}}]


def test_every_result_is_labelled_simulated_and_nothing_real_runs(monkeypatch) -> None:
    real_connect = socket.socket.connect

    def refuse(self, address=None, *a, **k):
        if isinstance(address, tuple) and address and address[0] in ("127.0.0.1", "::1"):
            return real_connect(self, address, *a, **k)          # asyncio's own self-pipe on Windows connects over loopback
        pytest.fail("the simulator must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)        # asyncio's own socketpair needs no connect()
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("the simulator must not open a network connection"))
    report = sim.simulate_workflow(REAL, registry=REG)
    text = report.describe()
    assert text.startswith(sim.BANNER) and "(simulated)" in text and "hypothetical" in text.lower()
    assert report.final_ok is True and sum(report.effects.values()) == 2


def test_unknown_tool_is_reported_before_anything_is_played() -> None:
    report = sim.simulate_workflow([{"tool": "no_such_tool"}], registry=REG)
    assert report.final_ok is None and "no tool called 'no_such_tool'" in report.problems[0]


def test_handlers_are_never_called() -> None:
    called = []
    names = [s["tool"] for s in REAL]
    originals = {n: REG.get(n).handler for n in names}
    for n in names:                                   # Tool is a frozen dataclass: swap the handler, restore afterwards
        object.__setattr__(REG.get(n), "handler", lambda *a, **k: called.append(1))
    try:
        sim.simulate_workflow(REAL, registry=REG)
    finally:
        for n, handler in originals.items():
            object.__setattr__(REG.get(n), "handler", handler)
    assert called == []


def test_failure_stops_the_run_and_resume_continues_at_the_failed_step_not_from_the_start() -> None:
    sc = Scenario(behaviours={"calculate": Behaviour(fail_times=1)}, resumes=1)
    report = sim.simulate_workflow(REAL, sc, REG)
    assert report.final_ok is True
    assert report.effects == {"get_datetime()": 1, "calculate(expression='1+1')": 1}   # step 1 was NOT repeated
    assert "resumed at step 2" in report.describe()


# a step that CHANGES something (send_email is level 2): the engine must not repeat it blindly after an unknown outcome
MAIL = [{"tool": "get_datetime"}, {"tool": "send_email", "arguments": {"to": "a@b.c", "subject": "s", "body": "b"}}]
SENT = "send_email(body='b', subject='s', to='a@b.c')"


def test_crash_after_the_effect_is_NOT_blindly_repeated_on_resume() -> None:
    report = sim.simulate_workflow(MAIL, Scenario(crash_on_tool="send_email", crash_after_effect=True), REG)
    text = report.describe()
    assert report.final_ok is False and "was NOT resumed" in text and "may already have happened" in text
    assert report.effects == {"get_datetime()": 1, SENT: 1} and report.duplicated == []    # sent once, not twice


def test_you_can_tell_resume_the_step_already_happened() -> None:
    sc = Scenario(crash_on_tool="send_email", crash_after_effect=True, on_uncertain="skip")
    report = sim.simulate_workflow(MAIL, sc, REG)
    assert report.final_ok is True and report.effects[SENT] == 1 and report.duplicated == []
    assert "skipped on your say-so" in report.describe()


def test_you_can_tell_resume_to_repeat_and_the_risk_is_shown_if_it_had_happened() -> None:
    report = sim.simulate_workflow(MAIL, Scenario(crash_on_tool="send_email", on_uncertain="repeat"), REG)
    assert report.final_ok is True and report.duplicated == [SENT] and "DESIGN PROBLEM" in report.describe()
    before = sim.simulate_workflow(MAIL, Scenario(crash_on_tool="send_email", crash_after_effect=False, on_uncertain="repeat"), REG)
    assert before.final_ok is True and before.effects[SENT] == 1 and before.duplicated == []   # it had not happened


def test_a_read_only_step_is_simply_repeated_after_a_crash() -> None:
    report = sim.simulate_workflow(REAL, Scenario(crash_on_tool="calculate"), REG)
    assert report.final_ok is True and "was NOT resumed" not in report.describe()


def test_an_idempotent_declaration_does_not_remove_the_safety_prompt_but_prevents_the_duplicate() -> None:
    sc = Scenario(behaviours={"send_email": Behaviour(idempotent=True)}, crash_on_tool="send_email", on_uncertain="repeat")
    report = sim.simulate_workflow(MAIL, sc, REG)
    assert report.final_ok is True and report.duplicated == [] and report.effects[SENT] == 1


def test_no_restart_leaves_the_run_unfinished() -> None:
    report = sim.simulate_workflow(REAL, Scenario(crash_on_tool="calculate", restart_after_crash=False), REG)
    assert report.final_ok is False and "not restarted" in report.describe()


def test_lost_response_blocks_a_blind_retry() -> None:
    sc = Scenario(behaviours={"send_email": Behaviour(outcome="lost_response")}, resumes=1)
    report = sim.simulate_workflow(MAIL, sc, REG)
    assert report.final_ok is False and "was NOT resumed" in report.describe()
    assert report.effects[SENT] == 1                              # applied once; the engine refused to send it again


def test_a_timeout_is_ambiguous_but_a_clean_refusal_is_retried_automatically() -> None:
    timeout = sim.simulate_workflow(MAIL, Scenario(behaviours={"send_email": Behaviour(outcome="timeout")}, resumes=1), REG)
    assert timeout.final_ok is False and "was NOT resumed" in timeout.describe()
    clean = sim.simulate_workflow(MAIL, Scenario(behaviours={"send_email": Behaviour(fail_times=1)}, resumes=1), REG)
    assert clean.final_ok is True and clean.effects[SENT] == 1 and "was NOT resumed" not in clean.describe()


@pytest.mark.parametrize("outcome", ["fail", "timeout", "denied"])
def test_failures_have_no_effect_and_stop_the_run(outcome: str) -> None:
    report = sim.simulate_workflow(REAL, Scenario(behaviours={"get_datetime": Behaviour(outcome=outcome)}), REG)
    assert report.final_ok is False and report.effects == {}
    assert "stopped at step 1" in report.describe()


def test_permission_denial_message_is_clear() -> None:
    report = sim.simulate_workflow(REAL, Scenario(behaviours={"get_datetime": Behaviour(outcome="denied")}), REG)
    assert "permission denied (simulated)" in report.describe()


def test_duplicate_trigger_is_refused_not_run_twice() -> None:
    report = sim.simulate_workflow(REAL, Scenario(duplicate_trigger=True), REG)
    assert "second trigger: refused" in report.describe() and report.effects["get_datetime()"] == 1


def test_emergency_stop_reaches_a_run_in_progress_and_the_real_flag_is_restored() -> None:
    before = dict(emergency.STATE.flags)
    report = sim.simulate_workflow(REAL, Scenario(stop_before_tool="calculate"), REG)
    assert report.final_ok is False and "emergency" in report.describe().lower()
    assert "calculate(expression='1+1')" not in report.effects
    assert emergency.STATE.flags == before                       # the simulation left no switch changed


def test_real_emergency_flags_survive_a_simulation() -> None:
    emergency.STATE.flags["locked"] = True
    try:
        sim.simulate_workflow(REAL, registry=REG)
        assert emergency.STATE.flags["locked"] is True
    finally:
        emergency.STATE.flags["locked"] = False


def test_simulated_time_shows_when_a_schedule_fires_without_waiting() -> None:
    start = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc).astimezone()
    report = sim.simulate_schedule([{"tool": "get_datetime"}], {"type": "daily", "time": "09:00"}, start, days=3, registry=REG)
    assert len(report.runs) == 3 and report.effects == {"get_datetime()": 3}
    assert "3 run(s) in 3 simulated day(s)" in report.describe()


def test_simulation_does_not_touch_the_users_database(tmp_path, monkeypatch) -> None:
    import tempfile

    seen = []
    real = tempfile.TemporaryDirectory

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.name)

    monkeypatch.setattr(sim.tempfile, "TemporaryDirectory", Spy)
    sim.simulate_workflow(REAL, registry=REG)
    assert seen and all("jarvis-sim-" in s for s in seen)
    import os

    assert not any(os.path.exists(s) for s in seen)               # scratch space deleted afterwards


def test_tool_simulates_a_saved_workflow_without_changing_it(tmp_path) -> None:
    from app.automation.tasks import AutomationStore, parse_steps
    from app.automation.triggers import ManualTrigger
    from app.config.settings import Settings
    from app.memory.database import Database
    from app.tools.automation import SimulateArgs, simulate_workflow_tool
    from app.tools.base import ToolContext, ToolError

    db = Database(tmp_path / "w.db")
    db.initialize()
    store = AutomationStore(db)
    store.save("morning", ManualTrigger(), parse_steps(MAIL), None)
    before = store.find("morning").to_dict()
    ctx = ToolContext(Settings(), database=db)
    blocked = simulate_workflow_tool(SimulateArgs(workflow="morning", what_if="crash_and_restart", tool="send_email",
                                                  resumes=0), ctx)
    assert blocked.data["simulated"] is True and blocked.data["final_ok"] is False and "SIMULATION" in blocked.message
    assert "was NOT resumed" in blocked.message and blocked.data["duplicated"] == []
    repeat = simulate_workflow_tool(SimulateArgs(workflow="morning", what_if="crash_and_restart", tool="send_email",
                                                 on_uncertain="repeat"), ctx)
    assert repeat.data["duplicated"] == [SENT]                         # you chose to repeat; the risk is shown
    assert store.find("morning").to_dict() == before                  # the saved workflow is untouched
    with pytest.raises(ToolError):
        simulate_workflow_tool(SimulateArgs(workflow="morning", tool="open_application"), ctx)
    safe = simulate_workflow_tool(SimulateArgs(workflow="morning", what_if="crash_and_restart", tool="send_email",
                                               treat_as_idempotent=True, on_uncertain="repeat"), ctx)
    assert safe.data["duplicated"] == []


def test_invalid_outcome_is_rejected() -> None:
    with pytest.raises(ValueError):
        Behaviour(outcome="explode")
