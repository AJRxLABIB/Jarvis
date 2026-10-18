"""Instant phone updates without Firebase: the signed, read-only ``wait_events`` long poll."""

import asyncio
import time
from types import SimpleNamespace

from app.remote import mobile
from app.remote.server import RemoteHub, RemoteServer


def _server():
    hub = RemoteHub(send=lambda t: None, answer=lambda a, i: True, pending=lambda: state["pending"], status=dict)
    state = {"pending": None}
    server = SimpleNamespace(hub=hub)
    server._wait_events = lambda args, step=0.02: RemoteServer._wait_events(server, args, step)
    return server, hub, state


DEVICE = SimpleNamespace(id="p1", name="Pixel", trust="trusted", permissions=["read"])


def test_wait_events_is_read_only_and_not_step_up():
    assert mobile.COMMANDS["wait_events"] == ("read", None)
    assert "wait_events" not in mobile.STEP_UP_COMMANDS


def test_answers_at_once_when_something_is_already_new():
    server, hub, _ = _server()
    hub.add("assistant", "hello")
    out = asyncio.run(RemoteServer._mobile_run(server, DEVICE, "wait_events", {"since": 0, "timeout": 5}))
    assert out["changed"] and [m["text"] for m in out["messages"]] == ["hello"]


def test_waits_and_answers_the_moment_a_message_arrives():
    server, hub, _ = _server()

    async def scenario():
        async def later():
            await asyncio.sleep(0.15)
            hub.add("assistant", "done")
        started = time.monotonic()
        task = asyncio.create_task(later())
        out = await RemoteServer._mobile_run(server, DEVICE, "wait_events", {"since": 0, "timeout": 5})
        await task
        return out, time.monotonic() - started

    out, took = asyncio.run(scenario())
    assert out["changed"] and out["messages"][0]["text"] == "done" and 0.1 < took < 1.5


def test_approval_question_changes_wake_the_phone_and_timeout_is_capped():
    server, hub, state = _server()
    state["pending"] = {"id": "c1", "text": "Shut down?"}
    out = asyncio.run(RemoteServer._mobile_run(server, DEVICE, "wait_events", {"since": 0, "pending_id": "", "timeout": 5}))
    assert out["changed"] and out["pending"]["id"] == "c1"
    started = time.monotonic()
    out = asyncio.run(RemoteServer._mobile_run(server, DEVICE, "wait_events",
                                               {"since": 0, "pending_id": "c1", "timeout": 1}))
    assert not out["changed"] and 0.9 < time.monotonic() - started < 2.0          # nothing new: waited, then answered
    state["pending"] = None
    out = asyncio.run(RemoteServer._mobile_run(server, DEVICE, "wait_events",
                                               {"since": 0, "pending_id": "c1", "timeout": 5}))
    assert out["changed"] and out["pending"] is None                               # answered elsewhere: the phone hears it


def test_bad_arguments_are_clamped():
    server, hub, _ = _server()
    hub.add("assistant", "x")
    out = asyncio.run(RemoteServer._mobile_run(server, DEVICE, "wait_events", {"since": "evil", "timeout": "lots"}))
    assert out["changed"]                                                          # since -> 0, timeout -> default
