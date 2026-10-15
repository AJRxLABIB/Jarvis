"""Fix the small argument mistakes local models make, for SAFE tools only.

"sort_by": "extension" when the choices are name / modified / size, "count": "five", an
extra field nobody asked for. Instead of failing (and the model often repeating the same
mistake), an invalid choice is mapped to the closest allowed one when it is clearly close,
and otherwise an optional field falls back to its default. Tools that change or remove
things (level 1+) are never repaired: there a wrong guess would act on the wrong target.
"""

from __future__ import annotations

import typing
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, ValidationError

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "twenty": 20, "fifty": 50, "hundred": 100}


# Only these fall back to the default. An out-of-range number ("volume 150") was said on purpose:
# it is refused with the reason, never silently replaced.
_DEFAULTABLE = frozenset({"literal_error", "enum", "int_parsing", "float_parsing", "bool_parsing", "int_type",
                          "float_type", "bool_type"})


def _choices(model: type[BaseModel], field: str) -> list[str]:
    info = model.model_fields.get(field)
    if info is None:
        return []
    annotation = info.annotation
    options: list[Any] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if typing.get_origin(current) is typing.Literal:
            options += list(typing.get_args(current))
        else:
            stack += [a for a in typing.get_args(current) if a is not type(None)]
    return [o for o in options if isinstance(o, str)]


def repair(model: type[BaseModel], arguments: dict[str, Any], error: ValidationError) -> tuple[dict, list[str]] | None:
    """(fixed arguments, what was changed) if every problem could be fixed, else None."""
    fixed = dict(arguments)
    changes: list[str] = []
    for issue in error.errors():
        loc = issue.get("loc") or ()
        if len(loc) != 1 or not isinstance(loc[0], str):
            return None  # nested problems: leave them to the model
        field, kind, value = loc[0], issue.get("type", ""), fixed.get(loc[0])
        info = model.model_fields.get(field)
        if kind == "extra_forbidden":
            fixed.pop(field, None)
            changes.append(f"dropped {field}")
            continue
        if info is None:
            return None
        if kind in ("literal_error", "enum") and isinstance(value, str):
            options = _choices(model, field)
            lowered = {o.lower(): o for o in options}
            close = get_close_matches(value.strip().lower().replace(" ", "_"), list(lowered), n=1, cutoff=0.75)
            if close:
                fixed[field] = lowered[close[0]]
                changes.append(f"{field}: {value!r} -> {fixed[field]!r}")
                continue
        if kind in ("int_parsing", "int_type", "float_parsing") and isinstance(value, str):
            number = _NUMBERS.get(value.strip().lower())
            if number is not None:
                fixed[field] = number
                changes.append(f"{field}: {value!r} -> {number}")
                continue
        if not info.is_required() and kind in _DEFAULTABLE:  # a wrong choice / unreadable value: the default
            fixed.pop(field, None)
            changes.append(f"{field}: {value!r} -> default")
            continue
        return None
    try:
        model.model_validate(fixed)
    except ValidationError:
        return None
    return fixed, changes
