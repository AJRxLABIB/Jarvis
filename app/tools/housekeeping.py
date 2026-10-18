"""Housekeeping and news: organize the Desktop (with undo), empty the Recycle Bin, headlines.

* organize_desktop moves loose files on the Desktop into "Organized Desktop\\<type>" folders
  (Images, Documents, ...). Shortcuts, folders and hidden files stay where they are. Every
  move is recorded, so "undo" puts each file back exactly where it was.
* empty_recycle_bin permanently deletes what is in the Recycle Bin, so it always asks first
  (level DANGEROUS), also when you say it yourself.
* get_news reads Google News headlines (RSS) for your region, in English or Bangla.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field

from app import __version__
from app.security.permissions import RiskLevel
from app.tools import winapi
from app.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolError, ToolResult, clamped_int
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.housekeeping")
ORGANIZED_FOLDER = "Organized Desktop"
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".svg", ".tif", ".tiff", ".ico", ".raw"),
    "Documents": (".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
                  ".odp", ".ods", ".epub"),
    "Videos": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v"),
    "Music": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"),
    "Installers": (".exe", ".msi", ".msix", ".appx", ".bat", ".cmd", ".ps1"),
    "Code": (".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".cs", ".php", ".go", ".rs",
             ".sql", ".ipynb", ".xml", ".yml", ".yaml", ".sh"),
}
KEEP_ON_DESKTOP = (".lnk", ".url", ".ini")
HISTORY_FILE = "organize_history.json"


def category_for(path: Path) -> str:
    ext = path.suffix.lower()
    for category, extensions in CATEGORIES.items():
        if ext in extensions:
            return category
    return "Others"


def desktop_dir() -> Path:
    found = winapi.known_folder("DESKTOP") if winapi.is_windows() else None
    return found or Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def _history_path(ctx: ToolContext) -> Path:
    base = ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis"
    return base / HISTORY_FILE


def _free_name(target: Path) -> Path:
    if not target.exists():
        return target
    for n in range(2, 1000):
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise ToolError(f"Too many files called {target.name}.", "name_conflict")


def _hidden(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except OSError:
        return True


class OrganizeArgs(ToolArgs):
    undo: bool = Field(default=False, description="true = put the files back where they were (undo the last organize)")


def organize_desktop(args: OrganizeArgs, ctx: ToolContext, desktop: Path | None = None) -> ToolResult:
    desktop = desktop or desktop_dir()
    if args.undo:
        return _undo(ctx)
    if not desktop.is_dir():
        raise ToolError("I couldn't find your Desktop folder.", "not_found")
    target_root = desktop / ORGANIZED_FOLDER
    moves: list[dict[str, str]] = []
    skipped = 0
    counts: dict[str, int] = {}
    for item in sorted(desktop.iterdir()):
        if not item.is_file() or item.suffix.lower() in KEEP_ON_DESKTOP or _hidden(item):
            continue
        category = category_for(item)
        destination = _free_name(target_root / category / item.name)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(destination))
        except OSError:
            skipped += 1  # in use, or no permission
            continue
        moves.append({"from": str(item), "to": str(destination)})
        counts[category] = counts.get(category, 0) + 1
    if not moves:
        return ToolResult.success("Your Desktop is already tidy: there were no loose files to organize.",
                                  moved=0, skipped=skipped)
    path = _history_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"when": datetime.now().isoformat(timespec="seconds"), "moves": moves}, indent=1),
                    encoding="utf-8")
    log_event(_log, "desktop_organized", moved=len(moves), skipped=skipped)
    summary = ", ".join(f"{n} {c.lower()}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    message = f"Organized {len(moves)} files on your Desktop into the '{ORGANIZED_FOLDER}' folder ({summary})."
    if skipped:
        message += f" {skipped} file(s) were in use and stayed where they were."
    return ToolResult.success(message + " Say 'undo organize' to put them back.", moved=len(moves),
                              skipped=skipped, categories=counts)


def _undo(ctx: ToolContext, history_file: str = HISTORY_FILE, place: str = "Desktop") -> ToolResult:
    path = _history_path(ctx).with_name(history_file)
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("There is no organize to undo.", "nothing_to_undo") from None
    restored = missing = 0
    for move in reversed(history.get("moves", [])):
        source, destination = Path(move["to"]), Path(move["from"])
        if not source.exists():
            missing += 1
            continue
        try:
            shutil.move(str(source), str(_free_name(destination)))
            restored += 1
        except OSError:
            missing += 1
    try:
        path.unlink()
    except OSError:
        pass
    root = None
    if history.get("moves"):
        root = Path(history["moves"][0]["to"]).parent.parent
    if root is not None and root.name == ORGANIZED_FOLDER:
        for folder in sorted(root.glob("*"), reverse=True):  # remove the folders it created, if now empty
            try:
                folder.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass
    for created in {Path(m["to"]).parent for m in history.get("moves", [])} if place == "Downloads" else ():
        try:
            created.rmdir()  # the category folder, if now empty
        except OSError:
            pass
    message = f"Put {restored} files back {'on' if place == 'Desktop' else 'in'} your {place}."
    if missing:
        message += f" {missing} had been moved or deleted since, so I left them."
    return ToolResult.success(message, restored=restored, missing=missing)


# ------------------------------------------------------------------ Downloads

DOWNLOADS_HISTORY = "downloads_history.json"
PARTIAL = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload", ".!ut", ".aria2")
KEEP_MOVES = 1000


def downloads_dir() -> Path:
    found = winapi.known_folder("DOWNLOADS") if winapi.is_windows() else None
    return found or Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"


def sort_downloads(ctx: ToolContext, folder: Path | None = None, *, min_age_seconds: float = 120,
                   now: float | None = None) -> dict[str, int]:
    """Move loose files in Downloads into Downloads/<Category>. Files still downloading, hidden, or
    changed in the last ``min_age_seconds`` stay. Returns {category: count}. Every move is recorded for undo."""
    import time as _time

    folder = folder or downloads_dir()
    if not folder.is_dir():
        raise ToolError("I couldn't find your Downloads folder.", "not_found")
    now = _time.time() if now is None else now
    moves: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for item in sorted(folder.iterdir()):
        try:
            if (not item.is_file() or _hidden(item) or item.suffix.lower() in PARTIAL + KEEP_ON_DESKTOP
                    or now - item.stat().st_mtime < min_age_seconds):
                continue
        except OSError:
            continue
        category = category_for(item)
        destination = _free_name(folder / category / item.name)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(destination))
        except OSError:
            continue  # in use
        moves.append({"from": str(item), "to": str(destination)})
        counts[category] = counts.get(category, 0) + 1
    if moves:
        path = _history_path(ctx).with_name(DOWNLOADS_HISTORY)
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("moves", [])
        except (OSError, ValueError, AttributeError):
            old = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"when": datetime.now().isoformat(timespec="seconds"),
                                    "moves": (old + moves)[-KEEP_MOVES:]}, indent=1), encoding="utf-8")
        log_event(_log, "downloads_sorted", moved=len(moves))
    return counts


def describe_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {c.lower()}" for c, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def organize_downloads(args: OrganizeArgs, ctx: ToolContext, folder: Path | None = None) -> ToolResult:
    if args.undo:
        return _undo(ctx, DOWNLOADS_HISTORY, "Downloads")
    counts = sort_downloads(ctx, folder, min_age_seconds=10)
    if not counts:
        return ToolResult.success("Your Downloads folder is already sorted.", moved=0)
    total = sum(counts.values())
    return ToolResult.success(f"Sorted {total} file(s) in Downloads into folders ({describe_counts(counts)}). "
                              "Say 'undo sort downloads' to put them back.", moved=total, categories=counts)


# ------------------------------------------------------------------ recycle bin


def _human_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _recycle_question(_: Any, __: ToolContext) -> str:
    try:
        items, size = winapi.recycle_bin_info()
    except ToolError:
        return "Empty the Recycle Bin? Everything in it will be deleted permanently."
    return f"Empty the Recycle Bin? {items} item(s), {_human_size(size)}, will be deleted permanently."


def empty_recycle_bin(_: NoArgs, ctx: ToolContext) -> ToolResult:
    items, size = winapi.recycle_bin_info()
    if items == 0:
        return ToolResult.success("The Recycle Bin is already empty.", items=0)
    winapi.empty_recycle_bin()
    log_event(_log, "recycle_bin_emptied", items=items)
    return ToolResult.success(f"Emptied the Recycle Bin: {items} item(s), {_human_size(size)} freed.",
                              items=items, bytes=size)


# ------------------------------------------------------------------ news

NEWS_URL = "https://news.google.com/rss"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests replace this
_NEWS_CACHE: dict[tuple, tuple[float, list[dict[str, str]]]] = {}
MAX_RSS_BYTES = 2_000_000


class NewsArgs(ToolArgs):
    topic: str | None = Field(default=None, max_length=80, description="Optional topic, e.g. 'cricket', 'technology'")
    language: Literal["en", "bn"] = Field(default="en", description="bn = Bangla headlines")
    count: clamped_int(1, 10, 5, "How many headlines")  # type: ignore[valid-type]


def parse_rss(xml_text: str, count: int) -> list[dict[str, str]]:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml_text, re.I):
        # the standard-library XML parser expands entities (a few lines can become gigabytes); a news feed never needs them
        raise ToolError("The news feed used features I don't accept, so I ignored it.", "service_error")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ToolError("The news feed sent an unreadable answer.", "service_error") from exc
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        if title:
            items.append({"title": title, "source": source, "link": (item.findtext("link") or "").strip()})
        if len(items) >= count:
            break
    return items


async def get_news(args: NewsArgs, ctx: ToolContext) -> ToolResult:
    region = (ctx.settings.news_region or "BD").upper()
    lang = "bn" if args.language == "bn" else "en"
    params = {"hl": f"{lang}-{region}" if lang == "en" else lang, "gl": region, "ceid": f"{region}:{lang}"}
    url = NEWS_URL
    if args.topic:
        url += "/search"
        params["q"] = args.topic
    key = (url, tuple(sorted(params.items())), args.count)
    cached = _NEWS_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 600:
        items = cached[1]
    else:
        try:
            async with httpx.AsyncClient(transport=TRANSPORT, timeout=12.0, follow_redirects=True,
                                         headers={"User-Agent": f"JarvisAssistant/{__version__}"}) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ToolError("I couldn't reach the news service. Check the internet connection.", "network") from exc
        if response.status_code >= 400:
            raise ToolError(f"The news service returned an error (HTTP {response.status_code}).", "service_error")
        items = parse_rss(response.text[:MAX_RSS_BYTES], args.count)
        _NEWS_CACHE[key] = (time.monotonic(), items)
    if not items:
        return ToolResult.success("I couldn't find any headlines" + (f" about {args.topic}." if args.topic else "."),
                                  headlines=[])
    spoken = " ".join(f"{i}. {h['title']}{' (' + h['source'] + ')' if h['source'] else ''}."
                      for i, h in enumerate(items[:3], 1))
    intro = "সেরা খবরগুলো:" if lang == "bn" else "Here are the top headlines:"
    return ToolResult.success(f"{intro} {spoken}", headlines=items)


TOOLS: tuple[Tool, ...] = (
    Tool("organize_desktop", "Tidy the Desktop: move loose files into 'Organized Desktop' folders by type (images, "
         "documents, videos...). Shortcuts stay. undo=true puts everything back.", organize_desktop, OrganizeArgs,
         RiskLevel.MODERATE, category="files", timeout=120,
         confirmation=lambda a, _: ("Put the organized files back on the Desktop?" if a.undo else
                                    "Move the loose files on your Desktop into 'Organized Desktop' folders? "
                                    "(You can undo it.)"),
         direct_without_confirmation=lambda a, _: True),  # your own command; everything can be undone
    Tool("organize_downloads", "Sort the Downloads folder: loose files go into folders by type (Documents, Images, "
         "Installers ...). undo=true puts them back.", organize_downloads, OrganizeArgs, RiskLevel.MODERATE,
         category="files", timeout=60, direct_without_confirmation=lambda a, _: True,
         confirmation=lambda a, _: "Put the sorted downloads back?" if a.undo else "Sort your Downloads folder into folders by type?"),
    Tool("empty_recycle_bin", "Empty the Windows Recycle Bin (permanently deletes what is in it).", empty_recycle_bin,
         NoArgs, RiskLevel.DANGEROUS, category="files", timeout=120, confirmation=_recycle_question),
    Tool("get_news", "Today's top news headlines (optionally about a topic), in English or Bangla.", get_news, NewsArgs,
         RiskLevel.SAFE, category="info", needs_network=True, timeout=20),
)
