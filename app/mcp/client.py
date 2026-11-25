"""A small MCP client (JSON-RPC 2.0) for stdio servers and Streamable HTTP servers.

Only what Jarvis needs: initialize, tools/list (with paging) and tools/call. Stdio servers are
started from the command YOU wrote in config\\mcp.json (never from AI output), without a shell.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"                       # the legacy (initialize-handshake) revision Jarvis speaks
MODERN_VERSIONS = ("2026-07-28",)                     # per-request-metadata revisions Jarvis speaks, newest first
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
UNSUPPORTED_VERSION = -32022                          # UnsupportedProtocolVersionError (spec 2026-07-28, basic/versioning)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
PROBE_TIMEOUT = 5.0
CLIENT_INFO = {"name": "jarvis-assistant", "version": "2.4.0"}


class McpError(Exception):
    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def pick_version(supported: Any) -> tuple[str, str] | None:
    """(era, version) Jarvis and the server both speak - modern first - or None when there is none."""
    offered = [str(v) for v in supported] if isinstance(supported, (list, tuple)) else []
    for v in MODERN_VERSIONS:
        if v in offered:
            return "modern", v
    for v in LEGACY_VERSIONS:
        if v in offered:
            return "legacy", v
    return None


class _Base:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: dict[str, Any] = {}
        self.era = "legacy"                 # "modern" once server/discover (or a modern error) says so
        self.version = PROTOCOL_VERSION
        self.capabilities: dict[str, Any] = {}
        self.probe = True                   # set False (config "era": "legacy") to skip the server/discover probe

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    def _meta(self) -> dict:
        return {META_VERSION: self.version, META_CLIENT: CLIENT_INFO, META_CAPS: {}}

    def _params(self, params: dict | None) -> dict:
        """Modern servers need the protocol version, client identity and capabilities on EVERY request; legacy ones get the params unchanged."""
        if self.era != "modern":
            return params or {}
        out = dict(params or {})
        out["_meta"] = {**self._meta(), **(out.get("_meta") or {})}
        return out

    def _adopt(self, supported: Any) -> bool:
        """Select a mutual version from a server's list. True = modern (no handshake needed)."""
        choice = pick_version(supported)
        if choice is None:
            raise McpError(f"no common MCP version: the server speaks {', '.join(map(str, supported or [])) or 'nothing it named'}; "
                           f"Jarvis speaks {', '.join(MODERN_VERSIONS + LEGACY_VERSIONS)}")
        self.era, self.version = choice
        return self.era == "modern"

    def initialize(self) -> dict:
        """Dual-era connect: probe with ``server/discover`` (modern, stateless); a recognised modern answer keeps it modern, anything else falls back
        to the ``initialize`` handshake (spec 2026-07-28, 'Backward Compatibility with Initialization-Based Versions')."""
        if self.probe:
            self.era, self.version = "modern", MODERN_VERSIONS[0]
            try:
                found = self.request("server/discover", {}, timeout=min(PROBE_TIMEOUT, self.timeout))
                if isinstance(found, dict) and isinstance(found.get("supportedVersions"), list):
                    if self._adopt(found["supportedVersions"]):
                        self.capabilities = found.get("capabilities") or {}
                        self.server_info = (found.get("_meta") or {}).get(META_SERVER, {}) or {}
                        return found
                else:
                    self.era, self.version = "legacy", PROTOCOL_VERSION
            except McpError as exc:
                if exc.code == UNSUPPORTED_VERSION and isinstance(exc.data, dict):
                    if self._adopt(exc.data.get("supported")):       # raises when there is no common version
                        return {"supportedVersions": exc.data.get("supported")}
                else:
                    self.era, self.version = "legacy", PROTOCOL_VERSION
        self.era = "legacy"
        result = self.request("initialize", {"protocolVersion": self.version, "capabilities": {},
                                             "clientInfo": CLIENT_INFO})
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo", {}) or {}
            self.capabilities = result.get("capabilities") or {}
            self.version = str(result.get("protocolVersion") or self.version)
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):  # pages
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def close(self) -> None:
        pass

    @staticmethod
    def _result(message: dict) -> Any:
        if "error" in message:
            error = message["error"] or {}
            raise McpError(str(error.get("message") or error)[:300], error.get("code") if isinstance(error, dict) else None,
                           error.get("data") if isinstance(error, dict) else None)
        return message.get("result", {})


class StdioClient(_Base):
    """A server started as a child process; one JSON message per line on stdin/stdout."""

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0) -> None:
        super().__init__(timeout)
        merged = dict(os.environ)
        merged.update({str(k): str(v) for k, v in (env or {}).items()})
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(  # an argument list, never a shell
                [command, *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=merged, cwd=cwd or None, creationflags=flags, text=True, encoding="utf-8", bufsize=1, shell=False)
        except OSError as exc:
            raise McpError(f"could not start '{command}': {exc}") from exc
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, name="mcp-stdio", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # a log line
            if isinstance(message, dict) and "id" in message and ("result" in message or "error" in message):
                waiting = self._pending.pop(message["id"], None)
                if waiting is not None:
                    waiting.put(message)
        for waiting in list(self._pending.values()):
            waiting.put({"error": {"message": "the server stopped"}})

    def _send(self, message: dict) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise McpError("the server is not running")
        with self._lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        ident = next(self._ids)
        waiting: queue.Queue = queue.Queue()
        self._pending[ident] = waiting
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": self._params(params)})
        try:
            message = waiting.get(timeout=timeout or self.timeout)
        except queue.Empty:
            self._pending.pop(ident, None)
            raise McpError(f"no answer to {method} in {timeout or self.timeout:.0f} s") from None
        return self._result(message)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.terminate()
            self.process.wait(3)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


class HttpClient(_Base):
    """A Streamable HTTP server: JSON-RPC POSTs; the answer is JSON or a short event stream."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(timeout)
        self.url = url
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        **{str(k): str(v) for k, v in (headers or {}).items()}}
        self.session: str | None = None
        self.client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def _post(self, message: dict, timeout: float | None) -> httpx.Response:
        headers = dict(self.headers)
        if self.era == "modern":
            headers["MCP-Protocol-Version"] = self.version
        elif self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = self.version
        try:
            response = self.client.post(self.url, json=message, headers=headers, timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise McpError(f"could not reach {self.url}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            try:                                       # a modern server explains a 400 with a JSON-RPC error body (e.g. unsupported version)
                error = (response.json() or {}).get("error") or {}
            except ValueError:
                error = {}
            if isinstance(error, dict) and error.get("code") is not None:
                raise McpError(str(error.get("message") or "error")[:300], error.get("code"), error.get("data"))
            raise McpError(f"HTTP {response.status_code} from the server")
        self.session = response.headers.get("mcp-session-id", self.session)
        return response

    @staticmethod
    def _events(text: str) -> list[dict]:
        messages, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line.strip() and data:
                try:
                    messages.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
        return messages

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        ident = next(self._ids)
        response = self._post({"jsonrpc": "2.0", "id": ident, "method": method, "params": self._params(params)}, timeout)
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = self._events(response.text)
        else:
            body = response.json()
            messages = body if isinstance(body, list) else [body]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == ident:
                return self._result(message)
        raise McpError(f"the server sent no answer to {method}")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, None)

    def close(self) -> None:
        self.client.close()
