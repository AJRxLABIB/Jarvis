"""When an automation runs. All times are local time."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import ToolError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = {name: i for i, name in enumerate(WEEKDAYS)} | {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "tues": 1, "wednes": 2, "thur": 3, "thurs": 3,
}
_TIME = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.I)


def parse_time(text: str) -> str:
    """'11 PM' -> '23:00', '7:05am' -> '07:05', '18:30' -> '18:30', 'noon' -> '12:00'."""
    value = text.strip().lower().replace("o'clock", "").strip()
    if value in ("noon", "midday"):
        return "12:00"
    if value == "midnight":
        return "00:00"
    match = _TIME.match(value)
    if not match:
        raise ToolError(f"'{text}' is not a time I understand. Use e.g. '8:00', '7:30 am' or '23:15'.", "invalid_time")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ToolError(f"'{text}' is not a valid time.", "invalid_time")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ToolError(f"'{text}' is not a valid time.", "invalid_time")
    return f"{hour:02d}:{minute:02d}"


def parse_days(text: str | list[str] | None) -> list[int]:
    """'weekdays' / 'mon,wed,fri' / ['monday'] / 'every day' -> weekday numbers (Mon = 0)."""
    if text is None:
        return list(range(7))
    parts = text if isinstance(text, list) else re.split(r"[,\s/]+|\band\b", text.lower())
    days: set[int] = set()
    for part in (p.strip().lower() for p in parts):
        if not part or part in ("every", "day", "days", "on", "each"):
            continue
        if part in ("daily", "everyday", "all"):
            days.update(range(7))
        elif part in ("weekday", "weekdays", "workdays", "workday"):
            days.update(range(5))
        elif part in ("weekend", "weekends"):
            days.update((5, 6))
        elif part.rstrip("s") in _DAY_NAMES or part in _DAY_NAMES:
            days.add(_DAY_NAMES.get(part, _DAY_NAMES.get(part.rstrip("s"), 0)))
        else:
            raise ToolError(f"'{part}' is not a day I understand.", "invalid_days")
    return sorted(days) if days else list(range(7))


def split_watch(value: str) -> tuple[str, str]:
    """'C:\\Users\\me\\Downloads|*.pdf' -> (folder, pattern); the pattern defaults to '*'."""
    folder, _, pattern = value.partition("|")
    return folder.strip(), (pattern.strip() or "*")


class _Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DailyTrigger(_Trigger):
    type: Literal["daily"] = "daily"
    time: str
    days: list[int] = Field(default_factory=lambda: list(range(7)))

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        return parse_time(value)

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        if not value or any(d not in range(7) for d in value):
            raise ValueError("days must be weekday numbers 0 (Mon) - 6 (Sun)")
        return sorted(set(value))

    def next_run(self, after: datetime) -> datetime:
        hour, minute = map(int, self.time.split(":"))
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for _ in range(8):
            if candidate > after and candidate.weekday() in self.days:
                return candidate
            candidate += timedelta(days=1)
        raise AssertionError("unreachable")

    def describe(self) -> str:
        if self.days == list(range(7)):
            when = "every day"
        elif self.days == list(range(5)):
            when = "on weekdays"
        elif self.days == [5, 6]:
            when = "on weekends"
        else:
            when = "on " + ", ".join(WEEKDAYS[d].capitalize() for d in self.days)
        return f"{when} at {self.time}"


class OnceTrigger(_Trigger):
    type: Literal["once"] = "once"
    at: datetime

    def next_run(self, after: datetime) -> datetime | None:
        return self.at if self.at > after else None

    def describe(self) -> str:
        return f"once at {self.at:%Y-%m-%d %H:%M}"


class IntervalTrigger(_Trigger):
    type: Literal["interval"] = "interval"
    minutes: int = Field(ge=5, le=7 * 24 * 60)

    def next_run(self, after: datetime) -> datetime:
        return after.replace(second=0, microsecond=0) + timedelta(minutes=self.minutes)

    def describe(self) -> str:
        return f"every {self.minutes} minutes" if self.minutes % 60 else f"every {self.minutes // 60} hour(s)"


class BatteryTrigger(_Trigger):
    type: Literal["battery_below"] = "battery_below"
    percent: int = Field(ge=1, le=99)

    def next_run(self, after: datetime) -> None:
        return None  # checked by the scheduler, not time-based

    def describe(self) -> str:
        return f"when the battery drops below {self.percent}% (while unplugged)"


def phone_percent(value: str) -> int:
    try:
        return max(1, min(int(str(value).strip().rstrip("%") or 20), 99))
    except ValueError:
        return 20


class EventTrigger(_Trigger):
    """Something that happens: joining a Wi-Fi network, opening an app, plugging in a USB drive."""
    type: Literal["event"] = "event"
    event: Literal["wifi", "app_opened", "usb", "file_changed", "phone_charging", "phone_battery_low"]
    # Wi-Fi name, app name ("" = any), folder[|*.pdf] for files, "started"/"stopped" ("" = either) for the phone's
    # charger, or a percent (default 20) for the phone's battery
    value: str = Field(default="", max_length=260)

    def next_run(self, after: datetime) -> None:
        return None  # checked by the scheduler, not time-based

    def describe(self) -> str:
        if self.event == "wifi":
            return f"when the PC joins the Wi-Fi '{self.value}'" if self.value else "when the PC joins a Wi-Fi network"
        if self.event == "app_opened":
            return f"when {self.value or 'an app'} is opened"
        if self.event == "file_changed":
            folder, pattern = split_watch(self.value)
            return f"when a file {'matching ' + pattern + ' ' if pattern != '*' else ''}is added or changed in {folder}"
        if self.event == "phone_charging":
            word = {"started": "is plugged in", "stopped": "is unplugged"}.get(self.value.strip().lower(), "is plugged in or unplugged")
            return f"when your phone {word}"
        if self.event == "phone_battery_low":
            return f"when your phone's battery drops below {phone_percent(self.value)}% (while unplugged)"
        return "when a USB drive is plugged in"


class ManualTrigger(_Trigger):
    type: Literal["manual"] = "manual"

    def next_run(self, after: datetime) -> None:
        return None

    def describe(self) -> str:
        return "when you ask for it"


Trigger = Annotated[Union[DailyTrigger, OnceTrigger, IntervalTrigger, BatteryTrigger, EventTrigger, ManualTrigger],
                    Field(discriminator="type")]


def once_at(now: datetime, time: str | None = None, in_minutes: int | None = None, date: str | None = None) -> OnceTrigger:
    """A one-off time: in N minutes, at HH:MM today (tomorrow if already past), or on a date."""
    if in_minutes is not None:
        if not 1 <= in_minutes <= 60 * 24 * 60:
            raise ToolError("Minutes must be between 1 and 86400.", "invalid_time")
        return OnceTrigger(at=(now + timedelta(minutes=in_minutes)).replace(second=0, microsecond=0))
    if time is None:
        raise ToolError("When? Give a time (e.g. '23:00') or a number of minutes.", "missing_time")
    hour, minute = map(int, parse_time(time).split(":"))
    if date:
        try:
            day = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            if date.strip().lower() == "tomorrow":
                day = (now + timedelta(days=1)).date()
            elif date.strip().lower() == "today":
                day = now.date()
            else:
                raise ToolError("Dates must look like 2026-09-30 (or 'today'/'tomorrow').", "invalid_date") from None
        at = now.replace(year=day.year, month=day.month, day=day.day, hour=hour, minute=minute, second=0, microsecond=0)
        if at <= now:
            raise ToolError("That time has already passed.", "in_the_past")
        return OnceTrigger(at=at)
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return OnceTrigger(at=at)
