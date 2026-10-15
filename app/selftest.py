"""Self-test: tries every feature for real on this PC and reports pass / fail / skip.

    python run.py --selftest          (or JARVIS: Menu -> Run self-test)

Nothing is changed: no apps are opened, no email is sent, nothing is spoken aloud (speech is
synthesized but not played), and the microphone and camera are only opened if you use them.
The speech round trip synthesizes "open Google Chrome", runs it through speech recognition
and command matching - the whole voice path except the speakers and the microphone.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

STATUS_LABELS = {"pass": "[PASS]", "fail": "[FAIL]", "warn": "[WARN]", "skip": "[SKIP]"}


@dataclass(frozen=True)
class Result:
    name: str
    status: str  # pass | fail | warn | skip
    detail: str
    ms: int = 0

    def line(self) -> str:
        timing = f"  ({self.ms} ms)" if self.ms else ""
        return f"{STATUS_LABELS[self.status]} {self.name}: {self.detail}{timing}"


@dataclass
class Parts:
    settings: Any
    ai: Any = None
    stt: Any = None
    tts_engine: Any = None
    microphone: Any = None
    models_dir: Any = None
    run_async: Callable[[Any], Any] | None = None
    before_microphone: Callable[[], None] | None = None


def _timed(name: str, fn: Callable[[], tuple[str, str]]) -> Result:
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # every failure becomes a line in the report
        status, detail = "fail", f"{type(exc).__name__}: {str(exc)[:200]}"
    return Result(name, status, detail, int((time.perf_counter() - started) * 1000))


def run_selftest(parts: Parts, *, use_microphone: bool = True, on_result: Callable[[Result], None] | None = None
                 ) -> list[Result]:
    s = parts.settings
    run = parts.run_async or asyncio.run
    results: list[Result] = []

    def add(result: Result) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    # --- AI
    def ai_chat():
        from app.ai.base import ChatMessage

        if parts.ai is None:
            return "skip", "no AI connection"
        reply = run(parts.ai.chat([ChatMessage.user("Reply with just the word OK.")]))
        text = reply.message.content.strip()
        return ("pass" if text else "fail"), f"{reply.model} answered '{text[:40]}'"

    add(_timed("AI model", ai_chat))

    def embeddings():
        if parts.ai is None:
            return "skip", "no AI connection"
        vectors = run(parts.ai.embed(["self test"]))
        return "pass", f"{s.embedding_model}: {len(vectors[0])} dimensions"

    add(_timed("Embeddings (memory, documents)", embeddings))

    # --- speech output
    clip_holder: dict[str, Any] = {}

    def speech_out():
        if parts.tts_engine is None:
            return "fail", "no speech engine"
        clip = parts.tts_engine.synthesize("Open Google Chrome.", 1.0)
        clip_holder["clip"] = clip
        return ("pass" if clip.samples.size else "fail"), f"{parts.tts_engine.description}, {clip.duration:.1f} s"

    add(_timed("Speech output (English)", speech_out))

    def bangla_out():
        if parts.tts_engine is None:
            return "skip", "no speech engine"
        clip = parts.tts_engine.synthesize("আমি প্রস্তুত।", 1.0)
        if clip.samples.size:
            return "pass", f"{clip.duration:.1f} s of Bangla speech"
        return "warn", "Bangla isn't spoken right now (offline voice not installed / neural voice unreachable)"

    add(_timed("Speech output (Bangla)", bangla_out))

    # --- speech recognition round trip
    def round_trip():
        from app.agent.shortcuts import match_shortcut
        from app.voice.audio import SAMPLE_RATE, resample

        clip = clip_holder.get("clip")
        if parts.stt is None or clip is None or not clip.samples.size:
            return "skip", "needs speech output and recognition"
        audio = resample(clip.samples, clip.sample_rate, SAMPLE_RATE)
        audio = np.concatenate([np.zeros(SAMPLE_RATE // 2, np.float32), audio, np.zeros(SAMPLE_RATE // 2, np.float32)])
        transcript = parts.stt.transcribe(audio, "en")
        call = match_shortcut(transcript.text, lambda name: name if "chrome" in name.lower() else None)
        where = getattr(parts.stt, "description", "")
        if call is not None and call.name == "open_application":
            return "pass", f"heard '{transcript.text}' -> open_application ({where}, {transcript.elapsed_ms} ms)"
        return "warn", f"heard '{transcript.text}' ({where})"

    add(_timed("Voice round trip (speech -> text -> command)", round_trip))

    def bangla_commands():
        from app.agent.language import to_english_command

        pairs = {"ক্রোম খোলো": "open chrome", "এখন কয়টা বাজে?": "what time is it", "chrome kholo": "open chrome"}
        wrong = [bn for bn, en in pairs.items() if to_english_command(bn) != en]
        return ("fail", f"not understood: {wrong}") if wrong else ("pass", f"{len(pairs)} Bangla commands understood")

    add(_timed("Bangla commands", bangla_commands))

    # --- microphone
    def microphone():
        if not use_microphone or parts.microphone is None:
            return "skip", "not tested"
        if parts.before_microphone is not None:
            parts.before_microphone()
        report, _ = parts.microphone.level_test(2.0)
        if report.peak_db <= -85:
            return "warn", ("no sound at all (digital silence). Check Windows Settings > Privacy & security > "
                            "Microphone > 'Let desktop apps access your microphone', the input device "
                            "(MICROPHONE_DEVICE; list them with --devices) and its mute switch")
        status = "pass" if report.peak_db > -45 else "warn"
        return status, f"peak {report.peak_db:.0f} dBFS - {report.verdict}"

    add(_timed("Microphone", microphone))

    # --- models on disk
    def wake_word():
        from app.voice.hands_free import resolve_engine
        from app.voice.wake_word import missing_models

        if resolve_engine(s) != "openwakeword":
            return "skip", "wake phrase is spotted with speech recognition"
        missing = missing_models(parts.models_dir / "wakeword", s.wake_word, s.wake_word_model)
        return ("fail", f"missing: {missing}") if missing else ("pass", "wake-word model installed")

    add(_timed("Wake word", wake_word))

    def speaker():
        from app.voice.speaker import VOICEPRINT_FILE, model_path

        if not s.speaker_verification:
            return "skip", "only-my-voice is off"
        if not model_path(parts.models_dir / "speaker").is_file():
            return "fail", "voice-print model missing: Menu -> Enroll my voice"
        voiceprint = parts.models_dir.parent / VOICEPRINT_FILE
        return ("pass", "enrolled") if voiceprint.is_file() else ("warn", "not enrolled yet: Menu -> Enroll my voice")

    add(_timed("Only my voice", speaker))

    def gpu():
        from app.voice.stt_select import cuda_available

        device = getattr(parts.stt, "device", "cpu")
        if cuda_available():
            return ("pass" if device == "cuda" else "warn"), f"NVIDIA GPU ready; speech recognition on {device.upper()}"
        return "skip", "no usable NVIDIA GPU (speech recognition on the CPU)"

    add(_timed("GPU", gpu))

    def internet():
        from app.utils import network

        return ("pass", "online") if not network.is_offline_now(s) else ("warn", "offline")

    add(_timed("Internet", internet))

    def outlook():
        if os.name != "nt":
            return "skip", "Windows only"
        from app.tools.outlook import _outlook

        _outlook()
        return "pass", "classic Outlook reachable"

    result = _timed("Outlook (calendar, email)", outlook)
    if result.status == "fail":
        result = Result(result.name, "skip", "classic Outlook not available", result.ms)
    add(result)

    def camera():
        if not s.presence_detection:
            return "skip", "presence detection is off"
        from app.vision.presence import Camera, FaceDetector

        camera = Camera()
        try:
            frame = camera.frame()
        finally:
            camera.close()
        if frame is None:
            return "fail", "the camera gave no picture"
        return "pass", "camera works; " + ("a face is visible" if FaceDetector().has_face(frame) else "no face in view")

    add(_timed("Camera (presence)", camera))
    return results


def summary(results: list[Result]) -> str:
    counts = {k: sum(r.status == k for r in results) for k in STATUS_LABELS}
    return f"{counts['pass']} passed, {counts['fail']} failed, {counts['warn']} warnings, {counts['skip']} skipped"
