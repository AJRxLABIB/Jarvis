"""``media_search`` (find what was said in your recordings, chapters, study notes, status) and ``media_manage`` (scan a folder, process recordings in the background,
cancel, forget). Processing is resumable and cancellable; the media files are only referenced (core/media_index.py, core/media_ffmpeg.py)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core import events, media_ffmpeg as FF, media_index as MI, speaker_turns as ST
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

MAX_SCAN = 2000
_JOB: dict = {"thread": None, "cancel": threading.Event(), "progress": ""}


def _index(ctx: ToolContext) -> MI.MediaIndex:
    if ctx.database is None:
        raise ToolError("Needs the local database.", "no_database")
    return MI.MediaIndex(ctx.database)


class SearchArgs(ToolArgs):
    action: Literal["search", "chapters", "notes", "status"] = Field(default="status", description="search = find where something was said; chapters / notes = for one recording; status = what is indexed")
    query: str = Field(default="", max_length=300, description="search: what to look for")
    file: str = Field(default="", max_length=300, description="chapters / notes: the recording's file name (or part of it)")


def media_search(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    idx = _index(ctx)
    if args.action == "status":
        assets = idx.assets()
        if not assets:
            return ToolResult.success("No recordings are indexed yet. Scan a folder with media_manage first.", assets=0)
        counts: dict[str, int] = {}
        for a in assets:
            counts[a["status"]] = counts.get(a["status"], 0) + 1
        lines = [f"{len(assets)} recording(s): " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + (f". Now: {_JOB['progress']}" if _JOB["progress"] else "")]
        lines += [f"  {Path(a['path']).name}: {a['status']}" + (f" ({a['step']})" if a["step"] else "") + (f" - {a['error']}" if a["error"] else "") + f", {a['segments']} segment(s)" for a in assets[:12]]
        return ToolResult.success("\n".join(lines), assets=len(assets))
    if args.action == "search":
        if not args.query.strip():
            raise ToolError("Say what to look for.", "invalid_arguments")
        hits = idx.search(args.query)
        if hits:
            try:
                events.BUS.publish(events.MEDIA_SEARCH_MATCH_FOUND, hits=len(hits))
            except Exception:
                pass
        return ToolResult.success(idx.evidence(args.query), hits=len(hits))
    match = [a for a in idx.assets() if args.file.lower() in Path(a["path"]).name.lower()] if args.file.strip() else []
    if len(match) != 1:
        raise ToolError("Name one indexed recording (part of its file name)." + (f" {len(match)} match." if match else ""), "ambiguous")
    segs = idx.segments(match[0]["id"])
    name = Path(match[0]["path"]).name
    if args.action == "chapters":
        ch = MI.make_chapters(segs)
        return ToolResult.success(f"Chapters of {name} (split at long pauses or topic changes; titles are the most frequent words):\n" + "\n".join(f"  {MI.ts(c.start)} - {MI.ts(c.end)}  {c.title}" for c in ch) if ch else f"{name} has no transcript yet.")
    return ToolResult.success(MI.study_notes(name, segs))


class ManageArgs(ToolArgs):
    action: Literal["scan", "process", "cancel", "forget"] = Field(description="scan = register recordings in a folder; process = transcribe and index them in the background; cancel = stop the running job; forget = drop one from the index")
    path: str = Field(default="", max_length=400, description="scan: a folder; forget: a recording")
    limit: int = Field(default=5, ge=1, le=50, description="process: how many pending recordings to work on in this job")
    speakers: bool = Field(default=False, description="process: also work out who spoke when ('Speaker 1 / 2', approximate, takes extra time); recordings already indexed are not redone")
    sensitivity: Literal["low", "normal", "high"] = Field(default="normal", description="process with speakers: high splits voices more eagerly, low merges similar voices")


def _worker(ctx: ToolContext, ids: list[int], speakers: bool = False, sensitivity: str = "normal") -> None:
    idx = _index(ctx)
    tools = FF.find()
    work = (ctx.paths.data_dir if ctx.paths is not None else Path.home() / ".jarvis") / "media_tmp"
    stt = None
    try:
        from app.cli_voice import create_stt

        stt = create_stt(ctx, background_upgrade=False)
    except Exception:
        stt = None

    def transcribe(wav: Path):
        if stt is None:
            raise FF.FfmpegError("speech recognition could not be loaded on this machine")
        return MI.transcribe_wav(wav, stt)

    threshold = {"low": 14.0, "normal": ST.THRESHOLD, "high": 6.0}.get(sensitivity, ST.THRESHOLD)

    def diarize(wav: Path):
        return ST.diarize_wav(wav, threshold, _JOB["cancel"].is_set)

    for aid in ids:
        if _JOB["cancel"].is_set():
            break
        name = Path(idx.asset(aid)["path"]).name
        out = MI.process(idx, aid, tools=tools, workdir=work, transcribe=transcribe, cancelled=_JOB["cancel"].is_set, diarize=diarize if speakers else None,
                         progress=lambda step, frac, n=name: _JOB.update(progress=f"{n}: {step} ({int(frac * 100)}%)"))
        if out.status == "done":
            try:
                events.BUS.publish(events.MEDIA_PROCESSING_COMPLETED, file=name)
            except Exception:
                pass
    _JOB["progress"] = ""


def media_manage(args: ManageArgs, ctx: ToolContext) -> ToolResult:
    idx = _index(ctx)
    if args.action == "cancel":
        _JOB["cancel"].set()
        return ToolResult.success("Cancelling: the current recording stops at its next safe point and keeps its progress.")
    if args.action == "forget":
        ok = idx.forget(ctx.get_path_policy().resolve(args.path, must_exist=False))
        return ToolResult.success("Removed from the index (the file itself is untouched)." if ok else "That recording was not in the index.")
    if args.action == "scan":
        root = ctx.get_path_policy().resolve(args.path, must_exist=True)
        if not root.is_dir():
            raise ToolError("That is not a folder.", "invalid_arguments")
        n = 0
        for p in root.rglob("*"):
            if n >= MAX_SCAN:
                break
            if p.is_file() and p.suffix.lower() in MI.MEDIA_EXT and not any(part.startswith(".") for part in p.relative_to(root).parts):
                try:
                    idx.add_asset(p, MI.quick_sha(p), p.stat().st_size, None, "video" if p.suffix.lower() in MI.VIDEO_EXT else "audio")
                    n += 1
                except OSError:
                    continue
        note = "" if FF.find() else " FFmpeg was not found on PATH: install it (optional) before processing."
        return ToolResult.success(f"Registered {n} recording(s) (referenced, not copied).{note}", registered=n)
    if _JOB["thread"] is not None and _JOB["thread"].is_alive():
        return ToolResult.success("A processing job is already running. " + (_JOB["progress"] or ""))
    pending = [a["id"] for a in idx.assets() if a["status"] != "done"][: args.limit]
    if not pending:
        return ToolResult.success("Nothing is waiting to be processed.")
    _JOB["cancel"].clear()
    _JOB["thread"] = threading.Thread(target=_worker, args=(ctx, pending, args.speakers, args.sensitivity), name="media-index", daemon=True)
    _JOB["thread"].start()
    return ToolResult.success(f"Started processing {len(pending)} recording(s) in the background (resumable; say 'cancel media processing' to stop). Check progress with media_search status.")


TOOLS: tuple[Tool, ...] = (
    Tool("media_search", "Search what was said in the user's indexed recordings (lectures, meetings, videos) with file names and timestamps, list a recording's chapters, make study notes "
         "from it, or show indexing status ('find where I explained the architecture', 'study notes for the lecture').", media_search, SearchArgs, RiskLevel.SAFE, "memory", timeout=30),
    Tool("media_manage", "Register recordings in a folder, transcribe and index them in the background (needs FFmpeg), cancel the job, or remove a recording from the index.",
         media_manage, ManageArgs, RiskLevel.MODERATE, "memory", timeout=60),
)
