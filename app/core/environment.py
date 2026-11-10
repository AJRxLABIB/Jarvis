"""Your surroundings today, in plain words: air quality, UV, heat, rain in the next hours, daylight - and what to do about each.

Data: Open-Meteo (the service the weather tool already uses; free, no account, only the city's coordinates are sent): its air-quality API for the US AQI and
PM2.5 / PM10, its forecast API for UV, "feels like" temperature, hourly rain chance and sunrise / sunset.

The category limits are the published ones, not made up here: the US EPA Air Quality Index bands (0-50 good ... 301+ hazardous) and the WHO UV index bands
(0-2 low ... 11+ extreme). The advice lines are the standard public-health guidance for those bands, shortened. A missing reading is reported as unknown -
never guessed, and never filled in from another day.

Limits: the air-quality numbers are a regional model (CAMS, roughly 10-40 km grid), not a sensor on your street - a busy road or indoor smoke can be much worse.
This is information, not medical advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
AQI_BANDS = ((50, "good", "Air quality is good."),
             (100, "moderate", "Acceptable; unusually sensitive people should consider shorter outdoor exertion."),
             (150, "unhealthy for sensitive groups", "Children, older people and anyone with asthma or heart or lung disease should limit long outdoor exertion."),
             (200, "unhealthy", "Everyone should cut down long or heavy outdoor exertion; a well-fitted N95 / KN95 mask helps outdoors; keep windows closed."),
             (300, "very unhealthy", "Avoid outdoor exertion; wear an N95 / KN95 mask outside; keep windows closed and run an air purifier if you have one."),
             (10_000, "hazardous", "Stay indoors with windows closed; avoid all outdoor exertion; wear an N95 / KN95 mask if you must go out."))
UV_BANDS = ((2.99, "low", ""), (5.99, "moderate", "Use sunscreen if you are outside around midday."), (7.99, "high", "Shade around midday, sunscreen, hat."),
            (10.99, "very high", "Avoid the midday sun; sunscreen, hat, sunglasses."), (99, "extreme", "Stay out of the midday sun; full protection if you must go out."))


def aqi_band(aqi: float | None) -> tuple[str, str]:
    if aqi is None or aqi < 0:
        return "unknown", ""
    for limit, name, advice in AQI_BANDS:
        if aqi <= limit:
            return name, advice
    return "hazardous", AQI_BANDS[-1][2]


def uv_band(uv: float | None) -> tuple[str, str]:
    if uv is None or uv < 0:
        return "unknown", ""
    for limit, name, advice in UV_BANDS:
        if uv <= limit:
            return name, advice
    return "extreme", UV_BANDS[-1][2]


def rain_ahead(times: list[str], chances: list[Any], now: datetime, hours: int = 6, threshold: int = 50) -> tuple[int | None, str]:
    """(highest chance in the next ``hours``, "HH:MM" of the first hour at or above ``threshold``). (None, "") when the forecast has no usable hours."""
    best, first = None, ""
    for t, c in zip(times, chances):
        try:
            when = datetime.fromisoformat(str(t))
        except ValueError:
            continue
        ahead = (when - now).total_seconds() / 3600
        if not (-1 < ahead <= hours) or not isinstance(c, (int, float)):
            continue
        best = int(c) if best is None else max(best, int(c))
        if c >= threshold and not first:
            first = when.strftime("%H:%M")
    return best, first


def heat_note(feels_c: float | None) -> str:
    if feels_c is None:
        return ""
    if feels_c >= 41:
        return "Dangerous heat: stay in shade or indoors, drink water often, avoid exertion outside."
    if feels_c >= 35:
        return "Very hot: drink water often and avoid heavy work outside around midday."
    if feels_c <= 5:
        return "Cold: dress warmly."
    return ""


@dataclass
class Report:
    place: str
    aqi: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    uv_max: float | None = None
    feels_c: float | None = None
    rain_max: int | None = None
    rain_at: str = ""
    sunrise: str = ""
    sunset: str = ""
    missing: list[str] = field(default_factory=list)

    def advice(self) -> list[str]:
        out = []
        band, tip = aqi_band(self.aqi)
        if tip and band != "good":
            out.append(tip)
        tip = uv_band(self.uv_max)[1]
        if tip:
            out.append(tip)
        if heat_note(self.feels_c):
            out.append(heat_note(self.feels_c))
        if self.rain_max is not None and self.rain_max >= 50:
            out.append("Take an umbrella" + (f" - rain is likely from about {self.rain_at}." if self.rain_at else "."))
        return out

    def format(self) -> str:
        lines = [f"Around you in {self.place}:"]
        if self.aqi is not None:
            detail = ", ".join(x for x in (f"PM2.5 {self.pm25:.0f} µg/m³" if self.pm25 is not None else "", f"PM10 {self.pm10:.0f} µg/m³" if self.pm10 is not None else "") if x)
            lines.append(f"  Air: AQI {self.aqi:.0f} - {aqi_band(self.aqi)[0]}" + (f" ({detail})" if detail else ""))
        if self.uv_max is not None:
            lines.append(f"  UV today: up to {self.uv_max:.0f} - {uv_band(self.uv_max)[0]}")
        if self.feels_c is not None:
            lines.append(f"  Feels like {self.feels_c:.0f}°C")
        if self.rain_max is not None:
            lines.append(f"  Rain in the next 6 hours: up to {self.rain_max}%" + (f", likely from {self.rain_at}" if self.rain_at else ""))
        if self.sunrise and self.sunset:
            lines.append(f"  Daylight: {self.sunrise} to {self.sunset}")
        tips = self.advice()
        if tips:
            lines.append("What to do: " + " ".join(tips))
        if self.missing:
            lines.append("Not available right now: " + ", ".join(self.missing) + ".")
        lines.append("Air quality is a regional model estimate, not a sensor on your street.")
        return "\n".join(lines)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clock(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M")
    except ValueError:
        return ""


def build(place: str, air: dict[str, Any] | None, forecast: dict[str, Any] | None, now: datetime) -> Report:
    """Pure: the two API answers -> a report. Either may be None (that part is listed as missing)."""
    r = Report(place)
    cur = (air or {}).get("current") or {}
    r.aqi, r.pm25, r.pm10 = _num(cur.get("us_aqi")), _num(cur.get("pm2_5")), _num(cur.get("pm10"))
    if r.aqi is None:
        r.missing.append("air quality")
    f = forecast or {}
    daily, hourly = f.get("daily") or {}, f.get("hourly") or {}
    r.feels_c = _num((f.get("current") or {}).get("apparent_temperature"))
    r.uv_max = _num((daily.get("uv_index_max") or [None])[0])
    r.sunrise, r.sunset = _clock((daily.get("sunrise") or [""])[0]), _clock((daily.get("sunset") or [""])[0])
    r.rain_max, r.rain_at = rain_ahead(list(hourly.get("time") or []), list(hourly.get("precipitation_probability") or []), now)
    if forecast is None:
        r.missing.append("weather")
    elif r.uv_max is None:
        r.missing.append("UV")
    return r
