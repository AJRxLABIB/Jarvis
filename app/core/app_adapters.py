"""Application adapters: one consistent interface over how each desktop program can be driven, built on ``app_profiles`` (data) - no second knowledge base.

Per application the adapter answers six questions:
``detect_application`` (which program is this process/window) - ``inspect_application`` (how well does it expose controls, what to prefer) -
``get_available_actions`` (what Jarvis knows how to do in it) - ``execute_action`` (do it by the most reliable route) - ``verify_action`` (did it happen) -
``recover_from_error`` (what to do when it did not).

Route order, most reliable first: STRUCTURED (the program's own API-style tool: ``vscode``, ``excel_control``, ``office_info``, ``browser_*``, ``find_my_files``)
-> CONTROL (Windows UI Automation press by name, with this app's other labels, ``press_control``) -> SHORTCUT (offered as text, never pressed silently).
Coordinates are never used here. Execution and verification are injected callables, so the adapter is testable without Windows and every real step still goes
through the normal tool executor (permissions, approvals, audit). Dialogs where a wrong press loses work are never auto-answered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core import app_profiles as P

STRUCTURED, CONTROL, SHORTCUT, NONE = "structured", "control", "shortcut", "none"

# process-family -> structured tools that exist in the registry (checked by a test) and what they cover
STRUCTURED_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "VS Code": (("vscode", "open files/folders, diffs, extensions"),),
    "Excel": (("excel_control", "read/write cells and sheets"), ("office_info", "read workbook info")),
    "Word": (("office_info", "read document info"),),
    "PowerPoint": (("office_info", "read presentation info"),),
    "Chrome / Edge": (("browser_open", "open a page"), ("browser_read", "read page text"), ("browser_click", "click page elements"),
                      ("browser_type", "type into page fields"), ("browser_tabs", "list/switch tabs"), ("browser_back", "go back")),
    "File Explorer": (("find_my_files", "search files"),),
}


@dataclass
class Inspection:
    app: str
    process: str
    uia: str
    structured: list[str]
    advice: str
    risky_dialogs: tuple[str, ...]


@dataclass
class ActionPlan:
    app: str
    action: str
    route: str                       # structured | control | shortcut | none
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    shortcut: str = ""
    note: str = ""


@dataclass
class Outcome:
    ok: bool
    verified: bool
    message: str
    plan: ActionPlan | None = None


class ApplicationAdapter:
    """Adapter for one profiled application; ``generic()`` covers programs with no profile."""

    def __init__(self, profile: P.Profile | None, process: str = "") -> None:
        self.profile, self.process = profile, process.lower().removesuffix(".exe")

    # -- detect / inspect
    @staticmethod
    def detect_application(process: str) -> "ApplicationAdapter":
        return ApplicationAdapter(P.profile_for(process), process)

    @property
    def name(self) -> str:
        return self.profile.name if self.profile else (self.process or "unknown application")

    def inspect_application(self) -> Inspection:
        if not self.profile:
            return Inspection(self.name, self.process, "unknown", [], "No profile for this program: Jarvis reads its controls by name with UI Automation and says so when it cannot find one.", ())
        tools = [t for t, _ in STRUCTURED_TOOLS.get(self.profile.name, ())]
        return Inspection(self.profile.name, self.process, self.profile.uia, tools, self.profile.note, self.profile.risky_dialogs)

    # -- what is possible
    def get_available_actions(self) -> list[str]:
        if not self.profile:
            return []
        return sorted(set(self.profile.labels) | set(self.profile.shortcuts))

    # -- plan + execute
    def plan_action(self, action: str, **args: Any) -> ActionPlan:
        key = " ".join(action.lower().split())
        if not self.profile:
            return ActionPlan(self.name, key, CONTROL, "press_control", {"name": action}, note="no profile: pressing by on-screen name")
        structured = STRUCTURED_TOOLS.get(self.profile.name, ())
        wanted = args.get("tool")
        for tool, _ in structured:
            if wanted == tool:
                return ActionPlan(self.name, key, STRUCTURED, tool, {k: v for k, v in args.items() if k != "tool"})
        if self.profile.uia != "poor" and (key in self.profile.labels or self.profile.uia == "good"):
            return ActionPlan(self.name, key, CONTROL, "press_control", {"name": action}, P.shortcut(self.process, key),
                              note=f"labels tried: {', '.join(self.profile.labels.get(key, ())) or action}")
        sc = P.shortcut(self.process, key)
        if sc:
            return ActionPlan(self.name, key, SHORTCUT, "", {}, sc, f"{self.name} hides its controls ({self.profile.uia} UI Automation); shortcut offered, not pressed")
        return ActionPlan(self.name, key, NONE, note=P.advice(self.process, key) or "Jarvis has no safe route for this in this program.")

    async def execute_action(self, action: str, run_tool: Callable[[str, dict], Awaitable[Any]], verify: Callable[[ActionPlan], Awaitable[bool]] | None = None, **args: Any) -> Outcome:
        plan = self.plan_action(action, **args)
        if plan.route in (SHORTCUT, NONE):
            return Outcome(False, False, plan.note + (f" Shortcut: {plan.shortcut}." if plan.shortcut else ""), plan)
        try:
            await run_tool(plan.tool, plan.args)
        except Exception as exc:                                  # never a false success
            return Outcome(False, False, self.recover_from_error(action, str(exc)), plan)
        ok = await self.verify_action(plan, verify)
        return Outcome(ok is not False, ok is True, "done and verified" if ok else ("done, but the result could not be verified" if ok is None else "the action ran but the check did not see the result"), plan)

    async def verify_action(self, plan: ActionPlan, verify: Callable[[ActionPlan], Awaitable[bool]] | None) -> bool | None:
        """True = seen, False = not seen, None = no verifier available (reported as unverified, never as success)."""
        if verify is None:
            return None
        try:
            return bool(await verify(plan))
        except Exception:
            return None

    def recover_from_error(self, action: str, error: str = "", dialog_text: str = "") -> str:
        if dialog_text and P.risky_dialog(self.process, dialog_text):
            return f"{self.name} is asking something that can lose work ('{dialog_text[:60]}'). I will not answer it myself - please choose."
        alts = P.alternatives(self.process, action)
        sc = P.shortcut(self.process, action)
        steps = []
        if alts:
            steps.append("try the other labels this app uses: " + ", ".join(alts))
        if sc:
            steps.append(f"or use the shortcut {sc} if you say so")
        if self.profile and self.profile.uia != "good":
            steps.append(self.profile.note or "this app exposes few controls; use its own tool")
        return f"'{action}' failed in {self.name}" + (f" ({error[:80]})" if error else "") + (". " + "; ".join(steps) + "." if steps else ". Check that the window is open and in front, then retry.")
