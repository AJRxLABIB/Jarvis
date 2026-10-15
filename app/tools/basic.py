"""Always-available safe tools: current date/time and a calculator."""

from __future__ import annotations

import ast
import re
import math
import operator
from typing import Any

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult

# ------------------------------------------------------------------ date/time


def get_datetime(_: NoArgs, ctx: ToolContext) -> ToolResult:
    now = ctx.clock()
    offset = now.strftime("%z")
    utc_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local"
    return ToolResult.success(
        f"It is {now:%H:%M} on {now:%A}, {now.day} {now:%B %Y}.",
        time_24h=now.strftime("%H:%M"),
        time_12h=now.strftime("%I:%M %p").lstrip("0"),
        date=now.strftime("%Y-%m-%d"),
        weekday=now.strftime("%A"),
        timezone=now.tzname() or utc_offset,
        utc_offset=utc_offset,
    )


# ----------------------------------------------------------------- calculator

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max, "log": math.log,
    "log10": math.log10, "sin": math.sin, "cos": math.cos, "tan": math.tan, "floor": math.floor, "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_EXPONENT = 1000
_MAX_MAGNITUDE = 1e100


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic without eval(): only numbers, operators and a few math functions."""
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression)  # 1,000 -> 1000 (keeps f(a, b))
    text = text.replace("^", "**").replace("×", "*").replace("÷", "/")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ToolError("That is not a valid calculation.", "invalid_expression") from None

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name) and node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
                raise ToolError("That exponent is too large.", "too_large")
            value = _BINARY[type(node.op)](left, right)
            if isinstance(value, complex) or abs(value) > _MAX_MAGNITUDE:
                raise ToolError("The result is too large or not a real number.", "too_large")
            return value
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
            and not node.keywords and len(node.args) <= 4
        ):
            return _FUNCTIONS[node.func.id](*(ev(a) for a in node.args))
        raise ToolError("Only numbers, + - * / ** %, parentheses and basic math functions are allowed.", "invalid_expression")

    try:
        result = ev(tree)
    except ToolError:
        raise
    except ZeroDivisionError:
        raise ToolError("Division by zero.", "math_error") from None
    except (ValueError, OverflowError, TypeError) as exc:
        raise ToolError(f"That calculation is not possible: {exc}.", "math_error") from None
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        return int(result)
    return round(result, 10) if isinstance(result, float) else result


class CalculateArgs(ToolArgs):
    expression: str = Field(min_length=1, max_length=200, description="Arithmetic expression, e.g. '(12.5*4)/3' or 'sqrt(144)'")


def calculate(args: CalculateArgs, _: ToolContext) -> ToolResult:
    value = safe_eval(args.expression)
    return ToolResult.success(f"{args.expression} = {value}", expression=args.expression, result=value)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_datetime",
        description="Get the current local time, date and weekday. Use for any question about the time or date.",
        handler=get_datetime,
        level=RiskLevel.SAFE,
        category="basic",
    ),
    Tool(
        name="calculate",
        description="Calculate a math expression with numbers, e.g. '18/100*2450'. Only for arithmetic, never for other requests.",
        handler=calculate,
        args_model=CalculateArgs,
        level=RiskLevel.SAFE,
        category="basic",
        timeout=3.0,
    ),
)
