"""Voice validation: end-of-speech latency, how fast the voice detector reacts, how fast the speaker stops, whether Jarvis hears itself.

All measurements come from the real microphone and speakers when YOU run ``python run.py --validate voice``; the analysis below is pure
and tested with synthetic frames, which proves the arithmetic, not that your hardware is fast. Bangla / English accuracy is measured by
``--benchmark`` (per-language word and character error, ``app/benchmark.py``) and summarised here from its last saved result.

What is measured (and the thresholds that turn a number into pass / warn / fail):

* END-OF-SPEECH LATENCY - from your last loud moment to the recording ending (``Microphone.record`` with its real voice detector). Expected
  about the silence timeout; much more feels sluggish, a timeout under 0.5 s tends to cut sentences.
* VOICE-DETECTOR REACTION - from the first loud frame to the detector saying "speech" (late = clipped word starts), and how long it keeps
  saying "speech" after you stop (the tail).
* SPEAKER STOP LATENCY - from asking the speech to stop to the audio really being idle (the interruption response).
* TIME TO FIRST AUDIO - from asking Jarvis to speak to the first sound.
* SELF-HEARING - while Jarvis speaks and you stay silent, how often the detector says "speech" (it would treat its own voice as you).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.validation.report import FAIL, PASS, PENDING, SKIP, WARN, Report, Result, timed

FRAME_SECONDS = 512 / 16000          # the detector's frame (32 ms)
ONSET_RISE_DB = 12.0                 # "loud" = this far above the room's noise
ONSET_RUN = 3                        # frames in a row


@dataclass(frozen=True)
class Frame:
    t: float            # seconds since the capture began
    db: float           # level of the frame
    vad: bool           # the detector's verdict


# ------------------------------------------------------------------ pure analysis


def noise_floor(frames: list[Frame], lead: int = 6) -> float:
    levels = sorted(f.db for f in frames[:lead]) or [-80.0]
    return levels[len(levels) // 2]


def loud_runs(frames: list[Frame], rise: float = ONSET_RISE_DB) -> tuple[float | None, float | None]:
    """(first loud time, last loud time): loud = ``rise`` dB over the noise floor, the start needing ONSET_RUN frames in a row."""
    floor = noise_floor(frames)
    loud = [f.db > floor + rise for f in frames]
    start = next((frames[i].t for i in range(len(frames) - ONSET_RUN + 1) if all(loud[i:i + ONSET_RUN])), None)
    end = next((frames[i].t for i in range(len(frames) - 1, -1, -1) if loud[i]), None)
    return start, end


def vad_reaction(frames: list[Frame]) -> dict[str, float | None]:
    """ms from the first loud frame to the detector's first 'speech' (>= 0), and ms it kept saying 'speech' after the last loud frame."""
    start, end = loud_runs(frames)
    first_vad = next((f.t for f in frames if f.vad and (start is None or f.t >= start - 0.2)), None)
    last_vad = next((f.t for f in reversed(frames) if f.vad), None)
    if start is None or first_vad is None:
        return {"onset_ms": None, "tail_ms": None}
    return {"onset_ms": max(0.0, (first_vad - start) * 1000),
            "tail_ms": max(0.0, ((last_vad or end or 0) - (end or 0)) * 1000)}


def judge_onset(ms: float | None) -> tuple[str, str]:
    if ms is None:
        return SKIP, "no clear speech was heard (speak louder or closer to the microphone)"
    if ms <= 250:
        return PASS, f"the detector reacted {ms:.0f} ms after you started"
    if ms <= 500:
        return WARN, f"the detector reacted {ms:.0f} ms after you started: soft word starts may be clipped"
    return FAIL, f"the detector reacted {ms:.0f} ms after you started: the start of what you say will be cut off"


def judge_end_of_speech(latency_s: float | None, silence_timeout: float) -> tuple[str, str]:
    if latency_s is None:
        return SKIP, "no clear speech was heard"
    extra = latency_s - silence_timeout
    text = f"recording ended {latency_s * 1000:.0f} ms after your last loud moment (silence timeout {silence_timeout * 1000:.0f} ms)"
    if silence_timeout < 0.5:
        return WARN, text + "; a timeout under 0.5 s tends to cut sentences at a pause"
    if extra <= 0.35:
        return PASS, text
    if extra <= 0.8:
        return WARN, text + f"; {extra * 1000:.0f} ms more than the timeout - it feels slow to answer"
    return FAIL, text + f"; {extra * 1000:.0f} ms more than the timeout - something is delaying the end of the recording"


def judge_stop(ms: float | None) -> tuple[str, str]:
    if ms is None:
        return PENDING, "no speech engine was available to test"
    if ms <= 300:
        return PASS, f"speech was silent {ms:.0f} ms after the stop request"
    if ms <= 700:
        return WARN, f"speech took {ms:.0f} ms to go silent after the stop request: interruptions will feel late"
    return FAIL, f"speech took {ms:.0f} ms to go silent after the stop request: the interruption does not stop it promptly"


