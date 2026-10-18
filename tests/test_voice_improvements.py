from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest

from app.voice import doctor
from app.voice.audio import SAMPLE_RATE, DeviceInfo
from app.voice.hands_free import FRAME_SECONDS, UtteranceCollector, ends_unfinished

# ---------------------------------------------------------------- unfinished sentences


@pytest.mark.parametrize("text", [
    "open the", "check my calendar and", "send it to", "open Chrome,", "I want to um", "turn on the lights in", "play my...",
    "খুলো আমার", "চালু করো এবং"])
def test_trailing_off_is_recognised(text: str) -> None:
    assert ends_unfinished(text)


@pytest.mark.parametrize("text", [
    "open Chrome", "what time is it", "Jarvis", "", "turn off the lights.", "is it going to rain today?", "stop!",
    "open the downloads folder", "সময় কত"])
def test_complete_sentences_are_not_held(text: str) -> None:
    assert not ends_unfinished(text)


# ---------------------------------------------------------------------- collector


class ScriptedVAD:
    pass


def feed(collector: UtteranceCollector, speech_seconds: float, quiet_seconds: float):
    frame = np.zeros(int(SAMPLE_RATE * FRAME_SECONDS), dtype=np.float32)
    result = None
    for _ in range(int(speech_seconds / FRAME_SECONDS)):
        result = collector.feed(frame, True) or result
    quiet_frames = 0
    for _ in range(int(quiet_seconds / FRAME_SECONDS)):
        quiet_frames += 1
        got = collector.feed(frame, False)
        if got is not None:
            return got, quiet_frames * FRAME_SECONDS
    return None, quiet_frames * FRAME_SECONDS


def make(hold=None, silence=0.9):
    return UtteranceCollector(ScriptedVAD(), start_timeout=10, silence_timeout=silence, max_seconds=15, hold=hold)


def test_without_a_hold_the_pause_ends_the_utterance_as_before() -> None:
    utterance, waited = feed(make(), 1.0, 3.0)
    assert utterance is not None and utterance.speech and 0.85 <= waited <= 1.0


def test_an_unfinished_sentence_gets_extra_time_then_ends() -> None:
    utterance, waited = feed(make(hold=lambda: True), 1.0, 5.0)
    assert utterance is not None
    assert 2.4 <= waited <= 2.6                       # 0.9 + two grants of 0.8 s, and no more than that


def test_a_finished_sentence_is_not_delayed() -> None:
    utterance, waited = feed(make(hold=lambda: False), 1.0, 3.0)
    assert utterance is not None and waited <= 1.0


def test_continuing_to_speak_during_the_grace_keeps_the_sentence_together() -> None:
    collector = make(hold=lambda: True)
    got, _ = feed(collector, 1.0, 1.2)                # a pause past the normal timeout, but the grace holds it open
    assert got is None
    got, _ = feed(collector, 1.0, 5.0)                # the rest of the sentence arrives, then a real end
    assert got is not None and got.audio.size > SAMPLE_RATE * 1.9


def test_a_crashing_hold_callback_never_breaks_listening() -> None:
    def boom() -> bool:
        raise RuntimeError("x")
    utterance, waited = feed(make(hold=boom), 1.0, 3.0)
    assert utterance is not None and waited <= 1.0


# ------------------------------------------------------------------ voice doctor


def device(index: int, name: str, default: bool = False, api: str = "MME") -> DeviceInfo:
    return DeviceInfo(index, name, api, 1, 0, 48000.0, default, False)


def speech(level: float, seconds: float = 3.0, room: float = 0.002) -> np.ndarray:
    rng = np.random.default_rng(3)
    audio = (rng.standard_normal(int(seconds * SAMPLE_RATE)) * room).astype(np.float32)
    for start in np.arange(0.2, seconds - 0.4, 0.5):                    # bursts of "speech"
        a, b = int(start * SAMPLE_RATE), int((start + 0.3) * SAMPLE_RATE)
        audio[a:b] += (level * np.sin(2 * np.pi * 180 * np.arange(b - a) / SAMPLE_RATE)).astype(np.float32)
    return audio


def test_analysis_measures_voice_room_and_snr() -> None:
    m = doctor.analyse(speech(0.2))
    assert m["peak_db"] > -20 and m["snr_db"] > 20 and not m["clipped"]
    silent = doctor.analyse(np.zeros(SAMPLE_RATE * 3, dtype=np.float32))
    assert silent["peak_db"] <= doctor.SILENCE_DB and silent["snr_db"] == 0.0


