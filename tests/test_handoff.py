"""Cross-device task handoff: signed, expiring, single-use, checked against the mission's real state."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core import handoff as H
from app.core.missions import MissionStore, run, validate_plan
from app.remote.mobile import MobileRegistry
from tests.test_v3150 import paired
from tests.test_v3210 import db, make, step  # noqa: F401  (fixtures and helpers)


def mission_of(db, extra=None):
    ex, _, log = make(db)
    store = MissionStore(db)
    plan = [step("look", "a"), step("create_item", "report", checkpoint=True), step("look", "b")]
    plan[1]["arguments"] = {"name": "report"}
    mission = store.create("Prepare submission", validate_plan(plan, ex.registry, ex.context))
    return ex, store, mission, log


def code(call):
    with pytest.raises(H.HandoffError) as e:
        call()
    return e.value.code


@pytest.fixture()
def world(db):
    registry = MobileRegistry(db)
    phone = paired(registry)
    ex, store, mission, log = mission_of(db)
    return db, registry, phone, ex, store, mission


def test_package_contents_and_what_it_leaves_out(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    assert p["v"] == 1 and p["checkpoint"] == 1 and p["completed"] == [] and p["next"].startswith("look a")
    assert p["pending_approvals"] == ["create_item report"] and "create_item" in p["needs"]
    assert all(set(s) == {"n", "title", "tool", "state"} for s in p["steps"])          # no arguments travel
    assert "report" not in json.dumps([s for s in p["steps"] if "arguments" in s])
    assert p["mac"] and datetime.fromisoformat(p["expires"]) > datetime.fromisoformat(p["created"])
    assert [r["id"] for r in H.listing(db)] == [p["id"]]


def test_a_valid_handoff_is_accepted_once_and_runs_nothing(world):
    db, registry, phone, ex, store, mission, = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    plan = H.accept(db, json.dumps(p), store, phone.device_id, registry=registry)
    assert isinstance(plan, H.ResumePlan) and "Waiting for your approval" in plan.describe() and "Nothing has run" in plan.describe()
    assert store.get(mission.id).state == "planned" and all(s.state == "pending" for s in store.get(mission.id).steps)
    assert code(lambda: H.accept(db, p, store, phone.device_id, registry=registry)) == "consumed"
    assert code(lambda: H.validate(db, p, registry=registry)) == "consumed"


def test_tampering_forgery_and_unknown_packages_are_refused(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    for field, value in (("title", "Delete everything"), ("checkpoint", 3), ("device_id", "someoneelse"), ("expires", "2099-01-01T00:00:00+00:00")):
        bad = dict(p, **{field: value})
        assert code(lambda bad=bad: H.validate(db, bad)) == "bad_mac", field
    forged = dict(p, mac="AAAA")
    assert code(lambda: H.validate(db, forged)) == "bad_mac"
    other = {**p, "id": "forged-id-12345"}
    other["mac"] = H._sign(db, other)                        # even correctly signed, this PC never issued it
    assert code(lambda: H.validate(db, other)) == "unknown"


@pytest.mark.parametrize("broken", [None, [], "not json", "{}", {"v": 1}, '{"v": 99}'])
def test_malformed_packages(world, broken):
    assert code(lambda: H.validate(world[0], broken)) == "malformed"


def test_wrong_version_size_and_identifier(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    assert code(lambda: H.validate(db, {**p, "v": 2})) == "malformed"
    assert code(lambda: H.validate(db, {**p, "steps": [{}] * 60})) == "malformed"
    assert code(lambda: H.validate(db, {**p, "id": "../../x"})) == "malformed"
    assert code(lambda: H.validate(db, "x" * (H.MAX_BYTES * 2 + 1))) == "malformed"


def test_expiry_wrong_device_and_revocation(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, ttl_minutes=5, registry=registry)
    late = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert code(lambda: H.validate(db, p, now=late)) == "expired"
    assert H.listing(db, now=late) == [] and H.listing(db, now=late, active_only=False)[0]["effective"] == "expired"
    assert code(lambda: H.validate(db, p, device_id="another-phone")) == "wrong_device"
    assert H.revoke(db, handoff_id=p["id"]) == 1
    assert code(lambda: H.validate(db, p)) == "revoked"


def test_revoking_the_device_kills_every_unused_package(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    registry.set_trust(phone.device_id, "revoked")                              # a revoked phone must pair again
    assert code(lambda: H.validate(db, p, registry=registry)) == "revoked"
    assert code(lambda: H.create(db, mission, phone.device_id, registry=registry)) == "wrong_device"


def test_revoke_by_device_and_by_id(world):
    db, registry, phone, ex, store, mission = world
    a = H.create(db, mission, phone.device_id, registry=registry)
    b = H.create(db, mission, phone.device_id, registry=registry)
    assert H.revoke(db, device_id=phone.device_id) == 2
    assert code(lambda: H.validate(db, a, registry=registry)) == "revoked"
    assert code(lambda: H.validate(db, b, registry=registry)) == "revoked"
    assert H.revoke(db, handoff_id=a["id"]) == 0                                # already revoked: nothing to do
    assert code(lambda: H.revoke(db)) == "missing_id"


def test_untrusted_or_unknown_devices_cannot_receive_a_handoff(world):
    db, registry, phone, ex, store, mission = world
    assert code(lambda: H.create(db, mission, "nonexistent", registry=registry)) == "wrong_device"
    pending = paired(registry, trust="pending")
    assert code(lambda: H.create(db, mission, pending.device_id, registry=registry)) == "wrong_device"


def test_secrets_never_travel(world):
    db, registry, phone, ex, store, mission = world
    store.set_state(mission, "running", "the token is sk-abcdefghijklmnopqrstuvwxyz0123456789 keep it")
    p = H.create(db, store.get(mission.id), phone.device_id, registry=registry)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(p)                 # redacted on the way out
    evil = dict(p, context="password=hunter2hunter2 api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789")
    evil["mac"] = H._sign(db, evil)
    with db.transaction() as conn:
        conn.execute("UPDATE handoffs SET digest = ? WHERE id = ?", (H._digest(evil), p["id"]))
    assert code(lambda: H.validate(db, evil)) == "secret"


def test_a_stale_handoff_is_a_conflict_and_is_not_consumed(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    live = store.get(mission.id)
    live.steps[0].state = "done"
    store.save_step(live, live.steps[0])                                       # the PC moved on after the package was made
    outcome = H.accept(db, p, store, phone.device_id, registry=registry)
    assert isinstance(outcome, H.Conflict) and "finished steps changed" in outcome.describe()
    assert [r["id"] for r in H.listing(db)] == [p["id"]]                       # still unused
    fresh = H.create(db, store.get(mission.id), phone.device_id, registry=registry)
    assert fresh["completed"] == [1] and fresh["checkpoint"] == 2
    assert isinstance(H.accept(db, fresh, store, phone.device_id, registry=registry), H.ResumePlan)


def test_finished_or_missing_missions(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    store.set_state(mission, "cancelled", "no longer needed")
    assert isinstance(H.accept(db, p, store, phone.device_id, registry=registry), H.Conflict)
    assert code(lambda: H.create(db, store.get(mission.id), phone.device_id, registry=registry)) == "finished"
    with db.transaction() as conn:
        conn.execute("DELETE FROM missions WHERE id = ?", (mission.id,))
    assert code(lambda: H.accept(db, p, store, phone.device_id, registry=registry)) == "conflict"


def test_two_devices_cannot_both_resume_one_package(world):
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    assert isinstance(H.accept(db, p, store, phone.device_id, registry=registry), H.ResumePlan)
    with db.transaction() as conn:                                              # simulate the second racer reaching the UPDATE
        conn.execute("UPDATE handoffs SET state = 'issued' WHERE id = ?", (p["id"],))
    first = H.accept(db, p, store, phone.device_id, registry=registry)
    assert isinstance(first, H.ResumePlan)
    assert code(lambda: H.accept(db, p, store, phone.device_id, registry=registry)) == "consumed"


def test_a_resumed_mission_still_asks_for_its_approvals(world):
    """Accepting a handoff never grants anything: running the mission goes through the normal approvals."""
    db, registry, phone, ex, store, mission = world
    p = H.create(db, mission, phone.device_id, registry=registry)
    H.accept(db, p, store, phone.device_id, registry=registry)
    outcome = asyncio.run(run(store.get(mission.id), store, ex, approved=False))
    assert not outcome.ok or store.get(mission.id).state != "completed"


def test_the_signing_key_is_stable_per_install_and_not_in_the_package(world):
    db, registry, phone, ex, store, mission = world
    k1 = H._key(db)
    assert k1 == H._key(db) and len(k1) == 32
    import base64

    assert base64.b64encode(k1).decode() not in json.dumps(H.create(db, mission, phone.device_id, registry=registry))


# ------------------------------------------------------------------ the tool


def test_handoff_tool_round_trip_and_read_only_actions(world):
    from tests.test_intelligence import run as run_tool

    db, registry, phone, ex, store, mission = world
    from app.config.files import PermissionsConfig
    from app.security.permissions import PermissionEngine
    from app.tools.catalog import build_default_registry
    from app.agent.tool_manager import ToolExecutor
    from app.config.settings import Settings
    from app.tools.base import ToolContext
    from tests.test_tools import ScriptedConfirmer

    confirmer = ScriptedConfirmer(True)
    real = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ToolContext(Settings(), database=db), confirmer=confirmer)
    made = run_tool(real, "handoff", action="create", mission_id=mission.id, device_id=phone.device_id)
    assert made.ok and "no arguments or secrets" in made.message
    package = made.data["package"]
    assert run_tool(real, "handoff", action="inspect", package=package).ok
    assert "1 handoff" in run_tool(real, "handoff", action="list").message or made.data["id"][:8] in run_tool(real, "handoff", action="list").message
    accepted = run_tool(real, "handoff", action="accept", package=package, device_id=phone.device_id)
    assert accepted.ok and "Resume" in accepted.message
    again = run_tool(real, "handoff", action="accept", package=package)
    assert not again.ok and again.error == "consumed"
    assert not run_tool(real, "handoff", action="create", mission_id=mission.id).ok
    from app.security import autonomy

    assert autonomy.is_read_only("handoff", {"action": "list"}) and not autonomy.is_read_only("handoff", {"action": "accept"})


def test_digit_heavy_identifiers_are_not_mistaken_for_secrets(world):
    """Regression: mission ids look like m20261027-192602-b81c; the card-number heuristic flagged one in ten of them."""
    import dataclasses

    db, registry, phone, ex, store, mission = world
    flagged = 0
    for n in range(60):
        candidate = f"m20261027-19260{n % 10}-{n:04d}"
        flagged += H._secret_in(candidate)                       # some of these really do look like card numbers to the heuristic
        package = H.create(db, dataclasses.replace(mission, id=candidate), phone.device_id, registry=registry)
        assert H.validate(db, package, registry=registry)["mission_id"] == candidate
    assert flagged > 0                                           # the test is meaningful: the old behaviour would have failed
