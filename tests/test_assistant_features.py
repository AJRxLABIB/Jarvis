"""Weather, media keys, the briefing, barge-in and proactive alerts (v1.2)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agent.shortcuts import match_shortcut
from app.config.settings import Settings
from app.tools import assistant, winapi
from app.tools.assistant import MediaArgs, WeatherArgs, daily_briefing, describe_code, get_weather, media_control
from app.tools.base import NoArgs, ToolContext, ToolError
from tests.test_hands_free import Harness, wake_then

try:
    from tests.test_gui import qapp  # noqa: F401  (the Qt application fixture)
except Exception:  # PySide6 missing
    qapp = None

NOW = datetime(2026, 9, 24, 7, 30, tzinfo=timezone(timedelta(hours=6)))


class FakeWeather:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "geocoding" in request.url.host:
            name = request.url.params["name"]
            if name.lower() == "atlantis":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"results": [
                {"name": name.title(), "country": "Bangladesh", "country_code": "BD", "latitude": 23.7, "longitude": 90.4}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 31.4, "apparent_temperature": 36.2, "relative_humidity_2m": 78,
                        "weather_code": 2, "wind_speed_10m": 9.5},
            "daily": {"weather_code": [61, 95], "temperature_2m_max": [33.1, 32.0], "temperature_2m_min": [26.4, 25.7],
                      "precipitation_probability_max": [65, 80]},
        })


@pytest.fixture
def weather(monkeypatch):
    fake = FakeWeather()
    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(assistant, "_GEO_CACHE", {})
    monkeypatch.setattr(assistant, "_WEATHER_CACHE", {})
    return fake


def ctx(**settings) -> ToolContext:
    return ToolContext(Settings(**settings), clock=lambda: NOW)


def test_current_weather_for_the_home_city(weather) -> None:
    result = asyncio.run(get_weather(WeatherArgs(), ctx(home_location="Dhaka")))
    assert result.ok
    assert result.message == "In Dhaka, Bangladesh it's 31°C with partly cloudy, feels like 36°C. 65% chance of rain today."
    forecast = [r for r in weather.requests if "forecast" in r.url.path][0]
    assert forecast.url.params["temperature_unit"] == "celsius" and forecast.url.params["latitude"] == "23.7"


def test_forecast_tomorrow_for_another_city_and_cache(weather) -> None:
    c = ctx(home_location="Dhaka", temperature_unit="F")
    result = asyncio.run(get_weather(WeatherArgs(location="Chittagong", when="tomorrow"), c))
    assert result.message == "Tomorrow in Chittagong, Bangladesh: a thunderstorm, 26 to 32°F, 80% chance of rain."
    asyncio.run(get_weather(WeatherArgs(location="chittagong", when="today"), c))
    assert len(weather.requests) == 2  # the place and the forecast were cached


def test_weather_errors(weather) -> None:
    with pytest.raises(ToolError, match="Which city"):
        asyncio.run(get_weather(WeatherArgs(), ctx()))
    with pytest.raises(ToolError, match="couldn't find a place called Atlantis"):
        asyncio.run(get_weather(WeatherArgs(location="Atlantis"), ctx()))
    assert describe_code(999) == "unknown conditions" and describe_code(None) == "unknown conditions"


def test_weather_service_down(monkeypatch) -> None:
    def down(request):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(down))
    monkeypatch.setattr(assistant, "_GEO_CACHE", {})
    with pytest.raises(ToolError, match="couldn't reach the weather service"):
        asyncio.run(get_weather(WeatherArgs(location="Dhaka"), ctx()))


def test_media_keys(monkeypatch) -> None:
    pressed: list[int] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "press_key", pressed.append)
    assert media_control(MediaArgs(action="next"), ctx()).message == "Next track."
    assert media_control(MediaArgs(action="play_pause"), ctx()).ok
    assert pressed == [0xB0, 0xB3]


def test_briefing(weather, monkeypatch) -> None:
    monkeypatch.setattr(assistant, "_battery_line", lambda: "Battery 80%, charging.")
    monkeypatch.setattr("app.utils.network.is_offline_now", lambda settings: False)
    result = asyncio.run(daily_briefing(NoArgs(), ctx(home_location="Dhaka", user_address="sir")))
    assert result.message.startswith("Good morning, sir. It's 07:30 on Thursday, 24 September.")
    assert "31°C" in result.message and result.message.endswith("Battery 80%, charging.")


def test_briefing_without_home_city_or_network(monkeypatch) -> None:
    monkeypatch.setattr(assistant, "_battery_line", lambda: None)
    result = asyncio.run(daily_briefing(NoArgs(), ctx()))
    assert result.message == "Good morning. It's 07:30 on Thursday, 24 September."


@pytest.mark.parametrize(("text", "tool", "args"), [
    ("what's the weather", "get_weather", {"when": "now"}),
    ("what's the weather like in Dhaka", "get_weather", {"when": "now", "location": "Dhaka"}),
    ("will it rain tomorrow", "get_weather", {"when": "tomorrow"}),
    ("is it going to rain", "get_weather", {"when": "today"}),
    ("pause the music", "media_control", {"action": "play_pause"}),
    ("next song", "media_control", {"action": "next"}),
    ("previous track", "media_control", {"action": "previous"}),
    ("volume up", "volume_control", {"action": "up"}),
    ("set volume to 40 percent", "volume_control", {"action": "set", "level": 40}),
    ("mute", "volume_control", {"action": "mute"}),
    ("Good morning, Jarvis.", "daily_briefing", {}),
    ("brief me", "daily_briefing", {}),
])
def test_direct_commands(text: str, tool: str, args: dict) -> None:
    call = match_shortcut(text, lambda name: None)
    assert call is not None and (call.name, dict(call.arguments)) == (tool, args)


@pytest.mark.parametrize("text", ["play despacito on youtube", "what's the weather going to do to my plans",
                                  "turn off the lights"])
def test_not_direct(text: str) -> None:
    call = match_shortcut(text, lambda name: None)
    assert call is None or call.name not in ("media_control", "get_weather", "volume_control")


# ------------------------------------------------------------------ barge-in


def test_wake_phrase_while_speaking_interrupts() -> None:
    speaking = {"on": True}
    barged: list[int] = []
    # paced like a real microphone: after a barge-in the queued audio (its own voice) is dropped, so audio
    # delivered all at once could be dropped with it on a slow PC
    h = Harness(wake_then(), pause=lambda: speaking["on"], realtime=0.01)

    def barge() -> bool:
        barged.append(1)
        speaking["on"] = False  # the host stopped the speech
        return True

    h.listener.can_barge_in = lambda: speaking["on"]
    h.listener.on_barge_in = barge
    try:
        assert h.run_until(lambda: h.results, timeout=10)
    finally:
        h.stop()
    assert barged == [1] and h.wakes and h.results[0].status == "ok"


def test_no_barge_in_when_not_allowed() -> None:
    h = Harness(wake_then(), pause=lambda: True)
    h.listener.can_barge_in = lambda: False
    h.listener.on_barge_in = lambda: pytest.fail("must not interrupt")
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
    finally:
        h.stop()


# ------------------------------------------------------------------ proactive alerts


def test_battery_and_charger_alerts(qapp, tmp_path: Path) -> None:
    from tests.test_gui import make_controller

    controller, _, engine = make_controller(tmp_path)
    try:
        controller.ctx.settings = controller.ctx.settings.model_copy(update={"user_address": "sir"})
        b = lambda percent, plugged: SimpleNamespace(percent=percent, power_plugged=plugged)  # noqa: E731
        assert controller.check_alerts(b(80, True)) is None  # first reading: nothing to say
        assert controller.check_alerts(b(80, False)) == "Running on battery now, sir. 80% left."
        assert controller.check_alerts(b(19, False)) == "Battery is at 19%, sir. You may want to plug in the charger."
        assert controller.check_alerts(b(18, False)) is None  # said once
        assert controller.check_alerts(b(9, False)).startswith("Battery critically low, sir: 9%.")
        assert controller.check_alerts(b(8, False)) is None
        assert controller.check_alerts(b(8, True)) == "Charger connected, sir. Battery at 8%."
        controller.ctx.settings = controller.ctx.settings.model_copy(update={"proactive_alerts": False})
        assert controller.check_alerts(b(5, False)) is None
    finally:
        controller.shutdown()


# ------------------------------------------------------------------ play music


def test_media_aliases_small_models_use() -> None:
    assert MediaArgs(action="play").action == "play_pause"
    assert MediaArgs(action="Skip").action == "next"
    assert MediaArgs(action="back").action == "previous"


@pytest.mark.parametrize(("text", "args"), [
    ("Play a music for me", {}), ("Can you play a song for me?", {}), ("Can you play music from YouTube?", {}),
    ("play Arijit Singh songs", {"query": "Arijit Singh"}), ("play believer on youtube", {"query": "believer"}),
])
def test_play_music_commands(text: str, args: dict) -> None:
    call = match_shortcut(text, lambda name: None)
    assert call is not None and call.name == "play_music" and dict(call.arguments) == args


def test_play_chess_is_not_music() -> None:
    assert match_shortcut("play chess", lambda name: None) is None


def test_play_music_opens_the_first_youtube_video(monkeypatch) -> None:
    from app.tools import applications
    from app.tools.assistant import PlayMusicArgs, play_music

    opened: list[str] = []
    monkeypatch.setattr(applications, "open_url", lambda url, ctx, browser=None, **_: opened.append(url) or "Chrome")

    def handler(request):
        assert request.url.params["search_query"] == "Arijit Singh song"
        return httpx.Response(200, text='junk "videoId":"abcDEF12345" more "videoId":"zzzzzzzzzzz"')

    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(handler))
    result = asyncio.run(play_music(PlayMusicArgs(query="Arijit Singh"), ctx()))
    assert result.message == "Playing Arijit Singh on YouTube in Chrome."
    assert opened == ["https://www.youtube.com/watch?v=abcDEF12345"]


def test_play_music_falls_back_to_the_results_page(monkeypatch) -> None:
    from app.tools import applications
    from app.tools.assistant import PlayMusicArgs, play_music

    opened: list[str] = []
    monkeypatch.setattr(applications, "open_url", lambda url, ctx, browser=None, **_: opened.append(url) or "Chrome")
    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, text="no ids")))
    result = asyncio.run(play_music(PlayMusicArgs(), ctx()))
    assert "results for top hits playlist" in result.message and opened[0].endswith("search_query=top+hits+playlist")
