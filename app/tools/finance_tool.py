"""The ``expenses`` tool: record what you spent, see this month's totals and budgets (all local)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.finance import Finance
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class ExpenseArgs(ToolArgs):
    action: Literal["add", "list", "report", "budget", "budgets", "delete", "export", "trend"] = Field(
        description="add an expense, list recent ones, report this month, set a budget, show budgets, delete one, "
                    "export to a CSV file, trend = totals of the last 6 months")
    amount: float | None = Field(default=None, gt=0, lt=1e12, description="Amount (add) or monthly budget (budget)")
    note: str = Field(default="", max_length=200, description="What it was for, e.g. 'lunch'")
    category: str = Field(default="", max_length=40, description="Optional: food, transport, bills ... (guessed if empty)")
    expense_id: int | None = Field(default=None, ge=1, description="For delete")
    all_months: bool = Field(default=False, description="export: every month instead of only this one")


def expenses(args: ExpenseArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Expense records need the local database.", "no_database")
    finance = Finance(ctx.database)
    currency = getattr(ctx.settings, "currency", "") or ""
    if args.action == "add":
        if args.amount is None:
            raise ToolError("How much was it?", "missing_amount")
        new_id = finance.add(args.amount, args.note, args.category, currency)
        report = finance.report()
        warn = report.warnings()
        return ToolResult.success(f"Noted {args.amount:,.2f} for {args.note or 'that'} (#{new_id})."
                                  + (" " + warn[0] + "." if warn else ""), id=new_id, warnings=warn)
    if args.action == "list":
        rows = finance.recent()
        if not rows:
            return ToolResult.success("No expenses recorded yet.", expenses=[])
        text = "; ".join(f"#{r['id']} {r['amount']:,.0f} {r['category']} ({r['note'] or '-'})" for r in rows)
        return ToolResult.success(f"Recent expenses: {text}.", expenses=rows)
    if args.action == "report":
        report = finance.report()
        if not report.count:
            return ToolResult.success(f"Nothing recorded for {report.month} yet.", total=0)
        parts = ", ".join(f"{c} {v:,.0f}" for c, v in sorted(report.by_category.items(), key=lambda kv: -kv[1]))
        warn = report.warnings()
        return ToolResult.success(f"{report.month}: {report.total:,.0f} in {report.count} expense(s) - {parts}."
                                  + (" Budgets: " + "; ".join(warn) + "." if warn else ""),
                                  total=report.total, by_category=report.by_category, warnings=warn)
    if args.action == "budget":
        if args.amount is None or not args.category:
            raise ToolError("Which category, and how much per month?", "missing_budget")
        finance.set_budget(args.category, args.amount)
        return ToolResult.success(f"Monthly budget for {args.category}: {args.amount:,.0f}.")
    if args.action == "budgets":
        budgets = finance.budgets()
        return ToolResult.success(("Budgets: " + ", ".join(f"{c} {v:,.0f}" for c, v in sorted(budgets.items())) + ".")
                                  if budgets else "No budgets set.", budgets=budgets)
    if args.action == "trend":
        months = finance.monthly_totals(6)
        if not any(v for _, v in months):
            return ToolResult.success("No expenses recorded in the last 6 months.", months=months)
        return ToolResult.success("Last 6 months: " + ", ".join(f"{m} {v:,.0f}" for m, v in months) + ".",
                                  months=months)
    if args.action == "export":
        from pathlib import Path

        base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
        path = base / "exports" / f"expenses-{'all' if args.all_months else f'{ctx.clock():%Y-%m}'}-{ctx.clock():%H%M%S}.csv"
        count = finance.export_csv(path, all_months=args.all_months)
        if not path.is_file():
            raise ToolError("The export file wasn't written.", "export_failed")
        return ToolResult.success(f"Exported {count} expense(s) to {path.name} in the exports folder.", path=str(path),
                                  count=count)
    if args.expense_id is None:
        raise ToolError("Which expense number?", "missing_id")
    if not finance.delete(args.expense_id):
        raise ToolError(f"There is no expense #{args.expense_id}.", "not_found")
    return ToolResult.success(f"Deleted expense #{args.expense_id}.")


def _risk(args: ExpenseArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "delete" else RiskLevel.SAFE


TOOLS = (
    Tool("expenses", "Personal expense records and monthly budgets, kept on this PC: add what was spent, list, this "
         "month's report by category, 6-month trend, set or show budgets, delete a record, export to CSV.", expenses, ExpenseArgs, RiskLevel.SAFE,
         "finance", risk=_risk),
)


# ------------------------------------------------------------------ expenses from bank / mobile-money SMS

class BankSmsArgs(ToolArgs):
    action: Literal["preview", "import"] = Field(
        default="preview", description="preview = show what would be added; import = add them as expenses")
    sender: str = Field(default="", max_length=60, description="Only messages from this sender, e.g. 'bKash' or a bank")
    count: int = Field(default=10, ge=1, le=10, description="How many of the latest SMS to look at")


async def import_bank_sms(args: BankSmsArgs, ctx: ToolContext) -> ToolResult:
    """Reads the latest SMS on your paired phone (JARVIS Mobile, 'Allow calls and messages'), finds payments and
    adds them as expenses - never the same message twice, nothing guessed."""
    import asyncio

    from app.core import bank_sms
    from app.tools.phone_link_tool import _phone

    if ctx.database is None:
        raise ToolError("Expense records need the local database.", "no_database")
    device, link = _phone(ctx, "")
    command_id = link.queue(device.id, "read_messages", {"count": args.count, "from": args.sender})
    outcome = await asyncio.to_thread(link.wait, command_id, 15.0)
    if outcome["status"] != "done":
        why = outcome["result"] or "open JARVIS Mobile or turn on Stay connected"
        raise ToolError(f"{device.name} didn't send its messages ({outcome['status']}: {why}).", "phone_failed")
    messages = bank_sms.split_phone_messages(outcome["result"] or "")
    found = [(who, bank_sms.parse(text)) for who, text in messages]
    payments = [(who, p) for who, p in found if p is not None]
    seen = bank_sms.already_imported(ctx.database, [p.fingerprint for _, p in payments])
    new = [(who, p) for who, p in payments if p.fingerprint not in seen]
    skipped = len(messages) - len(payments)
    lines = [f"{p.amount:,.2f} - {p.note.removeprefix('SMS: ')} (from {who})" for who, p in new]
    if args.action == "preview" or not new:
        head = (f"Found {len(new)} new payment(s) in {len(messages)} SMS" + (f"; {len(seen)} already added" if seen else "")
                + (f"; {skipped} weren't payments" if skipped else "") + ".")
        return ToolResult.success(head + (" " + "; ".join(lines) + ". Say 'import them' to add." if lines and
                                          args.action == "preview" else ""), new=len(new), skipped=skipped)
    finance = Finance(ctx.database)
    currency = getattr(ctx.settings, "currency", "") or ""
    for _who, p in new:
        finance.add(p.amount, f"{p.note} [sms:{p.fingerprint}]", "", currency)
    return ToolResult.success(f"Added {len(new)} expense(s) from your SMS: " + "; ".join(lines) + ".", added=len(new))


def _sms_risk(args: BankSmsArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE


TOOLS = TOOLS + (
    Tool("import_bank_sms", "Expenses from the bank / bKash / Nagad SMS on the user's phone: preview the payments "
         "found in the latest messages, or import them (never the same message twice).", import_bank_sms, BankSmsArgs,
         RiskLevel.MODERATE, "finance", timeout=25, risk=_sms_risk,
         confirmation=lambda a, _: ("Read your latest SMS on the phone and add the payments as expenses?" if a.action ==
                                    "import" else "Read your latest SMS on the phone to look for payments?")),
)
