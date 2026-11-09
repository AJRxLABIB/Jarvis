"""Searchable personal media: what was said (and shown) in your recordings, with the time it happened.

* Storage is ``media_assets`` / ``media_segments`` (migration 42). Media files are REFERENCED (path + SHA-256), never copied. A changed file (new hash) is re-processed;
  an unchanged one is not.
* A recording is processed in resumable steps - probe, audio, speech-to-text, index - and the step reached is saved; after a stop or crash the work continues from
  there. Cancel is checked between steps and inside FFmpeg.
* Search is keyword search over the stored segments (no embeddings yet), ranked by how many of the words match and how close together. Every hit carries the file name
  and a timestamp; a question with no hit answers "not found in your indexed media" - nothing is made up.
* Chapters and study notes are EXTRACTIVE: chapters start at long pauses or when the vocabulary changes; titles are the most frequent content words; notes quote the
  recording's own sentences with their time. They are summaries by selection, not by understanding - and say so.
* Speech timestamps are only as precise as the chunk the recognizer was given (30 s here). Who spoke is OPTIONAL and approximate (core/speaker_turns.py): turns are stored as kind='speaker' rows (text "Speaker N") and shown beside search hits.

Honest status: tested with fake FFmpeg runners and a fake transcriber. Not run on a real recording here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from app.core import media_ffmpeg as FF, speaker_turns as ST

MEDIA_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
STEPS = ("probed", "audio", "transcribed", "indexed")
_STOP = set("the a an and or but if then of to in on at for with is are was were be been it this that these those as by from i you he she we they me my your our so not no do does did have has had will would can could should there here what which who how when where why about into than also just very more most some any all".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")


def ts(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def file_sha(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def quick_sha(path: Path, edge: int = 1 << 20) -> str:
    """A fast fingerprint for big recordings: size + the first and last megabyte. A change anywhere else that keeps the size is not noticed (that is the trade-off)."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(edge))
        if size > 2 * edge:
            f.seek(size - edge)
            h.update(f.read(edge))
    return "q:" + h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Hit:
    asset: str
    start: float
    end: float
    text: str
    kind: str
    score: float
    speaker: str = ""

    def line(self) -> str:
        return f"{Path(self.asset).name} @ {ts(self.start)}" + (f" [{self.speaker}, approximate]" if self.speaker else "") + f" - {self.text[:200]}"


