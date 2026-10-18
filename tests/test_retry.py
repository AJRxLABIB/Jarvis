from __future__ import annotations

import pytest

from app.core import retry
from app.core.task_center import mission_status


@pytest.mark.parametrize(("error", "message", "kind"), [
    ("timeout", "", "transient"), ("offline", "", "transient"), ("rate_limited", "", "transient"),
    ("blocked", "", "permission"), ("invalid_arguments", "", "validation"), ("not_verified", "", "verification"),
    ("weird_new_code", "", "permanent"), (None, "the server returned 503, try again", "transient"),
    (None, "Access is denied", "permission"), (None, "something odd", "unknown"), ("out_of_memory", "", "resource"),
])
def test_classification(error, message, kind) -> None:
    assert retry.classify(error, message) == kind


def test_transient_errors_back_off_and_stop() -> None:
    assert retry.should_retry("timeout", "", 1, repeatable=True)[:2] == (True, 1.0)
    assert retry.should_retry("timeout", "", 2, repeatable=True)[:2] == (True, 2.0)
    again, wait, why = retry.should_retry("timeout", "", 3, repeatable=True)
    assert not again and wait == 0.0 and "no more tries" in why


def test_nothing_that_cannot_succeed_is_retried() -> None:
    for code in ("blocked", "invalid_arguments", "unknown_tool", "not_found", "weird"):
        assert retry.should_retry(code, "", 1, repeatable=True)[0] is False


def test_a_state_changing_action_is_never_repeated_automatically() -> None:
    again, _, why = retry.should_retry("timeout", "", 1, repeatable=False)
    assert not again and "could happen twice" in why


def test_total_wait_is_bounded() -> None:
    assert retry.should_retry("timeout", "", 1, repeatable=True, waited_seconds=29.5)[0] is False
    assert retry.delay_before(retry.POLICIES["transient"], 20) == retry.POLICIES["transient"].max_total_seconds


def test_an_interrupted_mission_shows_as_recovery_required() -> None:
    from types import SimpleNamespace as NS

    step = NS(state="failed", result="interrupted: it may or may not have completed")
    assert mission_status(NS(state="paused", steps=[step])) == "RECOVERY_REQUIRED"
    assert mission_status(NS(state="paused", steps=[NS(state="failed", result="boom")])) == "PAUSED"
