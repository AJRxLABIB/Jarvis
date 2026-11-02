"""Hardware validation harness - tested with FAKE hardware. These tests prove the checks decide correctly and never report a pass for
something that could not be checked; they do NOT prove that any real PC, microphone or phone passes."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.validation import phone_checks as P
from app.validation import voice_checks as V
from app.validation import windows_checks as W
from app.validation.report import FAIL, PASS, PENDING, SKIP, WARN, Report, Result, last_reports, save, status_text, timed


# ------------------------------------------------------------------ report


def test_a_crashing_check_is_a_fail_and_pending_is_never_a_pass():
    r = timed("x", lambda: 1 / 0)
    assert r.status == FAIL and "ZeroDivisionError" in r.detail
    rep = Report("windows")
    rep.add(Result("a", PASS, "ok"))
    assert rep.verdict == "PASSED"
    rep.add(Result("b", PENDING, "needs hardware"))
    assert rep.verdict == "INCOMPLETE"
    rep.add(Result("c", FAIL, "bad"))
    assert rep.verdict == "FAILED"
    assert Report("x").verdict == "INCOMPLETE"


def test_ledger_round_trip_and_never_run_message(tmp_path):
    assert "NEVER RUN" in status_text(tmp_path)
    rep = Report("voice")
    rep.add(Result("a", PASS, "ok"))
    save(tmp_path, rep)
    assert last_reports(tmp_path)["voice"]["verdict"] == "PASSED"
    assert "voice: PASSED" in status_text(tmp_path) and "windows: NEVER RUN" in status_text(tmp_path)


# ------------------------------------------------------------------ windows


class FakeWin(W.Env):
    def __init__(self, **kw):
        self.windows = kw.get("windows", True)
        self.awareness = kw.get("awareness", "per-monitor")
        self.mons = kw.get("mons", [W.MonitorInfo(1, 0, 0, 1920, 1080, True, 1.0, (0, 0, 1920, 1040))])
        self.cap = kw.get("cap")            # override capture size
        self.rect = kw.get("rects", W.Rects((100, 100, 900, 700), (100, 100, 900, 700), (100, 100, 900, 700)))
        self.uia = kw.get("uia", (60, 120, ["File", "Edit"]))
        self.focused = kw.get("focus", True)
        self.fg = kw.get("fg", 7)
        self.placed = kw.get("place_ok", True)
        self.closed = []
        self.moved = None

    def is_windows(self): return self.windows
    def dpi_awareness(self): return self.awareness
    def monitors(self): return self.mons
    def capture_size(self, m): return self.cap or (m.width, m.height)
    def open_notepad(self): return 7
    def rects(self, hwnd):
        if self.moved is not None and self.placed:
            m = self.moved
            v = (m.left + 40, m.top + 40, m.left + 400, m.top + 300)
            return W.Rects(v, v, v)
        return self.rect
    def uia_tree(self, hwnd):
        if isinstance(self.uia, Exception):
            raise self.uia
        return self.uia
    def focus(self, hwnd): return self.focused
    def foreground(self): return self.fg
    def move_to(self, hwnd, m): self.moved = m
    def close(self, hwnd): self.closed.append(hwnd)


def statuses(rep):
    return {r.name: r.status for r in rep.results}


def test_not_windows_is_pending_not_pass():
    rep = W.run(FakeWin(windows=False))
    assert rep.verdict == "INCOMPLETE" and statuses(rep) == {"platform": PENDING}


def test_without_launch_window_checks_are_pending():
    rep = W.run(FakeWin())
    s = statuses(rep)
    assert s["DPI awareness"] == PASS and s["window focus"] == PENDING and rep.verdict == "INCOMPLETE"


def test_full_pass_and_cleanup():
    env = FakeWin(mons=[W.MonitorInfo(1, 0, 0, 1920, 1080, True, 1.0, (0, 0, 1920, 1040)),
                        W.MonitorInfo(2, 1920, 0, 2560, 1440, False, 1.5, (1920, 0, 4480, 1400))])
    rep = W.run(env, launch=True)
    s = statuses(rep)
    assert s["window rectangles agree"] == PASS and s["UI Automation reads a real window"] == PASS and s["window focus"] == PASS
    assert s["window placement on monitor #2"] == PASS and env.closed == [7]
    assert s["monitors and scaling"] == WARN          # mixed scaling is called out
    assert rep.verdict == "PASSED WITH WARNINGS"


def test_dpi_mismatch_is_detected():
    bad = W.Rects((100, 100, 900, 700), (150, 150, 1350, 1050), None)       # capture is scaled 1.5x
    rep = W.run(FakeWin(rects=bad), launch=True)
    s = statuses(rep)
    assert s["window rectangles agree"] == FAIL and "DPI" in next(r.detail for r in rep.results if r.name == "window rectangles agree")


def test_capture_size_mismatch_and_unaware_process():
    rep = W.run(FakeWin(cap=(1280, 720), awareness="unaware"))
    s = statuses(rep)
    assert s["screenshots match the real screen size"] == FAIL and s["DPI awareness"] == WARN


def test_uia_unavailable_few_elements_slow():
    assert statuses(W.run(FakeWin(uia=RuntimeError("no comtypes")), launch=True))["UI Automation reads a real window"] == FAIL
    assert statuses(W.run(FakeWin(uia=(3, 10, [])), launch=True))["UI Automation reads a real window"] == FAIL
    assert statuses(W.run(FakeWin(uia=(50, 9000, ["File"])), launch=True))["UI Automation reads a real window"] == WARN


def test_focus_blocked_and_placement_failure():
    s = statuses(W.run(FakeWin(focus=False), launch=True))
    assert s["window focus"] == FAIL
    far = (5000, 5000, 5400, 5300)
    s = statuses(W.run(FakeWin(place_ok=False, rects=W.Rects(far, far, far)), launch=True))
    assert s["window placement on monitor #1"] == FAIL


def test_notepad_never_found_skips_and_still_runs_cleanly():
    env = FakeWin()
    env.open_notepad = lambda: 0
    rep = W.run(env, launch=True)
    assert statuses(rep)["window focus"] == SKIP


# ------------------------------------------------------------------ voice analysis


def test_vad_reaction_and_tail_from_synthetic_frames():
    frames = V.synth_frames(1.0, 3.0, 5.0, vad_delay=0.10, vad_tail=0.30)
    r = V.vad_reaction(frames)
    assert 60 <= r["onset_ms"] <= 200 and 250 <= r["tail_ms"] <= 400
    assert V.judge_onset(r["onset_ms"])[0] == PASS
    assert V.judge_onset(400)[0] == WARN and V.judge_onset(900)[0] == FAIL and V.judge_onset(None)[0] == SKIP


def test_no_speech_gives_skip_not_pass():
    quiet = V.synth_frames(10, 11, 3.0)
    assert V.vad_reaction(quiet) == {"onset_ms": None, "tail_ms": None}


def test_end_of_speech_judgement():
    assert V.judge_end_of_speech(0.8, 0.7)[0] == PASS
    assert V.judge_end_of_speech(1.3, 0.7)[0] == WARN
    assert V.judge_end_of_speech(2.5, 0.7)[0] == FAIL
    assert V.judge_end_of_speech(0.45, 0.3)[0] == WARN          # a very short timeout cuts sentences
    assert V.judge_end_of_speech(None, 0.7)[0] == SKIP


def test_speaker_judgements_and_self_hearing():
    assert V.judge_stop(120)[0] == PASS and V.judge_stop(500)[0] == WARN and V.judge_stop(1500)[0] == FAIL and V.judge_stop(None)[0] == PENDING
    assert V.judge_first_audio(800)[0] == PASS and V.judge_first_audio(5000)[0] == FAIL
    quiet = [V.Frame(i * 0.032, -70, False) for i in range(100)]
    echo = [V.Frame(i * 0.032, -30, i % 2 == 0) for i in range(100)]
    assert V.judge_self_hearing(quiet)[0] == PASS and V.judge_self_hearing(echo)[0] == FAIL and V.judge_self_hearing([])[0] == PENDING


class FakeVoice(V.VoiceEnv):
    def __init__(self, mic=True, tts=True, latency=0.9):
        self.mic, self.tts, self.latency = mic, tts, latency

    def has_microphone(self): return self.mic
    def say(self, text): pass
    def capture_frames(self, seconds): return V.synth_frames(1.0, 3.0, 5.0)
    def record_end_of_speech(self): return self.latency, 0.7
    def tts_available(self): return self.tts
    def tts_first_audio_and_stop(self): return 600.0, 150.0
    def frames_while_speaking(self, seconds): return [V.Frame(i * 0.032, -70, False) for i in range(50)]


def test_voice_run_pass_and_pending_paths(tmp_path):
    rep = V.run(FakeVoice(), None, say=lambda *_: None)
    s = statuses(rep)
    assert s["end-of-speech latency"] == PASS and s["speaker stop latency"] == PASS and s["does Jarvis hear itself?"] == PASS
    assert V.run(FakeVoice(mic=False), None, say=lambda *_: None).verdict == "INCOMPLETE"
    s = statuses(V.run(FakeVoice(tts=False), None, say=lambda *_: None))
    assert s["speaker stop latency"] == PENDING


# ------------------------------------------------------------------ Bangla accuracy by language (benchmark)


def test_error_rates_and_language_split():
    from app import benchmark as B

    assert B.error_rates("ক্রোম খোলো", "ক্রোম খোলো") == (0.0, 0.0)
    wer, cer = B.error_rates("open chrome now", "open chrome")
    assert wer == pytest.approx(1 / 3) and 0 < cer < 0.4
    assert B.error_rates("a", "") == (1.0, 1.0)
    assert B.language_of("ক্রোম খোলো") == "bn" and B.language_of("Open Chrome আর YouTube খোলো") == "mixed" and B.language_of("Volume up") == "en"


def test_language_verdict_never_claims_without_enough_data():
    from app import benchmark as B

    assert "NO CLAIM" in B.language_verdict(3, 0.0, 100.0)
    assert B.language_verdict(12, 0.05, 95.0).startswith("reliable")
    assert B.language_verdict(12, 0.2, 80.0).startswith("usable")
    assert B.language_verdict(12, 0.5, 30.0).startswith("NOT reliable")
    assert B.language_verdict(12, 0.05, 50.0).startswith("usable") or B.language_verdict(12, 0.05, 50.0).startswith("NOT")


def test_benchmark_run_reports_per_language_and_recognition_only_items(tmp_path):
    import numpy as np

    from app import benchmark as B

    folder = B.folder(tmp_path)
    folder.mkdir(parents=True)
    items = [{"file": "01.wav", "say": "Volume up", "tool": "volume_control", "contains": "up"},
             {"file": "02.wav", "say": "আজ ছাব্বিশে অক্টোবর", "tool": "", "contains": ""}]
    (folder / "index.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    tone = (0.3 * np.sin(np.linspace(0, 400, 16000))).astype(np.float32)
    for name in ("01.wav", "02.wav"):
        B.save_wav(folder / name, tone)

    heard = iter(["volume up", "আজ ছাব্বিশে অক্টোবর"])

    class T:
        def __init__(self, text): self.text = text

    out = []
    result = B.run(tmp_path, lambda audio: T(next(heard)), lambda name: None, out=out.append)
    assert result["commands"] == 1 and result["by_language"]["bn"]["cer"] == 0.0 and "en" in result["by_language"]
    assert any("NO CLAIM" in line for line in out)           # 1 recording per language is never a claim


def test_new_prompts_are_recognition_only_and_old_numbering_is_kept():
    from app import benchmark as B

    assert B.PROMPTS[21][0] == "ব্যাটারি কত আছে?" and all(p[1] == "" for p in B.PROMPTS[22:])
    assert {B.language_of(p[0]) for p in B.PROMPTS[22:]} == {"bn", "mixed", "en"}


def test_voice_benchmark_summary_uses_saved_results(tmp_path):
    folder = tmp_path / "benchmark"
    folder.mkdir()
    assert V.benchmark_summary(tmp_path)[0].status == PENDING
    (folder / "history.jsonl").write_text(json.dumps({"by_language": {"bn": {"items": 12, "wer": 10.0, "cer": 4.0, "command_accuracy": 95.0,
                                                                              "verdict": "reliable on your voice and microphone (small sample)"},
                                                                       "mixed": {"items": 3, "wer": 40.0, "cer": 20.0, "command_accuracy": None,
                                                                                 "verdict": "not enough recordings to say anything - NO CLAIM"}}}) + "\n")
    got = {r.name: r.status for r in V.benchmark_summary(tmp_path)}
    assert got["Bangla recognition (from --benchmark)"] == PASS and got["mixed Bangla-English recognition (from --benchmark)"] == PENDING


# ------------------------------------------------------------------ phone


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class FakePhone(P.PhoneEnv):
    def __init__(self, phones=True, age=30, control=True, version="1.4.0", outcome="done", confirm=True, secs=2.0, checkin=10.0):
        self._phones = [{"id": "p1", "name": "Pixel"}] if phones else []
        self.age, self.control, self.version, self.outcome, self.answer, self.secs, self.checkin = age, control, version, outcome, confirm, secs, checkin
        self.sent = []

    def phones(self): return self._phones
    def state(self, i):
        return {"seen": (NOW - timedelta(seconds=self.age)).isoformat(), "control": self.control, "app_version": self.version, "battery": 80} if self.age is not None else {}
    def now(self): return NOW
    def act(self, i, action, args, seconds):
        self.sent.append(action)
        return P.Outcome(self.outcome, "ok", self.secs)
    def confirm(self, q): return self.answer
    def expected_version(self): return "1.4.0"
    def wait_checkin(self, i, since, timeout): return self.checkin


def test_no_phone_is_pending_not_pass():
    rep = P.run(FakePhone(phones=False))
    assert rep.verdict == "INCOMPLETE" and statuses(rep) == {"a trusted phone is paired": PENDING}


def test_phone_happy_path_asks_you_and_lists_manual_items():
    env = FakePhone()
    rep = P.run(env, full=True)
    s = statuses(rep)
    assert s["show a message on the phone"] == PASS and s["flashlight on"] == PASS and s["reconnect after losing the network"] == PASS
    assert env.sent == ["show_text", "flashlight_on", "flashlight_off", "ring"]
    assert sum(r.status == PENDING for r in rep.results) == len(P.MANUAL_CHECKLIST) and rep.verdict == "INCOMPLETE"


def test_a_false_success_report_is_a_fail():
    rep = P.run(FakePhone(confirm=False))
    r = next(x for x in rep.results if x.name == "flashlight on")
    assert r.status == FAIL and "false success report" in r.detail
    assert next(x for x in P.run(FakePhone(confirm=None)).results if x.name == "flashlight on").status == PENDING


def test_stale_wrong_version_control_off_and_failures():
    assert statuses(P.run(FakePhone(age=4000)))["recent check-in"] == FAIL
    assert statuses(P.run(FakePhone(age=900)))["recent check-in"] == WARN
    assert statuses(P.run(FakePhone(version="1.3.0")))["app version"] == WARN
    off = P.run(FakePhone(control=False))
    assert statuses(off)["control from the PC is allowed"] == FAIL and statuses(off)["flashlight on"] == SKIP
    assert statuses(P.run(FakePhone(outcome="expired")))["flashlight on"] == FAIL
    assert statuses(P.run(FakePhone(outcome="unsupported")))["flashlight on"] == WARN
    assert statuses(P.run(FakePhone(secs=30)))["flashlight on"] == WARN
    assert statuses(P.run(FakePhone(age=None)))["recent check-in"] == FAIL
    assert statuses(P.run(FakePhone(checkin=None), full=True))["reconnect after losing the network"] == FAIL
    assert statuses(P.run(FakePhone(checkin=90), full=True))["reconnect after losing the network"] == WARN


def test_nothing_risky_is_ever_sent():
    env = FakePhone()
    P.run(env, full=True)
    assert not set(env.sent) & {"call", "send_sms", "lock_phone", "read_messages", "read_screen", "tap_text", "end_call"}
