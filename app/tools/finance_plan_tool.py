"""``finance_plan``: import a spending CSV (preview first), recurring payments with reminders, savings targets, a month-end forecast and
what-if scenarios. Local only: no bank login, nothing is paid. Rules and calculations: app/core/finance_plan.py."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from app.core import finance_plan as fp
from app.core.finance import Finance
from app.memory.settings_store import SettingsStore
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class PlanArgs(ToolArgs):
    action: Literal["import_preview", "import", "add_recurring", "remove_recurring", "upcoming", "add_savings", "remove_savings", "savings",
                    "forecast", "scenario"] = Field(
        description="import_preview = read a CSV and show what would be added; import = add it (asks first; duplicates skipped); "
                    "add_recurring (name, amount, day) / remove_recurring / upcoming = payments due soon (reminders only); add_savings "
                    "(name, amount = target, by = YYYY-MM-DD) / savings = progress; forecast = month-end projection with assumptions; "
                    "scenario = what if (cut_category + cut_percent, extra_monthly)")
    path: str = Field(default="", max_length=400, description="For import: the CSV file")
    name: str = Field(default="", max_length=60, description="Name of the recurring payment or savings target")
    amount: float = Field(default=0, ge=0, lt=1e12, description="Payment amount, or the savings target")
    day: int = Field(default=1, ge=1, le=31, description="Day of the month a recurring payment is due")
    category: str = Field(default="", max_length=40, description="Category of a recurring payment (guessed if empty)")
    by: str = Field(default="", max_length=10, description="Savings deadline, YYYY-MM-DD")
    days: int = Field(default=14, ge=1, le=60, description="For upcoming: how many days ahead")
    cut_category: str = Field(default="", max_length=40, description="Category of a recurring payment (guessed if empty)")
    cut_percent: float = Field(default=0, ge=0, le=100, description="For scenario: how many percent less")
    extra_monthly: float = Field(default=0, ge=0, lt=1e12, description="For scenario: one more monthly payment of this amount")


def finance_plan(args: PlanArgs, ctx: ToolContext) -> ToolResult:
    if ctx.database is None:
        raise ToolError("Finance planning needs the local database.", "no_database")
    finance, store, today = Finance(ctx.database), SettingsStore(ctx.database), date.today()
    a = args.action
    try:
        if a in ("import_preview", "import"):
            if not args.path.strip():
                raise ToolError("Which CSV file?", "missing_path")
            from pathlib import Path

            path = Path(ctx.get_path_policy().resolve(args.path))
            if not path.is_file():
                raise ToolError(f"{args.path} is not a file.", "not_found")
            report = fp.import_csv(finance, path, commit=a == "import", currency=getattr(ctx.settings, "currency", "") or "")
            return ToolResult.success(report.describe(), imported=report.imported, duplicates=report.duplicates, skipped=len(report.skipped),
                                      dry_run=report.dry_run)
        if a == "add_recurring":
            item = fp.add_recurring(store, args.name, args.amount, args.day, args.category)
            return ToolResult.success(f"Recurring: {item['name']} {item['amount']:,.0f} on day {item['day']} ({item['category']}). You get a reminder; nothing is paid.")
        if a == "remove_recurring":
            return ToolResult.success("Removed." if fp.remove_item(store, "recurring", args.name) else "No such recurring payment.")
        if a == "upcoming":
            items = fp.upcoming(store, today, args.days)
            return ToolResult.success("; ".join(f"{d:%d %b}: {i['name']} {i['amount']:,.0f}" for d, i in items) or f"Nothing due in the next {args.days} days.",
                                      due=len(items))
        if a == "add_savings":
            item = fp.add_savings(store, args.name, args.amount, args.by)
            return ToolResult.success(f"Savings target '{item['name']}': {item['target']:,.0f}" + (f" by {item['by']}" if item["by"] else "") + ".")
        if a == "remove_savings":
            return ToolResult.success("Removed." if fp.remove_item(store, "savings", args.name) else "No such savings target.")
        if a == "savings":
            rows = fp.savings_status(store, today)
            return ToolResult.success("; ".join(f"{r['name']}: {r['saved']:,.0f}/{r['target']:,.0f} ({r['percent']}%)"
                                                + (f", {r['per_month_needed']:,.0f}/month needed for {r['months_left']} month(s)" if "per_month_needed" in r else "")
                                                for r in rows) or "No savings targets yet.", targets=len(rows))
        if a == "forecast":
            f = fp.forecast(finance, store, today)
            return ToolResult.success(f.describe(), projected=round(f.projected_total, 2), spent=round(f.spent_so_far, 2))
        cut = {args.cut_category.strip().lower(): args.cut_percent} if args.cut_category.strip() and args.cut_percent else {}
        return ToolResult.success(fp.scenario(finance, store, today, cut=cut, extra_monthly=args.extra_monthly))
    except ValueError as exc:
        raise ToolError(str(exc), "bad_input") from exc


def _risk(args: PlanArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action == "import" else RiskLevel.SAFE


def _question(args: PlanArgs, _ctx: ToolContext) -> str:
    return f"Add the expenses from {args.path} to your records? (Rows already recorded are skipped; credits are ignored.)"


TOOLS: tuple[Tool, ...] = (
    Tool("finance_plan", "Money planning, all local: preview/import a spending CSV, recurring payments with reminders, savings targets, a month-end "
         "forecast with its assumptions, and what-if scenarios. No bank access; nothing is ever paid.", finance_plan, PlanArgs, RiskLevel.SAFE,
         "finance", timeout=30, risk=_risk, confirmation=_question),
)
