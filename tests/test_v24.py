"""Jarvis 2.4: benchmark history, Bangla eval, early transcription, verification, wake check, MCP, memory
suggestions, phone voice + https, tone, DPAPI, signed updates."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config.settings import Settings
from app.memory.database import Database
from app.tools.base import ToolContext


def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    database.initialize()
    return database


def tone(seconds=1.0, f=220.0, amp=0.2):
    t = np.arange(int(16000 * seconds)) / 16000
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


# ------------------------------------------------------------------ 1. benchmark history, Bangla eval


def test_benchmark_compares_with_the_last_run(tmp_path: Path) -> None:
    from app import benchmark
    from tests.test_v23 import _recordings

    _recordings(tmp_path, [("Volume up", "volume_control", "up"), ("Lock my PC", "power_control", "lock")])
    first = iter(["volume up", "lock my pie"])
    benchmark.run(tmp_path, lambda a: SimpleNamespace(text=next(first)), lambda n: None, out=lambda _: None)
    lines: list[str] = []
    second = iter(["volume up", "lock my pc"])
    benchmark.run(tmp_path, lambda a: SimpleNamespace(text=next(second)), lambda n: None, out=lines.append)
    text = "\n".join(lines)
    assert "accuracy +50.0 points" in text and "now understood: 'Lock my PC'" in text
    runs = benchmark.load_history(tmp_path)
    assert len(runs) == 2 and runs[-1]["per_item"] == {"Volume up": True, "Lock my PC": True}


def test_bangla_scoring_rules() -> None:
    from app.eval_bangla import QUESTIONS, Question, bangla_ratio, score, summary

    q = Question("বাংলাদেশের রাজধানী কোথায়?", ("ঢাকা",), 20)
    assert score(q, "বাংলাদেশের রাজধানী ঢাকা।").problems == ()
    assert "not in Bangla" in score(q, "The capital is Dhaka.").problems
    assert "wrong or missing fact" in score(q, "বাংলাদেশের রাজধানী চট্টগ্রাম।").problems
    assert score(q, "ঢাকা " * 30).problems == ("too long (30 words)",)
    assert score(q, "").problems[0] == "no answer"
    assert bangla_ratio("Hello ঢাকা") < 0.6 and len(QUESTIONS) >= 12
    result = summary([score(q, "রাজধানী ঢাকা"), score(q, "Dhaka")])
    assert result["fully_right"] == 1 and 0 < result["score"] < 100


def test_bangla_eval_runs_and_saves(tmp_path: Path) -> None:
    from app import eval_bangla

    async def ask(question):
        return "ঢাকা" if "রাজধানী" in question else ""

    lines: list[str] = []
    scores = asyncio.run(eval_bangla.evaluate(ask, lines.append, eval_bangla.QUESTIONS[:2]))
    assert not scores[0].problems and scores[1].problems
    eval_bangla.save(tmp_path, "qwen3:4b-instruct", scores)
    assert eval_bangla.previous(tmp_path)[0]["model"] == "qwen3:4b-instruct"


# ------------------------------------------------------------------ 2. early transcription


class CountingSTT:
    device = "cuda"

    def __init__(self):
        self.calls = 0

    @property
    def loaded(self):
        return True

    def load(self):
        pass

    def transcribe(self, audio, language=None):
        from app.voice.speech_to_text import Transcript

        self.calls += 1
        return Transcript("open chrome", "en", 0.9, 0.01, audio.size / 16000, 5)


@pytest.mark.parametrize("early", [True, False])
def test_the_pause_transcript_is_reused(early: bool) -> None:
    from tests.test_hands_free import Harness, wake_then

    h = Harness(wake_then(command_seconds=0.8), early_transcription=early)
    h.listener.stt = h.stt = CountingSTT()
    try:
        assert h.run_until(lambda: h.results, 8)
    finally:
        h.stop()
    assert h.results[0].text == "open chrome"
    assert h.stt.calls == 1  # early on: transcribed at the pause and reused; off: transcribed once at the end


# ------------------------------------------------------------------ 3. verification


def test_window_actions_are_checked(monkeypatch) -> None:
    from app.tools import applications, winapi

    window = SimpleNamespace(hwnd=7, title="Notepad")
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(applications, "_target_windows", lambda name, ctx: ("Notepad", [window], set()))
    monkeypatch.setattr(winapi, "show_window", lambda hwnd, cmd: None)
    monkeypatch.setattr(winapi, "window_state", lambda hwnd: "normal")  # nothing happened
    result = applications.window_control(applications.WindowArgs(action="minimize", name="notepad"),
                                         ToolContext(Settings()))
    assert not result.ok and "didn't change" in result.message
    monkeypatch.setattr(winapi, "window_state", lambda hwnd: "minimized")
    result = applications.window_control(applications.WindowArgs(action="minimize", name="notepad"),
                                         ToolContext(Settings()))
    assert result.ok and result.data["verified"] is True


def test_screen_change_detection() -> None:
    from PIL import Image

    from app.tools.screen_agent import screen_changed

    a = Image.new("L", (320, 180), 40)
    b = a.copy()
    assert not screen_changed(a, b)
    b.paste(200, (0, 0, 120, 90))  # a menu opened
    assert screen_changed(a, b)


# ------------------------------------------------------------------ 4. personal wake check


def _phrase(f0: float, rng) -> np.ndarray:
    t = np.arange(16000) / 16000
    clean = 0.2 * np.sin(2 * np.pi * (f0 + 200 * t) * t) * np.hanning(16000)
    return (clean + 0.002 * rng.normal(size=16000)).astype(np.float32)


def test_wake_templates_accept_the_phrase_and_reject_others(tmp_path: Path) -> None:
    from app.voice.wake_templates import WakeTemplates

    rng = np.random.default_rng(1)
    templates = WakeTemplates(tmp_path)
    assert templates.verify(tone()) is True  # not set up: never blocks
    with pytest.raises(ValueError):
        templates.enroll([_phrase(300, rng)])
    templates.enroll([_phrase(300 + rng.normal(0, 5), rng) for _ in range(5)])
    quiet = lambda n: (0.002 * rng.normal(size=n)).astype(np.float32)  # noqa: E731
    again = WakeTemplates(tmp_path)  # loaded from disk
    assert again.ready
    assert again.verify(np.concatenate([quiet(6000), _phrase(302, rng), quiet(4000)]))
    assert not again.verify(np.concatenate([quiet(6000), _phrase(900, rng), quiet(4000)]))
    assert not again.verify(quiet(25000))
    again.forget()
    assert not WakeTemplates(tmp_path).ready


def test_hands_free_ignores_wakeups_that_fail_the_check() -> None:
    from tests.test_hands_free import Harness, wake_then

    class Never:
        def verify(self, audio):
            return False

    h = Harness(wake_then())
    h.listener.wake_check = Never()
    try:
        h.run_until(lambda: h.results, 1.5)
    finally:
        h.stop()
    assert h.wakes == [] and h.results == []


# ------------------------------------------------------------------ 5. MCP


@pytest.fixture()
def mcp_config(tmp_path: Path) -> Path:
    server = Path(__file__).parent / "mcp_demo_server.py"
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": {
        "demo": {"command": sys.executable, "args": [str(server)], "keywords": ["add numbers"]},
        "off": {"command": "nothing", "disabled": True},
    }}), encoding="utf-8")
    return tmp_path


def test_mcp_tools_are_registered_and_called(mcp_config: Path) -> None:
    from app.agent.tool_manager import ToolRegistry
    from app.agent.tool_router import categories_for
    from app.mcp.tools import McpHub
    from app.security.permissions import RiskLevel

    registry = ToolRegistry()
    hub = McpHub(mcp_config, registry)
    hub.start(wait=True)
    try:
        assert hub.status == {"demo": "2 tool(s)"}
        add, wipe = registry.get("mcp_demo_add"), registry.get("mcp_demo_wipe")
        assert add.level == RiskLevel.MODERATE and wipe.level == RiskLevel.DANGEROUS  # destructive: always ask
        assert "mcp_demo" in categories_for("please add numbers 2 and 3")
        args = add.args_model.model_validate({"a": 2, "b-value": 3})
        result = add.handler(args, ToolContext(Settings()))
        assert "sum=5.0" in result.message and "sk-" not in result.message  # secrets in output are redacted
        assert "untrusted" in result.data["note"]
        assert "run add" in add.confirmation_text(args, None)
        with pytest.raises(ValueError):
            add.args_model.model_validate({"a": 1, "__injected__": "x"})
        with pytest.raises(ValueError):
            add.args_model.model_validate({})  # "a" is required
    finally:
        hub.close()


def test_mcp_levels_and_bad_servers(tmp_path: Path) -> None:
    from app.agent.tool_manager import ToolRegistry
    from app.mcp.tools import McpHub, build_tools, tool_name

    server = SimpleNamespace(name="files", config={"levels": {"read": 0, "rm": 3}, "disabled_tools": ["write"]})
    tools = build_tools(server, [{"name": n, "description": f"{n} a file on disk"} for n in ("read", "rm", "write")])
    assert [(t.name, int(t.level)) for t in tools] == [("mcp_files_read", 0)]
    assert tool_name("My Server!", "Get-Item") == "mcp_my_server_get_item"
    (tmp_path / "mcp.json").write_text(json.dumps({"servers": {"broken": {"command": "does-not-exist-123"}}}))
    notices: list[str] = []
    hub = McpHub(tmp_path, ToolRegistry(), notice=notices.append)
    hub.start(wait=True)
    assert hub.status["broken"].startswith("failed") and notices


def test_mcp_http_client() -> None:
    import httpx

    from app.mcp.client import HttpClient

    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((body.get("method"), request.headers.get("mcp-session-id")))
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": {"name": "x"}}},
                                  headers={"Mcp-Session-Id": "abc"})
        event = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "ping"}]}})
        return httpx.Response(200, text=f"event: message\ndata: {event}\n\n", headers={"content-type": "text/event-stream"})

    client = HttpClient("https://mcp.example/mcp", transport=httpx.MockTransport(handler))
    client.initialize()
    assert client.list_tools() == [{"name": "ping"}]
    assert seen[-1] == ("tools/list", "abc")


# ------------------------------------------------------------------ 6. memory suggestions


def test_memory_suggestions_flow(tmp_path: Path) -> None:
    from app.memory.manager import MemoryManager
    from app.memory.suggestions import SuggestionStore, parse_facts, suggest, worth_checking

    assert worth_checking("my thesis deadline is 15 October")
    assert worth_checking("আমার বাসা ধানমন্ডিতে")
    assert not worth_checking("open chrome") and not worth_checking("what is my name?")
    assert parse_facts('Sure! ["My thesis deadline is 15 October", 3, "x"]') == ["My thesis deadline is 15 October"]
    database = db(tmp_path)
    store, manager = SuggestionStore(database), MemoryManager(database)

    async def ask(prompt):
        return '["My thesis deadline is 15 October", "My password is hunter2"]'

    new = asyncio.run(suggest("my thesis deadline is 15 October and my password is hunter2", ask, store, []))
    assert new == ["My thesis deadline is 15 October"]  # the secret never gets in
    assert asyncio.run(suggest("my thesis deadline is 15 October", ask, store, [])) == []  # not offered twice
    (sid, text), = store.pending()
    store.accept(sid, manager)
    assert [m.content for m in manager.all()] == ["My thesis deadline is 15 October"] and store.pending() == []
    store.add("I work at FLUX IT")
    (sid, _), = store.pending()
    store.reject(sid)
    assert not store.add("I work at FLUX IT")  # rejected: not offered again


def test_memory_dialog_shows_suggestions(tmp_path: Path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.memory.manager import MemoryManager
    from app.memory.suggestions import SuggestionStore
    from app.ui.memory_dialog import MemoryDialog

    QApplication.instance() or QApplication([])
    database = db(tmp_path)
    store = SuggestionStore(database)
    store.add("My sister's name is Nabila")
    dialog = MemoryDialog(MemoryManager(database), suggestions=store)
    assert dialog.suggestion_list.count() == 1 and not dialog.suggestion_box.isHidden()
    dialog.accept_suggestion()
    assert dialog.suggestion_list.count() == 0 and dialog.table.rowCount() == 1


# ------------------------------------------------------------------ 7. phone voice + https


def test_https_certificate(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from app.remote.tls import ensure_certificate, fingerprint, server_context

    cert, key = ensure_certificate(tmp_path, "192.168.1.20")
    first = cert.read_bytes()
    assert ensure_certificate(tmp_path, "192.168.1.20")[0].read_bytes() == first  # kept
    assert ensure_certificate(tmp_path, "192.168.1.21")[0].read_bytes() != first  # new address -> new certificate
    assert len(fingerprint(cert).split(":")) == 32
    server_context(cert, key)


def test_phone_voice_endpoint() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from app.remote.pairing import PairingStore
    from app.remote.server import RemoteHub, RemoteServer

    sent: list[str] = []
    hub = RemoteHub(sent.append, lambda ok, rid: True, lambda: None, lambda: {"state": "IDLE"},
                    voice=lambda audio: "open chrome" if audio == b"webm-bytes" else "")
    pairing = PairingStore(None, encrypt=False)
    device, key = pairing.pair(pairing.new_code(), "phone")
    server = RemoteServer(hub, pairing, 0)

    async def go():
        client = TestClient(TestServer(server.app()))
        await client.start_server()
        try:
            headers = {"X-Device": device, "X-Key": key}
            ok = await client.post("/api/voice", data=b"webm-bytes", headers=headers)
            heard = await ok.json()
            empty = await client.post("/api/voice", data=b"", headers=headers)
            refused = await client.post("/api/voice", data=b"webm-bytes")
            page = await (await client.get("/")).text()
            return heard, empty.status, refused.status, page
        finally:
            await client.close()

    heard, empty, refused, page = asyncio.run(go())
    assert heard == {"ok": True, "text": "open chrome"} and sent == ["open chrome"]
    assert empty == 400 and refused == 401 and "Hold to talk" in page and "Notification" in page
    assert server.url.startswith("http://")
    assert RemoteServer(hub, "x", 1, ssl_context=object()).url.startswith("https://")


# ------------------------------------------------------------------ 8. tone


def speech_like(words_per_second: float, amp: float, words: int = 8) -> tuple[np.ndarray, str]:
    parts = []
    for _ in range(words):
        parts += [tone(0.6 / words_per_second * 1.0, 180, amp), np.zeros(int(16000 * 0.4 / words_per_second), np.float32)]
    return np.concatenate(parts), " ".join(["word"] * words)


def test_tone_is_judged_against_your_usual_voice(tmp_path: Path) -> None:
    from app.voice.tone import MIN_SAMPLES, ToneTracker, measure

    assert measure(tone(0.3), "hi") is None
    tracker = ToneTracker(tmp_path)
    for _ in range(MIN_SAMPLES):
        assert tracker.observe(*speech_like(2.5, 0.1)) is None
    assert tracker.observe(*speech_like(4.5, 0.25)) == "rushed"
    assert tracker.observe(*speech_like(1.2, 0.02)) == "tired"
    assert tracker.observe(*speech_like(2.6, 0.1)) is None
    assert ToneTracker(tmp_path).samples == MIN_SAMPLES + 3  # the averages are kept


def test_tone_hint_reaches_the_prompt() -> None:
    from datetime import datetime

    from app.ai.prompts import build_system_prompt
    from app.voice.tone import HINTS

    prompt = build_system_prompt(assistant_name="Jarvis", now=datetime(2026, 9, 24), tone=HINTS["rushed"])
    assert "in a hurry" in prompt


# ------------------------------------------------------------------ 9. DPAPI


class FakeDpapi:
    def protect(self, data: bytes) -> bytes:
        return b"P" + bytes(b ^ 0x5A for b in data)

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"P"):
            raise ValueError("bad")
        return bytes(b ^ 0x5A for b in data[1:])


@pytest.fixture()
def dpapi(monkeypatch):
    from app.security import protect

    monkeypatch.setattr(protect, "BACKEND", FakeDpapi())
    return protect


def test_secret_settings_are_written_encrypted(tmp_path: Path, dpapi) -> None:
    from app.config.env_writer import encrypt_plain_secrets, update_env_file
    from app.config.settings import load_settings

    env = tmp_path / ".env"
    env.write_text("HOME_ASSISTANT_TOKEN=typed-by-hand\nUSER_ADDRESS=sir\n", encoding="utf-8")
    assert encrypt_plain_secrets(env) == ["HOME_ASSISTANT_TOKEN"]
    text = env.read_text()
    assert "typed-by-hand" not in text and "dpapi:" in text and "USER_ADDRESS=sir" in text
    assert load_settings(env, environ={}).home_assistant_token == "typed-by-hand"
    settings = update_env_file(env, {"HOME_ASSISTANT_TOKEN": "new-token"})
    assert settings.home_assistant_token == "new-token" and "new-token" not in env.read_text()
    assert encrypt_plain_secrets(env) == []  # already encrypted


def test_files_are_encrypted_and_old_files_still_read(tmp_path: Path, dpapi) -> None:
    from app.remote.pairing import PairingStore
    from app.tools import phone

    ctx = ToolContext(Settings(), paths=SimpleNamespace(data_dir=tmp_path))
    (tmp_path / "contacts.json").write_text(json.dumps({"Ammu": "+8801712345678"}), encoding="utf-8")  # old, plain
    assert phone.load_contacts(ctx) == {"Ammu": "+8801712345678"}
    phone.save_contact(phone.SaveContactArgs(name="Rafi", number="01812345678"), ctx)
    raw = (tmp_path / "contacts.json").read_bytes()
    assert raw.startswith(dpapi.MAGIC) and b"Rafi" not in raw
    assert set(phone.load_contacts(ctx)) == {"Ammu", "Rafi"}
    store = PairingStore(tmp_path)
    device, key = store.pair(store.new_code(), "Pixel")
    raw = (tmp_path / "remote_devices.json").read_bytes()
    assert raw.startswith(dpapi.MAGIC) and b"Pixel" not in raw and PairingStore(tmp_path).check(device, key)


def test_encrypted_secret_elsewhere_gives_a_clear_error(tmp_path: Path, monkeypatch) -> None:
    from app.config.settings import ConfigError, load_settings
    from app.security import protect

    monkeypatch.setattr(protect, "BACKEND", None)
    monkeypatch.setattr(protect.sys, "platform", "linux")
    env = tmp_path / ".env"
    env.write_text("HOME_ASSISTANT_TOKEN=dpapi:" + base64.b64encode(b"xyz").decode() + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="HOME_ASSISTANT_TOKEN"):
        load_settings(env, environ={})


# ------------------------------------------------------------------ 10. signed updates


def test_updates_check_the_signature(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    import httpx

    from app.utils import updates

    payload = b"installer bytes"
    monkeypatch.setattr(updates, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
    info = updates.UpdateInfo("9.0.0", "https://example.com/JarvisSetup-9.0.0.exe", hashlib.sha256(payload).hexdigest(), "")
    for status, signed, required, ok in (("valid", True, False, True), ("unsigned", False, False, True),
                                         ("unsigned", True, False, False), ("unsigned", False, True, False),
                                         ("invalid", False, False, False)):
        monkeypatch.setattr(updates, "SIGNATURE_CHECK", lambda path, s=status: s)
        folder = tmp_path / f"{status}-{signed}-{required}"
        target = dict(info.__dict__, signed=signed)
        if ok:
            assert updates.download(updates.UpdateInfo(**target), folder, require_signature=required).is_file()
        else:
            with pytest.raises(updates.UpdateError, match="refused"):
                updates.download(updates.UpdateInfo(**target), folder, require_signature=required)
            assert not any(folder.iterdir())  # deleted


def test_tls_key_is_encrypted_at_rest_and_an_old_plain_key_is_migrated(tmp_path):
    from cryptography.hazmat.primitives import serialization

    from app.remote.tls import ensure_certificate, fingerprint, server_context

    cert, key = ensure_certificate(tmp_path, "192.168.1.30")
    assert b"ENCRYPTED" in key.read_bytes()[:60] and (tmp_path / "remote-key.pass").is_file()
    server_context(cert, key)
    before = fingerprint(cert)
    plain = serialization.load_pem_private_key(key.read_bytes(), password=(tmp_path / "remote-key.pass").read_bytes())
    key.write_bytes(plain.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    assert b"ENCRYPTED" not in key.read_bytes()[:60]                      # what an older version left on disk
    ensure_certificate(tmp_path, "192.168.1.30")
    assert b"ENCRYPTED" in key.read_bytes()[:60] and fingerprint(cert) == before   # migrated, same certificate (phone pin still valid)
    server_context(cert, key)