def test_pick_devices_dedupes_hostapis_and_skips_mixers() -> None:
    picked = doctor.pick_devices([
        device(0, "Microsoft Sound Mapper - Input"), device(1, "Microphone (USB Audio)", True),
        device(2, "Microphone (USB Audio)", False, "WASAPI"), device(3, "Stereo Mix (Realtek)"),
        device(4, "Microphone Array (Intel)")])
    assert [d.name for d in picked] == ["Microphone (USB Audio)", "Microphone Array (Intel)"]
    assert picked[0].index == 1                         # the default one, not the WASAPI duplicate


def run(recordings: dict[str, np.ndarray | Exception], devices=None, configured=""):
    devices = devices or [device(1, "Webcam Mic", True), device(2, "Headset Mic"), device(3, "Laptop Array")]

    def record(d: DeviceInfo) -> np.ndarray:
        got = recordings[d.name]
        if isinstance(got, Exception):
            raise got
        return got
    return doctor.verdict(results := doctor.examine(devices, record), configured), results


def test_the_best_microphone_is_recommended_over_a_silent_default() -> None:
    lines, results = run({"Webcam Mic": np.zeros(SAMPLE_RATE * 3, dtype=np.float32), "Headset Mic": speech(0.25),
                          "Laptop Array": speech(0.01, room=0.01)})
    text = "\n".join(lines)
    assert "Best microphone: 'Headset Mic'" in text
    assert "default input 'Webcam Mic' is the weaker one" in text and "MICROPHONE_DEVICE=Headset Mic" in text
    assert [r.usable for r in results] == [False, True, True]


def test_every_device_silent_points_at_the_windows_privacy_setting() -> None:
    zeros = np.zeros(SAMPLE_RATE * 3, dtype=np.float32)
    lines, _ = run({"Webcam Mic": zeros, "Headset Mic": zeros, "Laptop Array": RuntimeError("busy")})
    text = " ".join(lines)
    assert "Let desktop apps access your microphone" in text and "Best microphone" not in text


def test_a_quiet_microphone_is_called_out_with_the_fix() -> None:
    lines, results = run({"Webcam Mic": speech(0.02, room=0.0005), "Headset Mic": speech(0.015, room=0.0005),
                          "Laptop Array": speech(0.02, room=0.0005)})
    assert any("too quiet" in p for r in results for p in r.problems)
    assert "raise" in " ".join(lines).lower()


def test_clipping_and_noise_are_flagged() -> None:
    loud = np.clip(speech(2.0), -1, 1)
    noisy = speech(0.05, room=0.05)
    lines, results = run({"Webcam Mic": loud, "Headset Mic": noisy, "Laptop Array": speech(0.2)})
    assert any("clips" in p for p in results[0].problems) and any("noisy" in p for p in results[1].problems)
    assert "Best microphone: 'Laptop Array'" in "\n".join(lines)


def test_a_configured_but_worse_device_is_pointed_out() -> None:
    lines, _ = run({"Webcam Mic": speech(0.2), "Headset Mic": speech(0.3, room=0.0002), "Laptop Array": speech(0.1)},
                   configured="Webcam")
    assert any("MICROPHONE_DEVICE is 'Webcam'" in line for line in lines)


def test_one_broken_device_does_not_stop_the_test() -> None:
    _, results = run({"Webcam Mic": RuntimeError("device in use"), "Headset Mic": speech(0.25), "Laptop Array": speech(0.1)})
    assert "could not be opened" in results[0].problems[0] and results[1].usable


def test_no_microphones_at_all() -> None:
    assert "No microphone was found" in doctor.verdict([])[0]


def test_the_full_flow_prints_a_report_and_forgets_the_audio(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "list_devices", lambda kind, sd: [device(1, "Headset Mic", True)])
    out = io.StringIO()
    heard = SimpleNamespace(text="hey jarvis open chrome", confidence=0.91)
    code = doctor.run_doctor(SimpleNamespace(wake_word="hey jarvis", microphone_device=""), out=out, seconds=1,
                             record=lambda d: speech(0.25), transcribe=lambda a: heard)
    text = out.getvalue()
    assert code == 0 and "[OK" in text and "Jarvis heard: \"hey jarvis open chrome\"" in text
    assert "Windows default" in text


def test_the_real_microphone_path_works_with_a_fake_sound_device() -> None:
    from tests.fake_audio import FakeSoundDevice, tone

    sd = FakeSoundDevice(tone(4.0, 0.2))
    out = io.StringIO()
    code = doctor.run_doctor(SimpleNamespace(wake_word="hey jarvis", microphone_device=""), out=out, seconds=1, sd=sd)
    assert "Testing 1 microphone(s)" in out.getvalue() and "Microphone (USB Audio)" in out.getvalue() and code in (0, 1)
