"""Tool definition types.

A tool is: a name, a description for the model, a strict pydantic arguments
model, a risk level, and a handler. Handlers may be sync (run in a worker
thread) or async. Arguments always come from the model and are untrusted: they
are validated against ``args_model`` (extra fields forbidden) before the
handler sees them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.security.permissions import RiskLevel

if TYPE_CHECKING:
    from app.config.files import ConfigBundle
    from app.config.settings import Settings
    from app.memory.database import Database
    from app.utils.paths import AppPaths

MAX_RESULT_CHARS = 4000


_EMPTY_VALUES = frozenset({"", "none", "null", "n/a", "undefined"})


class ToolArgs(BaseModel):
    """Base class for tool arguments: unknown fields are rejected.

    Small models often fill optional fields with placeholders ("", "none",
    "null"); for fields whose default is None those simply mean "not given".
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _placeholders_mean_unset(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key, value in data.items():
            field = cls.model_fields.get(key)
            if field is None or field.is_required():
                continue
            if isinstance(value, str) and value.strip().lower() in _EMPTY_VALUES and field.annotation is not str:
                if field.default is None:
                    cleaned[key] = None
                else:
                    del cleaned[key]  # delay_seconds='null' -> the default
        return cleaned


class NoArgs(ToolArgs):
    pass


def clamped_int(low: int, high: int, default: int, description: str = "") -> Any:
    """An int field where out-of-range values are pulled into range instead of rejected.

    Only for harmless amounts (how many results, a volume step). Never use it for
    values that identify a target (a process id, a path), where "nearest valid
    value" would mean acting on something else.
    """

    def clamp(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return value  # let normal validation report it
        return max(low, min(high, number))

    # Constraints must come before the validator, or the JSON schema shows
    # non-standard "ge"/"le" instead of "minimum"/"maximum".
    return Annotated[int, Field(default=default, ge=low, le=high, description=description), BeforeValidator(clamp)]


class ToolError(Exception):
    """Raised by handlers for expected failures; the message is shown to the user."""

    def __init__(self, message: str, code: str = "failed", **data: Any) -> None:
        super().__init__(message)
        self.code = code
        self.data = data  # extra facts for the result, e.g. a "did you mean" suggestion


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str = ""  # short human-readable summary
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None  # machine-readable code when ok is False

    @classmethod
    def success(cls, message: str = "", **data: Any) -> "ToolResult":
        return cls(True, message, data)

    @classmethod
    def failure(cls, error: str, message: str, **data: Any) -> "ToolResult":
        return cls(False, message, data, error)

    def to_model_text(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.message:
            payload["message"] = self.message
        payload.update(self.data)
        if self.error:
            payload["error"] = self.error
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return text if len(text) <= MAX_RESULT_CHARS else text[: MAX_RESULT_CHARS - 20] + '..."(truncated)"}'


@dataclass
class ToolContext:
    settings: "Settings"
    paths: "AppPaths | None" = None
    database: "Database | None" = None
    config: "ConfigBundle | None" = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    path_policy: Any = None  # app.security.validator.PathPolicy, built on first use
    memory: Any = None  # app.memory.manager.MemoryManager, built on first use (needs a database)
    browser: Any = None  # app.browser.playwright_manager.BrowserSession, created on first use
    on_browser_active: Callable[[bool], None] | None = None  # privacy indicator
    executor: Any = None  # the ToolExecutor using this context (set by the executor)
    scheduler: Any = None  # app.automation.scheduler.Scheduler, when the host runs one
    notify: Callable[[str, str], None] | None = None  # (title, message) -> shown/spoken by the host
    on_screen_capture: Callable[[bool], None] | None = None  # privacy indicator (SCREEN CAPTURE ACTIVE)
    ai: Any = None  # app.ai.manager.AIManager, for tools that ask a vision model
    conversation: Any = None  # the ConversationManager using these tools ("where did that answer come from?")
    notes: Callable[[str], str] | None = None  # meeting notes: start / stop / status (set by the window)
    jarvis_control: Callable[[str], str] | None = None  # Jarvis's own screens/features by voice (set by the window)
    apply_setting: Callable[[str, str], None] | None = None  # change a setting by voice (set by the window)
    dictation: Callable[[str], str] | None = None  # dictation start / stop (set by the window)
    dim_screen: Callable[[int], bool] | None = None  # software screen dimming, percent (100 = off; set by the window)
    step_aside: Callable[[], None] | None = None  # Jarvis's window gets out of the way of your app (blocking)
    focus: Callable[[str, int, bool], str] | None = None  # focus mode start / stop / status (set by the window)
    mode_control: Callable[[str], str] | None = None  # modes (set by the window)
    emergency: Callable[[str], str] | None = None  # emergency controls by voice: off only (set by the window)
    agent_contract: Any = None  # app.core.agents.AgentContract: a role-scoped limit the current task runs under

    def get_path_policy(self) -> Any:
        if self.path_policy is None:
            from app.security.validator import PathPolicy

            self.path_policy = PathPolicy.from_context(self)
        return self.path_policy

    def get_browser(self) -> Any:
        if self.browser is None:
            from app.browser.playwright_manager import BrowserSession

            data = self.paths.data_dir if self.paths is not None else Path.home() / ".jarvis"
            s = self.settings
            self.browser = BrowserSession(
                data / "browser-profile", channel=s.browser_channel, headless=s.browser_headless,
                idle_minutes=s.browser_idle_minutes,
                on_active_change=lambda active: self.on_browser_active and self.on_browser_active(active),
            )
        return self.browser

    async def aclose(self) -> None:
        """Release resources opened by tools (the browser)."""
        if self.browser is not None:
            await self.browser.close()

    def get_tool_registry(self) -> Any:
        if self.executor is None:
            from app.tools.catalog import build_default_registry

            return build_default_registry()
        return self.executor.registry

    def get_automation_store(self) -> Any:
        if self.database is None:
            return None
        from app.automation.tasks import AutomationStore

        return AutomationStore(self.database)

    def get_scheduler(self) -> Any:
        """The host's scheduler, or a temporary one that runs steps through this context's executor."""
        if self.scheduler is not None:
            return self.scheduler
        if self.executor is None:
            raise ToolError("Workflows can't run here (no tool executor).", "unavailable")
        from app.automation.scheduler import Scheduler

        return Scheduler(self.get_automation_store(), self.executor,
                         self.notify or (lambda title, message: None), clock=self.clock)

    def get_memory(self) -> Any:
        if self.memory is None and self.database is not None:
            from app.memory.manager import MemoryManager

            self.memory = MemoryManager(self.database)
        return self.memory


Handler = Callable[[Any, ToolContext], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Handler
    args_model: type[ToolArgs] = NoArgs
    level: RiskLevel = RiskLevel.SAFE
    category: str = "general"
    timeout: float = 15.0
    needs_network: bool = False
    # Optional: validate arguments against the real world (e.g. "does the file exist?")
    # before permissions are evaluated. Raises ToolError -> fails without asking the user.
    precheck: Callable[[Any, "ToolContext"], None] | None = None
    # Optional: raise the level based on the concrete arguments (never lowers it).
    risk: Callable[[Any, "ToolContext"], RiskLevel] | None = None
    # Optional: the question shown when confirmation is needed.
    confirmation: Callable[[Any, "ToolContext"], str] | None = None
    # Optional: True when THE USER'S OWN direct command ("restart my PC", matched without the AI model)
    # may skip the confirmation dialog. Never used for calls the model makes.
    direct_without_confirmation: Callable[[Any, "ToolContext"], bool] | None = None
    # Optional: check that the action really happened (the file exists now, the volume changed...).
    # Returns (ok, what was found). A failed check turns the result into "not_verified": never claim unchecked success.
    verify: Callable[[Any, "ToolResult", "ToolContext"], tuple[bool, str]] | None = None

    def level_for(self, args: Any, ctx: "ToolContext") -> RiskLevel:
        if self.risk is None:
            return self.level
        return RiskLevel(max(self.level, self.risk(args, ctx)))

    def confirmation_text(self, args: Any, ctx: "ToolContext") -> str:
        if self.confirmation is not None:
            return self.confirmation(args, ctx)
        shown = ", ".join(f"{k}={v!r}" for k, v in args.model_dump().items())
        return f"Allow '{self.name}'" + (f" with {shown}?" if shown else "?")