def judge_first_audio(ms: float | None) -> tuple[str, str]:
    if ms is None:
        return PENDING, "no speech engine was available to test"
    if ms <= 1500:
        return PASS, f"first sound {ms:.0f} ms after the request"
    if ms <= 3500:
        return WARN, f"first sound {ms:.0f} ms after the request (the first sentence of every answer waits this long)"
    return FAIL, f"first sound {ms:.0f} ms after the request: speech is too slow to feel conversational"


def judge_self_hearing(frames: list[Frame]) -> tuple[str, str]:
    if not frames:
        return PENDING, "no microphone frames while speaking"
    ratio = sum(f.vad for f in frames) / len(frames)
    loud = float(np.mean([f.db for f in frames]))
    text = f"the detector called {100 * ratio:.0f}% of the frames 'speech' while Jarvis spoke and you were silent (mic level {loud:.0f} dB)"
    if ratio <= 0.05:
        return PASS, text + ": Jarvis does not mistake its own voice for you"
    if ratio <= 0.2:
        return WARN, text + ": echo is partly reaching the microphone; keep the echo protection on or use a headset"
    return FAIL, text + ": Jarvis hears itself. Use a headset, lower the speaker volume or move the microphone away"


def synth_frames(speech_from: float, speech_to: float, total: float, vad_delay: float = 0.05, vad_tail: float = 0.2,
                 quiet: float = -60.0, loud: float = -25.0) -> list[Frame]:
    """A synthetic capture for tests / dry runs: silence, speech between the two times, a detector with delay and tail."""
    out, t = [], 0.0
    while t < total:
        speaking = speech_from <= t <= speech_to
        out.append(Frame(round(t, 4), loud if speaking else quiet, speech_from + vad_delay <= t <= speech_to + vad_tail))
        t += FRAME_SECONDS
    return out


# ------------------------------------------------------------------ hardware access


class VoiceEnv:
    """What the checks need. ``RealVoiceEnv`` uses the real microphone, detector and speech engine; tests use a fake."""

    def has_microphone(self) -> bool: ...
    def say(self, text: str) -> None: ...
    def capture_frames(self, seconds: float) -> list[Frame]: ...
    def record_end_of_speech(self) -> tuple[float | None, float]: ...         # (latency s, silence timeout s)
    def _speaking(self) -> bool:
        value = getattr(self.tts, "is_speaking", False)          # a property on TtsManager, a method on some stand-ins
        return bool(value() if callable(value) else value)

    def tts_available(self) -> bool: ...
    def tts_first_audio_and_stop(self) -> tuple[float | None, float | None]: ...   # (first audio ms, stop ms)
    def frames_while_speaking(self, seconds: float) -> list[Frame]: ...


def benchmark_summary(data_dir: Path) -> list[Result]:
    """Per-language accuracy from the newest saved ``--benchmark`` run, with the verdict its numbers justify."""

    path = Path(data_dir) / "benchmark" / "history.jsonl"
    if not path.is_file():
        return [Result("Bangla / English accuracy", PENDING, "no benchmark yet: run --record-benchmark, then --benchmark (it measures word and character error per language)")]
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            last = json.loads(line)
        except ValueError:
            continue
    langs = (last or {}).get("by_language") or (last or {}).get("result", {}).get("by_language")
    if not langs:
        return [Result("Bangla / English accuracy", PENDING, "the last benchmark predates per-language results: run --record-benchmark (adds the new prompts) and --benchmark again")]
    out = []
    for lang, m in langs.items():
        name = {"bn": "Bangla", "mixed": "mixed Bangla-English", "en": "English"}.get(lang, lang)
        verdict = m["verdict"]
        status = PASS if verdict.startswith("reliable") else WARN if verdict.startswith("usable") else PENDING if "NO CLAIM" in verdict else FAIL
        out.append(Result(f"{name} recognition (from --benchmark)", status,
                          f"{m['items']} recordings, word error {m['wer']}%, character error {m['cer']}% -> {verdict}", evidence=m))
    return out