class MediaIndex:
    def __init__(self, db: Any) -> None:
        self.db = db

    # -- assets
    def add_asset(self, path: str | Path, sha: str, size: int, duration: float | None = None, kind: str = "video") -> int:
        """-> asset id. The same path with a changed hash is reset (its old segments are dropped); the same hash keeps its progress."""
        p = str(path)
        with self.db.transaction() as c:
            row = c.execute("SELECT id, sha FROM media_assets WHERE path = ?", (p,)).fetchone()
            if row is None:
                return c.execute("INSERT INTO media_assets (path, sha, size, kind, duration, added) VALUES (?, ?, ?, ?, ?, ?)", (p, sha, size, kind, duration, _now())).lastrowid
            if row["sha"] != sha:
                c.execute("DELETE FROM media_segments WHERE asset_id = ?", (row["id"],))
                c.execute("UPDATE media_assets SET sha=?, size=?, kind=?, duration=?, status='pending', step='', error='' WHERE id=?", (sha, size, kind, duration, row["id"]))
            return row["id"]

    def asset(self, asset_id: int) -> dict[str, Any] | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def set_state(self, asset_id: int, status: str, step: str | None = None, error: str = "", duration: float | None = None) -> None:
        with self.db.transaction() as c:
            c.execute("UPDATE media_assets SET status=?, step=COALESCE(?, step), error=?, duration=COALESCE(?, duration) WHERE id=?", (status, step, error[:300], duration, asset_id))

    def assets(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            return [dict(r) for r in c.execute("SELECT a.*, (SELECT COUNT(*) FROM media_segments s WHERE s.asset_id = a.id) AS segments FROM media_assets a ORDER BY a.id DESC")]

    def forget(self, path: str | Path) -> bool:
        with self.db.transaction() as c:
            return c.execute("DELETE FROM media_assets WHERE path = ?", (str(path),)).rowcount > 0

    # -- segments
    def put_segments(self, asset_id: int, segments: Sequence[tuple[float, float, str]], kind: str = "speech") -> int:
        rows = [(asset_id, float(a), float(b), t.strip()[:2000], kind) for a, b, t in segments if t and t.strip()]
        with self.db.transaction() as c:
            c.execute("DELETE FROM media_segments WHERE asset_id = ? AND kind = ?", (asset_id, kind))
            c.executemany("INSERT INTO media_segments (asset_id, start, end, text, kind) VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def segments(self, asset_id: int, kind: str = "speech") -> list[tuple[float, float, str]]:
        with self.db.connect() as c:
            return [(r["start"], r["end"], r["text"]) for r in c.execute("SELECT start, end, text FROM media_segments WHERE asset_id = ? AND kind = ? ORDER BY start", (asset_id, kind))]

    # -- search
    def search(self, query: str, limit: int = 8) -> list[Hit]:
        terms = [w for w in dict.fromkeys(w.lower() for w in _WORD.findall(query)) if w not in _STOP][:8]
        if not terms:
            return []
        like = " OR ".join("LOWER(s.text) LIKE ?" for _ in terms)
        with self.db.connect() as c:
            rows = c.execute(f"SELECT s.start, s.end, s.text, s.kind, a.path, s.asset_id FROM media_segments s JOIN media_assets a ON a.id = s.asset_id WHERE s.kind != 'speaker' AND ({like}) LIMIT 800",
                             [f"%{t}%" for t in terms]).fetchall()
        hits = []
        turns: dict[int, list[Any]] = {}
        for r in rows:
            low = r["text"].lower()
            found = [t for t in terms if t in low]
            who = ""
            if r["kind"] == "speech":
                if r["asset_id"] not in turns:
                    turns[r["asset_id"]] = self.speaker_turns(r["asset_id"])
                if turns[r["asset_id"]]:
                    who = ST.annotate([(r["start"], r["end"], r["text"])], turns[r["asset_id"]])[0][3]
            hits.append(Hit(r["path"], r["start"], r["end"], r["text"], r["kind"], len(found) / len(terms) + 0.01 * min(len(low), 100) / 100, who))
        return sorted(hits, key=lambda h: (-h.score, h.asset, h.start))[:limit]

    def put_turns(self, asset_id: int, turns: Sequence[Any]) -> int:
        """Stores speaker turns (core/speaker_turns.Turn) as kind='speaker' rows; replaces the earlier ones."""
        return self.put_segments(asset_id, [(t.start, t.end, t.name) for t in turns], "speaker")

    def speaker_turns(self, asset_id: int) -> list[Any]:
        out = []
        for a, b, name in self.segments(asset_id, "speaker"):
            try:
                out.append(ST.Turn(a, b, int(name.rsplit(" ", 1)[-1]) - 1))
            except ValueError:
                continue
        return out

    def evidence(self, query: str, limit: int = 5) -> str:
        hits = self.search(query, limit)
        if not hits:
            return "Not found in your indexed media (nothing was guessed). It may not be indexed yet, or be worded differently."
        return "Found in your recordings (each line: file @ time - what was said):\n" + "\n".join(h.line() for h in hits)


# ---------------------------------------------------------------- chapters and notes (extractive)

def _content_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text) if w.lower() not in _STOP]


@dataclass
class Chapter:
    start: float
    end: float
    title: str
    segments: list[tuple[float, float, str]]


