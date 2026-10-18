from __future__ import annotations

from pathlib import Path

import pytest

from app.memory.database import Database
from app.remote import mobile
from app.remote.sync import MAX_CHANGES, NOTIFICATION_TEXT, SyncError, SyncStore


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    store = SyncStore(db)
    return db, store


def add_todo(db, text="buy milk"):
    with db.transaction() as conn:
        return conn.execute("INSERT INTO todos (text, created_at) VALUES (?, 'x')", (text,)).lastrowid


def todo(db, record_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM todos WHERE id = ?", (record_id,)).fetchone()


def test_nothing_syncs_until_enabled_on_the_pc(env) -> None:
    db, store = env
    add_todo(db)
    with pytest.raises(SyncError) as exc:
        store.pull("phone-0001", "todos")
    assert exc.value.code == "not_enabled"
    with pytest.raises(SyncError):
        store.push("phone-0001", "todos", [{"op_id": "op-00000001", "op": "create", "fields": {"text": "x"}}])
    with pytest.raises(SyncError):
        store.notifications("phone-0001")


def test_unknown_datasets_cannot_be_enabled(env) -> None:
    _, store = env
    for bad in ("memories", "documents", "settings", "secrets"):
        with pytest.raises(SyncError) as exc:
            store.set_enabled("phone-0001", bad, True)
        assert exc.value.code == "unknown_dataset"


def test_first_pull_is_a_snapshot_then_only_changes(env) -> None:
    db, store = env
    first = add_todo(db, "one")
    store.set_enabled("p1", "todos", True)
    snap = store.pull("p1", "todos", 0)
    assert snap["snapshot"] and [c["fields"]["text"] for c in snap["changes"]] == ["one"] and snap["changes"][0]["rev"] == 1
    cursor = snap["next"]
    assert store.pull("p1", "todos", cursor)["changes"] == []
    with db.transaction() as conn:
        conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (first,))
    add_todo(db, "two")
    delta = store.pull("p1", "todos", cursor)
    assert not delta["snapshot"] and {c["fields"]["text"] for c in delta["changes"]} == {"one", "two"}
    done = next(c for c in delta["changes"] if c["fields"]["text"] == "one")
    assert done["fields"]["done"] is True and done["rev"] == 2


def test_deletes_arrive_as_tombstones(env) -> None:
    db, store = env
    rid = add_todo(db)
    store.set_enabled("p1", "todos", True)
    cursor = store.pull("p1", "todos", 0)["next"]
    with db.transaction() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (rid,))
    change = store.pull("p1", "todos", cursor)["changes"][0]
    assert change["op"] == "delete" and change["id"] == rid


def test_a_phone_that_was_away_longer_than_the_log_gets_a_fresh_snapshot(env, monkeypatch) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    monkeypatch.setattr("app.remote.sync.KEEP_LOG", 3)
    rid = add_todo(db)
    for i in range(10):
        with db.transaction() as conn:
            conn.execute("UPDATE todos SET text = ? WHERE id = ?", (f"v{i}", rid))
    store.pull("p1", "todos", 0)
    store._prune()
    result = store.pull("p1", "todos", 1)
    assert result["reset"] and result["snapshot"] and result["changes"][0]["fields"]["text"] == "v9"


def test_push_creates_updates_and_deletes(env) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    out = store.push("p1", "todos", [{"op_id": "op-aaaaaaaa", "op": "create", "client_ref": "tmp1",
                                      "fields": {"text": "  call mum ", "minutes": 30}}])["results"][0]
    assert out["status"] == "applied" and out["client_ref"] == "tmp1"
    rid, rev = out["record_id"], out["rev"]
    assert todo(db, rid)["text"] == "call mum" and todo(db, rid)["minutes"] == 30
    upd = store.push("p1", "todos", [{"op_id": "op-bbbbbbbb", "op": "update", "record_id": rid, "base_rev": rev,
                                      "fields": {"done": True}}])["results"][0]
    assert upd["status"] == "applied" and todo(db, rid)["done"] == 1 and todo(db, rid)["done_at"]
    dele = store.push("p1", "todos", [{"op_id": "op-cccccccc", "op": "delete", "record_id": rid,
                                       "base_rev": upd["rev"]}])["results"][0]
    assert dele["status"] == "applied" and todo(db, rid) is None


