"""Windows controls: volume, screen brightness and power (lock, sleep, sign out, restart, shut down)."""

from __future__ import annotations

import re
import threading
import time
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult, clamped_int
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.windows")

# ---------------------------------------------------------------------- volume


class VolumeArgs(ToolArgs):
    action: Literal["get", "set", "up", "down", "mute", "unmute"] = Field(description="What to do with the volume")
    level: int | None = Field(default=None, ge=0, le=100, description="Target percent for action 'set'")
    step: clamped_int(1, 50, 10, "Percent to change for 'up'/'down'")  # type: ignore[valid-type]


def volume_control(args: VolumeArgs, _: ToolContext) -> ToolResult:
    winapi.require_windows()
    try:
        current, muted = winapi.get_volume()
    except ToolError:
        raise
    except Exception:
        _log.warning("audio endpoint API failed; using volume keys", exc_info=True)
        return _volume_by_keys(args)

    if args.action == "get":
        return ToolResult.success(f"Volume is {current}%{' (muted)' if muted else ''}.", volume=current, muted=muted)
    if args.action in ("mute", "unmute"):
        winapi.set_mute(args.action == "mute")
        return ToolResult.success("Muted." if args.action == "mute" else f"Unmuted. Volume is {current}%.",
                                  volume=current, muted=args.action == "mute")
    if args.action == "set":
        if args.level is None:
            raise ToolError("Which volume level (0-100)?", "missing_level")
        target = args.level
    else:
        target = current + args.step if args.action == "up" else current - args.step
    target = max(0, min(100, target))
    winapi.set_volume(target)
    if muted and target > 0 and args.action in ("set", "up"):
        winapi.set_mute(False)
        muted = False
    try:
        now, _ = winapi.get_volume()
    except Exception:
        now = target  # can't check
    if abs(now - target) > 2:
        return ToolResult.failure("not_verified", f"I tried to set the volume to {target}%, but it is {now}%.",
                                  volume=now, previous=current)
    return ToolResult.success(f"Volume set to {target}%.", volume=target, previous=current, muted=muted)


