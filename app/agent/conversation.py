"""Conversation manager: one user message in, one assistant reply out.

With a ``ToolExecutor`` the manager runs the agent loop: the model may request
tools over several rounds; every request goes through the executor (validation,
permissions, confirmation). Limits: ``MAX_TOOL_ROUNDS`` rounds and
``MAX_TOOL_CALLS`` calls per user message; an identical call is never executed
twice in one turn. All AI errors become short messages suitable for speaking;
technical detail goes to the log.
"""

from __future__ import annotations

from app.utils.timefmt import time12

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.agent.context import ConversationContext
from app.ai.base import (
    AIError,
    AIRequestError,
    AITimeoutError,
    ChatMessage,
    ChatStats,
    ModelCapabilityError,
    ModelNotFoundError,
    NoModelAvailableError,
    ProviderUnavailableError,
    ToolCall,
    ToolSpec,
)
from app.agent.tool_manager import ToolExecution, ToolExecutor
from app.agent.language import detect_language, localize, misheard_bangla_command, to_english_command
from app.agent.shortcuts import RESUME, SAVE_SETUP, match_shortcut, split_commands
from app.agent.tool_router import select_specs
from app.ai.manager import AIManager
from app.ai.prompts import build_system_prompt
from app.config.settings import Settings
from app.memory.conversations import ConversationStore
from app.tools.base import ToolResult
from app.utils import network
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("conversation")
MAX_USER_CHARS = 8000
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8
REPLY_RESERVE = 1024  # tokens kept free for the answer
TASK_ROUNDS, TASK_CALLS = 10, 20  # multi-step tasks ("research ... and make a document")
_TASK = re.compile(r"\b(research|compare|find out|look up .* and|make (?:a|an|me a) (?:document|report|list|summary|table)|"
                   r"write (?:a|an|me a) (?:document|report|summary)|create (?:a|an) (?:document|report)|step by step|"
                   r"and then|then save|and save|put (?:it|them) in a (?:document|file))\b|গবেষণা|তুলনা করে|ডকুমেন্ট বানাও", re.I)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u263A\u2764\uFE0F\u200D]")  # not the ✓ / ⚠ signs tools use
_EMOJI_ASKED = re.compile(r"\bemoji", re.I)


def wants_emoji(text: str) -> bool:
    return bool(_EMOJI_ASKED.search(text or ""))


def strip_emoji(text: str, *, trim: bool = True) -> str:
    """The small model adds emoji although the prompt says not to; they get read out and look out of place."""
    cleaned = _EMOJI.sub("", text or "")
    if not trim:
        return cleaned
    return re.sub(r"[ \t]+\n", "\n", re.sub(r" {2,}", " ", cleaned)).strip()


def is_task(text: str) -> bool:
    """A request that needs several steps: more tool rounds, and the model states its plan first."""
    return bool(_TASK.search(text)) and len(text.split()) >= 5


_MORE = re.compile(r"\b(?:tell me more|more detail(?:s|ed)?|in detail|explain (?:more|that|it|in detail|further)|go on|"
                   r"elaborate|keep going|say more|the long version|expand on)\b|বিস্তারিত|আরও বলো|আরো বলো|আরেকটু বলো",
                   re.I)
_SHORT = re.compile(r"\b(?:in (?:one|a) (?:line|sentence|word)|one[- ]liner|shorter|briefly|in short|short answer|"
                    r"tl;?dr|just the answer)\b|সংক্ষেপে|এক কথায়|ছোট করে", re.I)


def answer_detail(text: str) -> str:
    """'long' for "tell me more", 'short' for "in one line", '' otherwise."""
    if not text:
        return ""
    if _SHORT.search(text):
        return "short"
    return "long" if _MORE.search(text) else ""


def _context_overflow(exc: Exception) -> bool:
    text = str(exc).lower()
    return "context" in text and ("exceed" in text or "too long" in text or "too many tokens" in text)


def _basic_names(tools) -> set[str]:
    from app.agent.tool_router import ALWAYS

    if tools is None:
        return set()
    return {tool.name for tool in tools.registry.tools if tool.category in ALWAYS}


def step_checklist(executions: list[ToolExecution] | tuple[ToolExecution, ...]) -> str:
    """The honest record of a multi-step task, built from the real tool results (not from the model's words)."""
    if len(executions) < 2:
        return ""
    lines = []
    for number, execution in enumerate(executions, 1):
        result = execution.result
        mark = "\u2713" if result.ok else "\u2717"
        detail = (result.message or "").strip().splitlines()[0][:120] if result.message else ""
        if not result.ok and result.error and result.error not in detail:
            detail = f"{result.error}: {detail}" if detail else result.error
        lines.append(f"{number}. {mark} {execution.call.name}" + (f" - {detail}" if detail else ""))
    done = sum(e.result.ok for e in executions)
    return f"Steps ({done} of {len(executions)} worked):\n" + "\n".join(lines)

def answer_sources(executions, memories_used=(), model: str | None = None) -> dict:
    """Where an answer's facts came from - built from what really happened, never from the model's own words:
    tools (each: worked or not, checked afterwards or not), saved memories that matched the request, and the model."""
    tools = []
    for execution in executions:
        result = execution.result
        data = getattr(result, "data", None) or {}
        verified = data.get("verified") if isinstance(data, dict) else None
        tools.append({"tool": execution.call.name, "ok": bool(result.ok),
                      "verified": verified if isinstance(verified, bool) else None})
    memories = [{"id": i, "content": c} for i, c, why in memories_used if why == "it matches words in your request"]
    return {"tools": tools, "memories": memories, "model": model}


def sources_line(sources: dict) -> str:
    """'Sources: open_application (worked, checked) · get_weather (worked) · 1 saved memory · the rest is the AI'"""
    parts = []
    for t in sources.get("tools", [])[:6]:
        state = "worked" if t["ok"] else "failed"
        if t["ok"] and t["verified"] is True:
            state += ", checked"
        elif t["ok"] and t["verified"] is False:
            state = "not confirmed"
        parts.append(f"{t['tool']} ({state})")
    count = len(sources.get("memories", []))
    if count:
        parts.append(f"{count} saved memor{'y' if count == 1 else 'ies'}")
    if not parts:
        return ""
    return "Sources: " + " · ".join(parts) + " · everything else is the AI's own wording (not checked)"


DeltaCallback = Callable[[str], None]
ToolCallback = Callable[[ToolExecution], None]


_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


@dataclass(frozen=True)
class AssistantReply:
    text: str
    ok: bool
    error: str | None = None  # machine-readable error code when ok is False
    model: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    stats: ChatStats | None = None
    elapsed_ms: int | None = None
    tools_used: tuple[ToolExecution, ...] = ()
    checklist: str = ""  # multi-step tasks: one line per step with its real result (shown, not spoken)
    sources: dict | None = None  # where the facts came from (tools / memories / the model) - see answer_sources()


