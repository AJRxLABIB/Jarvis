"""Talking over Jarvis: the echo gate tells your voice from its own, speech is held and then resumed or dropped,
and what you said is heard and answered."""

from __future__ import annotations

import threading
import time

import numpy as np

from app.config.settings import Settings
from app.voice.echo_gate import EchoGate
from tests.test_tts import FakeEngine, manager


# ------------------------------------------------------------------ echo gate
def test_its_own_echo_never_counts_as_you() -> None:  # noqa: D103
    gate = EchoGate()
    rng = np.random.default_rng(1)
    for _ in range(400):                                 # 32 s of Jarvis talking; the mic hears it ~6 dB quieter
        playing = float(rng.uniform(-30, -12))
        assert gate.feed(playing - 6 + float(rng.normal(0, 1.5)), playing, speech=True) is False
    assert -9 < gate.coupling < 0


def test_your_voice_over_it_is_noticed_within_a_third_of_a_second() -> None:
    gate = EchoGate()
    for _ in range(60):
        gate.feed(-26.0, -20.0, speech=True)              # learn: echo is 6 dB under the playback
    hits = [gate.feed(-8.0, -20.0, speech=True) for _ in range(4)]   # you: far louder than the echo explains
    assert hits == [False, False, False, True]


def test_the_recording_case_quiet_gaps_must_not_make_its_own_words_look_like_you() -> None:
    """From the user's log: between words the mic is at -95 dB, its own words come back at about -40 dB while it plays
    at -20 dB. The old gate learned only from the gaps (coupling about -60 dB) and took its own words for the user."""
    gate = EchoGate()
    rng = np.random.default_rng(4)
    triggered = 0
    for i in range(500):
        word = (i // 3) % 2 == 0
        mic = float(rng.normal(-40, 2)) if word else float(rng.normal(-95, 2))
        triggered += gate.feed(mic, -20.0, speech=word)
    assert triggered == 0 and gate.coupling > -30
    assert [gate.feed(-24.0, -20.0, True) for _ in range(4)][-1] is True       # you, at -24 dB: still heard


def test_between_sentences_any_clear_speech_counts_and_noise_does_not() -> None:
    gate = EchoGate()
    assert [gate.feed(-30.0, -100.0, speech=True) for _ in range(4)][-1] is True
    gate.reset()
    assert not any(gate.feed(-30.0, -100.0, speech=False) for _ in range(10))   # loud but not speech (a fan)
    assert not any(gate.feed(-60.0, -100.0, speech=True) for _ in range(10))    # speech-like but far away


def test_a_cough_is_too_short_and_a_false_alarm_makes_it_more_careful() -> None:
    gate = EchoGate()
    for _ in range(40):
        gate.feed(-26.0, -20.0, speech=True)
    assert not any(gate.feed(-5.0, -20.0, True) for _ in range(3))
    assert gate.feed(-26.0, -20.0, True) is False and gate.run == 0       # broken off: the count starts again
    gate.false_alarm()
    assert gate.extra_margin == 4.0
    assert not any(gate.feed(-14.0, -20.0, True) for _ in range(6))      # 12 dB over the echo is no longer enough


def test_it_does_not_trust_itself_until_it_has_heard_itself() -> None:
    gate = EchoGate()
    assert not any(gate.feed(-10.0, -20.0, True) for _ in range(EchoGate.WARMUP_FRAMES))


# ------------------------------------------------------------------ hold / resume / drop
class SlowPlayer:
    """Plays in real time, 50 ms per piece, like the sound-card player."""

    def __init__(self) -> None:
        self.started: list[int] = []

    def play(self, clip, *, volume, abort: threading.Event, on_chunk=None) -> bool:
        self.started.append(clip.samples.size)
        pieces = max(1, int(clip.samples.size / (clip.sample_rate * 0.05)))
        for _ in range(pieces):
            if abort.is_set():
                return False
            if on_chunk is not None:
                on_chunk(0.1)
            time.sleep(0.05)
        return True


class LongEngine(FakeEngine):
    def synthesize(self, text: str, speed: float = 1.0):
        from app.tts.base import AudioClip

        self.spoken.append(text)
        return AudioClip(np.full(22050 * 3, 0.1, dtype=np.float32), 22050)     # 3 s per sentence


def _speaking(tts, n: int = 3) -> FakeEngine:
    for i in range(n):
        tts.speak(f"Sentence number {i + 1}.")
    end = time.monotonic() + 2
    while time.monotonic() < end and not tts.current_text:
        time.sleep(0.01)
    return tts.engine


def test_hold_stops_at_once_keeps_the_rest_and_resume_carries_on() -> None:
    player = SlowPlayer()
    tts = manager(LongEngine(), player=player)
    try:
        _speaking(tts)
        time.sleep(1.0)                                   # a third of the first sentence has been heard
        assert tts.playback_db() > -25                    # it knows how loud it is playing
        assert tts.hold() is True and tts.holding
        assert tts._held_tail is not None and tts._held_tail[0] == "Sentence number 1."   # the rest of the cut sentence
        assert tts.held_text.startswith("Sentence number 2.")         # and everything after it
        assert tts.is_speaking                            # still "speaking": nothing else starts listening/answering
        tts.speak("Streamed while you talked.")          # the answer keeps arriving: held, not spoken over you
        assert "Streamed while you talked." in tts.held_text
        time.sleep(0.6)
        assert tts.playback_db() < -60                    # and the sound really stopped
        tts.resume_held()
        assert not tts.holding
        end = time.monotonic() + 2
        while time.monotonic() < end and not tts.current_text:
            time.sleep(0.01)
        assert tts.current_text == "Sentence number 1."   # carries on with the cut sentence ...
        assert player.started[-1] < 22050 * 2.4                        # ... from where it stopped, not from the start
    finally:
        tts.stop()
        tts.close()


def test_discard_drops_the_rest_and_goes_idle() -> None:
    tts = manager(player=SlowPlayer())
    try:
        _speaking(tts)
        assert tts.hold()
        tts.stop()
        tts.discard_held()
        assert tts.wait_until_idle(3) and not tts.holding and tts.held_text == ""
    finally:
        tts.close()


def test_hold_does_nothing_when_it_is_not_speaking() -> None:
    tts = manager()
    try:
        assert tts.hold() is False and not tts.holding
    finally:
        tts.close()


# ------------------------------------------------------------------ the listener's side
def _listener(text: str, confidence: float = 0.9, playing: float = -20.0):
    from app.voice.hands_free import HandsFreeListener
    from app.voice.microphone import Microphone
    from tests.fake_audio import FakeSoundDevice
    from tests.test_voice_pipeline import FakeSTT

    calls = {"hold": 0, "release": [], "barge": 0, "results": []}

    def hold():
        calls["hold"] += 1
        return True

    listener = HandsFreeListener(
        Settings(hands_free=True), Microphone(sd=FakeSoundDevice(np.zeros(16000, np.float32))), FakeSTT(text, confidence),
        detector_factory=None, on_result=calls["results"].append, can_duplex=lambda: True,
        on_barge_in=lambda: calls.__setitem__("barge", calls["barge"] + 1) or True,
        recent_speech=lambda: "The weather in Dhaka is thirty one degrees and sunny.",
        playback_level=lambda: playing, on_hold=hold, on_release=calls["release"].append)
    for frame in _frames(0.02, 30):                       # it has heard its own (quiet) echo for a while already
        listener._talk_over_onset(frame, True)
    return listener, calls


def _frames(level: float, n: int) -> list[np.ndarray]:
    rng = np.random.default_rng(2)
    return [(rng.standard_normal(1280) * level).astype(np.float32) for _ in range(n)]


def _feed(listener, frames, speech: bool) -> None:
    for f in frames:
        if listener._interrupt is not None:
            listener._interrupt_frame(f, speech)
        else:
            listener._talk_over_onset(f, speech)


def test_talking_over_it_stops_the_speech_and_your_words_are_answered() -> None:
    listener, calls = _listener("what time is it")
    _feed(listener, _frames(0.02, 40), speech=True)       # its own echo (quiet): ignored, coupling learned
    assert calls["hold"] == 0
    _feed(listener, _frames(0.4, 4), speech=True)          # you, loud and clear
    assert calls["hold"] == 1 and listener._interrupt is not None
    _feed(listener, _frames(0.4, 10), speech=True)
    _feed(listener, _frames(0.0, 12), speech=False)        # you stop: the utterance ends
    assert calls["release"] == [False] and calls["barge"] == 1
    assert [r.text for r in calls["results"]] == ["what time is it"]


def test_if_it_was_its_own_voice_after_all_it_carries_on() -> None:
    listener, calls = _listener("the weather in Dhaka is thirty one degrees")    # echo text
    _feed(listener, _frames(0.4, 14), speech=True)
    _feed(listener, _frames(0.0, 12), speech=False)
    assert calls["hold"] == 1 and calls["release"] == [True] and calls["barge"] == 0 and calls["results"] == []


def test_stop_just_makes_it_quiet_and_listen() -> None:
    listener, calls = _listener("stop")
    _feed(listener, _frames(0.4, 10), speech=True)
    _feed(listener, _frames(0.0, 12), speech=False)
    assert calls["release"] == [False] and calls["results"] == [] and listener.state in ("follow_up", "command")


# ------------------------------------------------------------------ the voice finish
def test_natural_finish_has_no_echo_copies_and_even_loudness() -> None:
    from app.tts.jarvis_voice import TARGET_RMS_DB, jarvis_effect, natural_finish

    rate = 24000
    t = np.arange(rate) / rate
    voice = (0.6 * np.sin(2 * np.pi * 180 * t) * (0.2 + 0.8 * (t > 0.5))).astype(np.float32)   # a quiet then loud word
    out = natural_finish(voice, rate)
    assert out.size == voice.size                                   # nothing appended (the classic one adds a tail)
    assert jarvis_effect(voice, rate).size > voice.size
    rms = 20 * np.log10(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
    assert abs(rms - TARGET_RMS_DB) < 1.5 and np.max(np.abs(out)) <= 0.9 + 1e-6
    quiet, loud = np.abs(out[:rate // 2]).max(), np.abs(out[rate // 2:]).max()
    assert loud / quiet < (0.6 / 0.12) * 0.8                        # compression brought the two closer together
    assert abs(out[0]) < 1e-6 and abs(out[-1]) < 1e-3               # soft edges: no clicks
    # no comb filter: the signal is not correlated with itself 6-53 ms later beyond what the tone itself gives
    dry = natural_finish(np.random.default_rng(3).standard_normal(rate).astype(np.float32) * 0.1, rate)
    for ms in (6, 11, 23, 37):
        lag = int(rate * ms / 1000)
        assert abs(np.corrcoef(dry[:-lag], dry[lag:])[0, 1]) < 0.05


def test_natural_prosody_is_gentle_and_classic_is_kept() -> None:
    from app.config.settings import Settings
    from app.tts.neural import prosody

    assert prosody("Sorry, that didn't work.", gentle=True) == ("-3%", "-1Hz")
    assert prosody("Sorry, that didn't work.") == ("-6%", "-3Hz")
    assert Settings().tts_style == "natural" and Settings(tts_style="classic").tts_style == "classic"


# ------------------------------------------------------------------ faster replies
def test_the_first_words_are_spoken_before_the_first_sentence_is_finished() -> None:
    from app.tts.text import SentenceSplitter

    quick = SentenceSplitter(quick_start=True)
    pieces = []
    for word in "Certainly, Sir, I have opened Chrome for you and the calendar shows three meetings today. Next.".split(" "):
        pieces += quick.feed(word + " ")
    assert pieces[0] == "Certainly, Sir,"                                        # spoken while the rest is written
    assert pieces[1].endswith("today.") and pieces[1].startswith("I have")       # then whole sentences as before
    slow = SentenceSplitter()
    assert slow.feed("Certainly, Sir, I have opened Chrome for you and") == []    # the old splitter waited
    long = SentenceSplitter(quick_start=True)
    out = long.feed("word " * 25)                                               # no punctuation for a long time
    assert out and len(out[0]) < SentenceSplitter.FIRST_FORCE_CHARS and long.feed("x") == []
    short = SentenceSplitter(quick_start=True)
    assert short.feed("Done.") == [] and short.flush() == "Done."              # short replies are unchanged


def test_repeated_short_replies_are_cached_and_prewarm_makes_them_ahead() -> None:
    engine = FakeEngine()
    tts = manager(engine)
    try:
        tts.prewarm(["Yes, Sir! How can I help you?"])
        end = time.monotonic() + 3
        while time.monotonic() < end and not engine.spoken:
            time.sleep(0.01)
        assert engine.spoken == ["Yes, Sir! How can I help you?"]               # made once, in advance
        tts.speak("Yes, Sir! How can I help you?")
        tts.speak("Yes, Sir! How can I help you?")
        assert tts.wait_until_idle(3)
        assert engine.spoken == ["Yes, Sir! How can I help you?"]               # played twice, synthesised once
        long = "x" * 120 + "."
        tts.speak(long)
        tts.speak(long)
        assert tts.wait_until_idle(3) and engine.spoken.count(long) == 2         # long answers are never cached
    finally:
        tts.close()


def test_the_end_of_sentence_wait_is_shorter() -> None:
    assert Settings().silence_timeout == 0.7


# ------------------------------------------------------------------ the recording's other cases
def test_garbled_echo_of_its_own_words_is_recognised() -> None:
    from app.agent.language import is_echo

    said = "Good catch, Sir. I'm sorry, I did repeat myself. That was a mistake on my part and I stopped."
    assert is_echo("Good Cat.", said) and is_echo("and Stopped.", said)
    assert not is_echo("What time is it?", said) and not is_echo("open chrome", said)


def test_a_video_playing_means_only_jarvis_comma_counts() -> None:
    class Playing:
        playing = True

    listener, calls = _listener("what time is it")
    listener.other_audio = Playing()
    _feed(listener, _frames(0.4, 14), speech=True)                    # loud voices from the video
    assert calls["hold"] == 0                                          # it doesn't even pause for them
    assert match_wake("Jarvis, what time is it")


def match_wake(text: str) -> bool:
    from app.voice.wake_word import strip_wake_phrase

    return strip_wake_phrase(text, "hey jarvis")[1]


def test_one_unclear_word_over_its_voice_is_not_a_command() -> None:
    listener, calls = _listener("Okay then", confidence=0.4)
    _feed(listener, _frames(0.4, 10), speech=True)
    _feed(listener, _frames(0.0, 12), speech=False)
    assert calls["release"] == [True] and calls["results"] == [] and listener._gate.extra_margin > 0
