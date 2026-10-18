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
