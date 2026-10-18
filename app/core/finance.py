"""Personal expense records and monthly budgets, kept only in the local database (no bank connection, no service).

You tell Jarvis what you spent ("I spent 450 taka on lunch"); it can total by month and category, compare with the
budgets you set, and warn when a category is close to its budget. Amounts are plain numbers in your currency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

CATEGORIES = {
    "food": ("food", "lunch", "dinner", "breakfast", "snack", "restaurant", "coffee", "tea", "grocer", "bazar", "bazaar"),
    "transport": ("transport", "bus", "uber", "pathao", "taxi", "rickshaw", "cng", "fuel", "petrol", "train", "metro"),
    "bills": ("bill", "electric", "internet", "wifi", "rent", "gas", "water", "mobile recharge", "recharge"),
    "shopping": ("shopping", "clothes", "shoes", "amazon", "daraz", "gadget"),
    "health": ("medicine", "doctor", "pharmacy", "hospital", "health"),
    "education": ("course", "book", "tuition", "class", "exam", "study"),
    "entertainment": ("movie", "game", "netflix", "spotify", "concert", "subscription"),
}


def guess_category(note: str) -> str:
    text = note.lower()
    for category, words in CATEGORIES.items():
        if any(w in text for w in words):
            return category
    return "other"


def clean_category(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", (value or "").lower()).strip()[:40]
    return value or "other"


def month_bounds(when: datetime) -> tuple[str, str]:
    start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class Report:
    month: str
    total: float
    by_category: dict[str, float]
    budgets: dict[str, float]
    count: int

    def warnings(self) -> list[str]:
        out = []
        for category, limit in sorted(self.budgets.items()):
            spent = self.by_category.get(category, 0.0)
            if spent >= limit:
                out.append(f"{category}: {spent:,.0f} of {limit:,.0f} - over budget")
            elif spent >= 0.8 * limit:
                out.append(f"{category}: {spent:,.0f} of {limit:,.0f} - {100 * spent / limit:.0f}% used")
        return out


class Finance:
    def __init__(self, database) -> None:
        self.db = database

    def add(self, amount: float, note: str = "", category: str = "", currency: str = "",
            when: datetime | None = None) -> int:
        if not 0 < amount < 1e12:
            raise ValueError("The amount must be a positive number.")
        category = clean_category(category) if category else guess_category(note)
        stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            cur = conn.execute("INSERT INTO expenses (ts, amount, currency, category, note) VALUES (?, ?, ?, ?, ?)",
                               (stamp, float(amount), currency[:8], category, note.strip()[:200]))
            return int(cur.lastrowid)

    def delete(self, expense_id: int) -> bool:
        with self.db.connect() as conn:
            return conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,)).rowcount > 0

    def recent(self, limit: int = 10) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, ts, amount, currency, category, note FROM expenses ORDER BY ts DESC, id DESC "
                                "LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_budget(self, category: str, monthly: float) -> None:
        if not 0 < monthly < 1e12:
            raise ValueError("The budget must be a positive number.")
        with self.db.connect() as conn:
            conn.execute("INSERT INTO budgets (category, monthly) VALUES (?, ?) ON CONFLICT(category) DO UPDATE SET "
                         "monthly = excluded.monthly", (clean_category(category), float(monthly)))

    def budgets(self) -> dict[str, float]:
        with self.db.connect() as conn:
            return {r["category"]: float(r["monthly"]) for r in conn.execute("SELECT category, monthly FROM budgets")}

    def report(self, when: datetime | None = None) -> Report:
        when = (when or datetime.now().astimezone())
        start, end = month_bounds(when)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT category, SUM(amount) AS total, COUNT(*) AS n FROM expenses WHERE ts >= ? AND "
                                "ts < ? GROUP BY category", (start, end)).fetchall()
        by_category = {r["category"]: float(r["total"]) for r in rows}
        return Report(when.strftime("%B %Y"), sum(by_category.values()), by_category, self.budgets(),
                      sum(int(r["n"]) for r in rows))

    def monthly_totals(self, months: int = 6, when: datetime | None = None) -> list[tuple[str, float]]:
        """[("May", 8200.0), ...] for the last ``months`` months, oldest first (for the chart)."""
        when = (when or datetime.now().astimezone())
        out = []
        cursor = when.replace(day=1)
        stamps = []
        for _ in range(max(1, min(months, 24))):
            stamps.append(cursor)
            cursor = (cursor.replace(year=cursor.year - 1, month=12) if cursor.month == 1
                      else cursor.replace(month=cursor.month - 1))
        with self.db.connect() as conn:
            for month in reversed(stamps):
                start, end = month_bounds(month)
                total = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE ts >= ? AND ts < ?",
                                     (start, end)).fetchone()[0]
                out.append((month.strftime("%b"), float(total)))
        return out

    def export_csv(self, path, when: datetime | None = None, *, all_months: bool = False) -> int:
        """Write expenses (this month, or all) as CSV; returns the row count. Cells that a spreadsheet would run as a
        formula (=, +, -, @) are prefixed with ' so an exported note can't execute anything."""
        import csv
        from pathlib import Path

        where, params = "", ()
        if not all_months:
            start, end = month_bounds(when or datetime.now().astimezone())
            where, params = "WHERE ts >= ? AND ts < ?", (start, end)
        with self.db.connect() as conn:
            rows = conn.execute(f"SELECT id, ts, amount, currency, category, note FROM expenses {where} ORDER BY ts",
                                params).fetchall()

        def safe(value) -> str:
            text = str(value)
            return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:      # BOM: Excel opens UTF-8 correctly
            writer = csv.writer(handle)
            writer.writerow(["id", "date", "amount", "currency", "category", "note"])
            for r in rows:
                local = datetime.fromisoformat(r["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
                writer.writerow([r["id"], local, f"{float(r['amount']):.2f}", safe(r["currency"]), safe(r["category"]),
                                 safe(r["note"])])
        return len(rows)
