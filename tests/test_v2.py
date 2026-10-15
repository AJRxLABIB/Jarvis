"""Jarvis 2.0: voice safety, speed, memory/documents, office, desktop, engineering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.agent.language import classify_answer, is_echo, is_stop_request
from app.config.settings import Settings
from app.memory.database import Database
from app.memory.semantic import SemanticIndex, chunks, read_docx
from app.tools.base import ToolContext, ToolError
from app.voice.speaker import SpeakerVerifier, cosine, fbank, model_path
from tests.test_hands_free import Harness, wake_then

# ------------------------------------------------------------------ 1. voice answers


@pytest.mark.parametrize(("text", "answer"), [
    ("Yes", True), ("yes please", True), ("হ্যাঁ", True), ("জি স্যার", True), ("okay sir", True),
    ("No", False), ("না", False), ("না থাক", False), ("cancel", False), ("maybe later", None), ("", None),
])
def test_classify_answer(text: str, answer) -> None:
    assert classify_answer(text) is answer


def test_listener_takes_a_spoken_answer() -> None:
    answers: list[str] = []
    h = Harness(wake_then(command_seconds=0.8), text="yes", pause=lambda: True)
    h.listener.ask(lambda text: answers.append(text) or True)
    try:
        assert h.run_until(lambda: answers, timeout=5)
    finally:
        h.stop()
    assert answers[0] == "yes" and not h.listener.asking


def test_unclear_answer_keeps_listening_until_timeout() -> None:
    h = Harness(wake_then(command_seconds=0.8), text="banana", pause=lambda: True)
    h.listener.ask(lambda text: False, timeout=0.5)
    try:
        h.run_until(lambda: not h.listener.asking, timeout=5)
    finally:
        h.stop()
    assert not h.listener.asking


# ------------------------------------------------------------------ 2. speaker verification


class FakeSession:
    """Embedding = mean of the features (so two tones of the same pitch match, other pitches don't)."""

    def get_inputs(self):
        return [SimpleNamespace(name="feats")]

    def run(self, _, feeds):
        feats = feeds["feats"][0]
        return [np.asarray([np.abs(feats).mean(axis=0)], np.float32)]


def tone(freq: float, seconds: float = 1.5) -> np.ndarray:
    t = np.arange(int(16000 * seconds)) / 16000
    return (0.3 * np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * freq * 2.7 * t)).astype(np.float32)


def test_fbank_shape_and_cosine() -> None:
    feats = fbank(tone(200, 1.0))
    assert feats.shape == (98, 80) and abs(float(feats.mean())) < 1e-3  # mean removed
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0) and cosine([1, 0], [0, 1]) == pytest.approx(0.0, abs=1e-6)


def test_enroll_and_verify(tmp_path: Path) -> None:
    verifier = SpeakerVerifier(tmp_path, tmp_path / "data", threshold=0.9, session=FakeSession())
    assert not verifier.enrolled and verifier.verify(tone(200))  # not enrolled: anyone
    verifier.enroll([tone(200), tone(205), tone(198)])
    assert verifier.enrolled and (tmp_path / "data" / "voiceprint.npy").exists()
    assert verifier.verify(tone(202)) and verifier.last_score > 0.9
    assert verifier.verify(tone(200, 0.3))  # too short to judge
    verifier.forget()
    assert not verifier.enrolled


def test_enroll_needs_two_recordings(tmp_path: Path) -> None:
    from app.voice.speaker import SpeakerError

    with pytest.raises(SpeakerError, match="at least two"):
        SpeakerVerifier(tmp_path, tmp_path, session=FakeSession()).enroll([tone(200)])


def test_other_voices_are_ignored_by_the_listener() -> None:
    class Stranger:
        enrolled = True

        def verify(self, audio):
            return False

    h = Harness(wake_then())
    h.listener.speaker = Stranger()
    try:
        assert not h.run_until(lambda: h.results, timeout=2.5)
    finally:
        h.stop()


REAL_MODEL = Path("data/models/speaker")