def test_duplicate_delivery_changes_nothing(env) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    change = [{"op_id": "op-dddddddd", "op": "create", "fields": {"text": "once"}}]
    first = store.push("p1", "todos", change)["results"][0]
    again = store.push("p1", "todos", change)["results"][0]
    assert first["status"] == "applied" and again["status"] == "duplicate" and again["record_id"] == first["record_id"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1


def test_stale_edit_is_refused_and_the_pc_version_stands(env) -> None:
    db, store = env
    rid = add_todo(db, "original")
    store.set_enabled("p1", "todos", True)
    rev = store.pull("p1", "todos", 0)["changes"][0]["rev"]
    with db.transaction() as conn:   # the PC changes it meanwhile
        conn.execute("UPDATE todos SET text = 'changed on pc' WHERE id = ?", (rid,))
    result = store.push("p1", "todos", [{"op_id": "op-eeeeeeee", "op": "update", "record_id": rid, "base_rev": rev,
                                         "fields": {"text": "phone edit"}}])["results"][0]
    assert result["status"] == "conflict" and result["server"]["fields"]["text"] == "changed on pc"
    assert todo(db, rid)["text"] == "changed on pc"
    retry = store.push("p1", "todos", [{"op_id": "op-ffffffff", "op": "update", "record_id": rid,
                                        "base_rev": result["server"]["rev"], "fields": {"text": "phone edit"}}])["results"][0]
    assert retry["status"] == "applied" and todo(db, rid)["text"] == "phone edit"


def test_edit_of_a_todo_deleted_on_the_pc_is_a_conflict(env) -> None:
    db, store = env
    rid = add_todo(db)
    store.set_enabled("p1", "todos", True)
    with db.transaction() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (rid,))
    r = store.push("p1", "todos", [{"op_id": "op-gggggggg", "op": "update", "record_id": rid, "base_rev": 1,
                                    "fields": {"text": "x"}}])["results"][0]
    assert r["status"] == "conflict" and r["server"] is None


@pytest.mark.parametrize("fields", [{"text": ""}, {"text": "x" * 301}, {"done": "yes"}, {"minutes": 0}, {"minutes": 5000},
                                    {"minutes": True}, {"due": "d" * 41}, {"secret": "x"}, {"text": 5}])
def test_invalid_fields_are_rejected_and_change_nothing(env, fields) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    fields = {"text": "ok", **fields} if "text" not in fields else fields
    r = store.push("p1", "todos", [{"op_id": "op-hhhhhhhh", "op": "create", "fields": fields}])["results"][0]
    assert r["status"] == "rejected"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0


def test_batch_limits_and_bad_op_ids(env) -> None:
    _, store = env
    store.set_enabled("p1", "todos", True)
    with pytest.raises(SyncError):
        store.push("p1", "todos", [])
    with pytest.raises(SyncError):
        store.push("p1", "todos", [{"op_id": f"op-{i:08d}", "op": "create", "fields": {"text": "x"}}
                                   for i in range(MAX_CHANGES + 1)])
    r = store.push("p1", "todos", [{"op": "create", "fields": {"text": "x"}}, "junk"])["results"]
    assert [x["status"] for x in r] == ["rejected", "rejected"]


def test_one_phone_cannot_reuse_another_phones_op_ids_or_data_choices(env) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    store.push("p1", "todos", [{"op_id": "op-iiiiiiii", "op": "create", "fields": {"text": "mine"}}])
    with pytest.raises(SyncError):
        store.pull("p2", "todos")


def test_forget_clears_prefs_and_operation_history(env) -> None:
    db, store = env
    store.set_enabled("p1", "todos", True)
    store.push("p1", "todos", [{"op_id": "op-jjjjjjjj", "op": "create", "fields": {"text": "x"}}])
    assert store.forget_device("p1") >= 2
    assert store.enabled("p1") == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_ops").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1     # the user's data is not touched


def test_notifications_are_generic_and_only_for_known_kinds(env) -> None:
    db, store = env
    store.set_enabled("p1", "notifications", True)
    with db.transaction() as conn:
        for name, data in (("Reminder", '{"text": "secret medical appointment"}'), ("SomethingPrivate", "{}"),
                           ("MissionCompleted", '{"goal": "private goal"}')):
            conn.execute("INSERT INTO events (ts, name, data) VALUES ('2026-01-01T00:00:00Z', ?, ?)", (name, data))
    out = store.notifications("p1", 0)
    assert [n["kind"] for n in out["notifications"]] == ["Reminder", "MissionCompleted"]
    blob = str(out)
    assert "secret" not in blob and "private" not in blob.replace("SomethingPrivate", "")
    assert {n["text"] for n in out["notifications"]} <= set(NOTIFICATION_TEXT.values())
    assert out["delivery"] == "polling"
    assert store.notifications("p1", out["next"])["notifications"] == []


def test_the_phone_commands_exist_with_the_right_permissions() -> None:
    assert mobile.COMMANDS["sync_pull"][0] == "sync" and mobile.COMMANDS["sync_push"][0] == "sync"
    assert mobile.COMMANDS["notifications"][0] == "read"
    assert "sync_push" in mobile.STEP_UP_COMMANDS and "sync_pull" not in mobile.STEP_UP_COMMANDS
    assert "sync" in mobile.PERMISSIONS and "sync" not in mobile.DEFAULT_PERMISSIONS
    assert "sync" not in mobile.RESTRICTED_PERMISSIONS


def test_revoking_a_phone_deletes_its_sync_data(env) -> None:
    db, store = env
    registry = mobile.MobileRegistry(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO mobile_devices (id, name, platform, os_version, app_version, public_key, fingerprint, "
                     "key_info, trust, permissions, paired_at) VALUES ('phone-0001', 'Pixel', 'android', '15', '1', 'k', 'f', "
                     "'{}', 'trusted', '[\"read\"]', 'x')")
    store.set_enabled("phone-0001", "todos", True)
    registry.set_trust("phone-0001", "revoked")
    assert store.enabled("phone-0001") == []
