"""Duplicate files and batch renaming - with a preview first, conflict checks, and an exact undo.

* find_duplicates: groups identical files (same size, then the same SHA-256) in a folder. It only READS; it never
  deletes anything - it says how much space the extra copies use so you can decide.
* batch_rename: renames many files by a rule (replace text, add a prefix/suffix, number them). Without apply=true it
  only shows what would happen. Applying checks every new name for clashes first (nothing is renamed if one would
  overwrite another file), renames in two steps so swaps work, and records the exact reverse for "undo".
"""

from __future__ import annotations

import fnmatch
import hashlib
import time
from pathlib import Path

from pydantic import Field

from app.security.permissions import RiskLevel
from app.security.validator import check_component
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

MAX_FILES = 20000           # files looked at by find_duplicates
MAX_SECONDS = 60.0
MAX_RENAMES = 500


def _size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _sha256(path: Path, limit: float) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                if time.monotonic() > limit:
                    return None
    except OSError:
        return None
    return digest.hexdigest()


def duplicate_groups(root: Path, *, recursive: bool = True, min_size: int = 1) -> tuple[list[list[Path]], bool]:
    """Groups of identical files (largest waste first) and whether the scan was cut short."""
    limit = time.monotonic() + MAX_SECONDS
    by_size: dict[int, list[Path]] = {}
    seen = 0
    walker = root.rglob("*") if recursive else root.glob("*")
    partial = False
    for path in walker:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size < min_size:
            continue
        by_size.setdefault(size, []).append(path)
        seen += 1
        if seen >= MAX_FILES or time.monotonic() > limit:
            partial = True
            break
    groups: list[list[Path]] = []
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in paths:
            digest = _sha256(path, limit)
            if digest is None:
                partial = True
                continue
            by_hash.setdefault(digest, []).append(path)
        groups += [sorted(g) for g in by_hash.values() if len(g) > 1]
    groups.sort(key=lambda g: -(g[0].stat().st_size * (len(g) - 1)))
    return groups, partial


class DuplicateArgs(ToolArgs):
    folder: str = Field(min_length=1, max_length=400, description="Folder to check, e.g. 'Downloads'")
    recursive: bool = Field(default=True, description="Include sub-folders")
    min_size_kb: int = Field(default=1, ge=0, le=10_000_000, description="Ignore files smaller than this (KB)")


def find_duplicates(args: DuplicateArgs, ctx: ToolContext) -> ToolResult:
    root = ctx.get_path_policy().resolve(args.folder, must_exist=True)
    if not root.is_dir():
        raise ToolError(f"{root.name} is not a folder.", "not_a_folder")
    groups, partial = duplicate_groups(root, recursive=args.recursive, min_size=max(1, args.min_size_kb * 1024))
    if not groups:
        return ToolResult.success(f"No duplicate files in {root.name}." + (" (I stopped early: very many files.)"
                                                                         if partial else ""), groups=[])
    waste = sum(g[0].stat().st_size * (len(g) - 1) for g in groups)
    top = groups[:10]
    lines = [f"{len(g)}x {g[0].name} ({_size_text(g[0].stat().st_size)} each): " +
             ", ".join(str(p.relative_to(root)) for p in g[:4]) for g in top]
    return ToolResult.success(
        f"{len(groups)} set(s) of identical files in {root.name}; the extra copies use {_size_text(waste)}. "
        "Nothing was deleted - tell me which copies to remove if you want." + (" (Partial scan.)" if partial else ""),
        groups=[[str(p) for p in g] for g in groups[:50]], wasted_bytes=waste, details=lines, partial=partial)


# ---------------------------------------------------------------------------------------------- batch rename
class BatchRenameArgs(ToolArgs):
    folder: str = Field(default="", max_length=400, description="Folder with the files")
    match: str = Field(default="*", max_length=80, description="Which files, e.g. '*.jpg' or 'IMG_*'")
    find: str = Field(default="", max_length=80, description="Text to replace in each name (optional)")
    replace: str = Field(default="", max_length=80, description="Replacement text")
    prefix: str = Field(default="", max_length=60, description="Add before each name")
    suffix: str = Field(default="", max_length=60, description="Add after each name (before the extension)")
    number: bool = Field(default=False, description="Number the files: name 001, name 002 ... (by current name)")
    base_name: str = Field(default="", max_length=80, description="With number=true: the new name before the number")
    apply: bool = Field(default=False, description="false = only show the preview; true = really rename")
    pairs: list[list[str]] = Field(default_factory=list, max_length=MAX_RENAMES,
                                   description="Internal (undo): exact [current path, new name] pairs")


def plan_renames(files: list[Path], args: BatchRenameArgs) -> list[tuple[Path, str]]:
    """(file, new name) for every file whose name changes."""
    plan: list[tuple[Path, str]] = []
    for index, path in enumerate(sorted(files, key=lambda p: p.name.lower()), start=1):
        stem, ext = path.stem, path.suffix
        if args.number:
            stem = f"{args.base_name or stem} {index:03d}"
        if args.find:
            stem = stem.replace(args.find, args.replace)
        new = f"{args.prefix}{stem}{args.suffix}{ext}"
        if new != path.name:
            plan.append((path, check_component(new)))
    return plan


