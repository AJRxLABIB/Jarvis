"""Jarvis 2.1: self-test, benchmark, watchdog, memory, screen agent, phone, habits, Bangla voice, remote."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config.settings import Settings
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError
from app.tts.base import AudioClip, TTSEngine

try:
    from tests.test_gui import qapp  # noqa: F401
except Exception:
    qapp = None


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


# ------------------------------------------------------------------ 1. self-test


class ToneEngine(TTSEngine):
    description = "test voice"

    def load(self):
        pass

    def synthesize(self, text, speed=1.0):
        return AudioClip(np.full(8000, 0.1, np.float32), 16000)


def test_selftest_reports_every_part() -> None:
    from app.selftest import Parts, run_selftest, summary

    class STT:
        device, description = "cpu", "small.en on CPU"

        def transcribe(self, audio, language=None):
            return SimpleNamespace(text="Open Google Chrome.", elapsed_ms=120)

    class AI:
        async def chat(self, messages, tools=()):
            return SimpleNamespace(model="qwen3:4b-instruct", message=SimpleNamespace(content="OK"))

        async def embed(self, texts):
            return [[0.1] * 768]

    seen = []
    results = run_selftest(Parts(Settings(), ai=AI(), stt=STT(), tts_engine=ToneEngine(), models_dir=Path("/nonexistent")),
                           use_microphone=False, on_result=seen.append)
    by_name = {r.name: r for r in results}
    assert by_name["AI model"].status == "pass" and "OK" in by_name["AI model"].detail
    assert by_name["Embeddings (memory, documents)"].detail.endswith("768 dimensions")
    assert by_name["Voice round trip (speech -> text -> command)"].status == "pass"
    assert by_name["Bangla commands"].status == "pass" and by_name["Microphone"].status == "skip"
    assert seen == results and "passed" in summary(results)


def test_selftest_turns_crashes_into_failures() -> None:
    from app.selftest import Parts, run_selftest

    class Broken:
        async def chat(self, *a, **k):
            raise RuntimeError("Ollama is not running")

        async def embed(self, texts):
            raise RuntimeError("no")

    results = run_selftest(Parts(Settings(), ai=Broken(), models_dir=Path("/x")), use_microphone=False)
    assert results[0].status == "fail" and "Ollama is not running" in results[0].detail


# ------------------------------------------------------------------ 2. benchmark


def test_benchmark_record_and_run(tmp_path: Path) -> None:
    from app import benchmark

    prompts = []
    speech = (0.2 * np.sin(np.arange(16000) / 3)).astype(np.float32)
    count = benchmark.record(tmp_path, lambda: speech, lambda say, n, t: prompts.append(say))
    assert count == len(benchmark.PROMPTS) and prompts[0] == "Open Google Chrome"
    heard = iter(p[0] for p in benchmark.PROMPTS)

    def transcribe(audio):
        return SimpleNamespace(text=next(heard))

    known = {"chrome": "Google Chrome", "google chrome": "Google Chrome", "phone link": "Phone Link",
             "notepad": "Notepad"}
    lines: list[str] = []
    result = benchmark.run(tmp_path, transcribe, lambda name: known.get(name.lower()), out=lines.append)
    assert result["items"] == len(benchmark.PROMPTS) and result["accuracy"] >= 90
    assert result["text_similarity"] == 100.0
    assert list((tmp_path / "benchmark").glob("report-*.txt"))
    # good recordings are kept; only bad ones are asked for again
    assert benchmark.record(tmp_path, lambda: speech, lambda *a: pytest.fail("re-recorded a good one")) == 0


def test_benchmark_asks_again_for_empty_recordings(tmp_path: Path) -> None:
    from app import benchmark

    takes = iter([np.zeros(0, np.float32), (0.2 * np.sin(np.arange(16000) / 3)).astype(np.float32)] * 40)
    retries: list[str] = []
    benchmark.record(tmp_path, lambda: next(takes), lambda *a: None, retry=retries.append)
    assert len(retries) == len(benchmark.PROMPTS)
    assert all(benchmark.audio_ok(benchmark.load_wav(p)) for p in (tmp_path / "benchmark").glob("*.wav"))


def test_silence_is_not_transcribed(tmp_path: Path) -> None:
    from app.voice.speech_to_text import FasterWhisperSTT

    stt = FasterWhisperSTT("base.en", download_root=tmp_path,
                           model_factory=lambda n, **k: pytest.fail("the model should not be needed"))
    stt._model = SimpleNamespace(transcribe=lambda *a, **k: pytest.fail("silence was sent to Whisper"))
    assert stt.transcribe(np.zeros(16000, np.float32)).is_empty


def test_benchmark_without_recordings(tmp_path: Path) -> None:
    from app import benchmark

    out: list[str] = []
    assert benchmark.run(tmp_path, lambda a: None, lambda n: None, out=out.append) == {}
    assert "record-benchmark" in out[0]


# ------------------------------------------------------------------ 3/4/7. watchdog, idle unload, suggestions


def test_watchdog_restarts_what_stopped(qapp, tmp_path: Path) -> None:
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    try:
        controller.ctx.settings = controller.ctx.settings.model_copy(update={"hands_free": True})
        dead = SimpleNamespace(running=False)
        controller.hands_free = dead
        started = []
        controller.start_hands_free = lambda: started.append(1) or True
        notices = []
        controller.notice.connect(notices.append)
        assert "listening" in controller.watchdog()
        assert started == [1] and notices and "Restarted listening" in notices[-1]
        assert controller.watchdog() == []  # nothing else to do
    finally:
        controller.shutdown()


def test_idle_model_is_unloaded(qapp, tmp_path: Path) -> None:
    from tests.test_gui import make_controller

    controller, fake, _ = make_controller(tmp_path)
    try:
        controller.last_status = SimpleNamespace(selected=SimpleNamespace(name="llama3.2:3b"))
        assert not controller.unload_if_idle()  # just used
        controller._last_activity -= 21 * 60
        assert controller.unload_if_idle() and not controller.unload_if_idle()  # only once
        import time

        time.sleep(0.3)
        body = next(b for _, p, b in fake.requests if p == "/api/generate")
        assert body["keep_alive"] == 0 and body["model"] == "llama3.2:3b"
    finally:
        controller.shutdown()


def test_habits(tmp_path: Path) -> None:
    from app.agent.habits import record, suggestions

    database = db(tmp_path)
    now = datetime(2026, 9, 25, 9, 5)
    for days in (1, 2, 3):
        record(database, "Visual Studio Code", now - timedelta(days=days, minutes=5))
    record(database, "Spotify", now - timedelta(days=1))
    record(database, "Chrome", (now - timedelta(days=2)).replace(hour=21))
    assert suggestions(database, now) == ["Visual Studio Code"]
    assert suggestions(database, now.replace(hour=15)) == []  # not around this time
    record(database, "Visual Studio Code", now - timedelta(minutes=1))
    assert suggestions(database, now) == []  # already opened today


def test_suggestion_once_a_day(qapp, tmp_path: Path) -> None:
    from app.agent.habits import record
    from tests.test_gui import make_controller

    controller, _, _ = make_controller(tmp_path)
    try:
        now = datetime.now().astimezone()
        for days in (1, 2, 3, 4):
            record(controller.ctx.database, "Notepad", now - timedelta(days=days))
        assert controller.maybe_suggest(now) == "Notepad"
        assert controller.maybe_suggest(now) is None
    finally:
        controller.shutdown()


# ------------------------------------------------------------------ 5. screen agent


def ocr_result():
    from app.vision.ocr import OcrLine, OcrResult

    return OcrResult((
        OcrLine("File Edit View", (0, 0, 200, 20), (("File", (0, 0, 40, 20)), ("Edit", (50, 0, 40, 20)),
                                                    ("View", (100, 0, 40, 20)))),
        OcrLine("Cancel Submit form", (10, 300, 300, 30), (("Cancel", (10, 300, 80, 30)), ("Submit", (100, 300, 90, 30)),
                                                          ("form", (200, 300, 60, 30)))),
    ), "test", "en")


def test_find_on_screen() -> None:
    from app.tools.screen_agent import find_on_screen

    targets = find_on_screen(ocr_result(), "submit", origin=(1920, 0))
    assert targets[0].text == "Submit" and (targets[0].x, targets[0].y) == (1920 + 145, 315)
    assert find_on_screen(ocr_result(), "Submit form", (0, 0))[0].text == "Submit form"
    assert find_on_screen(ocr_result(), "Delete everything", (0, 0)) == []


def test_click_needs_screen_permission_and_confirmation() -> None:
    from app.security.permissions import RiskLevel
    from app.tools.catalog import build_default_registry
    from app.tools.screen_agent import ClickArgs, click_text
    from app.tools import winapi

    tool = build_default_registry().get("click_text")
    assert tool.level == RiskLevel.MODERATE and tool.direct_without_confirmation is None
    if winapi.is_windows():
        pytest.skip("checks the non-Windows path")
    with pytest.raises(ToolError):
        asyncio.run(click_text(ClickArgs(text="Submit"), ToolContext(Settings())))


def test_typing_refuses_secrets(monkeypatch) -> None:
    from app.tools import screen_agent, winapi
    from app.tools.screen_agent import TypeArgs, type_text

    typed: list[str] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "type_unicode", typed.append)
    monkeypatch.setattr(screen_agent, "_check_foreground", lambda ctx: None)
    with pytest.raises(ToolError, match="won't type passwords"):
        asyncio.run(type_text(TypeArgs(text="my password is hunter2"), ToolContext(Settings())))
    assert asyncio.run(type_text(TypeArgs(text="Hello from Jarvis"), ToolContext(Settings()))).ok
    assert typed == ["Hello from Jarvis"]


# ------------------------------------------------------------------ 6. phone


def test_contacts_calls_and_texts(tmp_path: Path, monkeypatch) -> None:
    from app.tools import phone, winapi

    opened: list[str] = []
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "start_file", opened.append)
    ctx = ToolContext(Settings(), paths=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ToolError, match="no contacts"):
        phone.call_contact(phone.CallArgs(name="Ammu"), ctx)
    phone.save_contact(phone.SaveContactArgs(name="Ammu", number="+880 1712-345678"), ctx)
    assert phone.call_contact(phone.CallArgs(name="ammu"), ctx).message == "Calling Ammu through Phone Link."
    phone.text_contact(phone.TextArgs(name="Amu", message="I'm on my way"), ctx)  # sounds like Ammu
    assert opened == ["tel:+8801712345678", "sms:+8801712345678?body=I%27m%20on%20my%20way"]
    with pytest.raises(ToolError, match="can't read"):
        phone.read_messages(None, ctx)
    with pytest.raises(Exception):
        phone.SaveContactArgs(name="x", number="call me maybe")


# ------------------------------------------------------------------ 8. offline Bangla voice


class Voice(TTSEngine):
    def __init__(self, tag):
        self.tag, self.spoken = tag, []

    def load(self):
        pass

    def synthesize(self, text, speed=1.0):
        self.spoken.append(text)
        return AudioClip(np.ones(10, np.float32), 22050)


def test_bilingual_routes_bangla_to_the_bangla_voice() -> None:
    from app.tts.bangla import BilingualTTS

    english, bangla = Voice("en"), Voice("bn")
    tts = BilingualTTS(english, bangla)
    tts.synthesize("Opened Chrome.")
    tts.synthesize("ক্রোম খুলে দিয়েছি।")
    assert english.spoken == ["Opened Chrome."] and bangla.spoken == ["ক্রোম খুলে দিয়েছি।"]


def test_neural_voice_uses_the_offline_bangla_voice_when_offline() -> None:
    from app.tts.bangla import BilingualTTS
    from app.tts.neural import NeuralTTS

    bangla = Voice("bn")

    def down(*a):
        raise OSError("offline")

    tts = NeuralTTS("en-GB-RyanNeural", "bn-BD-PradeepNeural", fallback=BilingualTTS(Voice("en"), bangla),
                    synthesize_mp3=down)
    assert tts.synthesize("হ্যালো।").samples.size == 10 and bangla.spoken == ["হ্যালো।"]


def test_bangla_voice_download_is_checked_and_safe(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    import httpx

    from app.tts import bangla

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as tar:
        for name, data in (("voice/model.onnx", b"model"), ("voice/tokens.txt", b"a 1"), ("../../evil.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = buffer.getvalue()
    monkeypatch.setattr(bangla, "ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(bangla, "MODEL_SHA256", hashlib.sha256(b"model").hexdigest())
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=archive)))
    monkeypatch.setattr(httpx, "stream", lambda method, url, **k: client.stream(method, url, headers=k.get("headers")))
    folder = bangla.download(tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == ["model.onnx", "tokens.txt"]
    assert not (tmp_path.parent / "evil.txt").exists() and bangla.is_installed(tmp_path)


# ------------------------------------------------------------------ 9. phone remote


def test_remote_requires_a_paired_phone_and_a_private_address() -> None:
    aiohttp = pytest.importorskip("aiohttp")  # noqa: F841
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer, is_private

    sent: list[str] = []
    answers: list[tuple] = []
    hub = RemoteHub(sent.append, lambda ok, rid: answers.append((ok, rid)) or rid == "q1",
                    lambda: {"id": "q1", "text": "Empty the Recycle Bin?", "level": 2}, lambda: {"state": "IDLE"})
    hub.add("assistant", "Hello, sir.")
    pairing = PairingStore(None, encrypt=False)
    server = RemoteServer(hub, pairing, 0)
    code = pairing.new_code()

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            denied = await client.get("/api/messages")
            page = await client.get("/")
            wrong_code = await client.post("/api/pair", json={"code": "000000" if code != "000000" else "111111"})
            paired = await (await client.post("/api/pair", json={"code": code, "name": "Pixel"})).json()
            reused = await client.post("/api/pair", json={"code": code})
            headers = {"X-Device": paired["device"], "X-Key": paired["key"]}
            data = await (await client.get("/api/messages?since=0", headers=headers)).json()
            await client.post("/api/send", json={"text": "open chrome"}, headers=headers)
            stale = await (await client.post("/api/confirm", json={"approved": True, "id": "old"},
                                             headers=headers)).json()
            fresh = await (await client.post("/api/confirm", json={"approved": True, "id": "q1"},
                                             headers=headers)).json()
            wrong = await client.post("/api/send", json={"text": "x"},
                                      headers={"X-Device": paired["device"], "X-Key": "y" * 43})
            return (denied.status, page.status, await page.text(), wrong_code.status, paired, reused.status, data,
                    stale, fresh, wrong.status)
        finally:
            await client.close()

    denied, page_status, page, wrong_code, paired, reused, data, stale, fresh, wrong = asyncio.run(go())
    assert denied == 401 and wrong == 401 and page_status == 200 and "JARVIS" in page and "?t=" not in page
    assert wrong_code == 401 and paired["ok"] and reused == 401  # a code works once
    assert data["messages"][0]["text"] == "Hello, sir." and data["pending"]["id"] == "q1"
    assert sent == ["open chrome"] and stale == {"ok": False} and fresh == {"ok": True}
    assert "?t=" not in server.url and pairing.listing()[0]["name"] == "Pixel"
    assert is_private("192.168.1.20") and is_private("127.0.0.1") and not is_private("8.8.8.8")


def test_pairing_codes_expire_and_lock_out(tmp_path: Path) -> None:
    from app.remote.pairing import PairingStore

    now = [1000.0]
    store = PairingStore(tmp_path, clock=lambda: now[0], encrypt=False)
    code = store.new_code()
    now[0] += 301
    assert store.pair(code, "late") is None  # expired
    for _ in range(4):
        store.pair("999999" if code != "999999" else "888888", "guess")
    code = store.new_code()
    assert store.pair(code, "locked") is None and store.locked()  # 5 wrong codes: locked for 10 minutes
    now[0] += 601
    device, key = store.pair(store.new_code(), "ok")
    assert store.check(device, key) is not None and store.check(device, "nope") is None
    again = PairingStore(tmp_path, encrypt=False)  # remembered, but only as a hash
    assert again.check(device, key) is not None and key not in (tmp_path / "remote_devices.json").read_text()
    assert again.remove_all() == 1 and PairingStore(tmp_path, encrypt=False).check(device, key) is None


def test_gpu_saver_setting_default() -> None:
    assert Settings().gpu_memory_saver and Settings().idle_unload_minutes == 20
    assert json.loads("{}") == {}
