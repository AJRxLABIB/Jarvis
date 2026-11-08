"""Runs INSIDE the sandbox child process (started by plugin_sandbox.probe): imports one plugin file with an audit hook that blocks network, process
starts, native-library loads and file writes, then prints what the plugin declares as JSON. Not imported by the app itself.

argv: <repo root> <plugin file> <parent sys.path, os.pathsep-joined>. Output (last stdout line): {"ok": true, "tools": {name: level}, "version": ..., "doc": ..., "blocked": [...]}.
"""

import importlib.util
import json
import sys

BLOCKED_EVENTS = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname", "subprocess.Popen", "os.system", "os.exec", "os.spawn",
                  "os.posix_spawn", "os.fork", "os.forkpty", "ctypes.dlopen", "ctypes.cdll", "ctypes.CDLL", "os.remove", "os.rename", "os.rmdir", "os.mkdir",
                  "shutil.rmtree", "shutil.move", "shutil.copyfile", "winreg.SetValue", "winreg.CreateKey", "os.startfile", "webbrowser.open")
blocked: list[str] = []


def hook(event: str, args: tuple) -> None:
    if event == "open":
        mode = str(args[1]) if len(args) > 1 and args[1] is not None else "r"
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if any(c in mode for c in "wax+") or flags & 0o3:           # O_WRONLY / O_RDWR
            blocked.append(f"open for writing: {str(args[0])[:80]}")
            raise PermissionError("the plugin probe does not allow writing files")
        return
    if event in BLOCKED_EVENTS:
        blocked.append(event)
        raise PermissionError(f"the plugin probe blocks {event}")


def main() -> None:
    root, path = sys.argv[1], sys.argv[2]
    import os

    sys.path[:0] = [root] + [p for p in (sys.argv[3].split(os.pathsep) if len(sys.argv) > 3 else []) if p and p != root]   # -I hides user site: use the parent's
    from app.tools.base import Tool                       # imported before the hook so the app's own modules load normally

    sys.addaudithook(hook)
    out: dict = {"ok": False, "blocked": blocked}
    try:
        spec = importlib.util.spec_from_file_location("jarvis_plugin_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tools = getattr(module, "TOOLS", None)
        if not isinstance(tools, (list, tuple)) or not all(isinstance(t, Tool) for t in tools):
            raise TypeError("TOOLS must be a list of app.tools.base.Tool")
        out.update(ok=True, tools={t.name: int(t.level) for t in tools}, version=str(getattr(module, "VERSION", "1.0")),
                   doc=((module.__doc__ or "").strip().splitlines() or [""])[0][:200])
    except BaseException as exc:                          # SystemExit from a plugin counts as a failure to report, not a way out
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    sys.stdout.write("\n" + json.dumps(out) + "\n")


main()
