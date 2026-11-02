"""Personal finance planning on top of the existing expenses / budgets (app/core/finance.py): CSV import, recurring payments with
reminders, savings targets, a month-end forecast and what-if scenarios. All local; no bank access, no credentials, nothing is ever paid.

Every number comes with its assumptions and the figures it was calculated from, so it can be checked by hand. Forecasts are estimates and
say so. Plan items live in the settings store (no new tables).
"""

from __future__ import annotations

import calendar
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.finance import Finance, clean_category, guess_category

KEY = "finance_plan"
MAX_ITEMS = 60
MAX_CSV_BYTES = 5_000_000
MAX_CSV_ROWS = 20_000
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d %b %Y", "%b %d, %Y")
_HEAD = {"date": ("date", "transaction date", "posted", "time", "day"),
         "amount": ("amount", "debit", "value", "withdrawal", "expense", "sum", "total", "price"),
         "note": ("description", "note", "details", "memo", "narration", "payee", "merchant", "name", "reference"),
         "category": ("category", "type", "tag")}


# ---------------------------------------------------------------------------------------------------- CSV import
@dataclass
class ImportReport:
    rows: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: list[str] = field(default_factory=list)       # "line 7: reason"
    preview: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True

    def describe(self) -> str:
        head = (f"{'Preview' if self.dry_run else 'Imported'}: {self.imported} expense(s) {'would be added' if self.dry_run else 'added'}, "
                f"{self.duplicates} already recorded, {len(self.skipped)} skipped, of {self.rows} row(s).")
        shown = "; ".join(f"{p['date']} {p['amount']:,.2f} {p['category']} ({p['note'][:30]})" for p in self.preview[:5])
        return head + (f" First: {shown}." if shown else "") + (" Skipped: " + "; ".join(self.skipped[:4]) + "." if self.skipped else "")


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:20] if fmt != "%b %d, %Y" else text, fmt).replace(tzinfo=timezone.utc, hour=12)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc) if "T" in text else None
    except ValueError:
        return None


def _parse_amount(text: str) -> float | None:
    t = re.sub(r"[^\d.,()\-]", "", text or "")
    negative = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("()-")
    if "," in t and "." in t:
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", "") if re.search(r",\d{3}(?!\d)", t) else t.replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def _column(header: list[str], kind: str) -> int | None:
    names = [h.strip().lower() for h in header]
    for wanted in _HEAD[kind]:
        if wanted in names:
            return names.index(wanted)
    return None


def import_csv(finance: Finance, path: Path | str, *, commit: bool = False, currency: str = "", positive_is_expense: bool | None = None,
               opener: Any = None) -> ImportReport:
    """Read a bank / app CSV of spending. ``commit=False`` only previews. Rows already recorded (same day, amount and note) are skipped,
    so importing the same file twice adds nothing. Money coming in (credits) is ignored - this tracks spending."""
    report = ImportReport(dry_run=not commit)
    p = Path(path)
    raw = (opener or (lambda q: q.read_bytes()))(p)
    if len(raw) > MAX_CSV_BYTES:
        report.skipped.append("file is larger than 5 MB")
        return report
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if len(rows) < 2:
        report.skipped.append("no data rows")
        return report
    header = rows[0]
    di, ai, ni, ci = (_column(header, k) for k in ("date", "amount", "note", "category"))
    if di is None or ai is None:
        report.skipped.append("could not find a date and an amount column (headers: " + ", ".join(h[:15] for h in header[:8]) + ")")
        return report
    if positive_is_expense is None:                        # a bank file that has negative numbers uses them for spending
        sample = [_parse_amount(r[ai]) for r in rows[1:200] if len(r) > ai]
        positive_is_expense = not any(v is not None and v < 0 for v in sample)
    from collections import Counter

    existing = Counter((r["ts"][:10], round(r["amount"], 2), r["note"]) for r in finance.recent(5000))   # rows already recorded, counted
    for n, row in enumerate(rows[1:MAX_CSV_ROWS + 1], start=2):
        report.rows += 1
        if len(row) <= max(di, ai):
            report.skipped.append(f"line {n}: too few columns")
            continue
        when, amount = _parse_date(row[di]), _parse_amount(row[ai])
        if when is None or amount is None:
            report.skipped.append(f"line {n}: unreadable {'date' if when is None else 'amount'}")
            continue
        spend = amount if positive_is_expense else -amount
        if spend <= 0:
            continue                                        # a credit / refund: not spending
        note = (row[ni].strip() if ni is not None and ni < len(row) else "")[:200]
        category = clean_category(row[ci]) if ci is not None and ci < len(row) and row[ci].strip() else guess_category(note)
        key = (when.astimezone(timezone.utc).isoformat(timespec="seconds")[:10], round(spend, 2), note)
        if existing[key] > 0:                                # one recorded row covers one file row: two real identical purchases both import
            existing[key] -= 1
            report.duplicates += 1
            continue
        report.imported += 1
        if len(report.preview) < 8:
            report.preview.append({"date": key[0], "amount": spend, "category": category, "note": note})
        if commit:
            finance.add(spend, note, category, currency, when)
    return report


