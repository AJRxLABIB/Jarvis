"""Hearing quality: anti-aliased resampling, seamless streaming, rumble removal, click-proof level."""
import numpy as np

from app.voice.audio import StreamResampler, remove_rumble, resample
from app.voice.speech_to_text import normalize_level


def tone(freq, rate, seconds=1.0, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def test_speech_band_kept_and_length_right():
    for rate in (44100, 48000, 32000, 22050):
        out = resample(tone(1000, rate), rate, 16000)
        assert abs(out.size - 16000) < 400
        assert 0.3 < rms(out[800:-800]) < 0.4   # 0.5/sqrt2 = 0.354


def test_aliasing_is_suppressed():
    # 12 kHz is above the new Nyquist (8 kHz): it must vanish, not fold into 4 kHz at full strength
    for rate in (44100, 48000):
        out = resample(tone(12000, rate), rate, 16000)
        assert rms(out[800:-800]) < 0.02


def test_streaming_matches_whole_clip_without_seams():
    rate = 44100
    x = tone(700, rate, 1.0)
    whole = StreamResampler(rate, 16000).process(x)   # one chunk
    r = StreamResampler(rate, 16000)
    parts = [r.process(x[i:i + 1323]) for i in range(0, x.size, 1323)]   # 30 ms blocks
    streamed = np.concatenate(parts)
    n = min(whole.size, streamed.size)
    assert abs(whole.size - streamed.size) < 5
    assert float(np.max(np.abs(whole[:n] - streamed[:n]))) < 1e-3


def test_same_rate_passthrough_and_empty():
    x = tone(500, 16000, 0.1)
    assert np.array_equal(resample(x, 16000, 16000), x)
    assert resample(np.zeros(0, np.float32), 48000, 16000).size == 0


def test_upsampling_length():
    out = resample(tone(300, 8000), 8000, 16000)
    assert abs(out.size - 16000) < 4


def test_rumble_removed_speech_kept():
    mixed = tone(30, 16000, 2.0, 0.4) + tone(300, 16000, 2.0, 0.2) + 0.1
    out = remove_rumble(mixed)
    assert abs(float(np.mean(out))) < 0.01
    assert rms(out[3000:-3000]) < 0.17           # 0.2/sqrt2 = 0.141 left, 0.28 rumble gone
    assert rms(out[3000:-3000]) > 0.12


def test_click_does_not_stop_quiet_speech_from_being_raised():
    quiet = tone(300, 16000, 2.0, 0.05)
    clicked = quiet.copy()
    clicked[8000] = 0.95
    assert float(np.max(np.abs(normalize_level(clicked)))) >= 0.5
    # no click: behaves as before
    assert float(np.max(np.abs(normalize_level(quiet)))) > 0.5
    loud = tone(300, 16000, 1.0, 0.8)
    assert np.array_equal(normalize_level(loud), loud)