_ASKED_FOR = {  # (tool, action) -> words the user's own message must contain
    ("power_control", "restart"): re.compile(r"\b(restart|reboot|re-start)|রিস্টার্ট|রিবুট", re.I),
    ("power_control", "shutdown"): re.compile(r"\b(shut\s*down|shutdown|turn\s+off|power\s+off|switch\s+off)|শাটডাউন|"
                                              r"(?:পিসি|কম্পিউটার|ল্যাপটপ)\s*বন্ধ", re.I),
}


def user_asked_for(call: ToolCall, user_text: str) -> bool:
    """True when the model's call does exactly what the user's own words asked for ("restart my computer bomb"
    -> restart). Then it counts as the user's command, e.g. for CONFIRM_POWER_ACTIONS=false. Something the
    model decided on its own (for instance because of text on a web page) never counts."""
    action = call.arguments.get("action") if isinstance(call.arguments, dict) else None
    pattern = _ASKED_FOR.get((call.name, action))
    return bool(pattern and pattern.search(user_text or ""))


_APP_TOOLS = ("open_application", "close_application", "window_control", "open_website")
# "close it", "open that again", "minimize it" -> the app used last
_REFERENCE = re.compile(
    r"^(?P<head>(?:please\s+)?(?:re-?open|open|launch|start|close|quit|exit|minimi[sz]e|maximi[sz]e|restore|"
    r"focus(?:\s+on)?|switch\s+to|bring\s+up)\s+)(?:it|that|this|that\s+app|this\s+app|the\s+app)"
    r"(?:\s+again|\s+back(?:\s+up)?)?(?P<tail>\s*(?:please|now)?[.!?]*)$", re.I)
MAX_RECENT_ACTIONS = 6
_COMPLEX = re.compile(r"\b(why|explain|how\s+(?:do|does|can|to)|write|code|debug|analy[sz]e|summari[sz]e|compare|plan|"
                      r"translate|calculate|research|document|thesis|essay|email|story|pros\s+and\s+cons|difference)\b|"
                      r"কেন|ব্যাখ্যা|বুঝিয়ে|লিখে\s*দাও|তুলনা|সারাংশ|পরিকল্পনা", re.I)


def is_simple_request(text: str) -> bool:
    """Short everyday requests go to the fast model (AI_FAST_MODEL); the rest to AI_MODEL."""
    words = text.split()
    return len(words) <= 14 and len(text) <= 120 and not _COMPLEX.search(text)


def _known_setting(name: str) -> bool:
    from app.tools.base import ToolError
    from app.tools.voice_control import _setting

    try:
        _setting(name)
        return True
    except ToolError:
        return False


def is_hard_request(text: str) -> bool:
    """Questions that deserve the bigger model (AI_SMART_MODEL): explanations, writing, analysis, plans."""
    return (bool(_COMPLEX.search(text)) and len(text.split()) >= 4) or len(text) > 400


def resolve_reference(text: str, last_app: str | None) -> str:
    """ "close it" -> "close Phone Link" when Phone Link was the last app used. Otherwise unchanged."""
    if not last_app:
        return text
    match = _REFERENCE.match(" ".join(text.split()))
    if not match:
        return text
    head = re.sub(r"^((?:please\s+)?)re-?open", r"\1open", match.group("head"), flags=re.I)
    return f"{head}{last_app}{match.group('tail')}"