def make_chapters(segments: Sequence[tuple[float, float, str]], gap_s: float = 8.0, min_len_s: float = 90.0, shift: float = 0.08) -> list[Chapter]:
    chapters: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    for seg in segments:
        if cur:
            long_enough = seg[0] - cur[0][0] >= min_len_s
            pause = seg[0] - cur[-1][1] >= gap_s
            recent = set(w for s in cur[-6:] for w in _content_words(s[2]))
            nxt = set(_content_words(seg[2]))
            changed = bool(recent and nxt) and len(recent & nxt) / len(recent | nxt) < shift
            if long_enough and (pause or changed):
                chapters.append(cur)
                cur = []
        cur.append(seg)
    if cur:
        chapters.append(cur)
    out = []
    for ch in chapters:
        counts: dict[str, int] = {}
        for _, _, t in ch:
            for w in _content_words(t):
                counts[w] = counts.get(w, 0) + 1
        top = [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        out.append(Chapter(ch[0][0], ch[-1][1], ", ".join(top) or "(no clear topic)", ch))
    return out


_DEF = re.compile(r"\b(is|are|means|defined as|refers to|called|stands for)\b|\d", re.I)


def study_notes(title: str, segments: Sequence[tuple[float, float, str]], per_chapter: int = 3) -> str:
    chapters = make_chapters(segments)
    if not chapters:
        return f"{title}: nothing was transcribed, so there are no notes."
    lines = [f"Study notes: {title}", "(Extractive: the recording's own sentences, chosen by simple rules - not a rewritten summary. Times are approximate.)", ""]
    for i, ch in enumerate(chapters, 1):
        lines.append(f"{i}. {ch.title}  [{ts(ch.start)} - {ts(ch.end)}]")
        sentences = [(s[0], x.strip()) for s in ch.segments for x in re.split(r"(?<=[.!?])\s+", s[2]) if len(x.split()) >= 5]
        picked = [x for x in sentences if _DEF.search(x[1])][:per_chapter] or sentences[:per_chapter]
        lines += [f"   - ({ts(t)}) {x}" for t, x in picked]
    return "\n".join(lines)


# ---------------------------------------------------------------- the resumable pipeline

@dataclass
class Outcome:
    status: str           # done | cancelled | failed | skipped
    step: str
    message: str = ""


def process(index: MediaIndex, asset_id: int, *, tools: FF.Tools | None, workdir: Path, transcribe: Callable[[Path], list[tuple[float, float, str]]],
            cancelled: Callable[[], bool] = lambda: False, diarize: Callable[[Path], Sequence[Any]] | None = None, progress: Callable[[str, float], None] = lambda step, frac: None, run: Callable[..., tuple[str, str]] = FF.run) -> Outcome:
    """probe -> audio -> transcribe -> index. Resumes after the last finished step; checks for cancel between steps."""
    a = index.asset(asset_id)
    if a is None:
        return Outcome("failed", "", "unknown media")
    if a["status"] == "done":
        return Outcome("skipped", a["step"], "already indexed (unchanged file)")
    if tools is None:
        index.set_state(asset_id, "failed", error="FFmpeg is not installed")
        return Outcome("failed", a["step"], "FFmpeg is not installed (it is optional; install it and add it to PATH)")
    done = STEPS.index(a["step"]) + 1 if a["step"] in STEPS else 0
    wav = workdir / f"{asset_id}.wav"
    index.set_state(asset_id, "running")
    try:
        if done < 1:
            out, _ = run(FF.probe_args(tools, a["path"]), 60, cancelled)
            probe = FF.parse_probe(out)
            if not probe.has_audio:
                index.set_state(asset_id, "failed", "probed", "this file has no audio track", probe.duration)
                return Outcome("failed", "probed", "no audio track to transcribe")
            index.set_state(asset_id, "running", "probed", duration=probe.duration)
            progress("probed", 0.1)
        if cancelled():
            raise FF.FfmpegError("cancelled")
        if done < 2 or not wav.exists():
            workdir.mkdir(parents=True, exist_ok=True)
            run(FF.audio_args(tools, a["path"], wav), 3600, cancelled)
            index.set_state(asset_id, "running", "audio")
            progress("audio", 0.3)
        if cancelled():
            raise FF.FfmpegError("cancelled")
        if done < 3:
            segs = transcribe(wav)
            index.put_segments(asset_id, segs, "speech")
            if diarize is not None:                                       # optional: who spoke when (approximate); a failure here never loses the transcript
                try:
                    index.put_turns(asset_id, diarize(wav))
                except InterruptedError:
                    raise FF.FfmpegError("cancelled") from None
                except Exception:
                    index.put_turns(asset_id, [])
            index.set_state(asset_id, "running", "transcribed")
            progress("transcribed", 0.9)
        index.set_state(asset_id, "done", "indexed")
        progress("indexed", 1.0)
        return Outcome("done", "indexed")
    except FF.FfmpegError as exc:
        if str(exc) == "cancelled":
            index.set_state(asset_id, "paused", error="cancelled: it will continue from the last finished step")
            return Outcome("cancelled", index.asset(asset_id)["step"], "cancelled; progress was kept")
        index.set_state(asset_id, "failed", error=str(exc))
        return Outcome("failed", index.asset(asset_id)["step"], str(exc))
    finally:
        if index.asset(asset_id)["status"] == "done":
            wav.unlink(missing_ok=True)                                   # the extracted audio is a working file: gone once indexed


def transcribe_wav(wav: Path, stt: Any, chunk_s: float = 30.0) -> list[tuple[float, float, str]]:
    """Cuts a 16 kHz mono WAV into 30 s chunks for the existing speech recognizer (``stt.transcribe(ndarray) -> .text``). Timestamps = chunk boundaries."""
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    step = int(rate * chunk_s)
    out = []
    for i in range(0, len(data), step):
        text = (stt.transcribe(data[i:i + step]).text or "").strip()
        if text:
            out.append((i / rate, min(len(data), i + step) / rate, text))
    return out
