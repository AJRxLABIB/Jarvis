"""Terminal front-ends used until the GUI exists: model list and text chat.

The chat runs on one event loop. Blocking ``input()`` happens between turns,
and Ctrl+C during a reply cancels only that reply.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TextIO, TypeVar

from app.agent.conversation import ConversationManager, friendly_error
from app.agent.tool_manager import ToolExecution, ToolExecutor
from app.ai.base import AIError, ModelInfo
from app.ai.manager import AIManager, AIStatus
from app.memory.audit import AuditLog
from app.memory.conversations import ConversationStore
from app.memory.settings_store import SettingsStore
from app.security.confirmations import Confirmer, ConsoleConfirmer
from app.security.permissions import PermissionEngine
from app.tools.base import ToolContext
from app.tools.catalog import build_default_registry

if TYPE_CHECKING:
    from app.main import AppContext

T = TypeVar("T")

CHAT_HELP = """Commands:
  /models          list installed models
  /model <name>    switch model (remembered for next time)
  /status          show AI status
  /reset           forget the conversation so far
  /help            show this help
  /exit            quit (Ctrl+C also cancels a reply in progress)"""


def create_ai_manager(ctx: "AppContext") -> AIManager:
    return AIManager.from_settings(ctx.settings, SettingsStore(ctx.database))


def create_tool_executor(
    ctx: "AppContext", confirmer: Confirmer, *, ai: AIManager | None = None,
    on_screen_capture: Callable[[bool], None] | None = None,
) -> ToolExecutor:
    context = ToolContext(ctx.settings, ctx.paths, ctx.database, ctx.config)
    context.ai = ai  # vision tools ask the local model about screenshots
    context.on_screen_capture = on_screen_capture
    registry = build_default_registry()
    if ctx.settings.plugins_enabled:
        from app.plugins import load_plugins

        load_plugins(ctx.paths.data_dir / "plugins", registry)
    executor = ToolExecutor(
        registry,
        PermissionEngine(ctx.config.permissions),
        context,
        confirmer=confirmer,
        audit=AuditLog(ctx.database),
    )
    executor.mcp_hub = None
    if ctx.settings.mcp_enabled:
        from app.mcp.tools import McpHub

        executor.mcp_hub = McpHub(ctx.paths.config_dir, registry)
        executor.mcp_hub.start()  # in the background: their tools appear when the servers are ready
    executor.mcp_server = None
    if getattr(ctx.settings, "mcp_server_enabled", False):
        try:
            from app.mcp import server as mcp_server

            names = tuple(n.strip() for n in str(ctx.settings.mcp_server_tools or "").split(";") if n.strip()) or mcp_server.DEFAULT_TOOLS
            if mcp_server.token_hash(ctx.paths.data_dir):       # no token yet = nothing is served (run.py --mcp-server-token)
                executor.mcp_server = mcp_server.McpServer(registry, executor, ctx.paths.data_dir, ctx.settings.mcp_server_port, names)
                executor.mcp_server.start()
        except Exception:
            executor.mcp_server = None
    executor.a2a_server = None
    if getattr(ctx.settings, "a2a_server_enabled", False):
        try:
            from app.core import a2a_server

            skills = tuple(n.strip() for n in str(ctx.settings.a2a_server_tools or "").split(";") if n.strip()) or a2a_server.DEFAULT_TOOLS
            if a2a_server.token_hash(ctx.paths.data_dir):       # no token yet = nothing is served (run.py --a2a-server-token)
                executor.a2a_server = a2a_server.A2AServer(registry, executor, ctx.paths.data_dir, ctx.settings.a2a_server_port, skills)
                executor.a2a_server.start()
        except Exception:
            executor.a2a_server = None
    try:
        from app.tools.automation import seed_default_workflows

        seed_default_workflows(executor.context)
    except Exception:
        pass  # the built-in workflows are a convenience, never a startup blocker
    return executor


def describe_execution(execution: ToolExecution) -> str:
    """e.g. [tool] find_files(extension='pdf', folder='Downloads'): ok - Found 3 file(s) ..."""
    from app.utils.redaction import redact

    result = execution.result
    status = "ok" if result.ok else f"failed ({result.error})"
    args = ", ".join(f"{k}={v!r}" for k, v in execution.call.arguments.items())
    if len(args) > 120:
        args = args[:117] + "..."
    detail = f" - {result.message}" if result.message else ""
    return redact(f"[tool] {execution.call.name}({args}): {status}{detail}")


_LEVEL_LABELS = {0: "safe", 1: "moderate", 2: "dangerous", 3: "critical"}


def run_tools(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    registry = build_default_registry()
    engine = PermissionEngine(ctx.config.permissions)
    out.write(f"{len(registry)} tool(s) registered:\n")
    for tool in registry.tools:
        decision, level = engine.decide(tool.name, tool.level)
        if tool.risk is not None:
            label = f"level {int(level)}+ (depends on arguments)"
        else:
            label = f"level {int(level)} ({_LEVEL_LABELS[int(level)]}, {decision.value})"
        out.write(f"  {tool.name:<20} {label:<34} {tool.description}\n")
    return 0


def run_memory(ctx: "AppContext", query: str | None, out: TextIO = sys.stdout) -> int:
    """List (or search) saved memories."""
    from app.memory.manager import MemoryManager

    manager = MemoryManager(ctx.database)
    memories = manager.search(query) if query else manager.all()
    if not memories:
        out.write("No memories saved." if not query else f"No memories match '{query}'.")
        out.write("\n")
        return 0
    for memory in memories:
        topic = f"  [{memory.topic}]" if memory.topic else ""
        out.write(f"  #{memory.id:<4} {memory.updated_at[:10]}  {memory.content}{topic}\n")
    out.write(f"\n{len(memories)} memory(ies). Delete one with: python run.py --forget ID\n")
    return 0


def run_forget(ctx: "AppContext", memory_id: int, out: TextIO = sys.stdout) -> int:
    from app.memory.manager import MemoryManager
    from app.tools.base import ToolError

    try:
        memory = MemoryManager(ctx.database).forget(memory_id)
    except ToolError as exc:
        out.write(f"{exc}\n")
        return 1
    out.write(f"Forgotten: {memory.content}\n")
    return 0


def run_find_app(ctx: "AppContext", name: str, out: TextIO = sys.stdout) -> int:
    """Diagnostic: how an application name is resolved, and what each source knows."""
    from app.tools import applications

    out.write(f"Start Menu shortcuts indexed: {len(applications.START_MENU.entries())}\n")
    out.write(f"Windows Apps folder entries (incl. Store apps): {len(applications.APPS_FOLDER.entries())}\n")
    target = applications.resolve_application(name, ctx.config.apps)
    if target is None:
        out.write(f"'{name}': not found. Add it to config/apps.json or check it appears in the Start menu.\n")
        return 1
    out.write(f"'{name}' -> {target.display_name}\n  source: {target.source}\n  target: {target.target}\n")
    return 0


def run_windows(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    """Diagnostic: every visible top-level window and how the assistant classifies it."""
    from app.tools import winapi
    from app.tools.base import ToolError

    try:
        windows = winapi.top_level_windows()
    except ToolError as exc:
        out.write(f"{exc}\n")
        return 1
    out.write(f"{'hwnd':>10}  {'pid':>6}  {'process':<22} flags   title\n")
    for w in sorted(windows, key=lambda w: (w.process_name, w.title)):
        flags = ("A" if w.switchable else "-") + ("O" if w.owned else "-") + ("T" if w.tool_window else "-") + \
                ("C" if w.cloaked else "-") + ("M" if w.minimized else "-")
        out.write(f"{w.hwnd:>10}  {w.pid:>6}  {w.process_name[:22]:<22} {flags}   {w.title[:60]!r}\n")
    out.write("\nflags: A=in Alt+Tab (used first)  O=owned  T=tool window  C=cloaked (ignored)  M=minimized\n")
    return 0


def run_browser_profiles(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    """Diagnostic: the profiles 'open Chrome' can choose from, and which one it will open."""
    from app.tools.base import ToolError
    from app.tools.browser_profiles import CHROMIUM_BROWSERS, choose_profile, load_profiles, user_data_dir

    settings = {"chrome.exe": ctx.settings.chrome_profile, "msedge.exe": ctx.settings.edge_profile}
    shown = False
    for exe, (browser, _) in CHROMIUM_BROWSERS.items():
        folder = user_data_dir(exe)
        profiles = load_profiles(folder) if folder is not None else []
        if not profiles:
            continue
        shown = True
        wanted = settings.get(exe, "last")
        chosen = None
        try:
            chosen = choose_profile(profiles, wanted, browser)
            opens = f"opens '{chosen.name}' ({chosen.directory})" if chosen else "shows the profile picker"
        except ToolError as exc:
            opens = f"problem: {exc}"
        out.write(f"{browser}  (setting: {wanted!r} -> {opens})\n")
        for p in profiles:
            mark = "*" if chosen is not None and p.directory == chosen.directory else " "
            out.write(f"  {mark} {p.directory:<11} {p.name:<24} {p.account}{'   (last used)' if p.last_used else ''}\n")
    if not shown:
        out.write("No Chrome, Edge or Brave profiles found.\n")
        return 1
    out.write("\n* = opened by 'open ...'. Change it with CHROME_PROFILE / EDGE_PROFILE in .env "
              "(a profile name, 'last', or 'none').\n")
    return 0


def run_screen_test(ctx: "AppContext", seconds: int = 3, out: TextIO = sys.stdout) -> int:
    """Diagnostic: capture the active window after a countdown and read its text. Nothing is saved."""
    import time

    from app.tools.base import ToolError
    from app.vision import capture as cap
    from app.vision.ocr import WindowsOcr, create_ocr

    s = ctx.settings
    out.write(f"Screen capture for the assistant is {'ON' if s.screen_capture_enabled else 'OFF'} "
              "(SCREEN_CAPTURE_ENABLED). This test runs only because you started it.\n")
    seconds = max(0, min(seconds, 30))
    out.write(f"Switch to the window to read. Capturing in {seconds} s...\n")
    out.flush()
    time.sleep(seconds)
    context = ToolContext(s, ctx.paths, ctx.database, ctx.config)
    try:
        out.write("[SCREEN CAPTURE ACTIVE]\n")
        shot = cap.capture(context)
        out.write(f"Captured {shot.source}: {shot.size[0]}x{shot.size[1]} pixels (kept in memory only).\n")
        engine = create_ocr(s)
        if isinstance(engine, WindowsOcr):
            out.write(f"Windows OCR languages: {', '.join(engine.languages()) or 'none'}\n")
        started = time.perf_counter()
        result = asyncio.run(engine.recognize(shot.image))
    except ToolError as exc:
        out.write(f"{exc}\n")
        return 1
    from app.utils.redaction import redact

    elapsed = time.perf_counter() - started
    out.write(f"OCR ({result.engine}, {result.language}) found {len(result.lines)} line(s) in {elapsed:.1f} s:\n")
    for line in result.lines[:25]:
        out.write(f"  {redact(line.text)[:110]}\n")
    if len(result.lines) > 25:
        out.write(f"  ... and {len(result.lines) - 25} more\n")
    return 0


def probe_ai(ctx: "AppContext") -> AIStatus:
    async def _probe() -> AIStatus:
        ai = create_ai_manager(ctx)
        try:
            return await ai.status()
        finally:
            await ai.aclose()

    return asyncio.run(_probe())


def _format_size(size: int | None) -> str:
    return f"{size / 1024**3:.1f} GB" if size else "?"


def format_model(model: ModelInfo, selected: ModelInfo | None = None) -> str:
    marker = "*" if selected is not None and model.name == selected.name else " "
    if model.is_embedding_only:
        tools = "embedding only"
    else:
        tools = {True: "tools: yes", False: "tools: no", None: "tools: unknown"}[model.supports_tools]
    details = ", ".join(x for x in (model.parameter_size, model.quantization, _format_size(model.size_bytes), tools) if x)
    return f" {marker} {model.name}  ({details})"


def print_status(status: AIStatus, out: TextIO) -> None:
    if not status.reachable:
        out.write(f"{status.message}\n")
        return
    out.write(f"Ollama {status.version or ''} is running. {len(status.models)} model(s) installed.\n".replace("  ", " "))
    for model in status.models:
        out.write(format_model(model, status.selected) + "\n")
    if status.selected:
        out.write(f"Using: {status.selected.name} (selected by: {status.selection_source})\n")
        if status.selected.supports_tools is False:
            out.write("Warning: this model does not support tool calling, which later phases need.\n")
    if status.message:
        out.write(f"{status.message}\n")


def run_models(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    status = probe_ai(ctx)
    print_status(status, out)
    return 0 if status.reachable and status.selected else 1


def _run(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, T]) -> T:
    task = loop.create_task(coro)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        raise


def run_chat(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    ai = create_ai_manager(ctx)
    name = ctx.settings.assistant_name
    loop = asyncio.new_event_loop()

    def write(text: str) -> None:
        out.write(text)
        out.flush()

    executor = create_tool_executor(
        ctx, ConsoleConfirmer(write=lambda text: write(text + "\n")), ai=ai,
        on_screen_capture=lambda active: active and write("\n  [SCREEN CAPTURE ACTIVE]\n"))
    conversation = ConversationManager(
        ai, ctx.settings, ConversationStore(ctx.database), tools=executor,
        on_tool=lambda e: write(f"\n  {describe_execution(e)}\n"),
    )

    try:
        status = _run(loop, ai.status())
        print_status(status, out)
        if not status.reachable:
            return 1
        write(f"\nText chat with {name}. Type /help for commands.\n\n")

        while True:
            try:
                line = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                write("\n")
                break
            if not line:
                continue
            if line.startswith("/"):
                command, _, argument = line.partition(" ")
                command = command.lower()
                if command in ("/exit", "/quit"):
                    break
                if command == "/help":
                    write(CHAT_HELP + "\n")
                elif command == "/reset":
                    conversation.reset()
                    write("Conversation cleared.\n")
                elif command in ("/models", "/status"):
                    try:
                        print_status(_run(loop, ai.status()), out)
                    except KeyboardInterrupt:
                        write("\n[cancelled]\n")
                elif command == "/model":
                    if not argument.strip():
                        write("Usage: /model <name>\n")
                        continue
                    try:
                        model = _run(loop, ai.set_model(argument.strip()))
                        write(f"Now using {model.name}.\n")
                    except AIError as exc:
                        write(f"{friendly_error(exc)[1]}\n")
                    except KeyboardInterrupt:
                        write("\n[cancelled]\n")
                else:
                    write("Unknown command. Type /help.\n")
                continue

            write(f"{name}: ")
            try:
                reply = _run(loop, conversation.send(line, on_delta=write))
            except KeyboardInterrupt:
                write("\n[cancelled]\n\n")
                continue
            if not reply.ok:
                write(reply.text)
            if getattr(reply, "checklist", ""):
                write("\n\n" + reply.checklist)
            write("\n\n")
        return 0
    finally:
        loop.run_until_complete(executor.context.aclose())
        loop.run_until_complete(ai.aclose())
        loop.close()
