"""Action lifecycle: validated transitions, approvals bound to the exact action, uncertain outcomes, executor wiring."""

from __future__ import annotations

import time

import pytest

from app.core import action_record as ar
from app.core.action_record import LEDGER, ActionRecord, InvalidTransition, State
from tests.test_tools import Recorder, executor, run


def rec(**kw) -> ActionRecord:
    return ActionRecord("move_file", {"src": "a", "dst": "b"}, level=1, **kw)


def test_digest_ignores_key_order_but_not_values_or_target():
    a = ar.digest("t", {"x": 1, "y": 2}, level=1)
    assert a == ar.digest("t", {"y": 2, "x": 1}, level=1)
    assert a != ar.digest("t", {"x": 1, "y": 3}, level=1)
    assert a != ar.digest("t", {"x": 1, "y": 2}, level=2)
    assert a != ar.digest("t", {"x": 1, "y": 2}, level=1, target_device="phone")
    assert a != ar.digest("t", {"x": 1, "y": 2}, level=1, mission="m1")


def test_happy_path_and_illegal_jumps():
    r = rec()
    r.to(State.VALIDATED).to(State.AWAITING_APPROVAL)
    with pytest.raises(InvalidTransition):
        r.to(State.RUNNING)                      # not approved yet
    r.approve("user").to(State.RUNNING).to(State.VERIFYING).to(State.SUCCEEDED)
    assert r.finished
    with pytest.raises(InvalidTransition):
        r.to(State.RUNNING)                      # a finished action never restarts


def test_running_needs_an_approval_even_an_automatic_one():
    r = rec()
    r.to(State.VALIDATED)
    with pytest.raises(InvalidTransition):
        r.to(State.APPROVED).to(State.RUNNING)


def test_approval_is_void_when_the_action_changes_or_expires():
    r = rec().to(State.VALIDATED)
    r.approve("user")
    r.arguments["dst"] = "somewhere else"        # a material change after the yes
    with pytest.raises(InvalidTransition, match="changed"):
        r.to(State.RUNNING)
    r2 = rec().to(State.VALIDATED)
    r2.approve("user", ttl_s=0.01)
    time.sleep(0.03)
    with pytest.raises(InvalidTransition, match="expired"):
        r2.to(State.RUNNING)


def test_uncertain_and_duplicate_detection():
    ledger = ar.Ledger()
    first = ledger.add(rec(mission="m1")).to(State.VALIDATED)
    first.approve("user").to(State.RUNNING).to(State.UNCERTAIN, "timeout")
    again = ledger.add(rec(mission="m1"))
    assert ledger.duplicate_of(again) is first and ledger.uncertain() == [first]
    assert ledger.duplicate_of(ledger.add(rec(mission="other"))) is None


def test_ledger_is_bounded():
    ledger = ar.Ledger(limit=3)
    for _ in range(5):
        ledger.add(rec())
    assert len(ledger.recent(10)) == 3


# ------------------------------------------------------------------ the executor drives it


def test_executor_records_the_whole_lifecycle_and_audits_it(tmp_path):
    ex, confirmer, audit = executor(tmp_path, Recorder(), confirm=True)
    result = run(ex, "move_thing", path="a")
    assert result.result.ok and result.action_id
    record = LEDGER.get(result.action_id)
    assert [s for s, _ in record.public()["steps"]] == ["validated", "awaiting_approval", "approved", "running", "succeeded"]
    assert record.approval.given_by == "user"
    events = [(r["event"], r["decision"]) for r in audit.recent(50) if r["event"] == "action_state"]
    assert ("action_state", "succeeded") in events and ("action_state", "approved") in events


def test_safe_action_is_approved_by_policy_and_refusal_is_rejected(tmp_path):
    ex, _, _ = executor(tmp_path, Recorder(), confirm=False)
    safe = run(ex, "read_thing", path="a")
    assert LEDGER.get(safe.action_id).approval.given_by == "policy" and LEDGER.get(safe.action_id).state == State.SUCCEEDED
    no = run(ex, "move_thing", path="a")
    assert no.result.error == "declined" and LEDGER.get(no.action_id).state == State.REJECTED


def test_timeout_is_uncertain_and_a_retry_asks_again(tmp_path):
    ex, confirmer, _ = executor(tmp_path, Recorder(), confirm=True)
    first = run(ex, "slow_thing")                 # times out: it may have done its work
    assert first.result.error == "timeout"
    assert LEDGER.get(first.action_id).state == State.UNCERTAIN
    # an identical MODERATE action that ended uncertain must ask again
    prior = LEDGER.add(ActionRecord("move_thing", {"path": "z"}, level=1))
    prior.to(State.VALIDATED).approve("policy").to(State.RUNNING).to(State.UNCERTAIN)
    before = len(confirmer.requests)
    run(ex, "move_thing", path="z")
    assert len(confirmer.requests) == before + 1
    assert "could not confirm" in confirmer.requests[-1].summary