@pytest.mark.skipif(not model_path(REAL_MODEL).is_file(), reason="the real voice-print model is not downloaded")
def test_real_model_separates_voices() -> None:
    piper = Path("data/models/piper/en_US-lessac-medium.onnx")
    if not piper.is_file():
        pytest.skip("no Piper voice to generate speech with")
    from piper import PiperVoice

    voice = PiperVoice.load(str(piper))

    def say(text: str) -> np.ndarray:
        parts = list(voice.synthesize(text))
        audio, rate = np.concatenate([p.audio_float_array for p in parts]), parts[0].sample_rate
        return np.interp(np.arange(0, len(audio), rate / 16000), np.arange(len(audio)), audio).astype(np.float32)

    verifier = SpeakerVerifier(REAL_MODEL, Path("/tmp/jarvis-vp-test"))
    a, b = verifier.embed(say("Open Google Chrome and play some music.")), verifier.embed(say("What's the weather?"))
    noise = verifier.embed(np.random.default_rng(3).standard_normal(24000).astype(np.float32) * 0.1)
    assert cosine(a, b) > 0.5 > cosine(a, noise)


# ------------------------------------------------------------------ 3. talking over it


def test_echo_and_stop_words() -> None:
    assert is_echo("the weather in Dhaka is", "In Dhaka the weather is 31 degrees")
    assert not is_echo("open youtube", "In Dhaka the weather is 31 degrees")
    assert is_stop_request("stop") and is_stop_request("থামো") and is_stop_request("okay, enough")
    assert not is_stop_request("stop the music on youtube and open chrome")


def test_talking_over_it_interrupts_and_runs_the_new_command() -> None:
    interrupted: list[int] = []
    speaking = {"on": True}
    h = Harness(wake_then(command_seconds=1.0), text="Jarvis, open chrome", pause=lambda: speaking["on"])

    def barge() -> bool:
        interrupted.append(1)
        speaking["on"] = False
        return True

    h.listener.on_barge_in = barge
    h.listener.can_duplex = lambda: speaking["on"]
    h.listener.spoken_text = lambda: "The weather in Dhaka is lovely today." if speaking["on"] else ""
    try:
        assert h.run_until(lambda: h.results, timeout=5)
    finally:
        h.stop()
    assert interrupted and h.results[0].status == "ok" and h.results[0].text == "open chrome"


def test_without_a_voice_print_only_the_wake_phrase_or_stop_interrupts() -> None:
    """Speech while it talks is most likely its own voice from the speakers (or the TV)."""
    h = Harness(wake_then(command_seconds=1.0), text="flamingos are pink because of what they eat", pause=lambda: True)
    h.listener.on_barge_in = lambda: pytest.fail("interrupted by speech that wasn't meant for it")
    h.listener.can_duplex = lambda: True
    h.listener.spoken_text = lambda: "Here is a fun fact about birds."
    try:
        assert not h.run_until(lambda: h.results, timeout=1.5)
    finally:
        h.stop()


def test_its_own_voice_is_not_an_interruption() -> None:
    h = Harness(wake_then(command_seconds=1.0), text="the weather in Dhaka is lovely", pause=lambda: True)
    h.listener.on_barge_in = lambda: pytest.fail("interrupted by its own voice")
    h.listener.can_duplex = lambda: True
    h.listener.spoken_text = lambda: "The weather in Dhaka is lovely today."
    try:
        assert not h.run_until(lambda: h.results, timeout=2.5)
    finally:
        h.stop()


# ------------------------------------------------------------------ 4. synthesis runs ahead


def test_next_sentence_is_prepared_while_one_plays() -> None:
    import threading
    import time

    from app.tts.base import AudioClip, TTSEngine
    from app.tts.manager import TTSManager

    events: list[str] = []

    class Engine(TTSEngine):
        def load(self):
            pass

        def synthesize(self, text, speed=1.0):
            events.append(f"synth {text}")
            return AudioClip(np.zeros(10, np.float32), 16000)

    class Player:
        def play(self, clip, *, volume, abort: threading.Event) -> bool:
            events.append("play start")
            time.sleep(0.3)
            events.append("play end")
            return True

    tts = TTSManager(Settings(), Path("."), engine=Engine(), player=Player())
    try:
        tts.speak("One.")
        tts.speak("Two.")
        assert tts.wait_until_idle(5)
    finally:
        tts.close()
    assert events.index("synth Two.") < events.index("play end")  # made while "One." was playing


