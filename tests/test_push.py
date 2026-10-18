from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.memory.database import LATEST_SCHEMA_VERSION, Database
from app.remote import push
from app.remote.sync import SyncStore

TOKEN = "fcm-token-" + "a" * 40


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    account = tmp_path / "sa.json"
    account.write_text(json.dumps({"client_email": "svc@my-project.iam.gserviceaccount.com", "private_key": key,
                                   "project_id": "my-project"}), encoding="utf-8")
    sent: list[dict] = []
    state = {"fcm_status": 200, "fcm_body": "{}"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in request.content
            return httpx.Response(200, json={"access_token": "ya29.fake-access-token-value-xxxxxxxx", "expires_in": 3600})
        sent.append({"auth": request.headers["authorization"], "json": json.loads(request.content), "url": str(request.url)})
        return httpx.Response(state["fcm_status"], text=state["fcm_body"])

    push.TRANSPORT = httpx.MockTransport(handler)
    yield db, push.FcmClient(account), sent, state
    push.TRANSPORT = None


def phone(db, device="phone-0001", trust="trusted", notifications=True) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO mobile_devices (id, name, public_key, fingerprint, trust, paired_at) "
                     "VALUES (?, 'p', 'k', ?, ?, 'x')", (device, "fp" + device, trust))
    if notifications:
        SyncStore(db).set_enabled(device, "notifications", True)
    push.PushStore(db).register(device, TOKEN)


def event(db, name="Reminder") -> int:
    with db.transaction() as conn:
        return conn.execute("INSERT INTO events (ts, name) VALUES ('t', ?)", (name,)).lastrowid


def test_schema_has_push_tokens(env) -> None:
    db, *_ = env
    assert LATEST_SCHEMA_VERSION >= 31 and db.schema_version() == LATEST_SCHEMA_VERSION


def test_a_ping_contains_no_content_and_marks_progress(env) -> None:
    db, client, sent, _ = env
    phone(db)
    event_id = event(db)
    assert push.dispatch(db, client) == {"sent": 1, "skipped": 0, "removed": 0, "failed": 0}
    message = sent[0]["json"]["message"]
    assert message["data"] == {"kind": "sync"} and message["token"] == TOKEN and "notification" not in message
    assert "my-project" in sent[0]["url"] and sent[0]["auth"].startswith("Bearer ya29.")
    assert push.dispatch(db, client)["sent"] == 0                                   # nothing new: no second ping
    with db.connect() as conn:
        assert conn.execute("SELECT last_event_id FROM push_tokens").fetchone()[0] == event_id


def test_old_events_are_not_pushed_after_registering(env) -> None:
    db, client, sent, _ = env
    event(db)
    phone(db)
    assert push.dispatch(db, client)["sent"] == 0 and sent == []


def test_irrelevant_events_untrusted_phones_and_switched_off_datasets_get_nothing(env) -> None:
    db, client, sent, _ = env
    phone(db, "phone-0001", notifications=False)
    phone(db, "phone-0002", trust="revoked")
    phone(db, "phone-0003", trust="pending")
    event(db, "SomethingPrivate")
    event(db, "Reminder")
    assert push.dispatch(db, client)["sent"] == 0 and sent == []


def test_rate_limit_emergency_stop_and_dead_tokens(env) -> None:
    db, client, sent, state = env
    phone(db)
    event(db)
    assert push.dispatch(db, client, emergency_on=True)["sent"] == 0 and sent == []
    now = [1_000_000_000.0]
    assert push.dispatch(db, client, clock=lambda: now[0])["sent"] == 1
    event(db)
    assert push.dispatch(db, client, clock=lambda: now[0] + 5)["skipped"] == 1       # within 30 s
    state["fcm_status"], state["fcm_body"] = 404, '{"error": {"status": "UNREGISTERED"}}'
    now[0] += 4_000_000_000                                                          # long after the rate limit
    assert push.dispatch(db, client, clock=lambda: now[0])["removed"] == 1
    assert not push.PushStore(db).registered("phone-0001")                          # Google said it is gone: deleted


def test_failures_are_recorded_without_the_token_or_key(env) -> None:
    db, client, sent, state = env
    phone(db)
    event(db)
    state["fcm_status"] = 500
    assert push.dispatch(db, client)["failed"] == 1
    with db.connect() as conn:
        error = conn.execute("SELECT last_error FROM push_tokens").fetchone()[0]
    assert "500" in error and TOKEN not in error and "PRIVATE" not in error


def test_bad_tokens_and_bad_account_files_are_refused(env, tmp_path: Path) -> None:
    db, *_ = env
    with pytest.raises(push.PushError):
        push.PushStore(db).register("phone-0001", "short")
    with pytest.raises(push.PushError):
        push.PushStore(db).register("phone-0001", "has spaces and <script> " + "x" * 30)
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(push.PushError):
        push.FcmClient(bad)
    with pytest.raises(push.PushError):
        push.FcmClient(tmp_path / "missing.json")


def test_revoking_a_phone_deletes_its_token(env) -> None:
    from app.remote.mobile import MobileRegistry

    db, *_ = env
    phone(db)
    MobileRegistry(db).set_trust("phone-0001", "revoked")
    assert not push.PushStore(db).registered("phone-0001")


def test_the_access_token_is_cached(env) -> None:
    db, client, sent, _ = env
    phone(db)
    event(db)
    client.ping(TOKEN)
    first = client._access
    client.ping(TOKEN)
    assert client._access == first


def test_quiet_hours_only_let_emergencies_ping_and_the_rest_wait(env) -> None:
    db, client, sent, _ = env
    phone(db)
    event(db, "Reminder")
    assert push.dispatch(db, client, quiet=lambda: True)["sent"] == 0 and sent == []
    assert push.dispatch(db, client, quiet=lambda: False)["sent"] == 1                # after quiet hours: the waiting one goes
    event(db, "Emergency")
    result = push.dispatch(db, client, clock=lambda: 4_000_000_000.0, quiet=lambda: True)
    assert result["sent"] == 1 and len(sent) == 2
    assert push.dispatch(db, client, clock=lambda: 4_100_000_000.0, quiet=lambda: True)["sent"] == 0   # not re-sent every pass
