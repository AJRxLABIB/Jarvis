"""Meeting / lecture notes: listen to a whole class or meeting, write it down, then summarise it.

"Start meeting notes" (or Menu -> Meeting / lecture notes): the microphone stays open and
every sentence is transcribed into Documents\\Jarvis\\Notes\\<date> <time>.txt as it is
spoken. "Jarvis, stop the notes" (or "জার্ভিস, নোট বন্ধ করো", or the menu) ends it; the AI
then writes a summary - key points, decisions, action items - and a Word document with the
summary and the full transcript is saved next to the text file. Audio is never saved.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("meeting")
FRAME = 1280  # 80 ms at 16 kHz
PAUSE_SECONDS = 0.9  # silence that ends a sentence
MAX_SEGMENT_SECONDS = 25.0
MIN_SEGMENT_SECONDS = 0.6
SUMMARY_CHARS = 12000  # per summary request; longer notes are summarised in parts
_STOP = re.compile(r"\b(?:jarvis|javis|jervis)\b[\s,.!]*(?:please\s+)?(?:stop|end|finish)\s+(?:the\s+|taking\s+)?"
                   r"(?:notes?|meeting|lecture|recording)|(?:জার্ভিস|জারভিস)[\s,।]*(?:নোট|মিটিং|রেকর্ডিং)\s*"
                   r"(?:বন্ধ|শেষ)", re.I)


def is_stop_request(text: str) -> bool:
    return bool(_STOP.search(text or ""))


def notes_folder() -> Path:
    from app.tools.documents import output_folder

    return output_folder() / "Notes"


class MeetingNotes:
    """Continuous listening in a background thread; a second thread transcribes, so no speech is lost."""

    def __init__(self, microphone, transcribe: Callable[[np.ndarray], str], folder: Path, *,
                 vad_factory: Callable[[], object], on_line: Callable[[str], None] | None = None,
                 on_stop_request: Callable[[], None] | None = None, clock: Callable[[], datetime] = datetime.now) -> None:
        self.microphone = microphone
        self.transcribe = transcribe
        self.folder = Path(folder)
        self.vad_factory = vad_factory
        self.on_line = on_line
        self.on_stop_request = on_stop_request
        self.clock = clock
        self.lines: list[str] = []
        self.path: Path | None = None
        self.started_at: datetime | None = None
        self._stop = threading.Event()
        self._segments: queue.Queue[tuple[datetime, np.ndarray] | None] = queue.Queue()
        self._threads: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def start(self) -> Path:
        self.started_at = self.clock()
        self.folder.mkdir(parents=True, exist_ok=True)
        base = f"Notes {self.started_at:%Y-%m-%d %H-%M}"
        path, n = self.folder / f"{base}.txt", 2
        while path.exists():
            path, n = self.folder / f"{base} ({n}).txt", n + 1
        path.write_text(f"Notes - {self.started_at:%A %d %B %Y, %H:%M}\n\n", encoding="utf-8")
        self.path = path
        self._threads = [threading.Thread(target=self._listen, name="notes-listen", daemon=True),
                         threading.Thread(target=self._write, name="notes-write", daemon=True)]
        for thread in self._threads:
            thread.start()
        log_event(_log, "notes_started")
        return path

    def stop(self, timeout: float = 120.0) -> Path | None:
        """Stop listening; waits until what was already heard is written down."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.1, deadline - time.monotonic()))
        log_event(_log, "notes_stopped", lines=len(self.lines))
        return self.path

    # --------------------------------------------------------------- threads

    def _listen(self) -> None:
        vad = self.vad_factory()
        speech: list[np.ndarray] = []
        silence = 0.0
        started: datetime | None = None
        try:
            with self.microphone.open_stream(FRAME) as stream:
                while not self._stop.is_set():
                    frame = stream.read(0.5)
                    if frame is None:
                        continue
                    if vad.is_speech(frame):
                        if not speech:
                            started = self.clock()
                        speech.append(frame)
                        silence = 0.0
                    elif speech:
                        speech.append(frame)
                        silence += frame.size / 16000
                    length = sum(f.size for f in speech) / 16000
                    if speech and (silence >= PAUSE_SECONDS or length >= MAX_SEGMENT_SECONDS):
                        if length - silence >= MIN_SEGMENT_SECONDS:
                            self._segments.put((started or self.clock(), np.concatenate(speech)))
                        speech, silence = [], 0.0
        except Exception:
            _log.warning("meeting notes: the microphone stopped", exc_info=True)
        finally:
            if speech and sum(f.size for f in speech) / 16000 >= MIN_SEGMENT_SECONDS:
                self._segments.put((started or self.clock(), np.concatenate(speech)))
            self._segments.put(None)

    def _write(self) -> None:
        while True:
            item = self._segments.get()
            if item is None:
                return
            when, audio = item
            try:
                text = (self.transcribe(audio) or "").strip()
            except Exception:
                _log.warning("meeting notes: a sentence could not be transcribed", exc_info=True)
                continue
            if not text:
                continue
            if is_stop_request(text):
                self._stop.set()
                if self.on_stop_request is not None:
                    self.on_stop_request()
                continue
            line = f"[{when:%H:%M}] {text}"
            self.lines.append(line)
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            if self.on_line is not None:
                self.on_line(line)


# ------------------------------------------------------------------ summary

SUMMARY_PROMPT = ("Here is the transcript of a {kind} (it may mix Bangla and English, and speech recognition makes "
                  "mistakes). Write clear notes in the transcript's main language, in simple markdown:\n"
                  "# Summary (3-6 sentences)\n# Key points (bullets)\n# Decisions (bullets, or 'None')\n"
                  "# Action items (bullets with who and when if said, or 'None')\n"
                  "Only use what is in the transcript; do not invent names, numbers or dates.\n\nTranscript:\n{text}")


def summarize(lines: list[str], ask: Callable[[str], str], kind: str = "meeting or lecture") -> str:
    """``ask(prompt) -> answer`` (the local AI). Long transcripts are summarised in parts, then combined."""
    text = "\n".join(lines).strip()
    if not text:
        return "Nothing was said while the notes were on."
    parts, current = [], ""
    for line in lines:
        if len(current) + len(line) > SUMMARY_CHARS and current:
            parts.append(current)
            current = ""
        current += line + "\n"
    parts.append(current)
    if len(parts) == 1:
        return ask(SUMMARY_PROMPT.format(kind=kind, text=parts[0])).strip()
    partial = [ask(SUMMARY_PROMPT.format(kind=f"part {i} of a {kind}", text=p)).strip() for i, p in enumerate(parts, 1)]
    return ask("Combine these notes from the parts of one " + kind + " into one set of notes with the same four "
               "headings (Summary, Key points, Decisions, Action items). Keep every action item.\n\n"
               + "\n\n".join(partial)).strip()


def save_notes(path: Path, title: str, summary: str, lines: list[str]) -> Path:
    """A Word document next to the transcript: the summary, then the full transcript."""
    from app.tools.documents import write_docx

    target = path.with_suffix(".docx")
    write_docx(target, title, summary + "\n\n# Full transcript\n" + "\n".join(lines))
    return target