def _collect(args: BatchRenameArgs, ctx: ToolContext) -> tuple[Path | None, list[tuple[Path, str]]]:
    policy = ctx.get_path_policy()
    if args.pairs:                                     # undo: exact pairs
        plan = []
        for item in args.pairs:
            if len(item) != 2:
                raise ToolError("Each rename pair needs a path and a new name.", "bad_pairs")
            source = policy.resolve(item[0], must_exist=True)
            plan.append((source, check_component(item[1])))
        return (plan[0][0].parent if plan else None), plan
    if not args.folder:
        raise ToolError("Which folder?", "missing_folder")
    root = policy.resolve(args.folder, must_exist=True)
    if not root.is_dir():
        raise ToolError(f"{root.name} is not a folder.", "not_a_folder")
    if not (args.find or args.prefix or args.suffix or args.number):
        raise ToolError("Tell me how to rename them: replace text, add a prefix/suffix, or number them.", "no_rule")
    files = [p for p in root.iterdir() if p.is_file() and not p.is_symlink() and fnmatch.fnmatch(p.name.lower(),
                                                                                                args.match.lower())]
    plan = plan_renames(files, args)
    if len(plan) > MAX_RENAMES:
        raise ToolError(f"That would rename {len(plan)} files; I do at most {MAX_RENAMES} at once.", "too_many")
    return root, plan


def _conflicts(plan: list[tuple[Path, str]]) -> list[str]:
    targets: dict[str, Path] = {}
    moving = {p.resolve() for p, _ in plan}
    problems = []
    for source, new in plan:
        target = source.with_name(new)
        key = str(target).lower()
        if key in targets:
            problems.append(f"{targets[key].name} and {source.name} would both become {new}")
        targets[key] = source
        if target.exists() and target.resolve() not in moving:
            problems.append(f"{new} already exists")
    return problems


def _risk(args: BatchRenameArgs, ctx: ToolContext) -> RiskLevel:
    if not args.apply:
        return RiskLevel.SAFE
    root, plan = _collect(args, ctx)
    policy = ctx.get_path_policy()
    level = RiskLevel.MODERATE
    for source, _ in plan[:50]:
        level = max(level, policy.risk(source, "write"))
    return RiskLevel(level)


def _question(args: BatchRenameArgs, ctx: ToolContext) -> str:
    _, plan = _collect(args, ctx)
    examples = "; ".join(f"{s.name} -> {n}" for s, n in plan[:3])
    return f"Rename {len(plan)} file(s)? e.g. {examples}"


def batch_rename(args: BatchRenameArgs, ctx: ToolContext) -> ToolResult:
    root, plan = _collect(args, ctx)
    if not plan:
        return ToolResult.success("No file names would change.", renamed=[])
    problems = _conflicts(plan)
    preview = [f"{s.name} -> {n}" for s, n in plan]
    if problems:
        raise ToolError("Nothing renamed - " + "; ".join(problems[:5]) + ".", "conflict")
    if not args.apply:
        return ToolResult.success(f"Preview: {len(plan)} file(s) would be renamed. Say 'do it' to rename them.",
                                  preview=preview[:100], count=len(plan))
    temporary = []
    for n, (source, new) in enumerate(plan):            # step 1: to temporary names (so A->B, B->A works)
        temp = source.with_name(f".jarvis-rename-{n}-{source.name}")
        source.rename(temp)
        temporary.append((temp, source.with_name(new), source))
    renamed = []
    for temp, target, original in temporary:            # step 2: to the final names
        temp.rename(target)
        renamed.append([str(target), original.name])
    return ToolResult.success(f"Renamed {len(renamed)} file(s) in {root.name if root else 'the folder'}.",
                              renamed=renamed, preview=preview[:100], count=len(renamed))


def _verify(args: BatchRenameArgs, result: ToolResult, _ctx: ToolContext) -> tuple[bool, str]:
    if not args.apply:
        return True, "preview only"
    renamed = (result.data or {}).get("renamed", [])
    missing = [p for p, _ in renamed if not Path(p).exists()]
    return (not missing, "all renamed files are there" if not missing else f"missing: {missing[:3]}")


TOOLS = (
    Tool("find_duplicates", "Find identical (duplicate) files in a folder and how much space the extra copies use. "
         "Never deletes anything.", find_duplicates, DuplicateArgs, RiskLevel.SAFE, "files", timeout=90),
    Tool("batch_rename", "Rename many files at once by a rule (replace text, prefix, suffix, numbering). Shows a "
         "preview first (apply=false); apply=true renames, checking for clashes, and can be undone.", batch_rename,
         BatchRenameArgs, RiskLevel.SAFE, "files", timeout=60, risk=_risk, confirmation=_question, verify=_verify),
)
