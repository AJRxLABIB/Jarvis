"""Real switches: Wi-Fi, Bluetooth, dark mode on / off (not just opening the Settings page).

* Wi-Fi and Bluetooth go through Windows' radio API (Windows.Devices.Radios, the same switch as the quick
  settings panel). Windows can refuse it ("Let apps control device radios" off in Settings -> Privacy); then
  Jarvis says so and opens the page instead of pretending.
* Dark mode is two values under HKCU\\...\\Themes\\Personalize (apps and Windows), for your account only.
* Every change is read back afterwards (verified), and can be undone ("undo that").
* Turning Wi-Fi off also cuts the phone remote and online features - it asks first unless it's your own
  direct command.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.toggles")
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
NAMES = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth", "dark_mode": "Dark mode"}


class ToggleArgs(ToolArgs):
    setting: Literal["wifi", "bluetooth", "dark_mode"] = Field(description="What to switch")
    state: Literal["on", "off", "status"] = Field(description="on, off, or just tell the current state")


# ------------------------------------------------------------------ radios (Wi-Fi, Bluetooth)


async def _radios(kind: str):
    from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind

    access = await Radio.request_access_async()
    if access != RadioAccessStatus.ALLOWED:
        raise ToolError("Windows doesn't let apps switch radios on this PC (Settings -> Privacy & security -> Radios: "
                        "'Let apps control device radios').", "denied")
    wanted = RadioKind.WI_FI if kind == "wifi" else RadioKind.BLUETOOTH
    return [r for r in await Radio.get_radios_async() if r.kind == wanted]


async def radio_state(kind: str) -> bool | None:
    from winrt.windows.devices.radios import RadioState

    radios = await _radios(kind)
    if not radios:
        return None
    return any(r.state == RadioState.ON for r in radios)


async def set_radio(kind: str, on: bool) -> None:
    from winrt.windows.devices.radios import RadioAccessStatus, RadioState

    radios = await _radios(kind)
    if not radios:
        raise ToolError(f"This PC has no {NAMES[kind]} radio that Windows lets me switch.", "not_found")
    for radio in radios:
        status = await radio.set_state_async(RadioState.ON if on else RadioState.OFF)
        if status != RadioAccessStatus.ALLOWED:
            raise ToolError(f"Windows refused to switch {NAMES[kind]} ({status}).", "denied")


# ------------------------------------------------------------------ dark mode


def dark_mode() -> bool:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    return int(value) == 0


def set_dark_mode(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE, 0, winreg.KEY_SET_VALUE) as key:
        for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if on else 1)


# ------------------------------------------------------------------ the tool


async def _state(setting: str) -> bool | None:
    if setting == "dark_mode":
        return await asyncio.to_thread(dark_mode)
    return await radio_state(setting)


async def _set(setting: str, on: bool) -> None:
    if setting == "dark_mode":
        await asyncio.to_thread(set_dark_mode, on)
    else:
        await set_radio(setting, on)


async def device_toggle(args: ToggleArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    name = NAMES[args.setting]
    try:
        before = await _state(args.setting)
    except ImportError as exc:
        raise ToolError(f"The Windows radio package isn't installed ({exc.name}); run 'pip install -r "
                        "requirements.txt'.", "missing_dependency") from exc
    except ToolError as exc:
        if exc.code == "denied":
            _open_settings(args.setting)
        raise
    if before is None:
        raise ToolError(f"This PC has no {name} that Windows lets me switch.", "not_found")
    if args.state == "status":
        return ToolResult.success(f"{name} is {'on' if before else 'off'}.", on=before)
    want = args.state == "on"
    if before == want:
        return ToolResult.success(f"{name} is already {args.state}.", on=before, previous=args.state)
    await _set(args.setting, want)
    await asyncio.sleep(0.4)
    now = await _state(args.setting)
    log_event(_log, "toggled", setting=args.setting, on=want, verified=now == want)
    if now != want:
        return ToolResult.failure("not_verified", f"I asked Windows to turn {name} {args.state}, but it is still "
                                  f"{'on' if now else 'off'}.", on=now)
    return ToolResult.success(f"{name} is {args.state}.", on=now, previous="on" if before else "off", verified=True)


def _open_settings(setting: str) -> None:
    pages = {"wifi": "ms-settings:network-wifi", "bluetooth": "ms-settings:bluetooth", "dark_mode": "ms-settings:colors"}
    try:
        winapi.start_file(pages[setting])
    except Exception:
        pass


def _risk(args: ToggleArgs, ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.setting == "wifi" and args.state == "off" else RiskLevel.SAFE


TOOLS: tuple[Tool, ...] = (
    Tool("device_toggle", "Really switch Wi-Fi, Bluetooth or dark mode on or off (or tell whether it's on).",
         device_toggle, ToggleArgs, RiskLevel.SAFE, "windows", timeout=20, risk=_risk,
         confirmation=lambda a, c: "Turn Wi-Fi off? The phone remote and online features stop until it's back on.",
         direct_without_confirmation=lambda a, c: True),
)


# ------------------------------------------------------------------ screen recording (Xbox Game Bar)
# Windows' own recorder: Win+Alt+R starts / stops a recording of the app in front (not the desktop), saved in
# Videos\Captures. Jarvis presses the shortcut and, on stop, checks that a new video really appeared - if Game Bar
# is switched off (Settings -> Gaming -> Xbox Game Bar) nothing appears and it says so.

_RECORDING: dict[str, float] = {}


def _captures() -> Path | None:
    videos = winapi.known_folder("VIDEOS")
    return (videos / "Captures") if videos is not None else None


def _videos(folder: Path | None) -> dict[str, float]:
    if folder is None or not folder.is_dir():
        return {}
    return {p.name: p.stat().st_mtime for p in folder.glob("*.mp4")}


class RecordArgs(ToolArgs):
    action: Literal["start", "stop"] = Field(description="start or stop recording the app in front")


def screen_recording(args: RecordArgs, ctx: ToolContext) -> ToolResult:
    import time as _time

    from app.tools.screen_agent import hand_over

    winapi.require_windows()
    folder = _captures()
    if args.action == "start":
        if _RECORDING:
            return ToolResult.success("I'm already recording. Say 'stop recording' to finish.")
        hand_over(ctx)  # record YOUR app, not Jarvis
        _RECORDING["since"] = _time.time()
        _RECORDING.update({f"had:{k}": v for k, v in _videos(folder).items()})
        winapi.press_combo("win+alt+r")
        return ToolResult.success("Recording the app in front with Xbox Game Bar. Say 'stop recording' to finish.")
    if not _RECORDING:
        return ToolResult.failure("not_recording", "I'm not recording the screen. (Meeting notes stop with "
                                  "'stop the notes'.)")
    since = _RECORDING.get("since", 0.0)
    known = {k.removeprefix("had:") for k in _RECORDING if k.startswith("had:")}
    _RECORDING.clear()
    winapi.press_combo("win+alt+r")
    for _ in range(20):  # Game Bar finishes writing the file after it stops
        _time.sleep(0.5)
        new = [name for name, mtime in _videos(folder).items() if name not in known and mtime >= since - 5]
        if new:
            path = folder / sorted(new)[-1]
            return ToolResult.success(f"Saved the recording: {path.name} in {folder}.", path=str(path), verified=True)
    return ToolResult.failure("not_verified", "I pressed Win+Alt+R, but no new video appeared in Videos\\Captures. "
                              "Xbox Game Bar may be off (Settings -> Gaming -> Xbox Game Bar), or the app in front "
                              "can't be recorded (the desktop and File Explorer can't).")


TOOLS = TOOLS + (
    Tool("screen_recording", "Start or stop recording the app in front (Xbox Game Bar, saved in Videos\\Captures; "
         "the result is checked).", screen_recording, RecordArgs, RiskLevel.SAFE, "windows", timeout=30),
)
