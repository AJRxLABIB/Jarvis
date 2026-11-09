"""Reads an Obsidian-style vault (a folder of Markdown notes) into notes, tags, links and backlinks - locally, read-only, no Obsidian needed.

Pure parsing. ``.obsidian``, ``.trash`` and hidden folders are skipped; only ``.md`` files are read, each capped; notes in an excluded folder are never opened.
A ``[[wiki link]]`` is a relationship the note's author wrote: it is kept with the note path as its evidence. A link whose target note does not exist is
reported as unresolved - never turned into a node as if it were a fact. Tags come from front matter and inline ``#tags`` (not from code blocks or headings).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".stfolder"}
MAX_BYTES = 400_000
MAX_NOTES = 5000
_WIKI = re.compile(r"(?<!!)\[\[([^\]\|#\n]+)(?:#[^\]\|\n]*)?(?:\|[^\]\n]*)?\]\]")
_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/\-]{0,60})")
_FENCE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


@dataclass
class Note:
    path: str                       # relative, forward slashes
    title: str
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)       # target names as written
    meta: dict[str, str] = field(default_factory=dict)
    words: int = 0


def _front_matter(text: str) -> tuple[dict[str, str], list[str], str]:
    if not text.startswith("---"):
        return {}, [], text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, [], text
    meta: dict[str, str] = {}
    tags: list[str] = []
    in_tags = False
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if in_tags and stripped.startswith("-"):                  # a YAML list under "tags:"
            tags.append(stripped.lstrip("- ").strip().strip("\"'#"))
            continue
        in_tags = False
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip().strip("\"'")
        if not key or line.startswith((" ", "\t")):
            continue
        if key in ("tags", "tag"):
            tags += [t.strip().strip("\"'#") for t in re.split(r"[,\[\]]", value) if t.strip()]
            in_tags = not value
        elif value:
            meta[key] = value[:200]
    return meta, [t for t in tags if t], text[end + 4:]


def parse_note(text: str, rel_path: str) -> Note:
    meta, tags, body = _front_matter(text)
    prose = _FENCE.sub(" ", body)
    title = meta.get("title") or next((m.group(1).strip() for m in re.finditer(r"^#\s+(.+)$", prose, re.M)), "") or Path(rel_path).stem
    inline = [m.group(1) for line in prose.splitlines() if not re.match(r"\s*#{1,6}\s", line) for m in _TAG.finditer(line)]     # "# Heading" is not a tag
    links = []
    for m in _WIKI.finditer(prose):
        name = m.group(1).strip()
        if name and name not in links:
            links.append(name)
    return Note(rel_path.replace("\\", "/"), title[:120], sorted({t.lower() for t in tags + inline}), links, meta, len(prose.split()))


@dataclass
class Vault:
    notes: dict[str, Note] = field(default_factory=dict)         # by relative path
    backlinks: dict[str, list[str]] = field(default_factory=dict)
    unresolved: list[tuple[str, str]] = field(default_factory=list)   # (note path, link text)
    skipped: int = 0

    def resolve(self, name: str) -> str | None:
        key = name.lower().strip()
        for path, note in self.notes.items():
            if Path(path).stem.lower() == key or note.title.lower() == key or path.lower().removesuffix(".md") == key:
                return path
        return None

    def edges(self) -> list[tuple[str, str, str]]:
        """(source note, 'links_to', target note) with the source note as evidence - resolved links only."""
        out = []
        for path, note in self.notes.items():
            seen: set[str] = set()
            for name in note.links:
                target = self.resolve(name)
                if target and target != path and target not in seen:       # "[[A]]" and "[[a]]" are one link
                    seen.add(target)
                    out.append((path, "links_to", target))
        return out


def scan(root: Path, excluded: tuple[str, ...] = (), read: Callable[[Path], str] | None = None) -> Vault:
    root = Path(root)
    read = read or (lambda p: p.read_text(encoding="utf-8", errors="replace"))
    vault, ex = Vault(), tuple(e.lower().strip("/\\") for e in excluded if e)
    stack = [root]
    while stack and len(vault.notes) < MAX_NOTES:
        folder = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            vault.skipped += 1
            continue
        for p in entries:
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                if p.name in SKIP_DIRS or p.name.startswith(".") or any(rel.lower() == e or rel.lower().startswith(e + "/") for e in ex):
                    continue
                stack.append(p)
            elif p.suffix.lower() == ".md":
                try:
                    if p.stat().st_size > MAX_BYTES:
                        vault.skipped += 1
                        continue
                    vault.notes[rel] = parse_note(read(p), rel)
                except OSError:
                    vault.skipped += 1
    for path, note in vault.notes.items():
        for name in note.links:
            target = vault.resolve(name)
            if target is None:
                vault.unresolved.append((path, name))
            elif target != path and path not in vault.backlinks.get(target, []):
                vault.backlinks.setdefault(target, []).append(path)
    return vault


def summary(v: Vault) -> str:
    tags: dict[str, int] = {}
    for n in v.notes.values():
        for t in n.tags:
            tags[t] = tags.get(t, 0) + 1
    top = ", ".join(f"#{t} ({c})" for t, c in sorted(tags.items(), key=lambda kv: -kv[1])[:6]) or "none"
    hubs = sorted(v.backlinks.items(), key=lambda kv: -len(kv[1]))[:3]
    hub = "; ".join(f"{v.notes[p].title} ({len(b)} backlinks)" for p, b in hubs) or "none"
    return (f"{len(v.notes)} notes, {len(v.edges())} resolved links, {len(v.unresolved)} links to notes that do not exist, {v.skipped} skipped. "
            f"Top tags: {top}. Most linked: {hub}.")
