"""File and folder tools. Every path goes through ``PathPolicy`` (see
app/security/validator.py). Nothing is overwritten, and delete means Recycle Bin."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.security.permissions import RiskLevel
from app.security.validator import EXECUTABLE_EXTENSIONS, LOOKUP_FOLDERS, PathPolicy, check_component
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult, clamped_int
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("files")

MAX_LIST = 50
MAX_SCAN_ENTRIES = 200_000
MAX_SCAN_SECONDS = 8.0
MASS_DELETE_LIMIT = 500
MAX_TEXT = 20_000
SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv", "appdata", "$recycle.bin",
                       "system volume information", ".cache", "site-packages"})


def _policy(ctx: ToolContext) -> PathPolicy:
    return ctx.get_path_policy()


def _size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


def _walk(root: Path, *, include_hidden: bool = False):
    """os.walk with safety limits; yields (dirpath, dirnames, filenames)."""
    started, seen = time.monotonic(), 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and (include_hidden or not d.startswith("."))]
        seen += len(dirnames) + len(filenames)
        yield dirpath, dirnames, filenames
        if seen > MAX_SCAN_ENTRIES or time.monotonic() - started > MAX_SCAN_SECONDS:
            return


def _count_items(path: Path, limit: int) -> int:
    count = 0
    for _, dirnames, filenames in _walk(path, include_hidden=True):
        count += len(dirnames) + len(filenames)
        if count > limit:
            break
    return count


def _safe_risk(fn):
    """Risk functions must never raise: an invalid path is refused later by the handler
    with a clear message, and until then it counts as dangerous (never as safe)."""

    def wrapper(args: Any, ctx: ToolContext) -> RiskLevel:
        try:
            return fn(args, ctx)
        except ToolError:
            return RiskLevel.DANGEROUS

    return wrapper


def _described(path: Path) -> str:
    return f"'{path.name or path}' in {path.parent}" if path.name else str(path)


# ------------------------------------------------------------------ open / list


class PathArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="File or folder: a name, 'Downloads', 'Desktop\\report.pdf' or a full path")


def open_path(args: PathArgs, ctx: ToolContext) -> ToolResult:
    path = _policy(ctx).resolve(args.path, must_exist=True)
    winapi.start_file(str(path))
    kind = "folder" if path.is_dir() else "file"
    return ToolResult.success(f"Opened the {kind} {path.name or path}.", path=str(path), kind=kind)


class ListArgs(ToolArgs):
    path: str = Field(default="Desktop", max_length=400, description="Folder to list")
    sort_by: Literal["name", "modified", "size"] = Field(default="modified", description="Sort order")
    limit: clamped_int(1, MAX_LIST, 20, "How many items to show")  # type: ignore[valid-type]


def list_folder(args: ListArgs, ctx: ToolContext) -> ToolResult:
    folder = _policy(ctx).resolve(args.path, must_exist=True)
    if not folder.is_dir():
        raise ToolError(f"{folder.name} is a file, not a folder.", "not_a_folder")
    rows = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if winapi.is_hidden(entry):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                is_dir = entry.is_dir()
                rows.append({"name": entry.name, "type": "folder" if is_dir else "file",
                             "size": None if is_dir else stat.st_size, "modified": stat.st_mtime})
    except PermissionError:
        raise ToolError(f"Windows denied access to {folder}.", "access_denied") from None
    key = {"name": lambda r: r["name"].lower(), "modified": lambda r: -r["modified"],
           "size": lambda r: -(r["size"] or 0)}[args.sort_by]
    rows.sort(key=key)
    total = len(rows)
    shown = [dict(r, modified=_when(r["modified"]), size=_size_text(r["size"]) if r["size"] is not None else None)
             for r in rows[: args.limit]]
    names = ", ".join(r["name"] for r in shown[:5])
    return ToolResult.success(f"{folder.name or folder} has {total} item(s). {('First: ' + names) if names else ''}".strip(),
                              folder=str(folder), total=total, items=shown)


COMMON_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "jpg", "jpeg", "png", "gif", "bmp", "webp",
    "svg", "mp3", "wav", "flac", "mp4", "mkv", "avi", "mov", "zip", "rar", "7z", "exe", "msi", "py", "js", "ts",
    "html", "css", "json", "md", "php", "java", "c", "cpp", "iso", "epub", "psd",
})
_TYPE_WORDS = {"photo": "jpg", "photos": "jpg", "image": "jpg", "images": "jpg", "picture": "jpg", "pictures": "jpg",
               "word": "docx", "excel": "xlsx", "powerpoint": "pptx", "text": "txt", "video": "mp4", "videos": "mp4"}
_GENERIC_WORDS = {"file", "files", "document", "documents", "all", "my", "the", "any", "every", "some", "type", "types"}


def _clean_extension(value: str) -> str:
    value = value.strip().lower().lstrip("*.").strip()
    if value.endswith("s") and value[:-1] in COMMON_EXTENSIONS:  # "pdfs" -> "pdf"
        value = value[:-1]
    return _TYPE_WORDS.get(value, value)


class FindArgs(ToolArgs):
    folder: str = Field(default="all", max_length=400,
                        validation_alias=AliasChoices("folder", "path", "directory", "location", "in_folder"),
                        description="Where to search: 'all' (Desktop, Downloads, Documents) or a folder like 'Downloads'")
    name_contains: str | None = Field(default=None, max_length=100,
                                      validation_alias=AliasChoices("name_contains", "name", "query", "keyword", "search"),
                                      description="Part of the file NAME only. Leave empty when searching by type")
    extension: str | None = Field(default=None, max_length=24,
                                  validation_alias=AliasChoices("extension", "file_type", "type", "ext", "filetype", "format"),
                                  description="File type, e.g. 'pdf', 'docx', 'jpg'")
    modified_within_days: int | None = Field(default=None, ge=1, le=3650,
                                             description="Only if the user asked for recent files; otherwise leave empty")
    about: str | None = Field(default=None, max_length=120,
                              validation_alias=AliasChoices("about", "topic", "content", "containing", "text"),
                              description="What the file is ABOUT / the words inside it, e.g. 'thesis', "
                                          "'electricity bill'. Searches inside PDF, Word and text files")
    limit: clamped_int(1, MAX_LIST, 15, "How many results to show")  # type: ignore[valid-type]

    @field_validator("modified_within_days", mode="before")
    @classmethod
    def _zero_days_means_any(cls, value: Any) -> Any:
        try:
            return None if value is not None and int(float(value)) <= 0 else value
        except (TypeError, ValueError):
            return value

    @field_validator("extension")
    @classmethod
    def _ext(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = _clean_extension(value)
        if not value.isalnum():
            raise ValueError("extension must be letters/digits, e.g. 'pdf'")
        return value

    @model_validator(mode="after")
    def _tidy(self) -> "FindArgs":
        """Small models put the type into the name ("PDF files"): move it where it belongs."""
        if self.name_contains:
            kept = []
            for word in self.name_contains.split():
                bare = _clean_extension(word)
                if bare in COMMON_EXTENSIONS and self.extension in (None, bare):
                    self.extension = bare
                elif word.lower() not in _GENERIC_WORDS:
                    kept.append(word)
            self.name_contains = " ".join(kept) or None
        return self


def find_files(args: FindArgs, ctx: ToolContext) -> ToolResult:
    if not args.name_contains and not args.extension and not args.modified_within_days and not args.about:
        raise ToolError("What should I look for? Give part of a name, a file type or what it's about.", "missing_query")
    policy = _policy(ctx)
    if args.folder.strip().lower() in ("all", "everywhere", "my files", ""):
        roots = [policy.known[k] for k in LOOKUP_FOLDERS if k in policy.known]
    else:
        roots = [policy.resolve(args.folder, must_exist=True)]
    needle = (args.name_contains or "").lower()
    suffix = f".{args.extension}" if args.extension else None
    cutoff = time.time() - args.modified_within_days * 86400 if args.modified_within_days else None
    found: list[tuple[float, Path, int]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _, filenames in _walk(root):
            for name in filenames:
                lower = name.lower()
                if suffix and not lower.endswith(suffix):
                    continue
                if needle and needle not in lower:
                    continue
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if cutoff and stat.st_mtime < cutoff:
                    continue
                found.append((stat.st_mtime, path, stat.st_size))
    found.sort(key=lambda f: f[0], reverse=True)
    if args.about:
        found = _by_content(found, args.about)
    results = [{"name": p.name, "path": str(p), "size": _size_text(size), "modified": _when(mtime)}
               for mtime, p, size in found[: args.limit]]
    where = ", ".join(r.name for r in roots)
    filters = []
    if args.extension:
        filters.append(f"type .{args.extension}")
    if args.name_contains:
        filters.append(f"name containing '{args.name_contains}'")
    if args.modified_within_days:
        filters.append(f"changed in the last {args.modified_within_days} day(s)")
    if args.about:
        filters.append(f"about '{args.about}'")
    described = " and ".join(filters)
    searched = {"searched": [str(r) for r in roots], "filters": described}
    if not found:
        return ToolResult.success(f"No files with {described} in {where}.", total=0, files=[], **searched)
    return ToolResult.success(f"Found {len(found)} file(s) with {described} in {where}; newest: {results[0]['name']}.",
                              total=len(found), files=results, **searched)


CONTENT_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".rtf", ".json", ".html", ".htm"}
CONTENT_MAX_FILES = 150  # newest first; older files are only matched by name
CONTENT_MAX_BYTES = 15_000_000
CONTENT_SECONDS = 12.0
_STOP_WORDS = {"the", "a", "an", "about", "my", "of", "for", "and", "file", "files", "document", "documents", "pdf",
               "doc", "that", "this", "with", "in", "on", "to"}


def _terms(about: str) -> list[str]:
    return [w for w in re.findall(r"[\wঀ-৿]+", about.lower()) if len(w) >= 3 and w not in _STOP_WORDS]


def _document_text(path: Path) -> str:
    """The first part of a document's text (a few pages is enough to know what it's about)."""
    from app.memory.semantic import read_docx

    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:15])
    if ext == ".docx":
        return read_docx(path)[:200_000]
    with open(path, encoding="utf-8", errors="ignore") as handle:
        return handle.read(200_000)


def _by_content(found: list[tuple[float, Path, int]], about: str) -> list[tuple[float, Path, int]]:
    """Files whose name or text mention every word of ``about`` - best matches first, then newest."""
    terms = _terms(about)
    if not terms:
        return found
    started = time.monotonic()
    scored: list[tuple[int, float, Path, int]] = []
    read = 0
    for mtime, path, size in found:
        name = path.stem.lower().replace("_", " ").replace("-", " ")
        in_name = sum(term in name for term in terms)
        hits = 0
        if in_name < len(terms) and path.suffix.lower() in CONTENT_EXTENSIONS and size <= CONTENT_MAX_BYTES \
                and read < CONTENT_MAX_FILES and time.monotonic() - started < CONTENT_SECONDS:
            read += 1
            try:
                text = _document_text(path).lower()
            except Exception:
                text = ""
            hits = sum(text.count(term) for term in terms)
            covered = sum(term in name or term in text for term in terms)
        else:
            covered = in_name
        if covered == len(terms):
            scored.append((in_name * 100 + min(hits, 99), mtime, path, size))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    log_event(_log, "content_search", files_read=read, matches=len(scored))
    return [(mtime, path, size) for _, mtime, path, size in scored]


def file_info(args: PathArgs, ctx: ToolContext) -> ToolResult:
    path = _policy(ctx).resolve(args.path, must_exist=True)
    stat = path.stat()
    data: dict[str, Any] = {"path": str(path), "type": "folder" if path.is_dir() else "file",
                            "modified": _when(stat.st_mtime), "created": _when(stat.st_ctime)}
    if path.is_dir():
        total, files, folders = 0, 0, 0
        for dirpath, dirnames, filenames in _walk(path, include_hidden=True):
            folders += len(dirnames)
            for name in filenames:
                files += 1
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
        data.update(size=_size_text(total), files=files, folders=folders)
        message = f"{path.name} contains {files} files in {folders} folders, {_size_text(total)} in total."
    else:
        data.update(size=_size_text(stat.st_size), extension=path.suffix.lower())
        message = f"{path.name} is {_size_text(stat.st_size)}, last modified {data['modified']}."
    return ToolResult.success(message, **data)


# ----------------------------------------------------------------- create


class CreateArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="Where and what, e.g. 'Desktop\\Projects' or 'notes.txt'")
    kind: Literal["folder", "file"] = Field(default="folder", description="What to create: a folder or a text file")
    text: str = Field(default="", max_length=MAX_TEXT, description="Text to put in a new file (files only)")


def _create_risk(args: CreateArgs, ctx: ToolContext) -> RiskLevel:
    path = _policy(ctx).resolve(args.path)
    level = _policy(ctx).risk(path, "write")
    if args.kind == "file" and path.suffix.lower() in EXECUTABLE_EXTENSIONS:
        level = max(level, RiskLevel.DANGEROUS)  # a new script or program file
    return level


def create_item(args: CreateArgs, ctx: ToolContext) -> ToolResult:
    path = _policy(ctx).resolve(args.path)
    if path.exists():
        raise ToolError(f"'{path.name}' already exists in {path.parent}.", "exists")
    if not path.parent.is_dir():
        raise ToolError(f"The folder {path.parent} does not exist.", "not_found")
    if args.kind == "folder":
        path.mkdir()
        return ToolResult.success(f"Created the folder {path.name} in {path.parent}.", path=str(path))
    with open(path, "x", encoding="utf-8") as handle:  # "x": never overwrite
        handle.write(args.text)
    return ToolResult.success(f"Created {path.name} in {path.parent}.", path=str(path))


# ----------------------------------------------------------- rename / move / copy


class RenameArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="File or folder to rename")
    new_name: str = Field(min_length=1, max_length=255, description="New name only (no folder). Extension is kept if omitted")


def _rename_target(args: RenameArgs, ctx: ToolContext) -> tuple[Path, Path]:
    source = _policy(ctx).resolve(args.path, must_exist=True)
    new_name = check_component(args.new_name.strip())
    if source.is_file() and not Path(new_name).suffix and source.suffix:
        new_name += source.suffix
    return source, source.with_name(new_name)


def _rename_risk(args: RenameArgs, ctx: ToolContext) -> RiskLevel:
    source, target = _rename_target(args, ctx)
    policy = _policy(ctx)
    level = max(policy.risk(source, "write"), policy.risk(target, "write"))
    if policy.is_root_folder(source):
        level = RiskLevel.CRITICAL
    if target.suffix.lower() in EXECUTABLE_EXTENSIONS and target.suffix.lower() != source.suffix.lower():
        level = max(level, RiskLevel.DANGEROUS)  # e.g. notes.txt -> notes.bat
    return level


def _rename_question(args: RenameArgs, ctx: ToolContext) -> str:
    try:
        source, target = _rename_target(args, ctx)
        return f"Rename '{source.name}' to '{target.name}' (in {source.parent})?"
    except ToolError as exc:
        return f"Rename '{args.path}' to '{args.new_name}'? ({exc})"


def rename_item(args: RenameArgs, ctx: ToolContext) -> ToolResult:
    source, target = _rename_target(args, ctx)
    if target.exists():
        raise ToolError(f"'{target.name}' already exists there.", "exists")
    source.rename(target)
    return ToolResult.success(f"Renamed {source.name} to {target.name}.", old_path=str(source), path=str(target))


class MoveArgs(ToolArgs):
    source: str = Field(min_length=1, max_length=400, description="File or folder to move or copy")
    destination: str = Field(min_length=1, max_length=400, description="Destination folder, e.g. 'Documents'")
    mode: Literal["move", "copy"] = Field(default="move", description="move, or copy to keep the original")


def _move_paths(args: MoveArgs, ctx: ToolContext) -> tuple[Path, Path]:
    policy = _policy(ctx)
    source = policy.resolve(args.source, must_exist=True)
    folder = policy.resolve(args.destination, must_exist=True)
    if not folder.is_dir():
        raise ToolError(f"{folder.name} is not a folder.", "not_a_folder")
    return source, folder / source.name


def _move_risk(args: MoveArgs, ctx: ToolContext) -> RiskLevel:
    source, target = _move_paths(args, ctx)
    policy = _policy(ctx)
    level = policy.risk(target, "write")
    if args.mode == "move":  # moving changes where things are: at least moderate (asks by default)
        level = max(level, RiskLevel.MODERATE, policy.risk(source, "write"))
        if policy.is_root_folder(source):
            level = RiskLevel.CRITICAL
    return level


def _move_question(args: MoveArgs, ctx: ToolContext) -> str:
    verb = "Copy" if args.mode == "copy" else "Move"
    try:
        source, target = _move_paths(args, ctx)
        return f"{verb} '{source.name}' from {source.parent} to {target.parent}?"
    except ToolError as exc:
        return f"{verb} '{args.source}' to '{args.destination}'? ({exc})"


def move_item(args: MoveArgs, ctx: ToolContext) -> ToolResult:
    source, target = _move_paths(args, ctx)
    if target.exists():
        raise ToolError(f"'{target.name}' already exists in {target.parent}.", "exists")
    if is_inside(target, source):
        raise ToolError("A folder can't be moved or copied into itself.", "invalid_path")
    if args.mode == "copy":
        if source.is_dir():
            if _count_items(source, 10_000) > 10_000:
                raise ToolError("That folder is too large to copy with the assistant.", "too_large")
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
        return ToolResult.success(f"Copied {source.name} to {target.parent}.", path=str(target))
    shutil.move(str(source), str(target))
    return ToolResult.success(f"Moved {source.name} to {target.parent}.", old_path=str(source), path=str(target))


def is_inside(path: Path, folder: Path) -> bool:
    from app.security.validator import is_within

    return is_within(path, folder) and path != folder


# ------------------------------------------------------------------ delete


def _delete_risk(args: PathArgs, ctx: ToolContext) -> RiskLevel:
    policy = _policy(ctx)
    path = policy.resolve(args.path, must_exist=True)
    level = policy.risk(path, "delete")
    if path.is_dir() and _count_items(path, MASS_DELETE_LIMIT) > MASS_DELETE_LIMIT:
        level = RiskLevel.CRITICAL  # mass deletion
    return level


def _delete_question(args: PathArgs, ctx: ToolContext) -> str:
    try:
        path = _policy(ctx).resolve(args.path, must_exist=True)
    except ToolError as exc:
        return f"Delete '{args.path}'? ({exc})"
    if path.is_dir():
        count = _count_items(path, MASS_DELETE_LIMIT)
        return f"Move the folder {_described(path)} and its {count} item(s) to the Recycle Bin?"
    return f"Move {_described(path)} to the Recycle Bin?"


def delete_item(args: PathArgs, ctx: ToolContext) -> ToolResult:
    policy = _policy(ctx)
    path = policy.resolve(args.path, must_exist=True)
    if policy.is_root_folder(path):  # defence in depth: also blocked by the risk level
        raise ToolError("That folder can't be deleted.", "protected")
    winapi.recycle(str(path))
    if path.exists():
        return ToolResult.failure("still_exists", f"Windows reported success, but {path.name} is still there.", path=str(path))
    return ToolResult.success(f"Moved {path.name} to the Recycle Bin.", path=str(path))


# ------------------------------------------------------------------- risks


def _read_risk(args: Any, ctx: ToolContext) -> RiskLevel:
    policy = _policy(ctx)
    target = getattr(args, "path", None) or getattr(args, "folder", "all")
    if target.strip().lower() in ("all", "everywhere", "my files", ""):
        return RiskLevel.SAFE
    return policy.risk(policy.resolve(target, must_exist=True), "read")


def _open_risk(args: PathArgs, ctx: ToolContext) -> RiskLevel:
    policy = _policy(ctx)
    return policy.risk(policy.resolve(args.path, must_exist=True), "open")


def _open_question(args: PathArgs, ctx: ToolContext) -> str:
    try:
        path = _policy(ctx).resolve(args.path, must_exist=True)
    except ToolError as exc:
        return f"Open '{args.path}'? ({exc})"
    if path.suffix.lower() in EXECUTABLE_EXTENSIONS:
        return f"'{path.name}' is a program or script. Run it?"
    return f"Open {_described(path)}?"


def _read_question(args: Any, ctx: ToolContext) -> str:
    target = getattr(args, "path", None) or getattr(args, "folder", "")
    return f"'{target}' is outside your usual folders. Look inside it?"


def _must_exist(field: str) -> Any:
    def check(args: Any, ctx: ToolContext) -> None:
        _policy(ctx).resolve(getattr(args, field), must_exist=True)

    return check


def _find_precheck(args: FindArgs, ctx: ToolContext) -> None:
    if not args.name_contains and not args.extension and not args.modified_within_days and not args.about:
        raise ToolError("What should I look for? Give part of a name, a file type or what it's about.", "missing_query")
    if args.folder.strip().lower() not in ("all", "everywhere", "my files", ""):
        _policy(ctx).resolve(args.folder, must_exist=True)


def _create_precheck(args: CreateArgs, ctx: ToolContext) -> None:
    path = _policy(ctx).resolve(args.path)
    if path.exists():
        raise ToolError(f"'{path.name}' already exists in {path.parent}.", "exists")
    if not path.parent.is_dir():
        raise ToolError(f"The folder {path.parent} does not exist.", "not_found")


# ------------------------------------------------------------------ verification (the result, checked on disk)


def _verify_exists(args, result: ToolResult, ctx: ToolContext) -> tuple[bool, str]:
    path = Path(str(result.data.get("path", "")))
    return (path.exists(), f"{path.name} isn't there") if result.data.get("path") else (True, "")


def _verify_moved(args, result: ToolResult, ctx: ToolContext) -> tuple[bool, str]:
    target = Path(str(result.data.get("path", "")))
    if not target.exists():
        return False, f"{target.name} isn't in {target.parent}"
    old = result.data.get("old_path")
    if old and Path(str(old)).exists() and Path(str(old)).resolve() != target.resolve():
        return False, f"{Path(str(old)).name} is still in its old place"
    return True, ""


def _verify_gone(args, result: ToolResult, ctx: ToolContext) -> tuple[bool, str]:
    path = Path(str(result.data.get("path", "")))
    return (not path.exists(), f"{path.name} is still there") if result.data.get("path") else (True, "")


TOOLS: tuple[Tool, ...] = (
    Tool("open_path", "Open a file (in its default app) or a folder (in File Explorer), e.g. 'Downloads' or 'report.pdf'.",
         open_path, PathArgs, RiskLevel.SAFE, "files", precheck=_must_exist("path"),
         risk=_safe_risk(_open_risk), confirmation=_open_question),
    Tool("list_folder", "List what is inside a folder.", list_folder, ListArgs, RiskLevel.SAFE, "files",
         precheck=_must_exist("path"), risk=_safe_risk(_read_risk), confirmation=_read_question),
    Tool("find_files", "Search for files by type (pdf, docx, jpg...), part of the name, what they are about (words "
         "inside the file) and how recent, in Desktop, Downloads, Documents or a given folder. Example: 'the PDF about "
         "my thesis I downloaded last week' -> extension='pdf', about='thesis', folder='Downloads', "
         "modified_within_days=7.",
         find_files, FindArgs, RiskLevel.SAFE, "files", timeout=20, precheck=_find_precheck,
         risk=_safe_risk(_read_risk), confirmation=_read_question),
    Tool("file_info", "Get size, type and dates of a file, or the total size of a folder.", file_info, PathArgs,
         RiskLevel.SAFE, "files", timeout=20, precheck=_must_exist("path"), risk=_safe_risk(_read_risk),
         confirmation=_read_question),
    Tool("create_item", "Create a new folder, or a new text file, e.g. a folder called Projects on the Desktop.",
         create_item, CreateArgs, RiskLevel.SAFE, "files", precheck=_create_precheck, risk=_safe_risk(_create_risk),
         verify=_verify_exists),
    Tool("rename_item", "Rename a file or folder (give only the new name).", rename_item, RenameArgs,
         RiskLevel.MODERATE, "files", precheck=lambda a, c: _rename_target(a, c) and None,
         risk=_safe_risk(_rename_risk), confirmation=_rename_question, verify=_verify_moved),
    Tool("move_item", "Move a file or folder to another folder, or copy it (mode='copy').", move_item, MoveArgs,
         RiskLevel.SAFE, "files", timeout=60, precheck=lambda a, c: _move_paths(a, c) and None,
         risk=_safe_risk(_move_risk), confirmation=_move_question, verify=_verify_moved),
    Tool("delete_item", "Delete a file or folder by moving it to the Recycle Bin. Always asks first.", delete_item,
         PathArgs, RiskLevel.DANGEROUS, "files", precheck=_must_exist("path"), risk=_safe_risk(_delete_risk),
         confirmation=_delete_question, verify=_verify_gone),
)
