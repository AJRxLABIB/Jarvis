"""SPEECH_DATASET: your corrected voice commands kept (opt-in) for training; Bangla recognition hints."""

import csv
import wave
from types import SimpleNamespace

import numpy as np

from app.voice.speech_dataset import MAX_CLIPS, SpeechDataset, bangla_hints, is_bangla


def tone(seconds=1.0):
    t = np.arange(int(16000 * seconds)) / 16000
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_bangla_detection_and_hints():
    assert is_bangla("ফোন লিংক খোলো") and not is_bangla("open Phone Link") and not is_bangla("123")
    assert bangla_hints(["Phone Link", "হোয়াটসঅ্যাপ", "হোয়াটসঅ্যাপ", "ইউটিউব"]) == "হোয়াটসঅ্যাপ, ইউটিউব"
    assert bangla_hints(["Chrome"]) is None


def test_record_export_forget(tmp_path):
    data = SpeechDataset(tmp_path)
    assert data.record(tone(), "ফোন লিংক খোলো", "bn", "corrected") is not None
    assert data.record(tone(), "খোলো", "bn", "unverified") is not None
    assert data.record(tone(0.1), "too short", "bn", "corrected") is None
    assert data.record(tone(25), "too long", "bn", "corrected") is None
    assert data.record(tone(), "", "bn", "corrected") is None
    clip = tmp_path / "speech_dataset" / "bn" / data.entries("bn")[0]["file"]
    with wave.open(str(clip)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getnframes() == 16000
    assert data.summary()["bn"] == {"clips": 2, "corrected": 1, "minutes": 0.0}
    out = tmp_path / "export"
    assert data.export("bn", out) == 1                                   # only corrected clips by default
    rows = list(csv.DictReader((out / "metadata.csv").open(encoding="utf-8")))
    assert rows[0]["transcription"] == "ফোন লিংক খোলো" and (out / rows[0]["file_name"]).is_file()
    assert data.export("bn", tmp_path / "all", include_unverified=True) == 2
    assert data.forget() == 2 and data.summary() == {}


def test_language_folder_is_sanitised(tmp_path):
    data = SpeechDataset(tmp_path)
    data.record(tone(), "x y", "../../evil", "corrected")
    assert (tmp_path / "speech_dataset" / "evil").is_dir() and not (tmp_path.parent / "evil").exists()


def test_limit(tmp_path, monkeypatch):
    import app.voice.speech_dataset as sd

    monkeypatch.setattr(sd, "MAX_CLIPS", 2)
    data = SpeechDataset(tmp_path)
    for _ in range(3):
        data.record(tone(), "hello", "en", "corrected")
    assert data.count("en") == 2 and MAX_CLIPS == 5000


def _host(tmp_path, mode):
    from app.ui.controller_parts.features import FeaturesMixin

    host = FeaturesMixin.__new__(FeaturesMixin)
    host.ctx = SimpleNamespace(settings=SimpleNamespace(speech_dataset=mode), paths=SimpleNamespace(data_dir=tmp_path))
    return host


def test_correction_keeps_the_misheard_audio_with_the_right_text(tmp_path):
    host = _host(tmp_path, "corrections")
    heard = SimpleNamespace(text="close phone lane", language="en", confidence=0.6)
    host._keep_voice(tone(), heard)
    assert SpeechDataset(tmp_path).summary() == {}                      # nothing kept until you correct it
    host._on_correction("close phone lane", "close Phone Link")
    entries = SpeechDataset(tmp_path).entries("en")
    assert [(e["text"], e["source"]) for e in entries] == [("close Phone Link", "corrected")]
    host._on_correction("close phone lane", "close Phone Link")          # once only
    assert len(SpeechDataset(tmp_path).entries("en")) == 1


def test_bangla_correction_goes_to_the_bangla_folder_and_off_keeps_nothing(tmp_path):
    host = _host(tmp_path, "corrections")
    host._keep_voice(tone(), SimpleNamespace(text="ফোন লেন খোলো", language="en", confidence=0.5))
    host._on_correction("ফোন লেন খোলো", "ফোন লিংক খোলো")
    assert SpeechDataset(tmp_path).summary()["bn"]["corrected"] == 1
    off = _host(tmp_path / "off", "off")
    off._keep_voice(tone(), SimpleNamespace(text="hi there", language="en", confidence=0.9))
    off._on_correction("hi there", "hi their")
    assert SpeechDataset(tmp_path / "off").summary() == {}


def test_all_mode_keeps_confident_commands_as_unverified(tmp_path):
    host = _host(tmp_path, "all")
    host._keep_voice(tone(), SimpleNamespace(text="open chrome", language="en", confidence=0.9))
    host._keep_voice(tone(), SimpleNamespace(text="opn crom", language="en", confidence=0.4))
    assert [e["source"] for e in SpeechDataset(tmp_path).entries("en")] == ["unverified"]


def test_stt_uses_bangla_hints_only_for_bangla():
    from app.voice.speech_to_text import FasterWhisperSTT

    calls = []
    stt = FasterWhisperSTT.__new__(FasterWhisperSTT)
    stt.hotwords, stt.hotwords_bn, stt.beam_size = "Jarvis, Chrome", "ফোন লিংক", 5
    stt._model = SimpleNamespace(transcribe=lambda audio, **kw: calls.append(kw["hotwords"]))
    for language in ("en", "bn", "hi", None):
        stt._decode(np.zeros(10, np.float32), language)
    assert calls == ["Jarvis, Chrome", "ফোন লিংক", None, "Jarvis, Chrome"]


def test_voice_data_tool(tmp_path):
    from app.tools.speech_data_tool import VoiceDataArgs, voice_data

    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), settings=SimpleNamespace(speech_dataset="off"))
    assert "off" in voice_data(VoiceDataArgs(), ctx).message
    SpeechDataset(tmp_path).record(tone(), "ফোন", "bn", "corrected")
    assert "bn: 1 clips" in voice_data(VoiceDataArgs(), ctx).message
    assert voice_data(VoiceDataArgs(action="export", language="bn"), ctx).data["clips"] == 1
    assert "Erased 1" in voice_data(VoiceDataArgs(action="forget"), ctx).message


def test_conversation_reports_corrections_to_the_host(tmp_path):
    from app.agent.conversation import ConversationManager
    from app.voice.corrections import Corrections

    manager = ConversationManager.__new__(ConversationManager)
    manager._corrections_store = Corrections(tmp_path)
    manager._last_user_text = "close phone lane"
    seen = []
    manager.on_correction = lambda heard, corrected: seen.append((heard, corrected))
    text, note = manager._apply_corrections("no, I said Phone Link")
    assert text == "close Phone Link" and note and seen == [("close phone lane", "close Phone Link")]