# ---------------------------------------------------------------------------------------------------- plan items
def _load(store: Any) -> dict[str, list[dict[str, Any]]]:
    data = store.get(KEY, {})
    data = data if isinstance(data, dict) else {}
    return {"recurring": list(data.get("recurring", [])), "savings": list(data.get("savings", []))}


def _save(store: Any, data: dict[str, list[dict[str, Any]]]) -> None:
    store.set(KEY, data)


def add_recurring(store: Any, name: str, amount: float, day: int, category: str = "") -> dict[str, Any]:
    if not name.strip() or not 0 < amount < 1e12 or not 1 <= day <= 31:
        raise ValueError("A recurring payment needs a name, a positive amount and a day of the month (1-31).")
    data = _load(store)
    if len(data["recurring"]) >= MAX_ITEMS:
        raise ValueError("Too many recurring payments; remove one first.")
    data["recurring"] = [r for r in data["recurring"] if r["name"].lower() != name.strip().lower()]
    item = {"name": name.strip()[:60], "amount": float(amount), "day": int(day), "category": clean_category(category) if category else guess_category(name)}
    data["recurring"].append(item)
    _save(store, data)
    return item


def remove_item(store: Any, kind: str, name: str) -> bool:
    data = _load(store)
    before = len(data[kind])
    data[kind] = [r for r in data[kind] if r["name"].lower() != name.strip().lower()]
    _save(store, data)
    return len(data[kind]) < before


def add_savings(store: Any, name: str, target: float, by: str = "", saved: float = 0.0) -> dict[str, Any]:
    if not name.strip() or not 0 < target < 1e12 or saved < 0:
        raise ValueError("A savings target needs a name and a positive target.")
    if by:
        date.fromisoformat(by)
    data = _load(store)
    data["savings"] = [s for s in data["savings"] if s["name"].lower() != name.strip().lower()]
    item = {"name": name.strip()[:60], "target": float(target), "saved": float(saved), "by": by}
    data["savings"].append(item)
    _save(store, data)
    return item


def due_date(item: dict[str, Any], year: int, month: int) -> date:
    return date(year, month, min(item["day"], calendar.monthrange(year, month)[1]))


def upcoming(store: Any, today: date, days: int = 14) -> list[tuple[date, dict[str, Any]]]:
    """Recurring payments falling due in the next ``days`` days, soonest first. Reminders only - nothing is paid."""
    out = []
    for item in _load(store)["recurring"]:
        for y, m in {(today.year, today.month), ((today + timedelta(days=days)).year, (today + timedelta(days=days)).month)}:
            d = due_date(item, y, m)
            if today <= d <= today + timedelta(days=days):
                out.append((d, item))
    return sorted(out, key=lambda t: t[0])


def savings_status(store: Any, today: date) -> list[dict[str, Any]]:
    out = []
    for s in _load(store)["savings"]:
        left = max(0.0, s["target"] - s["saved"])
        row = {"name": s["name"], "target": s["target"], "saved": s["saved"], "left": left, "percent": round(100 * s["saved"] / s["target"], 1)}
        if s.get("by"):
            months = max(1, (date.fromisoformat(s["by"]).year - today.year) * 12 + date.fromisoformat(s["by"]).month - today.month)
            row["per_month_needed"] = round(left / months, 2)
            row["months_left"] = months
        out.append(row)
    return out


