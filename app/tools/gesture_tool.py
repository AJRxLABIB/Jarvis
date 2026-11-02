"""Hand-gesture control (rules in app/gesture): explicit opt-in, camera only while on, never saves frames, always stoppable.

The LLM can turn it on/off and change settings, but recognition and mouse control run without it (app/gesture/service.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

GESTURES = {
    "INDEX_POINT": "Point with the index finger: the cursor follows the fingertip.",
    "PINCH": "Touch thumb and index finger: a click (hold longer than ~0.45 s and move to drag, when drag is on).",
    "OPEN_PALM": "Show an open palm for about one second: control pauses (say 'resume gestures' or press Resume).",
    "TWO_HAND_ZOOM": "Pinch with BOTH hands and move them apart to zoom in, together to zoom out (Ctrl + wheel: browsers, Office, maps, photos).",
    "SWIPE": "Sweep an open palm quickly left or right: back / forward, next desktop, or slides - choose with the swipe setting (off by default).",
    "DWELL": "Optional (dwell_click on): hold your fingertip still for about a second to click, with no pinch needed.",
    "TWO_FINGER_SCROLL": "Index and middle fingers up: move your hand up or down to scroll.",
    "RIGHT_CLICK": "Optional (right_click on): touch thumb and middle finger (index not pinching) for a right click.",
    "FIST": "Optional drag mode (off by default): a closed fist holds the left button.",
}


class GestureArgs(ToolArgs):
    action: Literal["enable", "practice", "disable", "pause", "resume", "status", "calibrate", "settings", "list_gestures", "map", "unmap",
                    "diagnostic", "profile", "profiles", "macros", "benchmark"] = Field(
        default="status",
        description="enable = turn on camera hand control of the mouse (asks first); practice = show gestures without clicking anything; "
                    "disable / pause / resume; status; calibrate = set the active camera area; settings = change an option; "
                    "list_gestures; map / unmap = bind a trigger gesture (SWIPE_LEFT, SWIPE_RIGHT, FIST) to an approved tool or saved workflow "
                    "(macro); macros = list them; profile = switch use case (general, browsing, presentation, media, precision, "
                    "accessibility); profiles = list them; benchmark = run the repeatable synthetic accuracy / false-activation suite and compare with the saved baseline (value=baseline saves this run as the baseline); "
                    "diagnostic = check the camera, model and detector")
    top_left_x: float = Field(default=0.15, ge=0, le=1, description="For calibrate: left edge of the camera area that covers the screen (0-1)")
    top_left_y: float = Field(default=0.15, ge=0, le=1, description="For calibrate: top edge of that area (0-1)")
    bottom_right_x: float = Field(default=0.85, ge=0, le=1, description="For calibrate: right edge of that area (0-1)")
    bottom_right_y: float = Field(default=0.85, ge=0, le=1, description="For calibrate: bottom edge of that area (0-1)")
    setting: Literal["", "reach", "smooth", "zoom", "swipe", "dwell_click", "smoothing", "mirror", "drag", "fist_drag", "double_click", "right_click", "fps", "max_hands", "monitor", "preview", "auto_profile"] = Field(
        default="", description="For settings: which option to change")
    value: str = Field(default="", max_length=20, description="For settings: the new value (a number or on/off)")
    gesture: str = Field(default="", max_length=30, description="For map / unmap: the gesture name (see list_gestures)")
    tool: str = Field(default="", max_length=60, description="For map: the registered tool to run (never a shell command)")
    confirm: bool = Field(default=True, description="For map: ask for approval each time the gesture runs the tool")
    workflow: str = Field(default="", max_length=80, description="For map: a saved workflow to run (uses run_workflow); leave tool empty")
    profile: str = Field(default="", max_length=20, description="For profile: the profile name. For map: only active in this profile ('' = all)")
    hold: float = Field(default=-1, ge=-1, le=5, description="For map: seconds the gesture must be held (-1 = the default for that gesture)")


def _parse(setting: str, value: str):
    v = value.strip().lower()
    if setting == "reach":
        if v not in ("small", "medium", "large"):
            raise ToolError("Reach is small, medium or large.", "bad_value")
        return v
    if setting == "swipe":
        if v not in ("off", "browser", "desktops", "slides"):
            raise ToolError("Swipe is off, browser (back/forward), desktops or slides.", "bad_value")
        return v
    if setting == "smooth":
        if v not in ("smoother", "balanced", "quicker"):
            raise ToolError("Smoothness is smoother, balanced or quicker.", "bad_value")
        return v
    if setting in ("mirror", "drag", "fist_drag", "double_click", "right_click", "preview", "zoom", "dwell_click", "auto_profile"):
        if v not in ("on", "off", "true", "false", "yes", "no"):
            raise ToolError("Use on or off.", "bad_value")
        return v in ("on", "true", "yes")
    try:
        number = float(v)
    except ValueError as exc:
        raise ToolError("That option needs a number.", "bad_value") from exc
    return int(number) if setting in ("max_hands", "monitor") else number


def _detector_factory(ctx: ToolContext):
    def make(settings):
        from app.gesture.detector import MediaPipeDetector

        return MediaPipeDetector(ctx.paths.models_dir, max_hands=settings.max_hands, detection_confidence=settings.detection_confidence)

    return make


def _wire(ctx: ToolContext):
    import asyncio

    from app.gesture.service import SERVICE

    if SERVICE.detector_factory is None:
        SERVICE.detector_factory = _detector_factory(ctx)
    if SERVICE.store is None and ctx.paths is not None:
        from app.gesture.macros import MacroStore

        SERVICE.store = MacroStore(ctx.paths.data_dir / "gesture_macros.json")
        SERVICE.load_store()
    if SERVICE.limits_provider is None:
        def limits() -> tuple[float, int]:
            from app.core import perf

            plan = perf.current_plan(ctx.settings)
            return plan.gesture_fps, plan.gesture_hands

        SERVICE.limits_provider = limits
    if SERVICE.foreground_probe is None:
        def foreground() -> str:
            from app.tools import winapi

            if not winapi.is_windows():
                return ""
            import psutil
            import win32process

            _, pid = win32process.GetWindowThreadProcessId(winapi.foreground_window())
            return psutil.Process(pid).name().lower()

        SERVICE.foreground_probe = foreground
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and ctx.executor is not None:
        # the camera thread is not the event loop: hand the call over to the loop that owns the executor (the old code tried to
        # start a task from the camera thread, which fails - a mapped gesture never ran)
        def run(tool: str, args: dict, confirm: bool) -> None:
            from app.ai.base import ToolCall

            coro = ctx.executor.execute(ToolCall(tool, args), user_command=False)
            try:
                asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                coro.close()

        SERVICE.run_action = run
    return SERVICE


async def gesture_control(args: GestureArgs, ctx: ToolContext) -> ToolResult:
    from app.core import emergency
    from app.gesture.service import SERVICE

    a = args.action
    if a == "list_gestures":
        return ToolResult.success("\n".join(f"{k}: {v}" for k, v in GESTURES.items()), gestures=list(GESTURES))
    if a == "diagnostic":
        return ToolResult.success("\n".join(diagnostic(ctx)))
    if a == "status":
        st = SERVICE.status()
        return ToolResult.success(f"Gesture control: {st['state']} ({st['reason']}). Camera: {st['camera']}.", **st)
    if a in ("enable", "practice"):
        if not ctx.settings.gesture_control:
            # Confirming the on-screen question IS the consent: turn the permission on for you (it was off by default, nothing ran before this).
            apply = getattr(ctx, "apply_setting", None)
            if apply is None:
                raise ToolError("Gesture control is off. Turn on 'Gesture control' in Settings > Privacy first.", "disabled")
            apply("gesture_control", "true")
        if emergency.STATE.flags.get("locked", False):
            raise ToolError("Jarvis is locked (emergency controls); gesture control cannot start.", "locked")
        from app.gesture.detector import availability

        ok, why = availability(ctx.paths.models_dir)
        if not ok:
            raise ToolError(f"Hand tracking is not ready: {why}", "not_ready")
        _wire(ctx)
        return ToolResult.success(SERVICE.enable(practice=a == "practice") + " The camera indicator stays on while it runs; say 'stop gestures' or press Stop at any time.")
    if a == "disable":
        return ToolResult.success(SERVICE.disable())
    if a == "pause":
        if not SERVICE.running:
            return ToolResult.success("Gesture control is off, so there is nothing to pause.")
        SERVICE.pause()
        return ToolResult.success("Gesture control paused. The mouse is released.")
    if a == "resume":
        if SERVICE.resume():
            return ToolResult.success("Gesture control resumed.")
        if SERVICE.running:        # nothing to resume is not an error (it used to be logged as a failure)
            return ToolResult.success("Gesture control was not paused: it is already running.")
        return ToolResult.success("Gesture control is off. Say 'gesture control on' to start it.")
    if a == "calibrate":
        if not SERVICE.calibrate((args.top_left_x, args.top_left_y), (args.bottom_right_x, args.bottom_right_y)):
            raise ToolError("That area is too small or inverted (it must cover at least 20% of the camera view).", "bad_area")
        return ToolResult.success("Calibration saved. It applies the next time gesture control starts.", area=SERVICE.settings.calibration)
    if a == "settings":
        if not args.setting:
            return ToolResult.success("Options: " + ", ".join(vars(SERVICE.settings)), **vars(SERVICE.settings))
        SERVICE.configure(**{args.setting: _parse(args.setting, args.value)})
        return ToolResult.success(f"{args.setting} = {getattr(SERVICE.settings, args.setting)}. Applies the next time gesture control starts.")
    if a == "benchmark":
        from app.gesture import metrics

        now = metrics.run_suite()
        path = ctx.paths.data_dir / "gesture_baseline.json" if ctx.paths is not None else None
        base = metrics.load(path) if path else None
        if args.value == "baseline" and path is not None:
            metrics.save(path, now)
        problems = metrics.compare(base, now) if base else []
        verdict = ("saved as the new baseline" if args.value == "baseline" else
                   "no baseline yet (say value=baseline to save this run)" if base is None else
                   "no regression against the baseline" if not problems else "REGRESSION: " + "; ".join(problems))
        return ToolResult.success(
            f"Synthetic benchmark ({now['scenarios']} scenarios, seeded jitter): accuracy {now['recognition_accuracy']:.0%}, false activations "
            f"{now['false_activations']}, missed {now['missed']}, stuck buttons {now['stuck_buttons']}, {now['ms_per_frame_median']} ms/frame "
            f"(logic only). {verdict}. This uses generated hands - real-hand accuracy comes from --validate gesture on this PC.",
            **{k: v for k, v in now.items() if k != "failures"}, regression=bool(problems))
    if a == "profiles":
        from app.gesture import profiles

        return ToolResult.success("Profiles: " + "; ".join(profiles.describe(n) for n in profiles.PROFILES) +
                                  f" Now: {SERVICE.profile}.", profile=SERVICE.profile)
    if a == "profile":
        if not args.profile:
            note = f" ({SERVICE.suggested} is suggested for the app in front)" if SERVICE.suggested else ""
            return ToolResult.success(f"Profile: {SERVICE.profile}.{note}", profile=SERVICE.profile, suggested=SERVICE.suggested)
        try:
            text = SERVICE.apply_profile(args.profile.strip().lower())
        except ValueError as exc:
            raise ToolError(str(exc), "bad_profile") from exc
        return ToolResult.success(f"Profile switched. {text}", profile=SERVICE.profile)
    if a == "macros":
        items = SERVICE.mapping.items
        if not items:
            return ToolResult.success("No gesture macros. Bind one with: gesture_control map (gesture SWIPE_LEFT / SWIPE_RIGHT / FIST + a tool or workflow).")
        lines = [f"{g}: {m['tool']}" + (f" {m['args']}" if m["args"] else "") + f" [{'all profiles' if m['profile'] == '*' else m['profile']}"
                 + (f", hold {m['hold_s']:g}s" if m["hold_s"] else "") + ("" if m["enabled"] else ", OFF") + "]" for g, m in items.items()]
        return ToolResult.success("Gesture macros: " + "; ".join(lines), macros=list(items))
    if a in ("map", "unmap"):
        trigger = args.gesture.strip().upper()
        if trigger not in SERVICE.mapping.TRIGGERS:
            raise ToolError("Which gesture? " + ", ".join(SERVICE.mapping.TRIGGERS) + " (the others already move the mouse or pause).", "bad_gesture")
        if a == "unmap":
            removed = SERVICE.mapping.remove(trigger)
            SERVICE.save_store()
            SERVICE._sync_macros()
            return ToolResult.success("Macro removed." if removed else "That gesture had no macro.")
        tool, margs = (args.tool, {}) if args.tool else (("run_workflow", {"name": args.workflow}) if args.workflow else ("", {}))
        if not tool:
            raise ToolError("Map to what: a tool, or a saved workflow?", "bad_mapping")
        level = 0
        if ctx.executor is not None:
            registry = getattr(ctx.executor, "registry", None)
            SERVICE.mapping.known = registry               # supports `name in registry`
            found = registry.get(tool) if registry is not None else None
            level = int(found.level) if found is not None else 0
        profile = args.profile.strip().lower() or "*"
        from app.gesture import profiles

        if profile != "*" and profile not in profiles.PROFILES:
            raise ToolError("Profiles: " + ", ".join(profiles.PROFILES), "bad_profile")
        try:
            SERVICE.mapping.add(trigger, tool, margs, confirm=args.confirm, profile=profile, level=level,
                                hold_s=None if args.hold < 0 else args.hold)
        except ValueError as exc:
            raise ToolError(str(exc), "bad_mapping") from exc
        SERVICE.mapping.items[trigger]["level"] = level
        SERVICE.save_store()
        SERVICE._sync_macros()
        from app.gesture.macros import preview

        item = SERVICE.mapping.items[trigger]
        return ToolResult.success(preview(trigger, tool, margs, level=level, profile=profile, hold_s=item["hold_s"],
                                          cooldown_s=item["cooldown_s"]) + " It goes through the normal permission checks; "
                                  "try it first in practice mode (nothing runs there).")
    raise ToolError("Unknown action.", "bad_action")


def _risk(args: GestureArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.MODERATE if args.action in ("enable", "map") else RiskLevel.SAFE


def _question(args: GestureArgs, _ctx: ToolContext) -> str:
    if args.action == "map":
        return f"Let the {args.gesture} gesture run '{args.tool or args.workflow}'?"
    return ("Turn on hand-gesture control? It uses the webcam and moves the mouse. Frames stay on this PC and are never saved; "
            "Stop or 'stop gestures' ends it. This also switches the 'Gesture control' setting on.")


TOOLS: tuple[Tool, ...] = (
    Tool("gesture_control", "Control the mouse with hand gestures from the webcam: enable/disable/pause/resume, status, calibrate, settings, "
         "list gestures, map a gesture to a tool, diagnostic. Opt-in, camera only while on, local only, emergency stop always works.",
         gesture_control, GestureArgs, RiskLevel.SAFE, "system", timeout=30, risk=_risk, confirmation=_question),
)
