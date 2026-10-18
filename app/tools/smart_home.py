"""Smart home through Home Assistant: "turn off the fan", "is the door locked?".

Set HOME_ASSISTANT_URL (e.g. http://192.168.1.10:8123) and HOME_ASSISTANT_TOKEN (a long-lived
access token: Home Assistant -> your profile -> Security). Devices are found by their names in
Home Assistant, sound-alike matching included ("turn on the fen" -> Fan). Lights, switches, fans,
media players and covers: MODERATE. Locks, alarms and garage doors: DANGEROUS (always ask).
The token is never logged or shown.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx
from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.phonetic import best_sounding

TRANSPORT: httpx.BaseTransport | None = None  # tests replace this
CONTROLLABLE = ("light", "switch", "fan", "media_player", "cover", "lock", "climate", "scene", "script",
                "input_boolean", "alarm_control_panel", "vacuum", "humidifier")
SENSITIVE = ("lock", "alarm_control_panel")  # and garage covers
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
SERVICES = {
    "on": {"cover": "open_cover", "lock": "unlock", "scene": "turn_on", "script": "turn_on", "vacuum": "start",
           "alarm_control_panel": "alarm_disarm"},
    "off": {"cover": "close_cover", "lock": "lock", "vacuum": "return_to_base",
            "alarm_control_panel": "alarm_arm_away"},
}


def _config(ctx: ToolContext) -> tuple[str, str]:
    url, token = ctx.settings.home_assistant_url.rstrip("/"), ctx.settings.home_assistant_token
    if not url or not token:
        raise ToolError("Smart home isn't set up: add your Home Assistant address and access token in Settings "
                        "(HOME_ASSISTANT_URL, HOME_ASSISTANT_TOKEN).", "not_configured")
    return url, token


def _request(ctx: ToolContext, method: str, path: str, payload: dict | None = None) -> Any:
    url, token = _config(ctx)
    try:
        with httpx.Client(transport=TRANSPORT, timeout=10.0, trust_env=False) as client:
            response = client.request(method, url + path, json=payload,
                                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise ToolError(f"I couldn't reach Home Assistant at {url}.", "unreachable") from exc
    if response.status_code == 401:
        raise ToolError("Home Assistant refused the access token; create a new one and save it in Settings.", "auth")
    if response.status_code >= 400:
        raise ToolError(f"Home Assistant returned an error (HTTP {response.status_code}).", "service_error")
    return response.json() if response.content else None


def entities(ctx: ToolContext, refresh: bool = False) -> list[dict[str, Any]]:
    key = ctx.settings.home_assistant_url
    cached = _CACHE.get(key)
    if cached and not refresh and time.monotonic() - cached[0] < 60:
        return cached[1]
    states = _request(ctx, "GET", "/api/states") or []
    found = [s for s in states if isinstance(s, dict) and str(s.get("entity_id", "")).split(".")[0] in CONTROLLABLE]
    _CACHE[key] = (time.monotonic(), found)
    return found


def _name(entity: dict[str, Any]) -> str:
    return str(entity.get("attributes", {}).get("friendly_name") or entity["entity_id"].split(".", 1)[1].replace("_", " "))


def find_entity(ctx: ToolContext, name: str) -> dict[str, Any]:
    items = entities(ctx)
    if not items:
        raise ToolError("Home Assistant has no devices I can control.", "no_devices")
    by_name = {_name(e).lower(): e for e in items}
    wanted = name.strip().lower().removeprefix("the ").strip()
    if wanted in by_name:
        return by_name[wanted]
    contains = [e for n, e in by_name.items() if wanted in n or n in wanted]
    if len(contains) == 1:
        return contains[0]
    guess = best_sounding(wanted, [_name(e) for e in items], threshold=0.7)
    if guess is None:
        names = ", ".join(sorted(_name(e) for e in items)[:12])
        raise ToolError(f"I can't find a device called '{name}'. I know: {names}.", "unknown_device")
    return by_name[guess.lower()]


def known_device(ctx: ToolContext, name: str) -> str | None:
    """For direct commands: the device's name if it is surely one (from the cached device list only, no network)."""
    if not ctx.settings.home_assistant_url or not ctx.settings.home_assistant_token:
        return None
    cached = _CACHE.get(ctx.settings.home_assistant_url)
    if not cached:
        return None
    wanted = " ".join(name.lower().split()).removeprefix("the ").strip()
    names = {_name(e).lower(): _name(e) for e in cached[1]}
    if wanted in names:
        return names[wanted]
    contains = [n for low, n in names.items() if len(wanted) >= 3 and (wanted in low or low in wanted)]
    return contains[0] if len(contains) == 1 else None