# ---------------------------------------------------------------------------------------------------- forecast + scenarios
@dataclass
class Forecast:
    month: str
    spent_so_far: float
    projected_total: float
    variable_so_far: float
    daily_pace: float
    recurring_remaining: float
    budget_total: float
    lines: list[str]
    assumptions: list[str]

    def describe(self) -> str:
        return "\n".join(self.lines + ["Assumptions: " + "; ".join(self.assumptions) + ". This is an estimate, not a promise."])


def forecast(finance: Finance, store: Any, today: date, *, adjust: dict[str, float] | None = None, extra_recurring: float = 0.0) -> Forecast:
    """Month-end projection = spent so far + (days left x your daily pace on non-recurring spending) + recurring payments not yet due.
    ``adjust`` = {category: multiplier} scales the pace of that category (0.8 = spend 20% less); used by scenarios."""
    when = datetime(today.year, today.month, today.day, 12, tzinfo=timezone.utc)
    report = finance.report(when)
    days_in = calendar.monthrange(today.year, today.month)[1]
    elapsed, left = today.day, days_in - today.day
    recurring = _load(store)["recurring"]
    paid_names = {r["note"].lower() for r in finance.recent(500)}
    rec_done = sum(r["amount"] for r in recurring if due_date(r, today.year, today.month) <= today)
    rec_left = sum(r["amount"] for r in recurring if due_date(r, today.year, today.month) > today) + extra_recurring
    rec_cats = {r["category"] for r in recurring}
    by_cat = dict(report.by_category)
    variable = {c: v for c, v in by_cat.items() if c not in rec_cats} or by_cat
    pace_by_cat = {c: v / max(1, elapsed) for c, v in variable.items()}
    for c, mult in (adjust or {}).items():
        if c in pace_by_cat:
            pace_by_cat[c] *= mult
    pace = sum(pace_by_cat.values())
    projected = report.total + pace * left + rec_left
    budget_total = sum(report.budgets.values())
    lines = [f"{report.month}: spent {report.total:,.0f} so far ({report.count} expense(s), day {elapsed} of {days_in}).",
             f"Daily pace on non-recurring spending: {pace:,.0f}/day x {left} day(s) left = {pace * left:,.0f}.",
             f"Recurring payments still due this month: {rec_left:,.0f}.",
             f"Projected month total: {projected:,.0f}."]
    if budget_total:
        lines.append(f"Budgets add up to {budget_total:,.0f}: projected {'over' if projected > budget_total else 'under'} by {abs(projected - budget_total):,.0f}.")
    assumptions = ["your pace so far continues", "recurring payments are paid on their day", "no one-off purchases beyond what is recorded"]
    if elapsed < 5:
        assumptions.append("only a few days of data, so the pace is very uncertain")
    _ = paid_names, rec_done
    return Forecast(report.month, report.total, projected, sum(variable.values()), pace, rec_left, budget_total, lines, assumptions)


def scenario(finance: Finance, store: Any, today: date, *, cut: dict[str, float] | None = None, extra_monthly: float = 0.0) -> str:
    """Compare the baseline forecast with 'spend X% less on a category' and/or 'one more monthly payment'."""
    base = forecast(finance, store, today)
    mult = {c: max(0.0, 1 - pct / 100) for c, pct in (cut or {}).items()}
    alt = forecast(finance, store, today, adjust=mult, extra_recurring=extra_monthly)
    diff = alt.projected_total - base.projected_total
    what = [f"{c} spending {pct:g}% lower" for c, pct in (cut or {}).items()] + ([f"a new {extra_monthly:,.0f} monthly payment"] if extra_monthly else [])
    return (f"Baseline projected month total {base.projected_total:,.0f}. With {' and '.join(what) or 'no change'}: {alt.projected_total:,.0f} "
            f"({'+' if diff >= 0 else '-'}{abs(diff):,.0f}). Both use the same assumptions: " + "; ".join(base.assumptions) + ".")
