"""PC-side handlers for JARVIS Mobile commands that don't map to an existing tool: capability detection, files,
a still screen frame, and mouse/keyboard in an active session.

Safety kept from the desktop:
* Files use the SAME PathPolicy as the file tools (Desktop / Downloads / Documents and your configured safe
  roots only; the assistant's own data is off-limits). Reads and writes outside a safe root are refused here -
  a phone never gets the free run of the disk.
* Screen frames and mouse/keyboard work ONLY when Windows is unlocked and the session is active (checked live),
  never on the lock screen. A blocked window (password managers) is never captured, and the SCREEN CAPTURE
  ACTIVE indicator shows while a frame is taken. Passwords are never typed here.
* Every call still arrives through a signed, permitted, biometric-signed (for changes) envelope.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from app.remote.mobile import MobileError

MAX_LIST = 300
MAX_TEXT_FILE = 200_000
MAX_GET_BYTES = 25 * 1024 * 1024
MAX_PUT_BYTES = 25 * 1024 * 1024


def _policy(ctx: Any):
    return ctx.get_path_policy()


def _safe(policy, path: Path, *, for_write: bool) -> None:
    from app.security.validator import Zone

    zone = policy.zone(path)
    if zone == Zone.PROTECTED:
        raise MobileError(403, "protected", "That location is off-limits.")
    if for_write and zone != Zone.SAFE:
        raise MobileError(403, "outside_safe", "The phone can only write inside your Desktop, Downloads, Documents "
                          "or a folder you've allowed on the PC.")
    if not for_write and zone == Zone.OUTSIDE:
        raise MobileError(403, "outside_safe", "The phone can only read files in your Desktop, Downloads, Documents "
                          "or a folder you've allowed on the PC.")


def list_files(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = _policy(ctx)
    folder = policy.resolve(str(arguments.get("path", "Desktop"))[:400], must_exist=True)
    if not folder.is_dir():
        raise MobileError(400, "not_a_folder", "That isn't a folder.")
    _safe(policy, folder, for_write=False)
    entries = []
    try:
        for entry in sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:MAX_LIST]:
            try:
                stat = entry.stat()
                entries.append({"name": entry.name, "dir": entry.is_dir(),
                                "size": stat.st_size if entry.is_file() else None, "modified": int(stat.st_mtime)})
            except OSError:
                continue
    except PermissionError as exc:
        raise MobileError(403, "denied", "Windows wouldn't let me read that folder.") from exc
    return {"ok": True, "path": str(folder), "entries": entries}


def read_file(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = _policy(ctx)
    path = policy.resolve(str(arguments.get("path", ""))[:400], must_exist=True)
    _safe(policy, path, for_write=False)
    if not path.is_file() or path.stat().st_size > MAX_TEXT_FILE:
        raise MobileError(400, "too_big", "That file is too big to show as text; use Download instead.")
    try:
        return {"ok": True, "path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}
    except OSError as exc:
        raise MobileError(400, "unreadable", "Couldn't read that file.") from exc


def get_file(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = _policy(ctx)
    path = policy.resolve(str(arguments.get("path", ""))[:400], must_exist=True)
    _safe(policy, path, for_write=False)
    if not path.is_file():
        raise MobileError(400, "not_a_file", "That isn't a file.")
    if path.stat().st_size > MAX_GET_BYTES:
        raise MobileError(413, "too_big", f"That file is larger than {MAX_GET_BYTES // (1024 * 1024)} MB.")
    data = path.read_bytes()
    return {"ok": True, "name": path.name, "size": len(data), "data": base64.b64encode(data).decode()}


def put_file(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = _policy(ctx)
    folder = policy.resolve(str(arguments.get("path", "Downloads"))[:400], must_exist=True)
    if not folder.is_dir():
        raise MobileError(400, "not_a_folder", "The destination isn't a folder.")
    _safe(policy, folder, for_write=True)
    from app.security.validator import check_component

    from app.tools.base import ToolError

    name = str(arguments.get("name", "")).strip()
    if not name:
        raise MobileError(400, "malformed", "The upload needs a file name.")
    try:
        check_component(name)
    except ToolError as exc:
        raise MobileError(400, "malformed", "The file name isn't valid.") from exc
    try:
        blob = base64.b64decode(str(arguments.get("data", "")), validate=True)
    except Exception as exc:
        raise MobileError(400, "malformed", "The upload data isn't valid.") from exc
    if len(blob) > MAX_PUT_BYTES:
        raise MobileError(413, "too_big", f"Uploads are limited to {MAX_PUT_BYTES // (1024 * 1024)} MB.")
    target = folder / name
    if target.exists():
        raise MobileError(409, "exists", f"'{name}' already exists there.")
    _safe(policy, target, for_write=True)
    target.write_bytes(blob)
    return {"ok": True, "path": str(target), "size": len(blob)}


# ------------------------------------------------------------------ screen / input (active session only)


def _require_unlocked(ctx: Any) -> None:
    from app.remote import pc_state

    state = pc_state.session()
    if state.locked is not False:  # None (unknown) or True (locked) both block
        raise MobileError(409, "locked", "The screen is locked or its state is unknown, so I won't capture it or "
                          "move the mouse. Sign in to Windows first.")


def screen_frame(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from app.vision import capture as cap

    if not ctx.settings.screen_capture_enabled:
        raise MobileError(403, "screen_off", "Screen capture is turned off on the PC (Settings > Privacy).")
    _require_unlocked(ctx)
    max_side = arguments.get("max_side")
    max_side = max_side if isinstance(max_side, int) and 320 <= max_side <= 2000 else 1280
    with cap.capture_indicator(ctx):
        shot = cap.capture(ctx, target="screen")
    png = cap.png_bytes(shot.image, max_side=max_side)
    return {"ok": True, "image": base64.b64encode(png).decode(), "what": shot.source,
            "size": [shot.image.width, shot.image.height]}


def mouse(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from app.tools import winapi

    _require_unlocked(ctx)
    action = arguments.get("action")
    width, height = winapi.screen_size()
    if action in ("move", "click", "double", "right"):
        x, y = arguments.get("x"), arguments.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or not (0 <= x <= 1 and 0 <= y <= 1):
            raise MobileError(400, "malformed", "mouse needs x and y as fractions 0..1 of the screen.")
        px, py = int(x * width), int(y * height)
        if action == "move":
            winapi.move_cursor(px, py)
        else:
            winapi.click_at(px, py, {"click": "left", "double": "left", "right": "right"}[action],
                            double=action == "double")
        return {"ok": True}
    if action == "scroll":
        amount = arguments.get("amount", 0)
        if not isinstance(amount, int) or not -20 <= amount <= 20:
            raise MobileError(400, "malformed", "scroll amount must be -20..20")
        winapi.scroll(amount)
        return {"ok": True}
    raise MobileError(400, "malformed", "mouse action must be move/click/double/right/scroll")


def key(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    from app.tools import winapi

    _require_unlocked(ctx)
    if isinstance(arguments.get("combo"), str) and arguments["combo"].strip():
        combo = arguments["combo"].strip().lower()[:60]
        if any(part in ("win", "windows") for part in combo.split("+")) and len(combo.split("+")) == 1:
            raise MobileError(400, "blocked", "The Windows key alone isn't sent from the phone.")
        winapi.press_combo(combo)
        return {"ok": True}
    text = arguments.get("text")
    if isinstance(text, str) and text:
        winapi.type_unicode(text[:2000])
        return {"ok": True}
    raise MobileError(400, "malformed", "key needs 'combo' (e.g. 'ctrl+s') or 'text'.")
