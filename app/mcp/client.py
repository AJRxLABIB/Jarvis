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

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "jarvis-assistant", "version": "2.4.0"}


class McpError(Exception):
    pass


class _Base:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: dict[str, Any] = {}

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    def initialize(self) -> dict:
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": CLIENT_INFO})
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
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
            raise McpError(str(error.get("message") or error)[:300])
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
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
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
        if self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            response = self.client.post(self.url, json=message, headers=headers, timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise McpError(f"could not reach {self.url}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
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
        response = self._post({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}, timeout)
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
