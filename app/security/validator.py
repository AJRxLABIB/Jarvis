"""Path validation for every file tool.

A path from the model is untrusted text. ``PathPolicy.resolve`` turns it into a
real, absolute path, and ``PathPolicy.risk`` says how dangerous an operation on
it is:

* Rejected outright: network (UNC) paths, "..", reserved device names (CON, NUL,
  COM1 ...), alternate data streams ("a.txt:x"), wildcard/control characters.
* Links and junctions are resolved BEFORE any check, so a shortcut folder that
  points into C:\\Windows is treated as C:\\Windows.
* Zones: SAFE (configured safe roots, e.g. Desktop/Documents/Downloads),
  OUTSIDE (anywhere else), PROTECTED (Windows, Program Files, ProgramData, the
  assistant's own config and data). Writing to PROTECTED is critical (blocked).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.config.files import PATH_TOKENS, PermissionsConfig, default_permissions
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import ToolError

if TYPE_CHECKING:
    from app.tools.base import ToolContext

Operation = Literal["read", "write", "delete", "open"]
MAX_PATH_TEXT = 400

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
_BAD_CHARS = set('<>"|?*') | {chr(i) for i in range(32)}
_DRIVE = re.compile(r"^[A-Za-z]:$")
_TOKEN = re.compile(r"^\{([A-Z]+)\}$")

EXECUTABLE_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
    ".scr", ".hta", ".cpl", ".jar", ".reg", ".lnk", ".pif", ".application", ".appref-ms", ".url", ".py", ".pyw",
})

FOLDER_ALIASES: dict[str, str] = {
    "desktop": "DESKTOP", "documents": "DOCUMENTS", "document": "DOCUMENTS", "my documents": "DOCUMENTS",
    "downloads": "DOWNLOADS", "download": "DOWNLOADS", "pictures": "PICTURES", "photos": "PICTURES",
    "images": "PICTURES", "music": "MUSIC", "videos": "VIDEOS", "video": "VIDEOS", "home": "HOME",
    "user folder": "HOME", "my folder": "HOME",
}
# Where a bare file name ("report.pdf") is looked up, in this order.
LOOKUP_FOLDERS = ("DESKTOP", "DOWNLOADS", "DOCUMENTS")


class Zone(str, Enum):
    SAFE = "safe"
    OUTSIDE = "outside"
    PROTECTED = "protected"


def check_component(name: str) -> str:
    """Validate one path component (a file or folder name)."""
    if not name or name in (".", ".."):
        raise ToolError("Paths may not contain '..' or empty parts.", "invalid_path")
    if "/" in name or "\\" in name:
        raise ToolError(f"'{name}' must be a name, not a path.", "invalid_path")
    bad = set(name) & _BAD_CHARS
    if bad or ":" in name:
        raise ToolError(f"'{name}' contains characters that are not allowed in a file name.", "invalid_path")
    if name != name.rstrip(" ."):
        raise ToolError(f"'{name}' ends with a space or dot, which Windows does not allow.", "invalid_path")
    if name.split(".")[0].lower() in _RESERVED:
        raise ToolError(f"'{name}' is a reserved Windows device name.", "invalid_path")
    if len(name) > 255:
        raise ToolError("That name is too long.", "invalid_path")
    return name


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_within(path: Path, root: Path) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def default_known_folders() -> dict[str, Path]:
    home = Path.home()
    names = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOADS": "Downloads",
             "PICTURES": "Pictures", "MUSIC": "Music", "VIDEOS": "Videos"}
    folders = {"HOME": home}
    for key, name in names.items():
        folders[key] = winapi.known_folder(key) or home / name
    return folders


def default_protected_roots() -> list[Path]:
    roots = []
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    return roots


@dataclass
class PathPolicy:
    known: dict[str, Path]
    safe_roots: list[Path]
    protected_roots: list[Path] = field(default_factory=list)

    @classmethod
    def build(
        cls, permissions: PermissionsConfig, known: dict[str, Path] | None = None,
        protected: list[Path] | None = None,
    ) -> "PathPolicy":
        known = known or default_known_folders()
        safe: list[Path] = []
        for entry in permissions.safe_roots:
            token = next((t for t in PATH_TOKENS if entry.startswith(t)), None)
            if token:
                base = known.get(token.strip("{}"))
                if base is None:
                    continue
                rest = [p for p in re.split(r"[\\/]+", entry[len(token):]) if p]
                safe.append(base.joinpath(*rest))
            else:
                safe.append(Path(entry))
        resolved_safe = [Path(os.path.realpath(p)) for p in safe]
        resolved_protected = [Path(os.path.realpath(p)) for p in (protected if protected is not None else default_protected_roots())]
        return cls(known={k: Path(os.path.realpath(v)) for k, v in known.items()},
                   safe_roots=resolved_safe, protected_roots=resolved_protected)

    @classmethod
    def from_context(cls, ctx: "ToolContext") -> "PathPolicy":
        permissions = ctx.config.permissions if ctx.config is not None else default_permissions()
        protected = default_protected_roots()
        if ctx.paths is not None:  # the assistant's own settings and data are off-limits to file tools
            protected += [ctx.paths.config_dir, ctx.paths.data_dir, ctx.paths.env_file]
        return cls.build(permissions, protected=protected)

    # ------------------------------------------------------------- resolving

    def _base_for(self, first: str, second: str | None) -> tuple[Path | None, int]:
        """(base folder, number of leading parts it consumed) for a known-folder start."""
        token = _TOKEN.match(first)
        if token:
            key = token.group(1)
            if key not in self.known:
                raise ToolError(f"Unknown folder {first}.", "invalid_path")
            return self.known[key], 1
        if second is not None:
            two = f"{first} {second}".lower()
            if two in FOLDER_ALIASES:
                return self.known.get(FOLDER_ALIASES[two]), 2
        alias = FOLDER_ALIASES.get(first.lower())
        return (self.known.get(alias), 1) if alias else (None, 0)

    def resolve(self, text: str, *, must_exist: bool = False, default_folder: str = "DESKTOP") -> Path:
        """Untrusted path text -> real absolute path (links followed)."""
        raw = text.strip().strip("\"'").strip()
        if not raw or len(raw) > MAX_PATH_TEXT:
            raise ToolError("Please give a file or folder name.", "invalid_path")
        if raw.startswith(("\\\\", "//")):
            raise ToolError("Network paths are not supported.", "invalid_path")
        if raw.startswith("~"):
            raw = str(self.known["HOME"]) + raw[1:]

        base: Path | None = None
        parts = [p for p in re.split(r"[\\/]+", raw) if p != ""]
        if os.name == "nt" and parts and _DRIVE.match(parts[0]):
            base, parts = Path(parts[0] + "\\"), parts[1:]
        elif os.name != "nt" and raw.startswith("/"):
            base = Path("/")
        elif parts:
            known_base, used = self._base_for(parts[0], parts[1] if len(parts) > 1 else None)
            if known_base is not None:
                base, parts = known_base, parts[used:]

        for part in parts:
            check_component(part)

        if base is None:
            if len(parts) == 1:  # a bare name: look for it in the usual places
                matches = [self.known[k] / parts[0] for k in LOOKUP_FOLDERS if k in self.known and (self.known[k] / parts[0]).exists()]
                if len(matches) == 1:
                    return Path(os.path.realpath(matches[0]))
                if len(matches) > 1:
                    places = ", ".join(str(m.parent) for m in matches)
                    raise ToolError(f"There is a '{parts[0]}' in more than one place ({places}). Which one?", "ambiguous")
            if must_exist:
                raise ToolError(
                    f"I couldn't find '{raw}' on your Desktop, in Downloads or in Documents. Which folder is it in?",
                    "not_found",
                )
            base = self.known[default_folder]

        path = Path(os.path.realpath(base.joinpath(*parts)))
        if must_exist and not path.exists():
            raise ToolError(f"'{path}' does not exist.", "not_found")
        return path

    # ------------------------------------------------------------------ risk

    def zone(self, path: Path) -> Zone:
        if any(is_within(path, root) for root in self.protected_roots):
            return Zone.PROTECTED
        if any(is_within(path, root) for root in self.safe_roots):
            return Zone.SAFE
        return Zone.OUTSIDE

    def is_root_folder(self, path: Path) -> bool:
        """A drive root, a known user folder or a safe root itself."""
        if _norm(path) == _norm(Path(path.anchor or "/")):
            return True
        return any(_norm(path) == _norm(p) for p in [*self.known.values(), *self.safe_roots])

    def risk(self, path: Path, operation: Operation) -> RiskLevel:
        zone = self.zone(path)
        if operation == "read":
            return RiskLevel.SAFE if zone == Zone.SAFE else RiskLevel.MODERATE
        if operation == "open":
            executable = path.suffix.lower() in EXECUTABLE_EXTENSIONS and path.is_file()
            if executable:
                return RiskLevel.DANGEROUS
            return RiskLevel.SAFE if zone == Zone.SAFE else RiskLevel.MODERATE
        if operation == "write":
            if zone == Zone.PROTECTED:
                return RiskLevel.CRITICAL
            return RiskLevel.SAFE if zone == Zone.SAFE else RiskLevel.DANGEROUS
        # delete
        if zone == Zone.PROTECTED or self.is_root_folder(path):
            return RiskLevel.CRITICAL
        return RiskLevel.DANGEROUS