def prefetch(ctx: ToolContext) -> None:
    """Refresh the device list in the background, so "turn off the fan" can run directly."""
    if ctx.settings.home_assistant_url and ctx.settings.home_assistant_token:
        try:
            entities(ctx, refresh=True)
        except ToolError:
            pass


def _is_sensitive(entity: dict[str, Any]) -> bool:
    domain = entity["entity_id"].split(".")[0]
    device_class = str(entity.get("attributes", {}).get("device_class", ""))
    return domain in SENSITIVE or (domain == "cover" and device_class in ("garage", "door", "gate"))


class HomeArgs(ToolArgs):
    device: str = Field(min_length=1, max_length=80, description="Device name as in Home Assistant, e.g. 'fan'")
    action: Literal["on", "off", "toggle"] = Field(description="on / off / toggle (open/close for covers, "
                                                               "unlock/lock for locks)")


def _risk(args: HomeArgs, ctx: ToolContext) -> RiskLevel:
    try:
        return RiskLevel.DANGEROUS if _is_sensitive(find_entity(ctx, args.device)) else RiskLevel.MODERATE
    except ToolError:
        return RiskLevel.MODERATE


def home_control(args: HomeArgs, ctx: ToolContext) -> ToolResult:
    entity = find_entity(ctx, args.device)
    domain = entity["entity_id"].split(".")[0]
    if args.action == "toggle":
        service = "toggle"
    else:
        service = SERVICES[args.action].get(domain, f"turn_{args.action}")
    _request(ctx, "POST", f"/api/services/{domain}/{service}", {"entity_id": entity["entity_id"]})
    _CACHE.clear()
    expected = {"on": "on", "off": "off"}.get(args.action) if domain in CHECKABLE else None
    if expected is None:                       # covers, locks, scenes...: their state changes later / differently
        return ToolResult.success(f"Sent '{service}' to {_name(entity)} (Home Assistant accepted it; I can't confirm "
                                  "the device's new state yet).", device=_name(entity), service=service, verified=None)
    reported = _state_now(ctx, entity["entity_id"])
    if reported == expected:
        return ToolResult.success(f"{_name(entity)} is {expected} - Home Assistant confirms it.", device=_name(entity),
                                  service=service, verified=True)
    return ToolResult.failure("not_verified", f"I asked Home Assistant to turn {_name(entity)} {expected}, but it "
                              f"still reports '{reported}'. The device may be offline.", device=_name(entity),
                              service=service, verified=False)


CHECKABLE = ("light", "switch", "fan", "input_boolean", "humidifier")


def _state_now(ctx: ToolContext, entity_id: str, tries: int = 3) -> str:
    """The entity's state as Home Assistant reports it now (a device can take a moment: asked up to 3 times)."""
    state = "unknown"
    for attempt in range(tries):
        try:
            state = str((_request(ctx, "GET", f"/api/states/{entity_id}") or {}).get("state", "unknown"))
        except ToolError:
            return "unknown"
        if state in ("on", "off") or attempt == tries - 1:
            return state
        time.sleep(0.4)
    return state


def home_status(_: NoArgs, ctx: ToolContext) -> ToolResult:
    items = entities(ctx, refresh=True)
    listed = [{"device": _name(e), "state": e.get("state")} for e in items[:40]]
    on = [d["device"] for d in listed if d["state"] in ("on", "open", "playing", "unlocked")]
    return ToolResult.success(f"{len(items)} device(s); on or open: {', '.join(on) if on else 'none'}.",
                              devices=listed)


TOOLS: tuple[Tool, ...] = (
    Tool("home_control", "Turn a smart-home device on/off (lights, fan, switch, TV, curtains, lock) via Home "
         "Assistant.", home_control, HomeArgs, RiskLevel.MODERATE, category="home", timeout=20, needs_network=False,
         risk=_risk, confirmation=lambda a, _: f"Turn {a.action} {a.device}?",
         direct_without_confirmation=lambda a, ctx: _risk(a, ctx) == RiskLevel.MODERATE),
    Tool("home_status", "Which smart-home devices exist and what is on.", home_status, NoArgs, RiskLevel.SAFE,
         category="home", timeout=20),
)