def run(env: VoiceEnv, data_dir: Path | None = None, say=print) -> Report:
    report = Report("voice")
    if not env.has_microphone():
        report.add(Result("microphone", PENDING, "no microphone was found: every voice check is pending until run on your PC"))
        return report
    say("\nCheck 1/4 - voice detector. After the beep-free pause say 'Jarvis, one two three four five' at your normal volume, then stay quiet.")
    frames = env.capture_frames(7.0)
    react = vad_reaction(frames)
    report.add(timed("voice detector reaction", lambda: judge_onset(react["onset_ms"]) + ({"onset_ms": react["onset_ms"], "tail_ms": react["tail_ms"]},)))
    say("\nCheck 2/4 - end of speech. Say 'what time is it' and stop talking.")
    latencies = []
    for _ in range(3):
        latency, timeout = env.record_end_of_speech()
        latencies.append(latency)
    got = [x for x in latencies if x is not None]
    median = sorted(got)[len(got) // 2] if got else None
    report.add(timed("end-of-speech latency", lambda: judge_end_of_speech(median, timeout) + ({"trials": latencies},)))
    if env.tts_available():
        say("\nCheck 3/4 - speaker. Jarvis will speak a sentence and be stopped; listen and do not talk.")
        first, stop = env.tts_first_audio_and_stop()
        report.add(timed("time to first spoken audio", lambda: judge_first_audio(first)))
        report.add(timed("speaker stop latency", lambda: judge_stop(stop)))
        say("\nCheck 4/4 - hearing itself. Jarvis will speak again. Stay completely silent.")
        report.add(timed("does Jarvis hear itself?", lambda: judge_self_hearing(env.frames_while_speaking(5.0))))
    else:
        for name in ("time to first spoken audio", "speaker stop latency", "does Jarvis hear itself?"):
            report.add(Result(name, PENDING, "no speech engine is ready (run --setup-voice, or check Settings > Voice)"))
    if data_dir is not None:
        for r in benchmark_summary(data_dir):
            report.add(r)
    return report


class RealVoiceEnv(VoiceEnv):
    """The real microphone / detector / speech engine. Run by you; not exercised by the automated tests."""

    def __init__(self, ctx: Any, tts: Any = None) -> None:
        self.ctx, self.tts = ctx, tts
        from app.voice.microphone import Microphone

        self.mic = Microphone(ctx.settings.microphone_device, enabled=ctx.settings.microphone_enabled)

    def has_microphone(self) -> bool:
        try:
            from app.voice.audio import list_devices

            return bool(list_devices("input"))
        except Exception:
            return False

    def say(self, text: str) -> None:
        print(text, flush=True)

    def _stream_frames(self, seconds: float) -> list[Frame]:
        from app.voice.audio import rms_dbfs
        from app.voice.vad import create_vad

        vad = create_vad(self.ctx.settings.vad_engine)
        frames: list[Frame] = []
        with self.mic.open_stream(512) as stream:
            began = time.monotonic()
            while time.monotonic() - began < seconds:
                chunk = stream.read(0.5)
                if chunk is None:
                    continue
                frames.append(Frame(time.monotonic() - began, rms_dbfs(chunk), bool(vad.is_speech(chunk))))
        return frames

    def capture_frames(self, seconds: float) -> list[Frame]:
        return self._stream_frames(seconds)

    def record_end_of_speech(self) -> tuple[float | None, float]:
        from app.voice.vad import create_vad

        timeout = float(self.ctx.settings.silence_timeout)
        stamps: list[tuple[float, float]] = []
        self.mic.on_level = lambda db: stamps.append((time.monotonic(), db))
        try:
            recording = self.mic.record(silence_timeout=timeout, start_timeout=6, max_seconds=10, vad=create_vad(self.ctx.settings.vad_engine))
        finally:
            self.mic.on_level = None
        returned = time.monotonic()
        if not recording.speech_detected or not stamps:
            return None, timeout
        peak = max(db for _, db in stamps)
        last_loud = max((t for t, db in stamps if db > peak - 25.0), default=None)
        return (returned - last_loud) if last_loud else None, timeout

    def tts_available(self) -> bool:
        return self.tts is not None and bool(getattr(self.tts, "available", False))

    def tts_first_audio_and_stop(self) -> tuple[float | None, float | None]:
        asked = time.monotonic()
        self.tts.speak("This is a test sentence that is long enough to be interrupted halfway through, so please listen to it for a moment.")
        first = None
        while time.monotonic() - asked < 8:
            if self._speaking():
                first = (time.monotonic() - asked) * 1000
                break
            time.sleep(0.01)
        if first is None:
            return None, None
        time.sleep(0.8)
        requested = time.monotonic()
        self.tts.stop()
        while time.monotonic() - requested < 3:
            if not self._speaking():
                return first, (time.monotonic() - requested) * 1000
            time.sleep(0.005)
        return first, 3000.0

    def frames_while_speaking(self, seconds: float) -> list[Frame]:
        import threading

        done = threading.Event()
        frames: list[Frame] = []

        def listen() -> None:
            frames.extend(self._stream_frames(seconds))
            done.set()

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        time.sleep(0.4)
        self.tts.speak("I am speaking now, and I should not hear myself, even though the microphone is open while I talk.")
        done.wait(seconds + 3)
        self.tts.stop()
        return frames
