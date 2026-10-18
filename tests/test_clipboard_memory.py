from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.core.clipboard_memory import MAX_ITEMS, ClipboardStore, ClipboardWatcher, has_card_number, luhn, refusal
from app.memory.database import Database
from app.tools import clipboard_tool
from app.tools.base import ToolContext, ToolError
from app.tools.clipboard_tool import ClipboardHistoryArgs, clipboard_history

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    clock = Clock()
    return db, ClipboardStore(db, 7, clock), clock


def test_normal_text_is_kept_and_found(env) -> None:
    _, store, _ = env
    assert store.add("Invoice 4471 is due on Friday, send it to Sara") == "stored"
    store.add("shopping list: milk, eggs, bread")
    found = store.search("invoice due friday")
    assert len(found) == 1 and "4471" in found[0]["text"]
    assert store.search("zebra") == [] and len(store.search("")) == 2


@pytest.mark.parametrize("text, why", [
    ("", "empty"), ("   ", "empty"), ("x" * 4001, "too long"),
    ("Tr0ub4dor&3xK9!", "looks like a password or key"),
    ("api key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "contains something secret-looking"),
    ("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "contains something secret-looking"),
    ("pay with 4111 1111 1111 1111 please", "contains a card number"),
])
def test_secrets_cards_and_junk_are_never_kept(env, text: str, why: str) -> None:
    db, store, _ = env
    assert store.add(text) == why and store.count() == 0
    assert refusal(text) == why


def test_luhn_and_card_detection_do_not_catch_ordinary_numbers() -> None:
    assert luhn("4111111111111111") and not luhn("4111111111111112")
    assert not has_card_number("order 12345678 shipped on 2026-10-15") and has_card_number("4111-1111-1111-1111")


def test_private_mode_keeps_nothing(env) -> None:
    _, store, _ = env
    assert store.add("a perfectly normal note", remember=False) == "private mode" and store.count() == 0


def test_copying_the_same_text_again_moves_it_up_instead_of_duplicating(env) -> None:
    _, store, clock = env
    store.add("first thing to remember")
    store.add("second thing to remember")
    clock.now += timedelta(minutes=5)
    assert store.add("first thing to remember") == "moved" and store.count() == 2
    assert store.recent(1)[0]["text"].startswith("first")


def test_old_items_expire_but_pinned_ones_stay(env) -> None:
    _, store, clock = env
    store.add("temporary note for the week")
    store.add("keep this one forever please")
    keep = store.recent(1)[0]["id"]
    store.pin(keep)
    clock.now += timedelta(days=8)
    store.add("fresh note today")
    assert [i["text"] for i in store.recent(10)] == ["fresh note today", "keep this one forever please"]


def test_the_total_is_capped(env) -> None:
    _, store, clock = env
    for i in range(MAX_ITEMS + 20):
        clock.now += timedelta(seconds=1)
        store.add(f"note number {i} about something")
    assert store.count() == MAX_ITEMS and "note number 219" in store.recent(1)[0]["text"]


def test_forget_and_clear(env) -> None:
    _, store, _ = env
    store.add("one note about a thing")
    store.add("two note about a thing")
    first = store.recent(5)[-1]["id"]
    assert store.forget(first) and not store.forget(first) and store.count() == 1
    assert store.clear() == 1 and store.count() == 0


def test_watcher_respects_switch_privacy_and_repeats(env) -> None:
    _, store, _ = env
    on = {"v": False}
    remember = {"v": True}
    watcher = ClipboardWatcher(store, enabled=lambda: on["v"], remember=lambda: remember["v"])
    assert watcher.changed("some text I copied") == "off" and store.count() == 0
    on["v"] = True
    assert watcher.changed("some text I copied") == "stored" and watcher.changed("some text I copied") == "same"
    remember["v"] = False
    assert watcher.changed("a second text copied") == "private mode" and store.count() == 1
    remember["v"] = True
    assert watcher.changed("Tr0ub4dor&3xK9!") == "looks like a password or key"
    assert watcher.refused == {"private mode": 1, "looks like a password or key": 1}      # reasons only, never the text


def ctx(db, **settings) -> ToolContext:
    return ToolContext(Settings(**settings), database=db, clock=lambda: T0)


def test_tool_is_off_by_default_and_explains(env) -> None:
    db, store, _ = env
    store.add("something copied earlier")
    off = clipboard_history(ClipboardHistoryArgs(action="search", query="copied"), ctx(db))
    assert "Clipboard memory is off" in off.message
    assert "off; 1 item" in clipboard_history(ClipboardHistoryArgs(action="status"), ctx(db)).message


def test_tool_search_pin_forget_clear_and_restore(env, monkeypatch) -> None:
    db, store, _ = env
    store.add("meeting link: https://example.com/room/42 at 3pm")
    c = ctx(db, clipboard_history=True)
    listed = clipboard_history(ClipboardHistoryArgs(action="search", query="meeting link"), c)
    item = listed.data["items"][0]["id"]
    assert "untrusted" in listed.message and "example.com/room/42" in listed.message
    copied: list[str] = []
    monkeypatch.setattr(clipboard_tool.winapi, "require_windows", lambda: None)
    monkeypatch.setattr(clipboard_tool.winapi, "set_clipboard_text", copied.append)
    assert "back on the clipboard" in clipboard_history(ClipboardHistoryArgs(action="restore", item=item), c).message
    assert copied == ["meeting link: https://example.com/room/42 at 3pm"]
    assert "Pinned" in clipboard_history(ClipboardHistoryArgs(action="pin", item=item), c).message
    with pytest.raises(ToolError):
        clipboard_history(ClipboardHistoryArgs(action="forget"), c)
    with pytest.raises(ToolError):
        clipboard_history(ClipboardHistoryArgs(action="forget", item=9999), c)
    clipboard_history(ClipboardHistoryArgs(action="forget", item=item), c)
    store.add("another one to erase")
    assert clipboard_history(ClipboardHistoryArgs(action="clear"), c).message.startswith("Erased 1")


def test_risk_levels() -> None:
    from app.security.permissions import RiskLevel

    risk = clipboard_tool._risk
    assert risk(ClipboardHistoryArgs(action="clear"), None) == RiskLevel.DANGEROUS
    assert risk(ClipboardHistoryArgs(action="restore", item=1), None) == RiskLevel.MODERATE
    assert risk(ClipboardHistoryArgs(action="forget", item=1), None) == RiskLevel.MODERATE
    assert risk(ClipboardHistoryArgs(action="search"), None) == RiskLevel.SAFE
