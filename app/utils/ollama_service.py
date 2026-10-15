"""Starting the local Ollama app when Jarvis needs it and it isn't running.

Only for an Ollama on this computer (OLLAMA_HOST on localhost), only the
installed Ollama program itself (never a command line), and only when
AUTO_START_OLLAMA is on.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("ollama_service")


def find_ollama_app() -> Path | None:
    """The installed Ollama tray app ("ollama app.exe"), which starts the Ollama server."""
    base = os.environ.get("LOCALAPPDATA")
    candidates = []
    if base:
        candidates += [Path(base) / "Programs" / "Ollama" / "ollama app.exe",
                       Path(base) / "Programs" / "Ollama" / "ollama.exe"]
    for variable in ("ProgramFiles", "ProgramW6432"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "Ollama" / "ollama app.exe")
    return next((path for path in candidates if path.is_file()), None)


def ollama_running() -> bool:
    import psutil

    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith("ollama"):
            return True
    return False


def find_ollama_server() -> Path | None:
    """ollama.exe, the server on its own (no window)."""
    app = find_ollama_app()
    if app is None:
        return None
    server = app.with_name("ollama.exe")
    return server if server.is_file() else None


# Started by Jarvis, the Ollama server gets these (unless you set them yourself): flash attention, and the
# model's conversation memory (KV cache) at 8 bits instead of 16 - about half the GPU memory for it, with
# no noticeable change in answers. That is what lets a 4B model and speech recognition share a 6 GB GPU.
FAST_MEMORY_ENV = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}


def server_environment(fast_memory: bool = True) -> dict[str, str]:
    import os

    env = dict(os.environ)
    if fast_memory:
        for key, value in FAST_MEMORY_ENV.items():
            env.setdefault(key, value)  # a value you set yourself wins
    return env


def start_ollama(fast_memory: bool = True) -> bool:
    """Start Ollama if it is installed and not running. Returns True if it was started.

    Starts only the background server (`ollama serve`, no window) when it can, so nothing pops up
    over Jarvis; the Ollama app (which opens a window) is the fallback.
    """
    from app.tools import winapi
    from app.tools.base import ToolError

    if not winapi.is_windows() or ollama_running():
        return False
    server = find_ollama_server()
    app = find_ollama_app()
    try:
        if server is not None:
            winapi.start_program(str(server), ["serve"], hidden_console=True,  # its model runners stay hidden too
                                 env=server_environment(fast_memory))
            target = server
        elif app is not None:
            winapi.start_file(str(app))
            target = app
        else:
            return False
    except ToolError:
        _log.warning("could not start Ollama", exc_info=True)
        return False
    log_event(_log, "ollama_started", path=str(target), fast_memory=fast_memory and server is not None)
    return True