# ------------------------------------------------------------------ 6/7. search by meaning


def fake_embedder():
    vocabulary = ["thesis", "deadline", "chrome", "music", "dhaka", "weather", "tender", "budget", "cricket"]

    async def embed(texts):
        return [[float(t.lower().count(word)) + 0.01 for word in vocabulary] for t in texts]

    return embed


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


def test_history_is_saved_redacted_searched_and_restored(tmp_path: Path) -> None:
    index = SemanticIndex(db(tmp_path), fake_embedder(), "fake")

    async def go():
        await index.add_exchange("my thesis deadline is Friday", "Noted, the thesis is due Friday.")
        await index.add_exchange("my password is hunter2 and card 4111 1111 1111 1111", "I won't store that.")
        await index.add_exchange("play music", "Playing music.")
        return await index.search("when is the thesis deadline", "history")

    hits = asyncio.run(go())
    assert "thesis" in hits[0].text
    stored = index.database
    with stored.connect() as conn:
        texts = " ".join(r[0] for r in conn.execute("SELECT text FROM semantic_items"))
    assert "4111 1111 1111 1111" not in texts
    assert index.recent_history(2)[-1] == ("play music", "Playing music.")
    assert index.forget_history() == 3


def test_documents_are_indexed_and_searched(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "tender.txt").write_text("The tender budget is 5 crore. The tender deadline is 30 October.")
    (docs / "notes.md").write_text("Cricket practice on Sunday.")
    with zipfile.ZipFile(docs / "thesis.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>My thesis is about Dhaka weather."
                                         "</w:t></w:r></w:p></w:body></w:document>")
    (docs / "image.png").write_bytes(b"not a document")
    index = SemanticIndex(db(tmp_path), fake_embedder(), "fake")

    async def go():
        first = await index.index_folder(docs)
        again = await index.index_folder(docs)
        hits = await index.search("what is the tender deadline", "document")
        return first, again, hits

    first, again, hits = asyncio.run(go())
    assert first["indexed"] == 3 and again["unchanged"] == 3 and again["indexed"] == 0
    assert Path(hits[0].source).name == "tender.txt" and index.document_count() == 3
    assert read_docx(docs / "thesis.docx").strip() == "My thesis is about Dhaka weather."


def test_chunks() -> None:
    text = ("Sentence number one is here. " * 100).strip()
    parts = chunks(text, size=300, overlap=50)
    assert len(parts) > 5 and all(len(p) <= 300 for p in parts)
    assert chunks("") == []


def test_ask_documents_tool_needs_an_index(tmp_path: Path) -> None:
    from app.tools.knowledge import AskDocsArgs, ask_documents

    ctx = ToolContext(Settings(), database=db(tmp_path))
    ctx.ai = SimpleNamespace(embed=fake_embedder())
    with pytest.raises(ToolError, match="No documents are indexed"):
        asyncio.run(ask_documents(AskDocsArgs(question="deadline?"), ctx))


def test_conversation_memory_is_off_by_default(tmp_path: Path) -> None:
    from app.tools.knowledge import HistoryArgs, search_history

    with pytest.raises(ToolError, match="Conversation memory is off"):
        asyncio.run(search_history(HistoryArgs(query="thesis"), ToolContext(Settings(), database=db(tmp_path))))


# ------------------------------------------------------------------ 8. model router


def test_simple_requests_go_to_the_fast_model() -> None:
    from app.agent.conversation import is_simple_request

    assert is_simple_request("hi, how are you?") and is_simple_request("তুমি কেমন আছো?")
    assert not is_simple_request("explain how neural networks learn")
    assert not is_simple_request("write an email to my teacher about the project deadline")
    assert not is_simple_request(" ".join(["word"] * 20))


