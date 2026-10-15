"""Phase 13: the terminal voice commands (--devices, --say, --mic-test, --setup-voice) with fakes."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import cli_voice
from app.main import bootstrap
from app.voice.audio import AudioError


@pytest.fixture()
def ctx(tmp_path: Path):
    return bootstrap(tmp_path, environ={})


def device(index, name, inputs, outputs, default_in=False, default_out=False):
    return SimpleNamespace(index=index, name=name, host_api="Windows WASAPI", max_input_channels=inputs,
                           max_output_channels=outputs, is_default_input=default_in, is_default_output=default_out)


def test_devices_lists_microphones_and_speakers(ctx, monkeypatch) -> None:
    monkeypatch.setattr(cli_voice, "list_devices", lambda: [
        device(1, "Microphone Array (Realtek)", 2, 0, default_in=True), device(4, "Speakers (Realtek)", 0, 2)])
    out = io.StringIO()
    assert cli_voice.run_devices(ctx, out) == 0
    text = out.getvalue()
    assert "* [  1] Microphone Array" in text and "  [  4] Speakers" in text
    assert text.index("Microphones") < text.index("Microphone Array") < text.index("Speakers:")


def test_devices_reports_audio_errors(ctx, monkeypatch) -> None:
    def broken():
        raise AudioError("No audio devices found.")

    monkeypatch.setattr(cli_voice, "list_devices", broken)
    out = io.StringIO()
    assert cli_voice.run_devices(ctx, out) == 1 and "No audio devices" in out.getvalue()


class FakeTTS:
    def __init__(self, available=True, problem=None) -> None:
        self.available, self.problem, self.spoken, self.closed = available, problem, [], False
        self.engine = SimpleNamespace(description="Windows voice (Zira)")

    def wait_ready(self, timeout):
        return True

    def speak(self, text):
        self.spoken.append(text)

    def wait_until_idle(self, timeout):
        return True

    def close(self):
        self.closed = True


def test_say_speaks_and_closes(ctx, monkeypatch) -> None:
    tts = FakeTTS()
    monkeypatch.setattr(cli_voice, "create_tts", lambda c: tts)
    out = io.StringIO()
    assert cli_voice.run_say(ctx, "Hello there", out) == 0
    assert tts.spoken == ["Hello there"] and tts.closed and "Windows voice (Zira)" in out.getvalue()
    broken = FakeTTS(available=False, problem="No voice is installed.")
    monkeypatch.setattr(cli_voice, "create_tts", lambda c: broken)
    out = io.StringIO()
    assert cli_voice.run_say(ctx, "x", out) == 1 and "No voice" in out.getvalue() and broken.closed


class FakeMic:
    peak = -12.0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def level_test(self, seconds):
        report = SimpleNamespace(peak_db=self.peak, average_db=self.peak - 10, verdict="Good level.")
        return report, SimpleNamespace(audio=np.zeros(16000, dtype=np.float32))


def test_mic_test_records_and_transcribes(ctx, monkeypatch) -> None:
    monkeypatch.setattr(cli_voice, "Microphone", FakeMic)
    stt = SimpleNamespace(transcribe=lambda audio, lang: SimpleNamespace(text="open chrome", confidence=0.91,
                                                                         elapsed_ms=420))
    monkeypatch.setattr(cli_voice, "create_stt", lambda c: stt)
    out = io.StringIO()
    assert cli_voice.run_mic_test(ctx, out, seconds=0.1) == 0
    assert 'Heard: "open chrome"' in out.getvalue() and "Peak -12.0 dBFS" in out.getvalue()


def test_mic_test_silence_stops_before_transcribing(ctx, monkeypatch) -> None:
    class Silent(FakeMic):
        peak = -80.0

    monkeypatch.setattr(cli_voice, "Microphone", Silent)
    monkeypatch.setattr(cli_voice, "create_stt", lambda c: pytest.fail("must not load the model"))
    assert cli_voice.run_mic_test(ctx, io.StringIO(), seconds=0.1) == 1


def test_setup_voice_refuses_offline(tmp_path: Path) -> None:
    ctx = bootstrap(tmp_path, environ={"OFFLINE_MODE": "true"})
    out = io.StringIO()
    assert cli_voice.run_setup_voice(ctx, out) == 1 and "Offline mode" in out.getvalue()


def test_recognition_hints_include_configured_apps(ctx) -> None:
    words = cli_voice.vocabulary(ctx)
    assert "WhatsApp" in words and any(w.lower().startswith("visual studio code") or w == "VS Code" for w in words)
    hints = cli_voice.recognition_hints("hey jarvis", "Jarvis", words)
    assert hints.startswith("Hey Jarvis, Jarvis") and len(hints.split()) <= cli_voice.MAX_HINT_WORDS
