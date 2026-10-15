"""Jarvis 2.2: corrections, vocabulary, second pass, crash reports, tasks, documents, smart home, settings, privacy."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.config.settings import Settings, load_settings
from app.memory.database import Database
from app.security.permissions import RiskLevel
from app.tools.base import ToolContext, ToolError


def speech(n: int = 16000):
    return (0.1 * np.sin(np.arange(n) / 3)).astype(np.float32)


# ------------------------------------------------------------------ 1. corrections


def test_parse_correction() -> None:
    from app.voice.corrections import parse_correction

    assert parse_correction("no, I said Phone Link") == "Phone Link"
    assert parse_correction("No I meant Spotify.") == "Spotify"
    assert parse_correction("না, আমি বলেছি ফোন লিংক") == "ফোন লিংক"
    assert parse_correction("no thanks") is None
    assert parse_correction("open Chrome") is None
    assert parse_correction("no") is None


def test_find_misheard_picks_the_sound_alike_words() -> None:
    from app.voice.corrections import find_misheard

    assert find_misheard("close phone lane", "Phone Link") == "phone lane"
    assert find_misheard("open spot a fly", "Spotify") == "spot a fly"
    assert find_misheard("open Chrome", "Chrome") is None  # nothing was wrong
    assert find_misheard("what time is it", "Visual Studio Code") is None


def test_corrections_are_learned_applied_and_forgotten(tmp_path: Path) -> None:
    from app.voice.corrections import Corrections

    store = Corrections(tmp_path)
    store.learn("phone lane", "Phone Link")
    assert Corrections(tmp_path).apply("close PHONE LANE now") == "close Phone Link now"  # saved to disk
    assert Corrections(tmp_path).apply("telephone laneway") == "telephone laneway"  # whole words only
    assert store.vocabulary() == ["Phone Link"]
    assert json.loads((tmp_path / "corrections.json").read_text(encoding="utf-8")) == {"phone lane": "Phone Link"}
    assert store.forget() == 1 and Corrections(tmp_path).apply("phone lane") == "phone lane"


def test_conversation_redoes_the_corrected_request(tmp_path: Path) -> None:
    from app.agent.conversation import ConversationManager
    from app.voice.corrections import Corrections

    conv = ConversationManager(None, Settings(), None)
    conv._corrections = lambda: store  # type: ignore[method-assign]
    store = Corrections(tmp_path)
    conv._last_user_text = "close phone lane"
    text, note = conv._apply_corrections("no, I said Phone Link")
    assert text == "close Phone Link" and note == "Got it, Phone Link. I'll remember that."
    conv._last_user_text = "what's up"
    assert conv._apply_corrections("open phone lane") == ("open Phone Link", "")  # learned for next time
    conv._last_user_text = "close phone lane"
    assert conv._apply_corrections("না, আমি বলেছি Phone Link")[1] == "বুঝেছি, মনে রাখব।"


# ------------------------------------------------------------------ 2. vocabulary


def test_custom_vocabulary_and_corrections_come_first(tmp_path: Path) -> None:
    from app.cli_voice import vocabulary
    from app.voice.corrections import Corrections

    Corrections(tmp_path).learn("fluxed", "FLUX IT")
    apps = SimpleNamespace(applications={"c": SimpleNamespace(display_name="Chrome", enabled=True)})
    ctx = SimpleNamespace(settings=Settings(custom_vocabulary="Labib; Dhanmondi, "), paths=SimpleNamespace(data_dir=tmp_path),
                          config=SimpleNamespace(apps=apps))
    words = vocabulary(ctx)
    assert words[:4] == ["Labib", "Dhanmondi", "FLUX IT", "Chrome"]
    with pytest.raises(Exception):
        Settings(custom_vocabulary="x" * 1501)


# ------------------------------------------------------------------ 3. second pass


class Whisper:
    def __init__(self, passes):
        self.passes, self.calls = list(passes), []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        text, logprob = self.passes[min(len(self.calls), len(self.passes)) - 1]
        return iter([SimpleNamespace(text=text, avg_logprob=logprob, no_speech_prob=0.01)]), \
            SimpleNamespace(language=kwargs.get("language") or "en")


def _stt(tmp_path, passes, device="cuda", **kwargs):
    from app.voice.speech_to_text import FasterWhisperSTT

    model = Whisper(passes)
    stt = FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, model_factory=lambda name, **kw: model,
                           device=device, compute_type="int8_float16", background_upgrade=False, **kwargs)
    return stt, model


def test_unsure_speech_is_decoded_again_more_carefully(tmp_path: Path) -> None:
    stt, model = _stt(tmp_path, [("open crow", -1.2), ("open Chrome", -0.2)])
    result = stt.transcribe(speech(), language="en")
    assert result.text == "open Chrome" and len(model.calls) == 2
    assert model.calls[1]["beam_size"] == 10 and model.calls[1]["patience"] == 2.0


def test_second_pass_keeps_the_better_result_and_can_be_off(tmp_path: Path) -> None:
    stt, model = _stt(tmp_path, [("open crow", -1.0), ("oh pen", -2.0)])
    assert stt.transcribe(speech(), language="en").text == "open crow"
    sure, model = _stt(tmp_path, [("open Chrome", -0.1)])
    sure.transcribe(speech(), language="en")
    assert len(model.calls) == 1  # confident: no second pass
    off, model = _stt(tmp_path, [("open crow", -1.2), ("open Chrome", -0.2)], second_pass=False)
    assert off.transcribe(speech(), language="en").text == "open crow" and len(model.calls) == 1
    cpu, model = _stt(tmp_path, [("open crow", -1.2), ("open Chrome", -0.2)], device="cpu")
    cpu.transcribe(speech(), language="en")
    assert len(model.calls) == 1  # too slow on the CPU


# ------------------------------------------------------------------ 4. crash reports


def test_crash_marker_and_report(tmp_path: Path) -> None:
    from app.main import bootstrap
    from app.utils import crash

    ctx = bootstrap(tmp_path, environ={})
    assert crash.start(ctx) is None and (ctx.paths.data_dir / crash.MARKER).exists()
    crash.stop(ctx)
    assert not (ctx.paths.data_dir / crash.MARKER).exists()
    assert crash.start(ctx) is None  # clean exit last time: no report
    report = crash.start(ctx)  # the last run never called stop(): it crashed
    crash.stop(ctx)
    assert report is not None and report.parent.name == "crash-reports"
    with zipfile.ZipFile(report) as archive:
        assert "previous_run.txt" in archive.namelist()


# ------------------------------------------------------------------ 5. multi-step tasks + documents


def test_is_task_and_the_plan_prompt() -> None:
    from app.agent.conversation import is_task
    from app.ai.prompts import build_system_prompt

    assert is_task("research the best laptops under 1000 dollars and make a document")
    assert is_task("compare Python and Go for web servers and save it")
    assert not is_task("open Chrome")
    assert not is_task("compare")  # too short
    from datetime import datetime

    from app.tools.catalog import build_default_registry

    specs = build_default_registry().specs()[:3]
    plan = build_system_prompt(assistant_name="Jarvis", now=datetime(2026, 9, 24), tools=specs, task=True)
    plain = build_system_prompt(assistant_name="Jarvis", now=datetime(2026, 9, 24), tools=specs)
    assert "numbered plan" in plan and "numbered plan" not in plain


def test_write_document_docx_and_markdown(tmp_path: Path) -> None:
    from app.tools.documents import DocumentArgs, write_document

    content = "# Summary\n- **fast** and cheap\n| Name | Price |\n|---|---|\n| A | 10 |\nPlain line"
    ctx = ToolContext(Settings())
    result = write_document(DocumentArgs(title="Laptops: best/cheap", content=content, open_after=False), ctx, tmp_path)
    path = Path(result.data["path"])
    assert path.suffix == ".docx" and path.parent == tmp_path and "/" not in path.stem
    with zipfile.ZipFile(path) as archive:
        body = archive.read("word/document.xml").decode()
        assert "[Content_Types].xml" in archive.namelist()
    assert "Heading1" in body and "<w:tbl>" in body and "<w:b/>" in body and "Plain line" in body
    again = write_document(DocumentArgs(title="Laptops: best/cheap", content="x", format="md", open_after=False), ctx,
                           tmp_path)
    assert Path(again.data["path"]).read_text(encoding="utf-8").startswith("# Laptops: best/cheap")
    third = write_document(DocumentArgs(title="Laptops: best/cheap", content="y", open_after=False), ctx, tmp_path)
    assert third.data["path"] != result.data["path"]  # never overwrites


# ------------------------------------------------------------------ 6. smart home


STATES = [
    {"entity_id": "fan.bedroom_fan", "state": "off", "attributes": {"friendly_name": "Bedroom Fan"}},
    {"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk Lamp"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage", "device_class": "garage"}},
    {"entity_id": "sensor.temp", "state": "28", "attributes": {"friendly_name": "Temperature"}},
]


@pytest.fixture()
def home(monkeypatch):
    from app.tools import smart_home

    calls: list[tuple[str, str, dict | None, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body, request.headers.get("authorization", "")))
        if request.headers.get("authorization") != "Bearer good-token":
            return httpx.Response(401)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=STATES)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(smart_home, "TRANSPORT", httpx.MockTransport(handler))
    smart_home._CACHE.clear()
    ctx = ToolContext(Settings(home_assistant_url="http://192.168.1.10:8123/", home_assistant_token="good-token"))
    yield smart_home, ctx, calls
    smart_home._CACHE.clear()


def test_home_devices_are_found_by_sound(home) -> None:
    smart_home, ctx, _ = home
    assert smart_home.find_entity(ctx, "bedroom fan")["entity_id"] == "fan.bedroom_fan"
    assert smart_home.find_entity(ctx, "the desk")["entity_id"] == "light.desk"
    assert smart_home.find_entity(ctx, "bedroom fen")["entity_id"] == "fan.bedroom_fan"
    with pytest.raises(ToolError, match="can't find"):
        smart_home.find_entity(ctx, "microwave")
    assert all(not e["entity_id"].startswith("sensor.") for e in smart_home.entities(ctx))


def test_home_control_calls_the_right_service(home) -> None:
    smart_home, ctx, calls = home
    result = smart_home.home_control(smart_home.HomeArgs(device="bedroom fan", action="on"), ctx)
    assert result.ok and "Bedroom Fan is on" in result.message
    assert calls[-1][:3] == ("POST", "/api/services/fan/turn_on", {"entity_id": "fan.bedroom_fan"})
    smart_home.home_control(smart_home.HomeArgs(device="front door", action="off"), ctx)
    assert calls[-1][1] == "/api/services/lock/lock"
    smart_home.home_control(smart_home.HomeArgs(device="garage", action="on"), ctx)
    assert calls[-1][1] == "/api/services/cover/open_cover"


def test_locks_and_garage_doors_always_ask(home) -> None:
    smart_home, ctx, _ = home
    tool = next(t for t in smart_home.TOOLS if t.name == "home_control")
    lamp, lock = smart_home.HomeArgs(device="desk lamp", action="off"), smart_home.HomeArgs(device="front door", action="on")
    garage = smart_home.HomeArgs(device="garage", action="on")
    assert smart_home._risk(lamp, ctx) == RiskLevel.MODERATE and tool.direct_without_confirmation(lamp, ctx)
    assert smart_home._risk(lock, ctx) == RiskLevel.DANGEROUS and not tool.direct_without_confirmation(lock, ctx)
    assert smart_home._risk(garage, ctx) == RiskLevel.DANGEROUS


def test_home_errors_are_clear_and_the_token_is_secret(home) -> None:
    smart_home, _, _ = home
    with pytest.raises(ToolError, match="isn't set up"):
        smart_home.entities(ToolContext(Settings()))
    bad = ToolContext(Settings(home_assistant_url="http://192.168.1.10:8123", home_assistant_token="old"))
    with pytest.raises(ToolError, match="refused the access token"):
        smart_home.entities(bad)
    settings = Settings(home_assistant_token="super-secret-token")
    assert "super-secret-token" not in repr(settings)
    assert settings.public_dict()["home_assistant_token"] == "[REDACTED]"


def test_turn_off_the_fan_runs_directly_once_devices_are_known(home) -> None:
    from app.agent.shortcuts import match_shortcut

    smart_home, ctx, _ = home
    known = lambda name: smart_home.known_device(ctx, name)  # noqa: E731
    assert match_shortcut("turn off the bedroom fan", lambda n: None, known_device=known) is None  # nothing cached yet
    smart_home.prefetch(ctx)
    call = match_shortcut("turn off the bedroom fan", lambda n: None, known_device=known)
    assert call is not None and call.name == "home_control" and call.arguments == {"device": "Bedroom Fan", "action": "off"}
    call = match_shortcut("switch the desk lamp on please", lambda n: None, known_device=known)
    assert call.arguments == {"device": "Desk Lamp", "action": "on"}
    unknown = match_shortcut("turn off the computer", lambda n: None, known_device=known)
    assert unknown is None or unknown.name == "power_control"


# ------------------------------------------------------------------ 7. vision model in the wizard


def test_wizard_offers_the_vision_model() -> None:
    from app.ui import setup_wizard

    assert setup_wizard.RECOMMENDED_VISION_MODEL == "qwen2.5vl:3b"


# ------------------------------------------------------------------ 8/9. settings search + privacy


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except (ImportError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Qt is not available here: {exc}")
    yield QApplication.instance() or QApplication([])


def test_settings_search_filters_fields(qapp, tmp_path: Path) -> None:
    from app.ui.settings_dialog import TABS, SettingsDialog, matches

    dialog = SettingsDialog(load_settings(None, environ={}), tmp_path / ".env")
    assert dialog.sections.count() == len(TABS) and dialog.pages.count() == len(TABS)
    total = sum(len(specs) for _, specs in TABS)
    assert dialog.filter("") == total
    found = dialog.filter("home assistant")
    assert found == 2
    assert dialog.pages.currentIndex() == [name for name, _ in TABS].index("Smart home")
    assert dialog.filter("HOME_ASSISTANT_TOKEN") == 1  # the .env name works too
    assert dialog.filter("zzzz nothing") == 0
    assert dialog.filter("") == total
    token = dialog.widgets["home_assistant_token"]
    from PySide6.QtWidgets import QLineEdit

    assert isinstance(token, QLineEdit) and token.echoMode() == QLineEdit.EchoMode.Password
    spec = next(s for _, specs in TABS for s in specs if s.name == "custom_vocabulary")
    assert matches(spec, "words recognition") and not matches(spec, "gpu browser")


def _ctx(tmp_path: Path):
    from app.main import bootstrap

    return bootstrap(tmp_path, environ={})


def test_privacy_inventory_preview_and_erase(tmp_path: Path) -> None:
    from app import privacy

    ctx = _ctx(tmp_path)
    with ctx.database.connect() as conn:
        conn.execute("INSERT INTO usage_events (name, day, weekday, minute) VALUES ('Chrome', '2026-09-01', 1, 600)")
        conn.execute("INSERT INTO semantic_items (kind, source, text, created_at) VALUES "
                     "('history', 'chat', 'we talked about Dhaka', '2026-09-01')")
    data = Path(ctx.paths.data_dir)
    (data / "contacts.json").write_text(json.dumps({"Ammu": "+8801712345678"}), encoding="utf-8")
    (data / "benchmark").mkdir(exist_ok=True)
    (data / "benchmark" / "01.wav").write_bytes(b"RIFF" + b"0" * 100)
    items = {i.key: i for i in privacy.inventory(ctx)}
    assert {"memories", "history", "documents", "habits", "activity", "contacts", "corrections", "voiceprint",
            "benchmark", "crash_reports", "logs"} <= set(items)
    assert items["habits"].count == 1 and items["contacts"].count == 1 and items["benchmark"].count == 1
    assert items["history"].count >= 1 and items["voiceprint"].count == 0 and items["voiceprint"].summary == "nothing stored"
    assert "Chrome: 1 times" in privacy.preview(ctx, "habits")
    assert "Ammu" in privacy.preview(ctx, "contacts") and "01.wav" in privacy.preview(ctx, "benchmark")
    assert "Dhaka" in privacy.preview(ctx, "history")
    for key in ("habits", "history", "contacts", "benchmark"):
        privacy.erase(ctx, key)
    items = {i.key: i for i in privacy.inventory(ctx)}
    assert all(items[k].count == 0 for k in ("habits", "history", "contacts", "benchmark"))
    with pytest.raises(KeyError):
        privacy.erase(ctx, "everything")


def test_privacy_forgets_paired_phones(tmp_path: Path, monkeypatch) -> None:
    from app import privacy
    from app.remote import pairing

    monkeypatch.setattr(pairing.PairingStore, "__init__", _plain_store_init(pairing.PairingStore.__init__))
    ctx = _ctx(tmp_path)
    store = pairing.PairingStore(ctx.paths.data_dir)
    device, key = store.pair(store.new_code(), "Phone")
    privacy.renew_remote_key(ctx)
    assert pairing.PairingStore(ctx.paths.data_dir).check(device, key) is None


def _plain_store_init(original):
    def init(self, data_dir, clock=None, encrypt=False):
        import time

        original(self, data_dir, clock or time.time, encrypt=False)
    return init


def test_privacy_dialog_deletes_after_confirming(qapp, tmp_path: Path) -> None:
    from app.ui.privacy_dialog import PrivacyDialog

    ctx = _ctx(tmp_path)
    (Path(ctx.paths.data_dir) / "corrections.json").write_text(json.dumps({"phone lane": "Phone Link"}), encoding="utf-8")
    changed: list[str] = []
    answers = [False, True]
    dialog = PrivacyDialog(ctx, on_changed=changed.append, confirm=lambda text: answers.pop(0))
    assert dialog.delete_buttons["corrections"].isEnabled() and not dialog.delete_buttons["voiceprint"].isEnabled()
    dialog.show_preview("corrections")
    assert "phone lane  ->  Phone Link" in dialog.preview.toPlainText()
    dialog.delete_buttons["corrections"].click()  # "no"
    assert (Path(ctx.paths.data_dir) / "corrections.json").exists() and changed == []
    dialog.delete_buttons["corrections"].click()  # "yes"
    assert not (Path(ctx.paths.data_dir) / "corrections.json").exists() and changed == ["corrections"]
    assert not dialog.delete_buttons["corrections"].isEnabled()


def test_database_is_the_privacy_source(tmp_path: Path) -> None:
    database = Database(tmp_path / "x.db")
    database.initialize()
    from app.privacy import TABLES

    with database.connect() as conn:
        for table in TABLES.values():
            conn.execute(table.sql_count).fetchone()
            conn.execute(table.sql_preview, (5,)).fetchall()
