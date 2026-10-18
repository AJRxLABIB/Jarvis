"""Voice doctor: find out WHY Jarvis doesn't hear you, by measuring every microphone instead of guessing.

`python run.py --voice-doctor` records a few seconds from each distinct input device while you speak normally, and for each
reports how loud you are, how much louder than the room, whether it clips, and whether it delivered pure silence.
It then names the best microphone, says what is wrong with the others, and prints the exact setting to change.

Typical causes it separates (all of them look like "it doesn't hear me" from the outside):
  * Windows is blocking desktop apps from the microphone (every device returns exact silence),
  * the default input is the wrong one (a webcam or a virtual device) while another is fine,
  * the level is far too low (speaking loudly into a mic that delivers -50 dBFS),
  * the room is noisier than the voice (low signal-to-noise ratio), or the signal clips.
Nothing is saved; audio stays in memory and is discarded after the test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.voice.audio import SAMPLE_RATE, DeviceInfo, list_devices, peak_dbfs, rms_dbfs

SILENCE_DB = -80.0          # below this the device delivered (almost) exact zeros
NO_SIGNAL_PEAK_DB = -50.0   # nothing that could be speech
QUIET_PEAK_DB = -30.0       # speaking up close normally peaks well above this
QUIET_SPEECH_RMS_DB = -42.0
GOOD_SNR_DB = 18.0
POOR_SNR_DB = 10.0
_SKIP_NAMES = ("sound mapper", "primary sound capture", "stereo mix", "what u hear", "loopback")


@dataclass
class DeviceResult:
    index: int
    name: str
    host_api: str = ""
    is_default: bool = False
    error: str = ""
    peak_db: float = -100.0
    speech_db: float = -100.0       # loudness of the loudest tenth of a second blocks (your voice)
    noise_db: float = -100.0        # loudness of the quietest blocks (the room)
    snr_db: float = 0.0
    clipped: bool = False
    problems: list[str] = field(default_factory=list)
    score: float = -1e9
    audio: Any = None               # kept only until the verdict (transcription), then dropped

    @property
    def usable(self) -> bool:
        return not self.error and self.peak_db > NO_SIGNAL_PEAK_DB


def analyse(audio: np.ndarray) -> dict[str, float | bool]:
    """Pure measurement of one recording (16 kHz mono float)."""
    if audio.size < SAMPLE_RATE * 0.5:
        return {"peak_db": -100.0, "speech_db": -100.0, "noise_db": -100.0, "snr_db": 0.0, "clipped": False}
    blocks = np.array_split(audio, max(1, int(audio.size / (SAMPLE_RATE * 0.1))))
    levels = np.array([rms_dbfs(b) for b in blocks])
    speech = float(np.percentile(levels, 90))
    noise = float(np.percentile(levels, 15))
    return {"peak_db": peak_dbfs(audio), "speech_db": speech, "noise_db": noise,
            "snr_db": max(0.0, speech - noise) if speech > SILENCE_DB else 0.0,
            "clipped": bool(np.max(np.abs(audio)) >= 0.99)}


def judge(result: DeviceResult) -> DeviceResult:
    """Fill ``problems`` and ``score`` from the measurements."""
    r = result
    if r.error:
        r.problems.append(f"could not be opened ({r.error[:80]})")
        return r
    if r.peak_db <= SILENCE_DB:
        r.problems.append("delivered exact silence (muted, unplugged, or blocked by Windows)")
    elif r.peak_db < NO_SIGNAL_PEAK_DB:
        r.problems.append("heard almost nothing (wrong device, or the Windows input level is near zero)")
    elif r.peak_db < QUIET_PEAK_DB or r.speech_db < QUIET_SPEECH_RMS_DB:
        r.problems.append(f"too quiet (peaks at {r.peak_db:.0f} dBFS): raise its level in Windows or move closer")
    if r.clipped:
        r.problems.append("clips (too loud): lower its level in Windows")
    if r.usable and r.snr_db < POOR_SNR_DB:
        r.problems.append(f"noisy: your voice is only {r.snr_db:.0f} dB above the room (aim for {GOOD_SNR_DB:.0f}+)")
    if r.usable:
        r.score = min(r.snr_db, 40.0) + min(r.speech_db + 60.0, 25.0) - (15.0 if r.clipped else 0.0) \
            - (10.0 if r.peak_db < QUIET_PEAK_DB else 0.0)
    return r


def pick_devices(devices: list[DeviceInfo]) -> list[DeviceInfo]:
    """One entry per distinct microphone (Windows lists each once per host API), skipping mixers and loopbacks."""
    chosen: dict[str, DeviceInfo] = {}
    for d in devices:
        low = d.name.lower()
        if any(s in low for s in _SKIP_NAMES) or d.max_input_channels < 1:
            continue
        key = low.strip()
        if key not in chosen or (d.is_default_input and not chosen[key].is_default_input):
            chosen[key] = d
    return sorted(chosen.values(), key=lambda d: (not d.is_default_input, d.index))


def examine(devices: list[DeviceInfo], record: Callable[[DeviceInfo], np.ndarray],
            progress: Callable[[DeviceInfo], None] | None = None) -> list[DeviceResult]:
    out: list[DeviceResult] = []
    for device in devices:
        if progress is not None:
            progress(device)
        result = DeviceResult(device.index, device.name, device.host_api, device.is_default_input)
        try:
            audio = record(device)
            for key, value in analyse(audio).items():
                setattr(result, key, value)
            result.audio = audio
        except Exception as exc:                      # a busy, exclusive or broken device must not stop the others
            result.error = str(exc) or exc.__class__.__name__
        out.append(judge(result))
    return out


def verdict(results: list[DeviceResult], configured: str = "") -> list[str]:
    """Plain-language findings and what to change."""
    if not results:
        return ["No microphone was found. Plug one in, or check Settings -> System -> Sound -> Input."]
    lines: list[str] = []
    usable = [r for r in results if r.usable]
    if all(r.peak_db <= SILENCE_DB or r.error for r in results):
        lines.append("Every microphone returned silence or could not be opened. This is almost always Windows blocking the "
                     "microphone: Settings -> Privacy & security -> Microphone -> turn on 'Microphone access' AND 'Let desktop "
                     "apps access your microphone'. Also check the mute switch / key on the laptop.")
        return lines
    if not usable:
        lines.append("No microphone heard you clearly. Raise the input level (Settings -> System -> Sound -> Input -> "
                     "your microphone -> Input volume) and speak while testing.")
        return lines
    best = max(usable, key=lambda r: r.score)
    default = next((r for r in results if r.is_default), None)
    lines.append(f"Best microphone: '{best.name}' (voice {best.speech_db:.0f} dBFS, {best.snr_db:.0f} dB above the room).")
    if default is not None and default is not best and (not default.usable or default.score < best.score - 8):
        why = "; ".join(default.problems) or "it scored clearly lower"
        lines.append(f"Windows' default input '{default.name}' is the weaker one ({why}). Either set '{best.name}' as the "
                     f"default input in Windows, or put this in .env:  MICROPHONE_DEVICE={best.name}")
    elif configured and best.name.lower() not in configured.lower() and not configured.isdigit():
        lines.append(f"MICROPHONE_DEVICE is '{configured}', but '{best.name}' scored better. Consider MICROPHONE_DEVICE={best.name}")
    for r in results:
        if r is not best and r.problems:
            lines.append(f"  - {r.name}: {r.problems[0]}")
    for p in best.problems:
        lines.append(f"Even the best one: {p}.")
    if not best.problems:
        lines.append("Its level and clarity are fine, so if Jarvis still misses you the cause is on the recognition side "
                     "(try `python run.py --mic-test`, and say the wake phrase closer to the microphone).")
    return lines


def run_doctor(settings, *, out, seconds: float = 3.0, record: Callable[[DeviceInfo], np.ndarray] | None = None,
               sd: Any = None, transcribe: Callable[[np.ndarray], Any] | None = None) -> int:
    """The CLI flow. Returns 0 when a good microphone was found, 1 otherwise."""
    devices = pick_devices(list_devices("input", sd))
    if record is None:
        from app.voice.microphone import Microphone

        def record(device: DeviceInfo) -> np.ndarray:                  # noqa: E306
            mic = Microphone(str(device.index), enabled=True, sd=sd)
            return mic.record(fixed_seconds=seconds).audio

    out.write(f"Testing {len(devices)} microphone(s), {seconds:.0f} seconds each. Speak normally the whole time, e.g. "
              f"\"{getattr(settings, 'wake_word', 'hey jarvis').title()}, open Chrome and check my calendar\".\n")

    def announce(device: DeviceInfo) -> None:
        out.write(f"  {device.name} ... speak now\n")
        out.flush() if hasattr(out, "flush") else None

    results = examine(devices, record, announce)
    out.write("\n")
    for r in results:
        state = "OK" if r.usable and not r.problems else ("CHECK" if r.usable else "PROBLEM")
        detail = r.error[:60] if r.error else f"peak {r.peak_db:.0f} dBFS, voice {r.speech_db:.0f}, room {r.noise_db:.0f}, SNR {r.snr_db:.0f} dB"
        out.write(f"  [{state:7}] {r.name}{' (Windows default)' if r.is_default else ''}: {detail}\n")
    out.write("\n")
    lines = verdict(results, getattr(settings, "microphone_device", ""))
    for line in lines:
        out.write(line + "\n")
    usable = [r for r in results if r.usable]
    if usable and transcribe is not None:
        best = max(usable, key=lambda r: r.score)
        try:
            heard = transcribe(best.audio)
            out.write(f"\nOn the best microphone Jarvis heard: \"{heard.text or '(nothing)'}\" (confidence {heard.confidence:.2f}).\n")
        except Exception as exc:
            out.write(f"\n(The recognition check could not run: {str(exc)[:100]})\n")
    for r in results:
        r.audio = None
    return 0 if usable and not max(usable, key=lambda r: r.score).problems else 1