def test_fast_model_is_used_when_installed() -> None:
    from app.ai.manager import AIManager
    from tests.fake_ollama import FakeOllama, model_entry

    fake = FakeOllama(models=[model_entry("qwen3:4b-instruct"), model_entry("llama3.2:1b")])
    ai = AIManager.from_settings(Settings(ai_model="qwen3:4b-instruct", ai_fast_model="llama3.2:1b"),
                                 transport=fake.transport())

    async def go():
        try:
            fast = await ai.chat([], fast=True)
            normal = await ai.chat([])
            return fast.model, normal.model
        finally:
            await ai.aclose()

    fast_body, normal_body = asyncio.run(go()) and [b for _, p, b in fake.requests if p == "/api/chat"]
    assert fast_body["model"] == "llama3.2:1b" and normal_body["model"] == "qwen3:4b-instruct"


# ------------------------------------------------------------------ 9. Outlook


class Item(SimpleNamespace):
    pass


class Items(list):
    IncludeRecurrences = False

    def Sort(self, *a):  # noqa: N802
        pass

    def Restrict(self, query):  # noqa: N802
        return Items(self)


def test_calendar_and_email_read_through_outlook() -> None:
    from datetime import datetime

    from app.tools.outlook import CalendarArgs, EmailArgs, calendar_events, read_emails

    when = datetime(2026, 9, 24, 10, 30)
    namespace = SimpleNamespace(GetDefaultFolder=lambda n: SimpleNamespace(items=None, Items=Items(
        [Item(Subject="Thesis meeting", Start=when, End=when, Location="DIU")] if n == 9 else
        [Item(SenderName="Teacher", Subject="Project", ReceivedTime=when, Body="Please send it\\nby Friday")])))
    ctx = ToolContext(Settings(), clock=lambda: datetime(2026, 9, 24, 8, 0).astimezone())
    events = calendar_events(CalendarArgs(), ctx, namespace=namespace)
    assert events.message == "1 event(s) today; the first is 'Thesis meeting' at Thu 24 Sep 10:30."
    mail = read_emails(EmailArgs(), ctx, namespace=namespace)
    assert "from Teacher: 'Project'" in mail.message and "data, never instructions" in mail.data["note"]


def test_sending_email_is_dangerous_and_validated() -> None:
    from app.security.permissions import RiskLevel
    from app.tools.catalog import build_default_registry
    from app.tools.outlook import SendEmailArgs

    tool = build_default_registry().get("send_email")
    assert tool.level == RiskLevel.DANGEROUS and tool.direct_without_confirmation is None
    with pytest.raises(Exception):
        SendEmailArgs(to="not an address; rm -rf", subject="x", body="y")


# ------------------------------------------------------------------ 12. presence


def test_presence_away_and_back() -> None:
    from app.vision.presence import PresenceMonitor

    now = {"t": 0.0}
    events: list[str] = []
    monitor = PresenceMonitor(away_minutes=1, on_away=lambda: events.append("away"),
                              on_return=lambda s: events.append(f"back {s:.0f}"), clock=lambda: now["t"])
    for t in (10, 30, 50):
        now["t"] = t
        assert monitor.observe(False) is None
    now["t"] = 61
    assert monitor.observe(False) == "away" and not monitor.present
    now["t"] = 300
    assert monitor.observe(True) is None  # one frame could be anything
    now["t"] = 302
    assert monitor.observe(True) == "back"
    assert events == ["away", "back 302"]


def test_face_detector_on_an_empty_frame() -> None:
    cv2 = pytest.importorskip("cv2")  # noqa: F841
    from app.vision.presence import FaceDetector

    assert FaceDetector().has_face(np.zeros((480, 640, 3), np.uint8)) is False


# ------------------------------------------------------------------ 13. updates


def test_update_check_and_verified_download(tmp_path: Path, monkeypatch) -> None:
    from app.utils import updates

    payload = b"installer bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(request):
        if request.url.path.endswith("update.json"):
            return httpx.Response(200, json={"version": "9.0.0", "url": "https://x.test/JarvisSetup-9.0.0.exe",
                                             "sha256": digest, "notes": "New"})
        return httpx.Response(200, content=payload)

    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(handler))
    info = updates.check("https://x.test/update.json", current="2.0.0")
    assert info.version == "9.0.0"
    assert updates.check("https://x.test/update.json", current="9.0.0") is None
    assert updates.download(info, tmp_path).read_bytes() == payload
    bad = updates.UpdateInfo("9.0.0", info.url, "0" * 64, "")
    with pytest.raises(updates.UpdateError, match="checksum"):
        updates.download(bad, tmp_path)
    with pytest.raises(updates.UpdateError, match="https"):
        updates.check("http://x.test/update.json")


