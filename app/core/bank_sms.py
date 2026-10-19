"""Expenses from your bank / mobile-money SMS - read on YOUR phone, parsed on this PC, nothing sent anywhere.

"Tk 1,250.00 debited from A/C ...1234 at DARAZ" / "You have paid Tk 500 to 01712..." (bKash) / "BDT 300 sent to ..."
-> (amount, what for). Only money going OUT becomes an expense (credits / cash-in / OTPs are skipped). Each message is
remembered by a short fingerprint, so importing again never adds it twice. Plain patterns, no AI: what isn't clearly
a payment is left out and listed as skipped, never guessed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_AMOUNT = r"(?:tk\.?|bdt|taka|৳|rs\.?|inr|\$|usd)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)|(?P<amount2>\d[\d,]*(?:\.\d{1,2})?)\s*(?:tk|bdt|taka|৳|টাকা)"
_OUT = re.compile(r"\b(?:debited|debit|spent|purchase[d]?|paid|payment|sent|send\s+money|withdrawn|withdrawal|"
                  r"cash\s*out|transferred|bill\s+pay|charged|pos\s+txn|খরচ|পরিশোধ|পাঠানো|ক্যাশ\s*আউট)\b", re.I)
_IN = re.compile(r"\b(?:credited|received|cash\s*in|deposit(?:ed)?|refund(?:ed)?|salary|added\s+money|জমা|পেয়েছেন)\b", re.I)
_OTP = re.compile(r"\b(?:otp|one[- ]time|verification\s+code|pin\s+is|password)\b", re.I)
_WHERE = re.compile(r"\b(?:at|to|for|merchant[:\s]+|@)\s+(?P<who>[A-Za-z][\w&.\- ]{1,40}?)(?=\s+(?:on|at|ref|txn|trx|bal|avl|"
                    r"via|from|is|was|\d)|[.,;]|$)", re.I)


@dataclass(frozen=True)
class Payment:
    amount: float
    note: str
    fingerprint: str
    text: str


def fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:12]


def parse(text: str) -> Payment | None:
    """One SMS -> a Payment when it's clearly money going out, else None."""
    if not text:
        return None
    clean = " ".join(text.translate(_BN_DIGITS).split())
    if _OTP.search(clean) or _IN.search(clean) and not _OUT.search(clean):
        return None
    if not _OUT.search(clean):
        return None
    match = re.search(_AMOUNT, clean, re.I)
    if not match:
        return None
    raw = (match.group("amount") or match.group("amount2") or "").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None
    if not 0 < amount < 1e9:
        return None
    where = _WHERE.search(clean[match.end():]) or _WHERE.search(clean)
    who = where.group("who").strip(" .") if where else ""
    if re.fullmatch(r"[\d\s+-]+", who or ""):
        who = ""
    note = f"SMS: {who}" if who else "SMS payment"
    return Payment(amount, note[:80], fingerprint(clean), clean[:200])


def split_phone_messages(result: str) -> list[tuple[str, str]]:
    """The phone's read_messages answer 'Bank (1 Jan 09:00): text | bKash (...): text' -> [(sender, text)]."""
    out = []
    for part in (result or "").split(" | "):
        match = re.match(r"^(?P<who>.{1,60}?) \((?P<when>[^)]{1,30})\): (?P<body>.+)$", part.strip())
        if match:
            out.append((match.group("who"), match.group("body")))
    return out


def already_imported(database, prints: list[str]) -> set[str]:
    if not prints:
        return set()
    with database.connect() as conn:
        rows = conn.execute("SELECT note FROM expenses WHERE note LIKE '%[sms:%'").fetchall()
    found = set()
    for row in rows:
        m = re.search(r"\[sms:([0-9a-f]{12})\]", row["note"] or "")
        if m:
            found.add(m.group(1))
    return found & set(prints)
