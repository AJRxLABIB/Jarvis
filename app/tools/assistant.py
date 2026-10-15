"""Everyday assistant tools: weather, media playback and the daily briefing.

Weather comes from Open-Meteo (open-meteo.com): free, no account or API key, and
only the city name / coordinates are sent. Media control presses the Windows media
keys, so it works with Spotify, YouTube in the browser, VLC, Groove...
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator

from app import __version__
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.assistant")
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests replace this
_GEO_CACHE: dict[str, tuple[str, float, float]] = {}
_WEATHER_CACHE: dict[tuple, tuple[float, dict]] = {}
WEATHER_CACHE_SECONDS = 600

# WMO weather interpretation codes (Open-Meteo)
WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "a thunderstorm",
    96: "a thunderstorm with hail", 99: "a thunderstorm with heavy hail",
}


def describe_code(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "unknown conditions")
    except (TypeError, ValueError):
        return "unknown conditions"


async def _get_json(url: str, params: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=10.0,
                                     headers={"User-Agent": f"JarvisAssistant/{__version__}"}) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise ToolError("The weather service took too long to answer.", "timeout") from exc
    except httpx.HTTPError as exc:
        raise ToolError("I couldn't reach the weather service. Check the internet connection.", "network") from exc
    if response.status_code >= 400:
        raise ToolError(f"The weather service returned an error (HTTP {response.status_code}).", "service_error")
    try:
        return response.json()
    except ValueError as exc:
        raise ToolError("The weather service sent an unreadable answer.", "service_error") from exc


async def geocode(place: str) -> tuple[str, float, float]:
    key = " ".join(place.lower().split())
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    city = place.split(",")[0].strip()
    data = await _get_json(GEOCODE_URL, {"name": city, "count": 5, "language": "en", "format": "json"})
    results = data.get("results") or [] if isinstance(data, dict) else []
    if not results:
        raise ToolError(f"I couldn't find a place called {place}.", "unknown_place")
    wanted_country = place.split(",")[1].strip().lower() if "," in place else ""
    best = next((r for r in results if wanted_country and wanted_country in
                 (str(r.get("country", "")) + " " + str(r.get("country_code", ""))).lower()), results[0])
    name = ", ".join(str(p) for p in (best.get("name"), best.get("country")) if p)
    found = (name, float(best["latitude"]), float(best["longitude"]))
    _GEO_CACHE[key] = found
    return found


class WeatherArgs(ToolArgs):
    location: str | None = Field(default=None, max_length=80,
                                 description="City (optionally ', country'). Omit for the user's home city.")
    when: Literal["now", "today", "tomorrow"] = Field(default="now", description="now = current conditions")


async def weather_report(location: str | None, when: str, ctx: ToolContext) -> ToolResult:
    place = (location or ctx.settings.home_location or "").strip()
    if not place:
        raise ToolError("Which city? You can set your home city in Settings (Home city), then just ask "
                        "\"what's the weather\".", "missing_location")
    name, lat, lon = await geocode(place)
    unit = ctx.settings.temperature_unit
    cache_key = (round(lat, 3), round(lon, 3), unit)
    cached = _WEATHER_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < WEATHER_CACHE_SECONDS:
        data = cached[1]
    else:
        data = await _get_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": unit, "wind_speed_unit": "kmh",
        })
        _WEATHER_CACHE[cache_key] = (time.monotonic(), data)
    symbol = "°F" if unit == "fahrenheit" else "°C"
    current, daily = data.get("current") or {}, data.get("daily") or {}
    log_event(_log, "weather_fetched", when=when)
    if when == "now":
        if "temperature_2m" not in current:
            raise ToolError("The weather service didn't return current conditions.", "service_error")
        temp, feels = round(current["temperature_2m"]), round(current.get("apparent_temperature", current["temperature_2m"]))
        sky = describe_code(current.get("weather_code"))
        message = f"In {name} it's {temp}{symbol} with {sky}"
        message += f", feels like {feels}{symbol}." if abs(feels - temp) >= 2 else "."
        rain = (daily.get("precipitation_probability_max") or [None])[0]
        if isinstance(rain, (int, float)) and rain >= 40:
            message += f" {round(rain)}% chance of rain today."
        return ToolResult.success(message, place=name, temperature=temp, feels_like=feels, conditions=sky,
                                  humidity=current.get("relative_humidity_2m"),
                                  wind_kmh=current.get("wind_speed_10m"), unit=symbol)
    index = 0 if when == "today" else 1
    try:
        high = round(daily["temperature_2m_max"][index])
        low = round(daily["temperature_2m_min"][index])
        sky = describe_code(daily["weather_code"][index])
        rain = daily.get("precipitation_probability_max", [None, None])[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ToolError("The weather service didn't return a forecast.", "service_error") from exc
    message = f"{'Today' if index == 0 else 'Tomorrow'} in {name}: {sky}, {low} to {high}{symbol}"
    message += f", {round(rain)}% chance of rain." if isinstance(rain, (int, float)) else "."
    return ToolResult.success(message, place=name, high=high, low=low, conditions=sky, rain_chance=rain, unit=symbol)


async def get_weather(args: WeatherArgs, ctx: ToolContext) -> ToolResult:
    return await weather_report(args.location, args.when, ctx)


# ------------------------------------------------------------------ media


_MEDIA_ALIASES = {
    "play": "play_pause", "pause": "play_pause", "resume": "play_pause", "toggle": "play_pause", "unpause": "play_pause",
    "playpause": "play_pause", "play/pause": "play_pause", "skip": "next", "forward": "next", "next_track": "next",
    "back": "previous", "prev": "previous", "previous_track": "previous", "last": "previous", "halt": "stop",
}


class MediaArgs(ToolArgs):
    action: Literal["play_pause", "next", "previous", "stop"] = Field(
        description="play_pause toggles (use it for play, pause and resume)")

    @field_validator("action", mode="before")
    @classmethod
    def _alias(cls, value: Any) -> Any:
        """Small models write "play", "pause" or "skip": they mean the same keys."""
        if isinstance(value, str):
            key = value.strip().lower().replace(" ", "_").replace("-", "_")
            return _MEDIA_ALIASES.get(key, key)
        return value


MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
_MEDIA_MESSAGES = {"play_pause": "Toggled play/pause.", "next": "Next track.", "previous": "Previous track.",
                   "stop": "Stopped playback."}


def media_control(args: MediaArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    if args.action == "next":
        from app.tools.youtube import PLAYER

        song = PLAYER.pop()
        if song is not None:  # "next song" with a queue: the queued one
            PLAYER.step_aside = ctx.step_aside
            if PLAYER.play(song):
                return ToolResult.success(f"Playing {song.label} from your queue.", action="next")
            PLAYER.add(song, front=True)
    winapi.press_key(MEDIA_KEYS[args.action])
    return ToolResult.success(_MEDIA_MESSAGES[args.action], action=args.action)


# ------------------------------------------------------------------ play music

YOUTUBE_SEARCH = "https://www.youtube.com/results"
_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


class PlayMusicArgs(ToolArgs):
    query: str | None = Field(default=None, max_length=120,
                              description="Song, artist or kind of music, e.g. 'Arijit Singh', 'lofi'. Omit for any music.")


async def first_youtube_video(query: str) -> str | None:
    """The id of the first video YouTube lists for ``query`` (None if the page can't be read)."""
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=10.0, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.8"}) as client:
            response = await client.get(YOUTUBE_SEARCH, params={"search_query": query})
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    match = _VIDEO_ID.search(response.text[:3_000_000])
    return match.group(1) if match else None


def _remember_song(url: str, query: str, ctx: ToolContext) -> None:
    """For "what song is this", "play this again" and the queue (app/tools/youtube.py)."""
    try:
        from pathlib import PureWindowsPath

        from app.tools.applications import browser_target
        from app.tools.youtube import PLAYER

        target = browser_target(ctx) if winapi.is_windows() else None
        PLAYER.started(url, query, PureWindowsPath(target.target).name.lower() if target is not None else None)
    except Exception:
        _log.debug("could not note the song", exc_info=True)


async def play_music(args: PlayMusicArgs, ctx: ToolContext) -> ToolResult:
    """Actually starts music: the best YouTube match plays in your browser (it keeps playing more)."""
    from urllib.parse import quote_plus

    from app.tools.applications import open_url

    query = (args.query or ctx.settings.music_default_query).strip()
    video = await first_youtube_video(f"{query} song" if args.query and len(query.split()) <= 3 else query)
    if video:
        url = f"https://www.youtube.com/watch?v={video}"
        where = open_url(url, ctx, same_tab="youtube")  # songs always play in the same YouTube tab
        _remember_song(url, query, ctx)
        log_event(_log, "music_started", source="youtube")
        return ToolResult.success(f"Playing {query} on YouTube in {where}.", url=url, query=query)
    url = f"{YOUTUBE_SEARCH}?search_query={quote_plus(query)}"
    where = open_url(url, ctx, same_tab="youtube")
    return ToolResult.success(f"Opened YouTube results for {query} in {where}; pick one to play.", url=url, query=query)


# ------------------------------------------------------------------ briefing


def _greeting(now: datetime, address: str) -> str:
    part = "morning" if 5 <= now.hour < 12 else "afternoon" if 12 <= now.hour < 17 else "evening"
    return f"Good {part}{', ' + address if address else ''}."


def _battery_line() -> str | None:
    import psutil

    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None
    if battery is None:
        return None
    state = "charging" if battery.power_plugged else "on battery"
    return f"Battery {battery.percent:.0f}%, {state}."


def _reminders_today(ctx: ToolContext, now: datetime) -> list[str]:
    store = ctx.get_automation_store()
    if store is None:
        return []
    names = []
    try:
        for task in store.all():
            if not task.enabled or not task.next_run_at or task.is_workflow:
                continue
            try:
                when = datetime.fromisoformat(task.next_run_at).astimezone(now.tzinfo)
            except ValueError:
                continue
            if when.date() == now.date() and when >= now:
                names.append(f"{task.name} at {when:%H:%M}")
    except Exception:
        _log.warning("could not read reminders for the briefing", exc_info=True)
    return names[:3]


async def daily_briefing(_: NoArgs, ctx: ToolContext) -> ToolResult:
    now = ctx.clock()
    parts = [_greeting(now, ctx.settings.user_address), f"It's {now:%H:%M} on {now:%A}, {now.day} {now:%B}."]
    data: dict[str, Any] = {}
    if ctx.settings.home_location:
        from app.utils import network

        if not network.is_offline_now(ctx.settings):
            try:
                weather = await weather_report(None, "now", ctx)
                parts.append(weather.message)
                data["weather"] = weather.data
            except ToolError as exc:
                log_event(_log, "briefing_weather_skipped", reason=exc.code)
    battery = _battery_line()
    if battery:
        parts.append(battery)
    reminders = _reminders_today(ctx, now)
    if reminders:
        parts.append("Coming up today: " + "; ".join(reminders) + ".")
    from app.tools.productivity import todo_summary

    todos = todo_summary(ctx)
    if todos:
        parts.append(todos)
    goals = _goals_line(ctx, now)
    if goals:
        parts.append(goals)
    office = _outlook_lines(ctx)
    parts += office
    resume = _last_session_line(ctx, now)
    if resume:
        parts.append(resume)
    return ToolResult.success(" ".join(parts), **data, reminders=reminders)


def _outlook_running() -> bool:
    import psutil

    try:
        return any((p.info.get("name") or "").lower() == "outlook.exe" for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def _outlook_lines(ctx: ToolContext, namespace: Any = None) -> list[str]:
    """Today's meetings and the unread count - only when classic Outlook is already open (never started for this)."""
    if namespace is None and not (winapi.is_windows() and _outlook_running()):
        return []
    lines = []
    try:
        from app.tools.outlook import CalendarArgs, _outlook, calendar_events

        ns = namespace or _outlook()
        events = calendar_events(CalendarArgs(day="today"), ctx, ns).data.get("events") or []
        if events:
            first = events[0]
            when = str(first.get("start", "")).split()[-1]
            lines.append(f"{len(events)} meeting{'s' if len(events) > 1 else ''} today; the first is "
                         f"'{first['subject']}' at {when}.")
        unread = ns.GetDefaultFolder(6).Items.Restrict("[UnRead] = True").Count
        if unread:
            lines.append(f"{unread} unread email{'s' if unread != 1 else ''}.")
    except Exception:
        log_event(_log, "briefing_outlook_skipped")
    return lines


def _goals_line(ctx: ToolContext, now) -> str:
    if ctx.database is None:
        return ""
    try:
        from app.core.goals import GoalStore

        return GoalStore(ctx.database).briefing_line(now.date())
    except Exception:
        _log.warning("could not read goals for the briefing", exc_info=True)
        return ""


def _last_session_line(ctx: ToolContext, now) -> str:
    """In the morning: what you were working on last time, and how to pick it up."""
    if ctx.database is None or now.hour >= 12:
        return ""
    try:
        from app.agent import activity

        calls = activity.reopen_calls(activity.last_session(ctx.database, now))
    except Exception:
        return ""
    names = [str(c.arguments.get("name") or c.arguments.get("site") or "") for c in calls]
    names = [n for n in names if n][:3]
    if not names:
        return ""
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"Last time you were working with {listed}; say 'continue where I left off' to open them again."


TOOLS: tuple[Tool, ...] = (
    Tool("get_weather", "Current weather or today's/tomorrow's forecast for a city (default: the user's home city).",
         get_weather, WeatherArgs, RiskLevel.SAFE, category="info", needs_network=True, timeout=20),
    Tool("media_control", "Control music/video that is ALREADY playing in any app: pause, resume, next or previous "
         "track, stop. To start new music use play_music.",
         media_control, MediaArgs, RiskLevel.SAFE, category="info", timeout=5),
    Tool("play_music", "Start playing music now: a song, artist or kind of music (or anything, if none is named). "
         "Plays the best match from YouTube in the browser.", play_music, PlayMusicArgs, RiskLevel.SAFE,
         category="info", needs_network=True, timeout=25),
    Tool("daily_briefing", "A short briefing: greeting, time and date, weather, battery, today's reminders, to-dos, "
         "meetings and unread email (Outlook), and what you worked on last time "
         "('good morning', 'brief me', 'what's my day like').", daily_briefing, NoArgs, RiskLevel.SAFE,
         category="info", timeout=25),
)