def _volume_by_keys(args: VolumeArgs) -> ToolResult:
    if args.action in ("mute", "unmute"):
        winapi.press_volume_key(winapi.VK_VOLUME_MUTE)
        return ToolResult.success("Toggled mute.", exact=False)
    if args.action in ("up", "down"):
        winapi.press_volume_key(winapi.VK_VOLUME_UP if args.action == "up" else winapi.VK_VOLUME_DOWN, max(1, args.step // 2))
        return ToolResult.success(f"Turned the volume {args.action}.", exact=False)
    raise ToolError("I can't read or set an exact volume on this system.", "unsupported")


# ----------------------------------------------------------------------- power

PowerAction = Literal["lock", "sleep", "hibernate", "sign_out", "restart", "shutdown", "cancel_shutdown"]
_POWER_LEVELS = {
    "lock": RiskLevel.SAFE, "cancel_shutdown": RiskLevel.SAFE, "sleep": RiskLevel.MODERATE, "hibernate": RiskLevel.MODERATE,
    "sign_out": RiskLevel.DANGEROUS, "restart": RiskLevel.DANGEROUS, "shutdown": RiskLevel.DANGEROUS,
}


class PowerArgs(ToolArgs):
    action: PowerAction = Field(description="lock, sleep, hibernate, sign_out, restart, shutdown, or cancel_shutdown")
    delay_seconds: int = Field(default=10, ge=0, le=3600, description="Countdown before restart/shutdown")
    save_work: bool = Field(default=False, description="true if the user said to save their work first")


def _power_question(args: PowerArgs, ctx: ToolContext) -> str:
    when = "now" if args.delay_seconds == 0 else f"in {args.delay_seconds} seconds"
    save = (" Open work will be saved first." if args.save_work or ctx.settings.save_before_power
            else " Apps with unsaved work will ask you first.")
    return {
        "sleep": "Put the computer to sleep now?",
        "hibernate": "Hibernate the computer now (it saves everything and powers off)?",
        "sign_out": "Sign out of Windows now? Apps with unsaved work will ask you first.",
        "restart": f"Restart the computer {when}?{save}",
        "shutdown": f"Shut down the computer {when}?{save}",
    }.get(args.action, f"{args.action}?")


def _direct_power_ok(args: PowerArgs, ctx: ToolContext) -> bool:
    """With CONFIRM_POWER_ACTIONS=false, YOUR "restart / shut down my PC" needs no dialog (the countdown remains)."""
    return args.action in ("restart", "shutdown", "sleep", "hibernate") and not ctx.settings.confirm_power_actions


# ---- pending restart/shutdown: the countdown runs here, then Windows is asked to do it gracefully.
# (shutdown.exe with its own countdown implies /f, which closes apps WITHOUT saving: never used.)
_pending_lock = threading.Lock()
_pending: dict[str, object] = {}
_timer_factory = threading.Timer  # tests replace this


def power_pending() -> str | None:
    """'restart' / 'shutdown' while a countdown is running, else None."""
    with _pending_lock:
        return _pending.get("action")  # type: ignore[return-value]


def cancel_pending_power() -> bool:
    with _pending_lock:
        timer = _pending.pop("timer", None)
        _pending.pop("action", None)
    if timer is None:
        return False
    timer.cancel()  # type: ignore[attr-defined]
    log_event(_log, "power_cancelled")
    return True


def _schedule_power(action: str, delay: int) -> None:
    cancel_pending_power()

    def fire() -> None:
        with _pending_lock:
            _pending.clear()
        try:
            winapi.run_shutdown(action, 0)  # /t 0 without /f: apps with unsaved work can still ask
        except ToolError:
            _log.warning("the scheduled %s failed", action, exc_info=True)

    if delay <= 0:
        winapi.run_shutdown(action, 0)
        return
    timer = _timer_factory(delay, fire)
    timer.daemon = True
    with _pending_lock:
        _pending.update(action=action, timer=timer)
    timer.start()
    log_event(_log, "power_scheduled", action=action, delay=delay)


# ---- "save and restart": Ctrl+S in windows whose title says they have unsaved changes
_UNSAVED = re.compile(r"^\s*[*●•]|\s[●•]\s|\*\s*-\s|\(unsaved\)|- unsaved\b", re.I)
_UNTITLED = re.compile(r"\b(untitled|unbenannt|sans titre|new document|document\d+)\b", re.I)
# These bring back unsaved (even never-saved) tabs after a restart, so an untitled file there is safe.
_SESSION_RESTORE = frozenset({"notepad.exe", "code.exe", "notepad++.exe", "sublime_text.exe", "cursor.exe"})


def unsaved_windows() -> list[winapi.WindowInfo]:
    return [w for w in winapi.visible_windows() if w.title and _UNSAVED.search(w.title)]


def save_open_work(wait: float = 1.2) -> tuple[list[str], list[str]]:
    """Press Ctrl+S in each window that shows unsaved changes. Returns (saved, need_you)."""
    saved: list[str] = []
    need_you: list[str] = []
    for window in unsaved_windows():
        name = window.title.strip()[:60]
        if _UNTITLED.search(window.title):
            if window.process_name.lower() not in _SESSION_RESTORE:
                need_you.append(name)  # never saved before: it would need a file name
            continue  # (Notepad / VS Code keep unsaved tabs across a restart by themselves)
        if not winapi.focus_window(window.hwnd):
            need_you.append(name)
            continue
        time.sleep(0.3)
        winapi.press_ctrl_s()
        time.sleep(wait)
        title = winapi.window_title(window.hwnd)
        if title and _UNSAVED.search(title):
            need_you.append(name)  # still unsaved (a Save As dialog, or the app doesn't use Ctrl+S)
        else:
            saved.append(name)
    return saved, need_you


def power_control(args: PowerArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    from app.remote import pc_state

    if args.action in ("sleep", "hibernate", "restart", "shutdown"):
        pc_state.announce_power(args.action)  # so a watching phone can say why the PC went quiet
    if args.action == "lock":
        winapi.lock_workstation()
        return ToolResult.success("Locked the computer.")
    if args.action == "sleep":
        winapi.suspend()
        return ToolResult.success("The computer went to sleep and has woken up again.")
    if args.action == "hibernate":
        states = winapi.power_states_text().lower()
        if "hibernate" not in states.split("the following sleep states are not available")[0]:
            raise ToolError("Hibernate isn't available on this PC (powercfg /a). Sleep or shut down instead.",
                            "unsupported")
        winapi.hibernate()
        return ToolResult.success("The computer hibernated and has started again.")
    if args.action == "cancel_shutdown":
        if cancel_pending_power():
            return ToolResult.success("Cancelled. Nothing will restart or shut down.")
        try:
            winapi.run_shutdown("cancel")
        except ToolError as exc:
            if exc.code == "nothing_to_cancel":
                return ToolResult.success("There was no restart or shutdown to cancel.")
            raise
        return ToolResult.success("Cancelled the scheduled shutdown or restart.")
    if args.action == "sign_out":
        winapi.run_shutdown("sign_out", 0)
        return ToolResult.success("Signing out.")
    verb = "Restarting" if args.action == "restart" else "Shutting down"
    note = ""
    if args.save_work or ctx.settings.save_before_power:
        saved, need_you = save_open_work()
        if need_you:
            names = "; ".join(need_you[:3])
            return ToolResult.failure(
                "unsaved_work", f"I didn't {args.action.replace('shutdown', 'shut down')} because this still needs "
                f"you: {names}. Save it (it may need a file name), then ask again.", saved=saved, need_you=need_you)
        note = f" Saved {len(saved)} window(s) first." if saved else " Nothing needed saving."
    _schedule_power(args.action, args.delay_seconds)
    if args.delay_seconds:
        return ToolResult.success(f"{verb} in {args.delay_seconds} seconds.{note} Say 'cancel' or press Esc to stop it.",
                                  delay_seconds=args.delay_seconds)
    return ToolResult.success(f"{verb} now.{note}")


# ---------------------------------------------------------------------- brightness


class BrightnessArgs(ToolArgs):
    action: Literal["get", "set", "up", "down", "check"] = Field(
        description="What to do with the screen brightness; check = why it can or can't be changed on this PC")
    level: int | None = Field(default=None, ge=0, le=100, description="Target percent for action 'set'")
    step: clamped_int(1, 50, 10, "Percent to change for 'up'/'down'")  # type: ignore[valid-type]


_NO_BRIGHTNESS = ("I can't change the brightness of this screen from here: the monitor doesn't take brightness "
                  "commands (turn on DDC/CI in its own menu) and there is no Jarvis window to dim it in software.")
SOFT = {"level": 100}  # the software dimmer's level (100 = no veil); kept while Jarvis runs


def _soft(ctx: ToolContext, level: int) -> bool:
    dim = getattr(ctx, "dim_screen", None)
    if dim is None:
        return False
    try:
        done = bool(dim(level))
    except Exception:
        _log.warning("software dimming failed", exc_info=True)
        return False
    if done:
        SOFT["level"] = level
    return done


DDC_HELP = ("To let me change the monitor itself: open the monitor's own menu (the buttons on the monitor) -> "
            "Settings / System / Other -> DDC/CI -> On. If it's already on, connect the monitor straight to the PC "
            "with DisplayPort or HDMI (docks, USB-C hubs and adapters often block it), and update the graphics "
            "driver. Then say 'check brightness'.")


def brightness_check() -> ToolResult:
    report = winapi.brightness_report()
    parts = []
    if report["laptop_screen"]:
        parts.append("the laptop screen can be set directly")
    for monitor in report["monitors"]:
        state = ("takes brightness commands" if monitor["ddc"] or monitor["vcp"] else
                 "answers but refuses brightness" if monitor["capabilities"] else "doesn't answer (DDC/CI off or blocked)")
        parts.append(f"{monitor['name']}: {state}")
    if not report["monitors"] and not report["laptop_screen"]:
        parts.append("Windows reports no screen whose brightness software can change")
    works = report["laptop_screen"] or any(m["ddc"] or m["vcp"] for m in report["monitors"])
    message = "Brightness check: " + "; ".join(parts) + "." + ("" if works else " " + DDC_HELP)
    return ToolResult.success(message, **report, hardware=works)


def brightness_control(args: BrightnessArgs, ctx: ToolContext) -> ToolResult:
    winapi.require_windows()
    if args.action == "check":
        return brightness_check()
    hardware = winapi.get_brightness()
    soft = SOFT["level"]
    current = soft if soft < 100 else (hardware if hardware is not None else 100)  # what you see now
    if args.action == "get":
        if hardware is None and soft >= 100 and getattr(ctx, "dim_screen", None) is None:
            return ToolResult.failure("unsupported", _NO_BRIGHTNESS)
        if hardware is None:
            return ToolResult.success(f"Brightness is {soft}% (I dim this screen in software; the monitor doesn't "
                                      "report its own level).", brightness=soft, software=True)
        return ToolResult.success(f"Brightness is {current}%.", brightness=current)
    if args.action == "set":
        if args.level is None:
            raise ToolError("Which brightness level (0-100)?", "missing_level")
        target = args.level
    else:
        target = current + args.step if args.action == "up" else current - args.step
    target = max(0, min(100, target))
    previous = {"previous": current} if current is not None else {}
    if hardware is not None and winapi.set_brightness(target) > 0:
        if soft < 100:
            _soft(ctx, 100)  # the monitor does it now: lift the veil
        time.sleep(0.3)
        now = winapi.get_brightness()
        log_event(_log, "brightness_set", level=target)
        if now is not None and abs(now - target) > 6:  # screens have steps; a small difference is the nearest step
            return ToolResult.failure("not_verified", f"I tried to set the brightness to {target}%, but it is "
                                      f"{now}%.", brightness=now, **previous)
        return ToolResult.success(f"Brightness set to {target}%.", brightness=target, **previous)
    if hardware is None and winapi.set_brightness(target) > 0:  # read failed but writing worked
        log_event(_log, "brightness_set", level=target, verified=False)
        return ToolResult.success(f"Brightness set to {target}% (this monitor doesn't report it back, so I "
                                  "can't double-check).", brightness=target, **previous)
    if _soft(ctx, target):
        log_event(_log, "brightness_set", level=target, software=True)
        note = "" if target < 100 else " That's as bright as the monitor's own setting goes."
        return ToolResult.success(f"Brightness set to {target}% by dimming the picture in software: your monitor "
                                  "doesn't take brightness commands right now (say 'check brightness' to see why "
                                  f"and how to fix it).{note}", brightness=target, software=True, **previous)
    return ToolResult.failure("unsupported", _NO_BRIGHTNESS)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="volume_control",
        description="Get or change the system volume: set a level, turn it up or down, mute or unmute.",
        handler=volume_control, args_model=VolumeArgs, level=RiskLevel.SAFE, category="windows", timeout=10,
    ),
    Tool(
        name="brightness_control",
        description="Get or change the screen brightness: set a level, turn it up (brighter) or down (dimmer).",
        handler=brightness_control, args_model=BrightnessArgs, level=RiskLevel.SAFE, category="windows", timeout=15,
    ),
    Tool(
        name="power_control",
        description="Lock the PC (shows the Windows lock screen: 'lock my PC/laptop/screen'), sleep, sign out, restart "
                    "or shut down the COMPUTER (save_work=true for 'save and restart'), or cancel a restart/shutdown. Not for restarting Jarvis itself (jarvis_control).",
        handler=power_control, args_model=PowerArgs, level=RiskLevel.SAFE, category="windows", timeout=30,
        risk=lambda a, _: _POWER_LEVELS[a.action], confirmation=_power_question,
        direct_without_confirmation=_direct_power_ok,
    ),
)