def parse_text_tool_call(text: str, known: set[str]) -> ToolCall | None:
    """Some small models write a tool call as JSON text instead of using the
    tool-call channel, e.g. {"name": "get_datetime", "parameters": {}}."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("function"), dict):
        data = data["function"]
    name = data.get("name")
    arguments = data.get("parameters", data.get("arguments", {}))
    if not isinstance(name, str) or name not in known or not isinstance(arguments, dict):
        return None
    return ToolCall(name=name, arguments=arguments)


def _call_key(call: ToolCall) -> str:
    return call.name + json.dumps(dict(call.arguments), sort_keys=True, default=str)


def friendly_error(exc: BaseException) -> tuple[str, str]:
    """(error code, user-facing message) for an exception from the AI layer."""
    if isinstance(exc, ProviderUnavailableError):
        return "provider_unavailable", str(exc)
    if isinstance(exc, NoModelAvailableError):
        return "no_model", str(exc)
    if isinstance(exc, ModelNotFoundError):
        return "model_not_found", str(exc)
    if isinstance(exc, ModelCapabilityError):
        return "model_capability", str(exc)
    if isinstance(exc, AITimeoutError):
        return "timeout", "The AI model took too long to respond. Try again, or choose a smaller model."
    if isinstance(exc, AIRequestError):
        return "request_failed", "The AI model returned an error. The details are in the log."
    return "internal", "Something went wrong while talking to the AI model. The details are in the log."


class ConversationManager:
    def __init__(
        self,
        ai: AIManager,
        settings: Settings,
        store: ConversationStore | None = None,
        clock: Callable[[], datetime] | None = None,
        tools: ToolExecutor | None = None,
        on_tool: ToolCallback | None = None,
        spoken: bool = False,
        on_tool_start: Callable[[ToolCall], None] | None = None,
    ) -> None:
        self.ai = ai
        self.spoken = spoken  # replies are read aloud (voice mode)
        self.route_tools = True  # offer only the relevant tool groups per request
        self.direct_commands = settings.direct_commands  # "open X"/"close X" without the model
        self.tools = tools
        if tools is not None and getattr(tools, "context", None) is not None:
            tools.context.conversation = self
        self.on_tool = on_tool
        self.on_tool_start = on_tool_start
        self.settings = settings
        self.store = store
        self.context = ConversationContext(settings.context_max_messages, settings.context_max_chars)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = asyncio.Lock()
        self._conversation_id: str | None = None
        self.last_app: str | None = None  # for "close it" / "open that again"
        self.reply_language = "en"  # the language of the user's latest message: answers use it
        self._current_text = ""
        self._last_user_text = ""
        self._corrections_store = None
        self._history_restored = False
        self.recent_actions: deque[str] = deque(maxlen=MAX_RECENT_ACTIONS)
        from app.security.taint import Taint

        self.taint = Taint()  # untrusted text (web, email, screen...) in this conversation: app/security/taint.py

    def note_execution(self, execution: ToolExecution) -> None:
        """Remember what was done (session only), so follow-ups like "close it" work."""
        result, call = execution.result, execution.call
        if not result.ok:
            return
        if call.name in _APP_TOOLS:
            app = result.data.get("app") or call.arguments.get("name") or call.arguments.get("site")
            if isinstance(app, str) and app.strip():
                self.last_app = app.strip()
                from app.core import modes

                if call.name == "open_application" and self.tools is not None and self.tools.context.database \
                        and modes.STATE.current.remember:
                    try:
                        from app.agent.habits import record

                        record(self.tools.context.database, self.last_app, self._clock())
                    except Exception:
                        _log.warning("could not record app usage", exc_info=True)
        if call.name in _APP_TOOLS or call.name in ("power_control", "volume_control", "brightness_control", "run_workflow"):
            stamp = time12(self._clock())
            self.recent_actions.append(f"{stamp} {result.message[:120]}" if result.message else f"{stamp} {call.name}")

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    def reset(self) -> None:
        """Forget the short-term context and start a new conversation."""
        self.context.clear()
        self.taint.clear()  # the untrusted text is gone with the context
        self._history_restored = True  # a new conversation starts empty
        self.last_app = None
        self.recent_actions.clear()
        self._conversation_id = None
        log_event(_log, "conversation_reset")

    last_sources: dict | None = None

    def _memories_used(self) -> list:
        try:
            manager = self.tools.context.get_memory() if self.tools is not None else None
            return list(getattr(manager, "last_used", []) or [])
        except Exception:
            return []

    def _memories(self) -> str:
        if self.tools is None:
            return ""
        try:
            manager = self.tools.context.get_memory()
            return manager.prompt_section(self._current_text or "") if manager is not None else ""
        except Exception:  # memory must never break a conversation
            _log.warning("could not load memories", exc_info=True)
            return ""

    def _tool_specs(self, text: str = "") -> list[ToolSpec]:
        if self.tools is None:
            return []
        if not self.route_tools or not text:
            return self.tools.registry.specs()
        recent = [m.tool_name for m in self.context.messages[-6:] if m.role == "tool" and m.tool_name]
        if is_task(text):  # research + writing tasks also need the web and document tools
            text = f"{text} search web website read page document file"
        return select_specs(self.tools.registry, text, recent, max_tokens=int(self._context_window() * 0.4))

    def _context_window(self) -> int:
        """Tokens the model can read - smaller than AI_CONTEXT_WINDOW when the GPU memory saver lowered it."""
        window = getattr(getattr(self.ai, "provider", None), "context_window", None)
        return int(window) if isinstance(window, int) and window > 0 else int(self.settings.ai_context_window)

    def _fit(self, messages: list[ChatMessage], specs: Sequence[ToolSpec]) -> list[ChatMessage]:
        """Drop the oldest history until system prompt + history + this turn + tools fit, leaving room to reply.
        Only this request is shortened; the stored conversation stays as it is."""
        from app.agent.tool_router import spec_tokens

        def tokens(message: ChatMessage) -> int:
            text = message.content or ""
            bengali = sum("\u0980" <= ch <= "\u09ff" for ch in text)
            return int((len(text) - bengali) / 3.3 + bengali / 1.5) + 6 + 20 * len(message.tool_calls or ())

        limit = self._context_window() - REPLY_RESERVE - sum(spec_tokens(s) for s in specs)
        system, rest = messages[0], list(messages[1:])
        history = len(self.context.messages)
        total = tokens(system) + sum(tokens(m) for m in rest)
        dropped = 0
        leaving: list[ChatMessage] = []
        while total > limit and dropped < history and len(rest) > 1:
            leaving.append(rest[0])
            total -= tokens(rest.pop(0))
            dropped += 1
        while rest and rest[0].role == "tool":  # never start with a tool answer whose call was dropped
            leaving.append(rest[0])
            total -= tokens(rest.pop(0))
            dropped += 1
        if dropped:
            self.context.note_dropped(leaving)      # the model keeps a one-line digest of what it no longer sees
            log_event(_log, "context_trimmed", dropped=dropped, tokens=total, limit=limit)
        return [system, *rest]

    def _system_message(self, specs: Sequence[ToolSpec]) -> ChatMessage:
        if self.settings.offline_mode == "auto":
            network.MONITOR.refresh_in_background()  # never waited for; the prompt uses the last result
        return ChatMessage.system(
            build_system_prompt(
                assistant_name=self.settings.assistant_name,
                user_address=self.settings.user_address,
                now=self._clock(),
                tools=specs,
                offline_mode=network.is_offline_now(self.settings),
                spoken=self.spoken,
                memories="\n\n".join(p for p in (self._memories(), self._examples()) if p),
                recent_actions=tuple(self.recent_actions),
                reply_language=self.reply_language,
                task=is_task(self._current_text),
                tone=self.tone_hint,
                detail=answer_detail(self._current_text) or self._mode_detail(),
                situation=self._situation(),
                earlier=tuple(self.context.digest),
            )
        )

    @staticmethod
    def _mode_detail() -> str:
        from app.core import modes

        return "" if modes.STATE.current.verbose else "short"

    def _situation(self) -> str:
        """Context compiler (app/core/world.py): the facts about your environment this request needs."""
        if self.tools is None or not getattr(self.settings, "world_context", True):
            return ""
        parts = []
        try:  # the context engine: what you're doing right now (app, project, branch, goal, approval waiting)
            from app.core import context_engine

            context_engine.ENGINE.last_request = self._current_text
            line = context_engine.ENGINE.snapshot(self.tools.context).line()
            if line:
                parts.append(line)
        except Exception:
            _log.warning("context engine failed", exc_info=True)
        try:  # one merged, evidence-labelled view of everything relevant (app/core/request_context.py)
            from app.core import request_context

            unified = request_context.for_request(self.tools.context, self._current_text)
            block = unified.render()
            if block:
                parts.append(block)
            question = request_context.clarify(self._current_text, unified)
            if question:
                parts.append(f"If the user is asking you to ACT on something they only pointed at, and you cannot resolve it from what I know, ask exactly this instead of guessing: \"{question}\"")
        except Exception:
            _log.warning("unified context failed", exc_info=True)
            try:
                from app.core.world import compile_context

                compiled = compile_context(self.tools.context, self._current_text)
                if compiled:
                    parts.append(compiled)
            except Exception:
                _log.warning("context compiler failed", exc_info=True)
        return "\n".join(parts)

    async def _record(self, model: str, user_text: str, messages_added: int = 2) -> None:
        if self.store is None:
            return
        try:
            first = self._conversation_id is None
            if first:
                self._conversation_id = await asyncio.to_thread(self.store.start, model)
            await asyncio.to_thread(
                self.store.record_exchange,
                self._conversation_id,
                model=model,
                first_user_text=user_text if first else None,
                messages_added=messages_added,
            )
        except Exception:  # metadata is optional; never fail a reply because of it
            _log.warning("Could not record conversation metadata", exc_info=True)

    async def send(self, user_text: str, on_delta: DeltaCallback | None = None,
                   source: str = "text") -> AssistantReply:
        """Send one user message. With ``on_delta``, text is streamed as it arrives. ``source``: voice, text,
        phone, cli... Every request is a run (app/core/runs.py): an ID that ties its audit entries together."""
        from app.core.runs import Run, running

        run = Run(source if source in ("voice", "text", "phone", "automation", "cli", "internal") else "text",  # type: ignore[arg-type]
                  user_text.strip()[:500], taint=self.taint, user_words=frozenset(self._user_words(user_text)))
        with running(run, self._run_store()):
            reply = await self._send(user_text, on_delta)
        self._learn_from(user_text, reply, run.source)
        return reply

    _learning_used: list[int] = []

    def _learning_on(self, source: str = "text") -> bool:
        from app.core import modes

        settings = self.tools.context.settings if self.tools is not None else self.settings
        return (bool(getattr(settings, "learn_from_use", True)) and source not in ("automation", "internal")
                and modes.STATE.current.remember and self.tools is not None and self.tools.context.database is not None)

    def _learn_from(self, user_text: str, reply: "AssistantReply", source: str) -> None:
        """Every request is recorded with what really happened; its outcome is settled by what you do next
        (app/agent/growth.py). Never in private mode, never for automations."""
        try:
            if not self._learning_on(source) or reply.error == "empty_input":
                return
            from app.agent.growth import Growth

            Growth(self.tools.context.database).record(user_text, reply.tools_used, reply.model or "",
                                                       list(self._learning_used))
        except Exception:
            _log.warning("could not record the request for learning", exc_info=True)
        finally:
            self._learning_used = []

    def _user_words(self, text: str) -> set[str]:
        """What the user said in this request and the last few: the only words egress may carry on its own."""
        from app.agent.language import to_english_command
        from app.security.taint import words

        found = words(text) | words(to_english_command(text) or "")
        for message in [m for m in self.context.messages if m.role == "user"][-6:]:
            found |= words(message.content or "")
        return found

    def _run_store(self):
        database = self.tools.context.database if self.tools is not None else None
        if database is None:
            return None
        if getattr(self, "_runs", None) is None or self._runs_db is not database:
            from app.core.runs import RunStore

            self._runs, self._runs_db = RunStore(database), database
        return self._runs

    _runs = None
    _runs_db = None

    async def _send(self, user_text: str, on_delta: DeltaCallback | None = None) -> AssistantReply:
        text = user_text.strip()
        if not text:
            return AssistantReply("I didn't catch that.", ok=False, error="empty_input")
        if len(text) > MAX_USER_CHARS:
            return AssistantReply(
                f"That message is too long. Please keep it under {MAX_USER_CHARS} characters.",
                ok=False, error="input_too_long",
            )

        async with self._lock:
            in_session = await self._session_turn(text)
            if in_session is not None:
                if on_delta is not None:
                    on_delta(in_session.text)
                return in_session
            answered = await self._answer_suggestion(text)
            if answered is not None:
                if on_delta is not None:
                    on_delta(answered.text)
                return answered
            rated = self._rate_last_answer(text)
            if rated is not None:
                if on_delta is not None:
                    on_delta(rated)
                return AssistantReply(rated, ok=True, model="direct")
            text, learned = self._apply_corrections(text)
            if learned:
                self._note("corrected", text)
            self.reply_language = detect_language(text)
            self._current_text = text
            self._last_user_text = text
            if learned and on_delta is not None:
                on_delta(learned + " ")
            await self._restore_history()
            user_message = ChatMessage.user(text)
            started = time.perf_counter()
            try:
                delta = on_delta if on_delta is None or wants_emoji(text) else (
                    lambda piece, _out=on_delta: _out(strip_emoji(piece, trim=False)))
                model, reply_text, turn, stats, executions = await self._agent_loop(user_message, delta)
            except asyncio.CancelledError:
                log_event(_log, "chat_cancelled")
                raise
            except Exception as exc:
                code, message = friendly_error(exc)
                if isinstance(exc, AIError):
                    log_event(_log, "chat_failed", error=code, detail=str(exc))
                else:
                    _log.exception("Unexpected error during chat", extra={"event": "chat_crashed"})
                return AssistantReply(message, ok=False, error=code)

            if model != "direct":
                reply_text, executions = await self._keep_words_and_deeds(reply_text, executions, on_delta)
            elif executions:
                self._unbacked_action = None
            elapsed = int((time.perf_counter() - started) * 1000)
            if not wants_emoji(text):
                reply_text = strip_emoji(reply_text)
            if not reply_text:
                reply_text = "I couldn't produce a response to that."
            if learned:
                reply_text = f"{learned} {reply_text}"
            notice = self._fallback_notice()
            if notice:  # a different model answered than planned: say so, the quality may differ
                reply_text += notice
                if on_delta is not None:
                    on_delta(notice)
            turn.append(ChatMessage.assistant(reply_text))
            self.context.add_turn(turn)
            log_event(
                _log, "chat_completed", model=model, elapsed_ms=elapsed,
                prompt_tokens=stats.prompt_tokens if stats else None,
                completion_tokens=stats.completion_tokens if stats else None,
                load_ms=stats.load_ms if stats else None, prompt_ms=stats.prompt_ms if stats else None,
                eval_ms=stats.eval_ms if stats else None,
                tools_used=[e.call.name for e in executions], context_messages=len(self.context),
            )
            await self._record(model, text, len(turn))
            self._last_exchange = (text, reply_text)
            self._remember_suggestion(executions)
            for execution in executions:  # what went wrong, for the weekly review
                error = getattr(execution.result, "error", None)
                if error in ("not_found", "unknown", "unknown_page", "unknown_device", "not_running", "unknown_target"):
                    self._note("not_found", text, execution.call.name)
                elif error == "declined":
                    self._note("cancelled", text, execution.call.name)
            from app.core import modes

            if self.settings.conversation_memory and model != "direct" and modes.STATE.current.remember:
                asyncio.get_running_loop().create_task(self._remember(text, reply_text))
            if getattr(self.settings, "memory_suggestions", False) and model != "direct" and \
                    modes.STATE.current.remember:
                asyncio.get_running_loop().create_task(self._suggest_memories(text))
            sources = answer_sources(executions, self._memories_used(), model)
            self.last_sources = sources
            return AssistantReply(
                reply_text, ok=True, model=model, stats=stats, elapsed_ms=elapsed,
                tools_used=tuple(executions),
                checklist=step_checklist(executions) if model != "direct" and is_task(text) else "",
                sources=sources,
            )

    async def _session_turn(self, text: str) -> AssistantReply | None:
        """Interpreter / study sessions (app/core/sessions.py): the model gets no tools here."""
        from app.core import sessions

        active = sessions.STATE.active
        if active is None:
            return None
        if sessions.is_stop(text):
            return AssistantReply(sessions.STATE.stop(), ok=True, model="direct")
        try:
            if isinstance(active, sessions.Interpreter):
                spoken = detect_language(text)
                translation, language = await sessions.translate(self.ai, text, active, spoken)
                self.reply_language = "bn" if language == "bn" else "en"
                return AssistantReply(translation or "(I couldn't translate that.)", ok=bool(translation),
                                      model="interpreter")
            current = active.current
            if current is None:
                return AssistantReply(sessions.STATE.stop(), ok=True, model="direct")
            right, why = await sessions.check_answer(self.ai, current[0], current[1], text)
        except AIError as exc:
            _, message = friendly_error(exc)
            return AssistantReply(message, ok=False, error="ai_unavailable")
        active.index += 1
        active.score += int(right)
        verdict = "Correct!" if right else f"Not quite - the answer is: {current[1]}."
        reply = f"{verdict} {why}".strip()
        following = active.current
        if following is None:
            reply += " " + sessions.STATE.stop()
        else:
            reply += f" Question {active.index + 1}: {following[0]}"
        return AssistantReply(reply, ok=True, model="study")

    _last_exchange: tuple[str, str] | None = None
    _suggestion: tuple[dict, float] | None = None
    SUGGESTION_SECONDS = 60.0
    SUGGESTION_TOOLS = frozenset({"open_application", "close_application", "window_control", "search_in_browser",
                                  "windows_update", "change_setting", "restart_application"})
    _YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|right|correct|that'?s? (?:it|right|the one)|do it|"
                      r"(?:yes,?\s+)?(?:please\s+)?(?:open|close|install|update|search|turn|switch|do)\s+(?:it|them|that|those|"
                      r"it on|them all|for it)|install|please|হ্যাঁ|হ্যা|জি|ঠিক আছে|হুম)(?:\s*(?:please|jarvis|sir))?[.!।]*$", re.I)
    _NO = re.compile(r"^(?:no|nope|nah|not that|wrong|না|নাহ)(?:\s*(?:thanks|jarvis|sir))?[.!।]*$", re.I)

    def _remember_suggestion(self, executions) -> None:
        """A "did you mean X? say yes" from a tool: keep it for a minute, for the next yes / no."""
        self._suggestion = None
        for execution in reversed(list(executions)):
            data = execution.result.data or {}  # a "did you mean" (failed) or an offer ("say yes to install")
            suggestion = data.get("suggestion") if isinstance(data, dict) else None
            if isinstance(suggestion, dict) and suggestion.get("tool") in self.SUGGESTION_TOOLS:
                self._suggestion = (suggestion, time.monotonic())
                return

    async def _answer_suggestion(self, text: str) -> AssistantReply | None:
        if self._suggestion is None:
            return None
        suggestion, at = self._suggestion
        self._suggestion = None
        if time.monotonic() - at > self.SUGGESTION_SECONDS or self.tools is None:
            return None
        if self._NO.match(text.strip()):
            return AssistantReply("Okay, I won't.", ok=True, model="direct")
        if not self._YES.match(text.strip()):
            return None  # something else: handled normally
        call = ToolCall(suggestion["tool"], dict(suggestion.get("arguments") or {}))
        execution = await self.tools.execute(call, user_command=True)
        self.note_execution(execution)
        if self.on_tool is not None:
            try:
                self.on_tool(execution)
            except Exception:
                _log.warning("tool callback failed", exc_info=True)
        message = execution.result.message or ("Done." if execution.result.ok else "That didn't work.")
        heard, name = str(suggestion.get("heard") or ""), str(suggestion.get("name") or "")
        if execution.result.ok and heard and name and self.tools.context.paths is not None:
            try:  # next time "WordShop" simply opens Photoshop
                from app.agent.aliases import AliasStore

                AliasStore(self.tools.context.paths.data_dir).set(heard, "app", name)
                log_event(_log, "alias_learned_from_yes", tool=call.name)
                message += f" I'll remember that {heard} means {name}."
            except (OSError, ValueError):
                _log.warning("could not save the nickname", exc_info=True)
        return AssistantReply(message, ok=execution.result.ok, model="direct", tools_used=(execution,))
    _GOOD = re.compile(r"^(?:good|great|perfect|nice|excellent|that'?s? (?:a )?(?:good|great|perfect|right))\s*"
                       r"(?:answer|reply|job|one)?[.!]*$|^(?:ভালো|দারুণ|চমৎকার)\s*(?:উত্তর)?[।.!]*$", re.I)
    _BAD = re.compile(r"^(?:bad|wrong|terrible|that'?s? (?:a )?(?:bad|wrong))\s*(?:answer|reply)[.!]*$|"
                      r"^(?:ভুল|খারাপ)\s*উত্তর[।.!]*$", re.I)

    def _rate_last_answer(self, text: str) -> str | None:
        """ "good answer" -> the last answer becomes an example the model follows; "bad answer" -> noted."""
        cleaned = " ".join(text.split())
        good, bad = self._GOOD.match(cleaned), self._BAD.match(cleaned)
        if not (good or bad) or self._last_exchange is None:
            return None
        database = self.tools.context.database if self.tools is not None else None
        bangla = detect_language(cleaned) == "bn"
        if good:
            if database is not None:
                from app.agent.learning import save_example

                save_example(database, *self._last_exchange)
            return "ধন্যবাদ! এরকমভাবেই উত্তর দেব।" if bangla else "Thanks! I'll answer like that more often."
        self._note("bad_answer", self._last_exchange[0], self._last_exchange[1][:200])
        return ("দুঃখিত। কী ভুল ছিল বললে ঠিক করে দেব।" if bangla
                else "Sorry about that. Tell me what was wrong and I'll fix it.")

    def _note(self, kind: str, text: str, detail: str = "") -> None:
        database = self.tools.context.database if self.tools is not None else None
        if database is None:
            return
        try:
            from app.agent.learning import record

            record(database, kind, text, detail)
        except Exception:
            _log.warning("could not note feedback", exc_info=True)

    def _examples(self) -> str:
        database = self.tools.context.database if self.tools is not None else None
        if database is None or not self._current_text:
            return ""
        parts = []
        try:
            from app.agent.learning import examples_prompt, similar_examples

            parts.append(examples_prompt(similar_examples(database, self._current_text)))
        except Exception:
            pass
        try:
            if self._learning_on():
                from app.agent.growth import Growth

                items = Growth(database).relevant(self._current_text)   # skills + lessons from your own use
                self._learning_used = [i["id"] for i in items]
                parts.append(Growth.prompt(items))
        except Exception:
            _log.warning("could not load what was learned", exc_info=True)
        return "\n\n".join(p for p in parts if p)

    def _corrections(self):
        if self._corrections_store is None and self.tools is not None and self.tools.context.paths is not None:
            from app.voice.corrections import Corrections

            self._corrections_store = Corrections(self.tools.context.paths.data_dir)
        return self._corrections_store

    def _apply_corrections(self, text: str) -> tuple[str, str]:
        """ "no, I said Phone Link" after a misheard request: learn it and redo the request, corrected.
        Otherwise apply what was learned before. Returns (text to handle, note for the reply or "")."""
        store = self._corrections()
        if store is None:
            return text, ""
        from app.voice.corrections import find_misheard, parse_correction

        fix = parse_correction(text)
        previous = self._last_user_text
        if fix and previous:
            from app.agent.language import BENGALI, _app_name

            meant = _app_name(fix) if BENGALI.search(fix) else fix
            heard = find_misheard(previous, meant)
            if heard:
                store.learn(heard, meant)
                log_event(_log, "correction_learned", words=len(meant.split()))
                corrected = re.sub(re.escape(heard), meant, previous, count=1, flags=re.I)
                if self.on_correction is not None:          # the host may keep the audio + corrected text
                    try:
                        self.on_correction(previous, corrected)
                    except Exception:
                        _log.warning("on_correction failed", exc_info=True)
                note = ("বুঝেছি, মনে রাখব।" if detect_language(text) == "bn"
                        else f"Got it, {meant}. I'll remember that.")
                return corrected, note
        try:
            return store.apply(text), ""
        except Exception:
            _log.warning("could not apply corrections", exc_info=True)
            return text, ""

    def _semantic(self):
        if self.tools is None or self.tools.context.database is None:
            return None
        from app.memory.semantic import SemanticIndex

        return SemanticIndex(self.tools.context.database, self.ai.embed, self.settings.embedding_model)

    async def _remember(self, user_text: str, reply_text: str) -> None:
        """Long-term memory (CONVERSATION_MEMORY): save the exchange for search by meaning. Never fails a reply."""
        index = self._semantic()
        if index is None:
            return
        try:
            await index.add_exchange(user_text, reply_text, self._clock())
        except Exception as exc:  # e.g. the embedding model isn't pulled yet
            log_event(_log, "history_not_saved", detail=str(exc)[:160])

    on_memory_suggestion = None  # callback(list of facts): the host tells the user
    on_correction = None  # callback(heard request, corrected request): SPEECH_DATASET keeps the audio + right text
    tone_hint = ""  # "the user sounds in a hurry..." for the next reply (set by the host from the voice)

    async def _suggest_memories(self, user_text: str) -> None:
        """MEMORY_SUGGESTIONS: offer lasting facts from what you said as memories (you accept or reject them)."""
        from app.memory.suggestions import SuggestionStore, suggest, worth_checking

        if not worth_checking(user_text) or self.tools is None or self.tools.context.database is None:
            return
        database = self.tools.context.database
        try:
            from app.ai.ollama import strip_think
            from app.memory.manager import MemoryManager

            known = [m.content for m in MemoryManager(database).all(500)]

            async def ask(prompt: str) -> str:
                reply = await self.ai.chat([ChatMessage.user(prompt)], fast=True)
                return strip_think(reply.message.content or "")

            new = await suggest(user_text, ask, SuggestionStore(database), known)
        except Exception as exc:  # suggestions are a nicety, never a failure
            log_event(_log, "memory_suggestion_failed", detail=str(exc)[:160])
            return
        if new:
            log_event(_log, "memory_suggested", count=len(new))
            if self.on_memory_suggestion is not None:
                try:
                    self.on_memory_suggestion(new)
                except Exception:
                    _log.warning("memory suggestion callback failed", exc_info=True)

    async def _restore_history(self) -> None:
        """After a restart, continue where we left off: the last few exchanges go back into the context."""
        if self._history_restored or not self.settings.conversation_memory:
            return
        self._history_restored = True
        index = self._semantic()
        if index is None or len(self.context):
            return
        try:
            for user_text, reply_text in await asyncio.to_thread(index.recent_history, 4):
                self.context.add_turn([ChatMessage.user(user_text), ChatMessage.assistant(reply_text)])
        except Exception:
            _log.warning("could not restore the conversation history", exc_info=True)

    def _shortcut(self, text: str) -> ToolCall | None:
        if not self.direct_commands or self.tools is None or "open_application" not in self.tools.registry:
            return None
        from app.tools.applications import known_application

        def known_workflow(name: str) -> str | None:
            store = self.tools.context.get_automation_store()
            if store is None:
                return None
            wanted = " ".join(name.lower().split())
            for task in store.all():
                if task.is_workflow and task.enabled and task.name.lower() == wanted:
                    return task.name
            return None

        try:
            from app.tools.applications import site_key

            from app.tools.windows import power_pending

            call = match_shortcut(text, lambda name: known_application(name, self.tools.context), known_workflow,
                                  known_site=lambda name: site_key(name) is not None,
                                  power_pending=lambda: power_pending() is not None,
                                  known_device=self._known_device, known_setting=_known_setting,
                                  known_alias=self._known_alias, known_computer=self._known_computer)
        except Exception:
            _log.warning("direct command check failed", exc_info=True)
            return None
        if call is not None and call.name not in self.tools.registry:
            return None
        return call

    def _known_alias(self, name: str) -> ToolCall | None:
        """Your nicknames ("code" -> Visual Studio Code)."""
        paths = self.tools.context.paths if self.tools is not None else None
        if paths is None:
            return None
        from app.agent.aliases import AliasStore, call_for

        alias = AliasStore(paths.data_dir).get(name)
        return call_for(alias) if alias else None

    def _known_computer(self, name: str) -> bool:
        """A device in the device list (other than this PC) - for "wake my desktop" / "is my desktop on"."""
        database = self.tools.context.database if self.tools is not None else None
        if database is None:
            return False
        from app.core.devices import DeviceRegistry

        try:
            key = name.lower().strip()
            return any(not d.is_self and (d.name.lower() == key or key in d.name.lower())
                       for d in DeviceRegistry(database).all())
        except Exception:
            return False

    def _known_device(self, name: str) -> str | None:
        if self.tools is None or "home_control" not in self.tools.registry:
            return None
        from app.tools.smart_home import known_device

        return known_device(self.tools.context, name)

    def _shortcuts(self, text: str) -> list[ToolCall]:
        """Direct commands for the request: one, several ("open Chrome and Spotify"), or none (-> the model)."""
        text = text.translate(_BN_DIGITS)          # "ভলিউম ৫০" / "remind me in ১০ minutes" -> ASCII digits
        save = SAVE_SETUP.match(" ".join(text.split()))
        if save and self.tools is not None and self.tools.context.database:
            from app.agent import activity

            calls = activity.reopen_calls(activity.current_session(self.tools.context.database, self._clock()))
            if not calls:
                return [ToolCall("recall_activity", {"period": "last 2 hours"})]
            name = save.group("name").strip()
            if not re.search(r"\b(?:setup|routine|workflow)\b", name, re.I):
                name += " setup"  # "work" -> "work setup": "start my work setup" then runs it
            return [ToolCall("save_workflow", {"name": name, "steps": activity.as_steps(calls)})]
        if RESUME.match(" ".join(text.split())) and self.tools is not None and self.tools.context.database:
            from app.agent import activity

            session = activity.last_session(self.tools.context.database, self._clock())
            calls = activity.reopen_calls(session)
            log_event(_log, "resume_session", items=len(calls))
            # nothing to reopen: say what the last session was instead of guessing
            return calls or [ToolCall("recall_activity", {"period": "last session"})]
        single = self._shortcut(text)
        if single is not None:
            return [single]
        parts = split_commands(text)
        if not parts:
            return []
        calls = [self._shortcut(resolve_reference(part, self.last_app)) for part in parts]
        if any(c is None for c in calls):
            return []  # something in it needs the model ("open Chrome and search for cats")
        if sum(c.name == "power_control" and c.arguments.get("action") in ("restart", "shutdown")
               for c in calls) and calls[-1].name != "power_control":
            return []  # a restart/shutdown must be the last step
        return calls  # type: ignore[return-value]

    async def _direct(
        self, user_message: ChatMessage, calls: list[ToolCall], on_delta: DeltaCallback | None
    ) -> tuple[str, str, list[ChatMessage], ChatStats | None, list[ToolExecution]]:
        executions: list[ToolExecution] = []
        texts: list[str] = []
        for index, call in enumerate(calls):
            if index and self.on_tool_start is not None:
                self.on_tool_start(call)
            execution = await self.tools.execute(call, user_command=True)  # type: ignore[union-attr]
            executions.append(execution)
            self.note_execution(execution)
            if self.on_tool is not None:
                try:
                    self.on_tool(execution)
                except Exception:
                    _log.warning("tool callback failed", exc_info=True)
            result = execution.result
            texts.append(localize(result.message or ("Done." if result.ok else "That didn't work."),
                                  self.reply_language))
            log_event(_log, "direct_command", tool=call.name, ok=result.ok, step=index + 1, steps=len(calls))
        text = " ".join(texts)
        if on_delta is not None:
            on_delta(text)
        turn = [user_message, ChatMessage.assistant("", tuple(calls))]
        turn += [ChatMessage.tool(e.call.name, e.result.to_model_text()) for e in executions]
        return "direct", text, turn, None, executions

    async def _agent_loop(
        self, user_message: ChatMessage, on_delta: DeltaCallback | None
    ) -> tuple[str, str, list[ChatMessage], ChatStats | None, list[ToolExecution]]:
        command = user_message.content
        if self.reply_language == "bn":  # "ক্রোম খোলো" -> "open chrome"
            command = to_english_command(command) or command
        shortcuts = self._shortcuts(resolve_reference(command, self.last_app))
        if not shortcuts:
            misheard = misheard_bangla_command(user_message.content)  # "Google Chrome Club" = "... খোলো"
            if misheard is not None:
                shortcuts = self._shortcuts(misheard)
                if shortcuts:
                    self.reply_language = "bn"
        if not shortcuts:
            follow = self._follow_up_action(user_message.content)
            if follow:                        # "do it" after "I can open WhatsApp" / after a claim that didn't run
                shortcuts = self._follow_up_calls(follow)
                if shortcuts:
                    log_event(_log, "follow_up_action", action=follow.split()[0])
        if self.reply_language == "bn":  # Bangla headlines for a Bangla question
            shortcuts = [ToolCall(c.name, {**c.arguments, "language": "bn"}) if c.name == "get_news" else c
                         for c in shortcuts]
        if shortcuts:
            if self.on_tool_start is not None:
                self.on_tool_start(shortcuts[0])
            return await self._direct(user_message, shortcuts, on_delta)
        specs = self._tool_specs(user_message.content)
        known = {spec.name for spec in specs}
        turn: list[ChatMessage] = [user_message]
        executions: list[ToolExecution] = []
        results_by_key: dict[str, ToolExecution] = {}
        model, text, stats = "", "", None

        task = is_task(user_message.content)
        rounds, max_calls = (TASK_ROUNDS, TASK_CALLS) if task else (MAX_TOOL_ROUNDS, MAX_TOOL_CALLS)
        if task:
            log_event(_log, "task_mode")
        for round_index in range(rounds + 1):
            # The last round offers no tools, so the model has to answer in words.
            offered = specs if round_index < rounds and len(executions) < max_calls else []
            messages = self._fit([self._system_message(specs), *self.context.messages, *turn], offered)
            try:
                model, text, calls, stats = await self._complete(messages, offered, on_delta, known)
            except AIRequestError as exc:
                if not _context_overflow(exc):
                    raise
                # Still too long (a long document in this turn, a model with a smaller window): once more
                # with only the basic tools and this turn alone.
                log_event(_log, "context_overflow_retry")
                offered = [s for s in offered if s.name in _basic_names(self.tools)]
                tail = turn[1:][-5:]
                while tail and tail[0].role == "tool":
                    tail = tail[1:]
                messages = [self._system_message(offered), turn[0], *tail]
                model, text, calls, stats = await self._complete(messages, offered, on_delta, known)
            except ModelCapabilityError:
                if not offered:
                    raise
                log_event(_log, "tools_unsupported_by_model")
                specs, known = [], set()
                model, text, calls, stats = await self._complete(
                    [self._system_message([]), *self.context.messages, *turn], [], on_delta, known
                )
            if not calls or not offered:
                return model, text, turn, stats, executions

            turn.append(ChatMessage.assistant(text if not self._is_text_call(text, known) else "", calls))
            for call in calls:
                key = _call_key(call)
                if key in results_by_key:  # never repeat an action within one request
                    execution = ToolExecution(call, results_by_key[key].result)
                elif len(executions) >= max_calls:
                    execution = ToolExecution(call, ToolResult.failure("limit", "Too many actions for one request."))
                else:
                    if self.on_tool_start is not None:
                        try:
                            self.on_tool_start(call)
                        except Exception:
                            _log.warning("tool start callback failed", exc_info=True)
                    execution = await self.tools.execute(  # type: ignore[union-attr]
                        call, user_command=user_asked_for(call, user_message.content))
                    results_by_key[key] = execution
                    executions.append(execution)
                    self.note_execution(execution)
                    health = getattr(self.ai, "health", None)       # was the model's tool call well-formed?
                    if health is not None and hasattr(health, "tool_call") and model:
                        health.tool_call(model, execution.result.error not in ("unknown_tool", "invalid_arguments"))
                    if self.on_tool is not None:
                        try:
                            self.on_tool(execution)
                        except Exception:
                            _log.warning("tool callback failed", exc_info=True)
                turn.append(ChatMessage.tool(call.name, execution.result.to_model_text()))
        return model, text, turn, stats, executions

    _unbacked_action: str | None = None   # an action the last reply claimed but no tool ran (app/agent/honesty.py)

    def _follow_up_action(self, text: str) -> str | None:
        """'do it' / 'yes' / "I'm ready" right after Jarvis offered (or claimed without doing) an action."""
        from app.agent.honesty import is_affirmation, offered_action

        if not is_affirmation(text):
            return None
        last = getattr(self, "_last_exchange", None)
        return self._unbacked_action or (offered_action(last[1]) if last else None)

    def _follow_up_calls(self, action: str) -> list[ToolCall]:
        calls = self._shortcuts(action)
        if not calls and action.startswith("open ") and self.tools is not None and \
                self.tools.registry.get("open_application"):
            calls = [ToolCall("open_application", {"name": action[5:]})]
        return calls

    async def _keep_words_and_deeds(self, reply_text: str, executions: list, on_delta) -> tuple[str, list]:
        """The model said it did something but called no tool: do it for real (through the executor, with the usual
        checks - as the model's choice, not yours, so anything risky still asks) or say plainly that nothing ran."""
        from app.agent.honesty import CORRECTION, CORRECTION_GENERIC, claimed_action, claims_done

        self._unbacked_action = None
        if executions or self.tools is None:
            return reply_text, executions
        claim = claimed_action(reply_text)
        calls = self._shortcuts(claim) if claim else []
        if not calls and claim and claim.startswith("open ") and self.tools.registry.get("open_application"):
            calls = [ToolCall("open_application", {"name": claim[5:]})]   # the app the reply named; checked as usual
        if calls:
            log_event(_log, "unbacked_claim_done", action=claim.split()[0])
            done: list = []
            for call in calls[:3]:
                if self.on_tool_start is not None:
                    self.on_tool_start(call)
                execution = await self.tools.execute(call)
                done.append(execution)
                self.note_execution(execution)
                if self.on_tool is not None:
                    try:
                        self.on_tool(execution)
                    except Exception:
                        _log.warning("tool callback failed", exc_info=True)
            failed = [e for e in done if not e.result.ok]
            if failed:
                extra = f"\n\n({failed[0].result.message or 'It did not work.'})"
                if on_delta is not None:
                    on_delta(extra)
                reply_text += extra
            return reply_text, done
        if claim or claims_done(reply_text):
            log_event(_log, "unbacked_claim_corrected")
            self._unbacked_action = claim
            extra = CORRECTION.format(cmd=claim) if claim else CORRECTION_GENERIC
            if on_delta is not None:
                on_delta(extra)
            return reply_text + extra, executions
        return reply_text, executions

    @staticmethod
    def _is_text_call(text: str, known: set[str]) -> bool:
        return bool(known) and parse_text_tool_call(text, known) is not None

    def _route(self):
        """Model router 2.0 (app/ai/router.py): which model role answers this request, and why."""
        from app.ai.router import route
        from app.core import modes

        in_editor = False
        try:
            from app.core.context_engine import ENGINE

            history = ENGINE.history()
            in_editor = bool(history) and "editor" in history[-1].facts
        except Exception:
            pass
        text = self._current_text
        decision = route(text, simple=is_simple_request(text), hard=is_hard_request(text), task=is_task(text),
                         in_editor=in_editor, private_mode=not modes.STATE.current.remember)
        decision = self._apply_performance(decision)
        self.last_route = decision
        return decision

    def _apply_performance(self, decision):
        """Performance mode (app/core/perf.py): one step toward a smaller / larger model role, only among roles that are configured."""
        from dataclasses import replace

        from app.core import perf

        try:
            if perf.normalise(getattr(self.settings, "performance_mode", "balanced")) == "balanced":
                return decision                      # nothing to shift: do not even measure
            plan = perf.current_plan(self.settings)
            if plan.route_shift == 0:
                return decision
            configured = {k for k, name in (("fast", "ai_fast_model"), ("smart", "ai_smart_model")) if getattr(self.settings, name, "")}
            kind, note = perf.shift_route(decision.kind, plan, configured)
            if kind != decision.kind:
                return replace(decision, kind=kind, reason=f"{decision.reason} ({note})")
        except Exception:
            _log.warning("performance mode could not be applied to the model choice", exc_info=True)
        return decision

    last_route = None

    def _fallback_notice(self) -> str:
        route = getattr(self.ai, "last_route", None)
        if isinstance(route, dict) and route.get("auto"):
            return f"\n\n(Note: {route['auto']}.)"
        if isinstance(route, dict) and route.get("fallback"):
            return (f"\n\n(Note: {route['fallback']}, so I answered with {route['model']}. "
                    "This answer may be less detailed than usual.)")
        return ""

    async def _complete(
        self, messages: list[ChatMessage], tools: Sequence[ToolSpec], on_delta: DeltaCallback | None, known: set[str]
    ) -> tuple[str, str, tuple[ToolCall, ...], ChatStats | None]:
        if getattr(self.settings, "taint_lockdown", False) and self.taint.active:
            from app.security.taint import LOCKDOWN_TOOLS, PERSISTENT_TOOLS

            tools = [t for t in tools if t.name not in LOCKDOWN_TOOLS and t.name not in PERSISTENT_TOOLS]
        choice = self._route()
        fast = bool(self.settings.ai_fast_model) and choice.kind == "fast"
        coding = choice.kind == "coding" and bool(getattr(self.settings, "ai_coding_model", ""))
        smart = not fast and not coding and bool(self.settings.ai_smart_model) and choice.kind in ("smart", "coding")
        extra = {"coding": True} if coding else {}
        if choice.sensitive:
            extra["sensitive"] = True
        if getattr(self.settings, "ai_auto_switch", False) and self.taint.active:
            extra["untrusted"] = True       # AI_AUTO_SWITCH may then pick a model with a clean safety record
        if smart or coding:
            log_event(_log, "smart_model_chosen" if smart else "coding_model_chosen", reason=choice.reason)
        if on_delta is None:
            response = await (self.ai.chat(messages, tools, fast=fast, smart=True, **extra) if smart
                              else self.ai.chat(messages, tools, fast=fast, **extra))
            model, text = response.model, response.message.content.strip()
            calls, stats = response.message.tool_calls, response.stats
        else:
            model, text, calls, stats = await self._stream(messages, tools, on_delta, fast, smart, extra)
        if not calls and tools:
            parsed = parse_text_tool_call(text, known)
            if parsed is not None:
                log_event(_log, "text_tool_call_recovered", tool=parsed.name)
                calls = (parsed,)
        return model, text, calls, stats

    async def _stream(
        self, messages: list[ChatMessage], tools: Sequence[ToolSpec], on_delta: DeltaCallback, fast: bool = False,
        smart: bool = False, extra: dict | None = None,
    ) -> tuple[str, str, tuple[ToolCall, ...], ChatStats | None]:
        """Stream text to ``on_delta``. If the reply starts with '{' or a code fence it
        may be a tool call written as text, so it is held back until complete."""
        parts: list[str] = []
        tool_calls: list[ToolCall] = []
        stats: ChatStats | None = None
        model = ""
        started_text = False
        holding = False
        extra = extra or {}
        if smart:
            stream = self.ai.stream_chat(messages, tools, smart=True, **extra)
        else:
            stream = self.ai.stream_chat(messages, tools, fast=fast, **extra) if fast else \
                self.ai.stream_chat(messages, tools, **extra)
        async for model, chunk in stream:  # noqa: B007 - the last model name is returned
            piece = chunk.content
            if not started_text:  # drop leading whitespace (e.g. left after a think block)
                piece = piece.lstrip()
                started_text = bool(piece)
                holding = bool(tools) and piece.startswith(("{", "`"))
            if piece:
                parts.append(piece)
                if not holding:
                    on_delta(piece)
            tool_calls.extend(chunk.tool_calls)
            if chunk.done:
                stats = chunk.stats
        text = "".join(parts).strip()
        if holding and not tool_calls and parse_text_tool_call(text, {t.name for t in tools}) is None:
            on_delta(text)  # it was ordinary text after all
        return model, text, tuple(tool_calls), stats
