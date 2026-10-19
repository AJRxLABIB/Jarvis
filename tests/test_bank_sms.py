"""Expenses from bank / bKash / Nagad SMS: only money going out, never OTPs or money coming in, never twice."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core import bank_sms
from app.memory.database import Database
from app.remote.phone_link import PhoneLink
from tests.test_phone_link import _ctx, _phone


@pytest.mark.parametrize("text,amount,note", [
    ("Tk 1,250.00 debited from A/C ...1234 at DARAZ on 12-Oct. Avl Bal Tk 5,000", 1250.0, "SMS: DARAZ"),
    ("You have paid Tk 500.00 to Rahim Store. Fee Tk 0. Balance Tk 1200.", 500.0, "SMS: Rahim Store"),
    ("BDT 89.50 spent on card ending 1111 at FOODPANDA", 89.5, "SMS: FOODPANDA"),
    ("Send Money Tk 300 to 01712345678 successful. Fee Tk 5.", 300.0, "SMS payment"),
    ("আপনার একাউন্ট থেকে ৫০০ টাকা খরচ হয়েছে", 500.0, "SMS payment"),
])
def test_payments(text, amount, note):
    p = bank_sms.parse(text)
    assert p is not None and p.amount == amount and p.note == note


@pytest.mark.parametrize("text", ["Cash In Tk 2,000.00 from 017xxxxxxxx successful.", "Your OTP is 482913.",
                                  "Tk 5,000 credited to your account. Salary Oct.", "Hello, how are you?", ""])
def test_not_payments(text):
    assert bank_sms.parse(text) is None


def test_split_phone_answer():
    out = bank_sms.split_phone_messages("bKash (1 Oct 09:00): You have paid Tk 50 to X. | Ammu (2 Oct 10:00): hi")
    assert out == [("bKash", "You have paid Tk 50 to X."), ("Ammu", "hi")]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "x.db")
    database.initialize()
    return database


def test_preview_then_import_once(db, tmp_path, monkeypatch):
    from app.tools.finance_tool import BankSmsArgs, import_bank_sms

    _phone(db)
    answer = ("bKash (1 Oct 09:00): You have paid Tk 500.00 to Rahim Store. | Bank (1 Oct 10:00): Your OTP is •••• | "
              "Bank (2 Oct 11:00): BDT 89.50 spent on card at FOODPANDA")

    def phone_answers(self, command_id, seconds=8.0, sleep=None):
        self.collect("phone-0001", {})
        self.report("phone-0001", command_id, True, answer)
        return self.outcome(command_id)

    monkeypatch.setattr(PhoneLink, "wait", phone_answers)
    ctx = _ctx(db, tmp_path)
    preview = asyncio.run(import_bank_sms(BankSmsArgs(), ctx))
    assert preview.data["new"] == 2 and preview.data["skipped"] == 1 and "import them" in preview.message
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0          # preview adds nothing
    done = asyncio.run(import_bank_sms(BankSmsArgs(action="import"), ctx))
    assert done.data["added"] == 2
    again = asyncio.run(import_bank_sms(BankSmsArgs(action="import"), ctx))
    assert again.data["new"] == 0 and "2 already added" in again.message                 # never twice
