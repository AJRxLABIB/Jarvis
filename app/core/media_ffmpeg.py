"""FFmpeg / ffprobe for media intelligence: the argument lists, the parsers, and ONE reviewed place that starts the processes.

* ``find`` looks for ffmpeg and ffprobe on PATH (or a folder you give); neither is bundled or downloaded. No FFmpeg -> ``None`` and the caller says so.
* Every command is an argument LIST (shell=False); the input path is passed as one argument after ``-i``; a path starting with ``-`` is refused; every run has a timeout
  and can be cancelled (the process is killed, nothing is left running).
* Outputs go to a folder the caller names; audio is extracted as 16 kHz mono WAV (what speech recognition expects). The original media is never modified or copied.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FfmpegError(Exception):
    """Safe to show."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


def find(folder: str = "") -> Tools | None:
    def look(name: str) -> str | None:
        return shutil.which(name, path=folder or None)

    ff, fp = look("ffmpeg"), look("ffprobe")
    return Tools(ff, fp) if ff and fp else None


def _path(p: str | Path) -> str:
    s = str(p)
    if s.startswith("-"):
        raise FfmpegError("a media path may not start with '-'")
    return s


def probe_args(tools: Tools, media: str | Path) -> list[str]:
    return [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", _path(media)]


@dataclass(frozen=True)
class Probe:
    duration: float | None
    has_video: bool
    has_audio: bool
    width: int | None = None
    height: int | None = None


def parse_probe(text: str) -> Probe:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise FfmpegError("ffprobe did not return readable information (is this a media file?)") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return Probe(duration, video is not None, any(s.get("codec_type") == "audio" for s in streams),
                 video.get("width") if video else None, video.get("height") if video else None)


def audio_args(tools: Tools, media: str | Path, out_wav: str | Path) -> list[str]:
    return [tools.ffmpeg, "-v", "error", "-y", "-i", _path(media), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", _path(out_wav)]


def scene_args(tools: Tools, media: str | Path, threshold: float = 0.4) -> list[str]:
    if not 0.05 <= threshold <= 0.95:
        raise FfmpegError("the scene threshold must be between 0.05 and 0.95")
    return [tools.ffmpeg, "-v", "info", "-i", _path(media), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]


_PTS = re.compile(r"pts_time:([0-9.]+)")


def parse_scene_times(stderr: str, limit: int = 300) -> list[float]:
    times = sorted({round(float(m.group(1)), 2) for m in _PTS.finditer(stderr)})
    return times[:limit]


def frame_args(tools: Tools, media: str | Path, at_s: float, out_jpg: str | Path, width: int = 640) -> list[str]:
    if at_s < 0 or not 64 <= width <= 1920:
        raise FfmpegError("bad frame time or width")
    return [tools.ffmpeg, "-v", "error", "-y", "-ss", f"{at_s:.2f}", "-i", _path(media), "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "4", _path(out_jpg)]


def run(args: list[str], timeout: float = 600.0, cancelled: Callable[[], bool] = lambda: False) -> tuple[str, str]:
    """Runs one FFmpeg/ffprobe command; returns (stdout, stderr). Cancel or timeout kills it."""
    if not isinstance(args, list) or not args or any(not isinstance(a, str) for a in args):
        raise FfmpegError("internal: arguments must be a list of strings")
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False, creationflags=NO_WINDOW)
    except OSError as exc:
        raise FfmpegError(f"could not start FFmpeg ({exc.__class__.__name__})") from exc
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancelled() or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise FfmpegError("cancelled" if cancelled() else f"FFmpeg ran longer than {timeout:.0f} s and was stopped") from None
    if proc.returncode != 0:
        raise FfmpegError("FFmpeg could not read this file: " + (err.strip().splitlines() or ["unknown error"])[-1][:160])
    return out, err