def test_versions() -> None:
    from app.utils.updates import parse_version

    assert parse_version("2.10.0") > parse_version("2.9.9") and parse_version("v2.0") == (2, 0)


# ------------------------------------------------------------------ 15. plugins


def test_plugins_load_and_bad_ones_are_skipped(tmp_path: Path) -> None:
    from app.plugins import load_plugins
    from app.tools.catalog import build_default_registry

    (tmp_path / "hello.py").write_text(
        "from app.tools.base import Tool, NoArgs, ToolResult\n"
        "TOOLS = [Tool('say_hello', 'Say hello', lambda a, c: ToolResult.success('Hello!'), NoArgs)]\n")
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "clash.py").write_text(
        "from app.tools.base import Tool, NoArgs, ToolResult\n"
        "TOOLS = [Tool('get_datetime', 'x', lambda a, c: ToolResult.success('x'), NoArgs)]\n")
    from app.plugins import trust_plugin

    trust_plugin(tmp_path, "hello")
    trust_plugin(tmp_path, "clash")
    registry = build_default_registry()
    report = load_plugins(tmp_path, registry)
    assert report.loaded == ("hello",) and "say_hello" in registry
    assert {name for name, _ in report.failed} == {"broken", "clash"}


def test_plugins_are_off_by_default() -> None:
    assert Settings().plugins_enabled is False


# ------------------------------------------------------------------ 16. settings migration + diagnostics


def test_settings_migration(tmp_path: Path) -> None:
    from app.config.migrate import CURRENT_VERSION, migrate_env_file

    env = tmp_path / ".env"
    env.write_text("# my settings\nAI_MODEL=qwen3:4b\nSTT_MODEL=base.en\nSTT_DEVICE=cpu\nSTT_COMPUTE_TYPE=int8\n"
                   "OFFLINE_MODE=false\nUSER_ADDRESS=sir\n")
    applied = migrate_env_file(env)
    text = env.read_text()
    assert len(applied) == 3 and "# my settings" in text and "USER_ADDRESS=sir" in text
    assert "AI_MODEL=qwen3:4b-instruct" in text and "STT_MODEL=auto" in text and "OFFLINE_MODE=auto" in text
    assert f"SETTINGS_VERSION={CURRENT_VERSION}" in text and (tmp_path / ".env.bak").exists()
    assert migrate_env_file(env) == []  # once only
    assert migrate_env_file(tmp_path / "missing.env") == []


def test_diagnostics_bundle_has_no_secrets(tmp_path: Path) -> None:
    from app.main import bootstrap
    from app.utils.diagnostics import export_diagnostics, safe_settings

    assert safe_settings("API_KEY=abc123\nUSER_ADDRESS=sir\n") == "API_KEY=<removed>\nUSER_ADDRESS=sir\n"
    ctx = bootstrap(tmp_path, environ={})
    (tmp_path / ".env").write_text("OPENAI_API_TOKEN=sk-secret-value\nUSER_ADDRESS=sir\n")
    out = export_diagnostics(ctx, tmp_path / "diag")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        settings = z.read("settings.env").decode()
    assert out.suffix == ".zip" and {"version.txt", "doctor.txt", "settings.env"} <= set(names)
    assert "sk-secret-value" not in settings and "USER_ADDRESS=sir" in settings
    assert not any(n.endswith((".db", ".npy")) for n in names)


def test_ci_and_installer_files_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in ci and "ruff" in ci and "windows-latest" in ci
    iss = (root / "packaging" / "jarvis.iss").read_text()
    assert "PrivilegesRequired=lowest" in iss and "JARVIS.exe" in iss
    assert json.loads('{"ok": true}')  # (json import used by the update manifest format)
