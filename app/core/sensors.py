"""Physical-world sensors over MQTT (ESP32, ESPHome, Zigbee2MQTT ...): what do they say, and is that reading still TRUE?

``config/sensors.json``::

    {"broker": "192.168.1.5:1883",
     "sensors": [{"name": "room temperature", "topic": "home/room/temp", "kind": "temperature", "unit": "C", "key": "temperature", "max_age_s": 900}, ...]}

Rules (the same honesty as the rest of Jarvis):
* a reading older than ``max_age_s`` is STALE, a sensor that sent nothing is UNKNOWN - neither is ever reported as a value ("the door is closed" is only
  said when the sensor said so recently);
* values are parsed from a plain number / word, or a JSON object (``key``); words map to states (ON/OFF, open/closed, motion/clear, true/false);
* numbers outside a plausible range for the kind (e.g. temperature -60..120) are INVALID, not shown as data;
* read-only: this module cannot switch anything (use ``local_device`` / ``smart_home`` for that, they ask first and verify).
Broker address must be on your own network and anonymous. Verified with a fake broker only, not with real sensors.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core import mqtt_lite
from app.core.local_home import LocalDeviceError, check_address

FILE = "sensors.json"
KINDS = {"temperature": (-60.0, 120.0), "humidity": (0.0, 100.0), "light": (0.0, 200_000.0), "distance": (0.0, 20_000.0), "number": (-1e9, 1e9),
         "door": None, "motion": None, "switch": None}
_ON = {"on", "true", "1", "open", "opened", "motion", "detected", "occupied", "active", "yes"}
_OFF = {"off", "false", "0", "closed", "close", "clear", "none", "idle", "inactive", "no", "unoccupied"}
_WORD = {"door": ("open", "closed"), "motion": ("motion detected", "no motion"), "switch": ("on", "off")}


@dataclass
class Sensor:
    name: str
    topic: str
    kind: str = "number"
    unit: str = ""
    key: str = ""
    max_age_s: int = 900


@dataclass
class Reading:
    sensor: Sensor
    status: str                      # ok | stale | unknown | invalid
    value: Any = None
    age_s: float | None = None
    note: str = ""

    def text(self) -> str:
        n = self.sensor.name
        if self.status == "unknown":
            return f"{n}: unknown ({self.note or 'no message from the sensor'})"
        if self.status == "invalid":
            return f"{n}: invalid reading ({self.note})"
        age = f"{int(self.age_s)} s ago" if (self.age_s or 0) < 120 else f"{int((self.age_s or 0) // 60)} min ago"
        shown = self.value if isinstance(self.value, str) else f"{self.value:g}{(' ' + self.sensor.unit) if self.sensor.unit else ''}"
        if self.status == "stale":
            return f"{n}: last said {shown} {age} - too old to trust (unknown now)"
        return f"{n}: {shown} ({age})"


def load(config_dir: Path) -> tuple[str, list[Sensor]]:
    try:
        data = json.loads((Path(config_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []
    sensors = []
    for s in (data.get("sensors") or [])[:60]:
        if not isinstance(s, dict) or not s.get("name") or not s.get("topic") or s.get("kind", "number") not in KINDS:
            continue
        sensors.append(Sensor(str(s["name"])[:40], str(s["topic"])[:200], s.get("kind", "number"), str(s.get("unit", ""))[:8], str(s.get("key", ""))[:40],
                              max(10, min(int(s.get("max_age_s", 900) or 900), 86400))))
    return str(data.get("broker", "")), sensors


def parse(sensor: Sensor, payload: bytes) -> tuple[str, Any, str]:
    """-> (status, value, note) for one message."""
    text = payload.decode("utf-8", "replace").strip()
    raw: Any = text
    if text[:1] in "{[":
        try:
            obj = json.loads(text)
        except ValueError:
            return "invalid", None, "not valid JSON"
        if isinstance(obj, dict):
            if not sensor.key or sensor.key not in obj:
                return "invalid", None, f"no '{sensor.key or 'key'}' field in the message"
            raw = obj[sensor.key]
        else:
            return "invalid", None, "unexpected JSON"
    if sensor.kind in _WORD:
        word = str(raw).strip().lower()
        on, off = _WORD[sensor.kind]
        if word in _ON:
            return "ok", on, ""
        if word in _OFF:
            return "ok", off, ""
        return "invalid", None, f"'{word[:20]}' is not a state I know"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "invalid", None, f"'{str(raw)[:20]}' is not a number"
    lo, hi = KINDS[sensor.kind] or (-1e9, 1e9)
    if value != value or not lo <= value <= hi:
        return "invalid", None, f"{value:g} is outside what a {sensor.kind} sensor can report"
    return "ok", value, ""


def evaluate(sensors: list[Sensor], messages: dict[str, tuple[bytes, float]], now: float | None = None) -> list[Reading]:
    now = time.time() if now is None else now
    out = []
    for s in sensors:
        hit = next(((p, t) for topic, (p, t) in messages.items() if mqtt_lite.topic_matches(s.topic, topic)), None)
        if hit is None:
            out.append(Reading(s, "unknown"))
            continue
        status, value, note = parse(s, hit[0])
        age = max(0.0, now - hit[1])
        if status == "ok" and age > s.max_age_s:
            status = "stale"
        out.append(Reading(s, status, value, age, note))
    return out


def read_all(config_dir: Path, seconds: float = 3.0, collector: Callable[..., dict] = mqtt_lite.collect) -> tuple[list[Reading], str]:
    """Ask the broker now. -> (readings, problem). A problem leaves every sensor UNKNOWN rather than showing old values."""
    broker, sensors = load(config_dir)
    if not sensors:
        return [], "No sensors configured. Create config/sensors.json (see docs/round68-sensors.md)."
    try:
        host, _, port = check_address(broker).partition(":")
        messages = collector(host, int(port or mqtt_lite.PORT), sorted({s.topic for s in sensors}), seconds)
    except (LocalDeviceError, mqtt_lite.MqttError) as exc:
        return [Reading(s, "unknown", note="broker not reachable") for s in sensors], str(exc)
    return evaluate(sensors, messages), ""
