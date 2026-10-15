"""Bangla + English, the neural voice, and the housekeeping/news tools (v1.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from app.agent.language import (
    bangla_greeting, detect_language, is_bangla_dismissal, is_bangla_presence, localize, strip_bangla_wake,
    to_english_command,
)
from app.ai.prompts import build_system_prompt
from app.config.settings import Settings
from app.tools import housekeeping, winapi
from app.tools.base import NoArgs, ToolContext, ToolError
from app.tools.housekeeping import NewsArgs, OrganizeArgs, empty_recycle_bin, get_news, organize_desktop, parse_rss
from app.tts.neural import NeuralTTS, prosody
from app.tts.base import AudioClip, TTSEngine
from app.tts.text import SentenceSplitter
from app.voice.hands_free import is_dismissal, is_presence_check, wake_greeting
from app.voice.speech_to_text import FasterWhisperSTT, choose_language
from app.voice.wake_word import strip_wake_phrase
from tests.test_agent_loop import NOW
from tests.test_stt import FakeWhisper, seg
from tests.test_upgrade import _conversation, _send

# ------------------------------------------------------------------ language

def speech(n: int = 16000):
    """Audible test audio (pure silence is skipped before recognition)."""
    return (0.1 * np.sin(np.arange(n) / 3)).astype(np.float32)



@pytest.mark.parametrize(("text", "lang"), [
    ("ক্রোম খোলো", "bn"), ("chrome kholo", "bn"), ("koyta baje ekhon", "bn"), ("open chrome", "en"),
    ("what time is it", "en"), ("Chrome খোলো", "bn"), ("", "en"), ("ki koro", "bn"), ("ok cool", "en"),
])
def test_detect_language(text: str, lang: str) -> None:
    assert detect_language(text) == lang


@pytest.mark.parametrize(("bangla", "english"), [
    ("ক্রোম খোলো", "open chrome"), ("জার্ভিস, ইউটিউব খুলে দাও", "open youtube"),
    ("ফোন লিংক বন্ধ করো", "close phone link"), ("ক্রোম আর স্পটিফাই খোলো", "open chrome and open spotify"),
    ("এখন কয়টা বাজে?", "what time is it"), ("ব্যাটারি কত", "how much battery do i have"),
    ("আবহাওয়া কেমন", "what's the weather"), ("পিসি লক করো", "lock my pc"),
    ("কম্পিউটার বন্ধ করো", "shut down my pc"), ("রিস্টার্ট করো", "restart my pc"), ("ভলিউম বাড়াও", "volume up"),
    ("গান থামাও", "pause"), ("পরের গান", "next song"), ("ডেস্কটপের সব ফাইল গুছিয়ে দাও", "organize my desktop"),
    ("রিসাইকেল বিন খালি করো", "empty the recycle bin"), ("আজকের খবর", "what's the news"),
    ("chrome kholo", "open chrome"), ("koyta baje", "what time is it"),
])
def test_bangla_commands(bangla: str, english: str) -> None:
    assert to_english_command(bangla) == english


def test_not_a_command() -> None:
    assert to_english_command("আমি আজ খুব খুশি") is None
    assert to_english_command("open chrome") is None


def test_localized_replies() -> None:
    assert localize("Opened Google Chrome. Closed Spotify.", "bn") == "Google Chrome খুলে দিয়েছি। Spotify বন্ধ করে দিয়েছি।"
    assert localize("It is 21:48 on Wednesday, 23 September 2026.", "bn") == "এখন সময় 21:48।"
    assert localize("Something new.", "bn") == "Something new."
    assert localize("Opened Chrome.", "en") == "Opened Chrome."


def test_bangla_wake_presence_and_goodbye() -> None:
    s = Settings(user_address="sir")
    assert strip_bangla_wake("জার্ভিস, ক্রোম খোলো") == ("ক্রোম খোলো", True)
    assert strip_wake_phrase("জার্ভিস, তুমি আছো?", "hey jarvis") == ("তুমি আছো?", True)
    assert is_bangla_presence("তুমি আছো?") and is_presence_check("তুমি আছো?", s)
    assert not is_presence_check("ক্রোম খোলো", s)
    assert is_bangla_dismissal("ধন্যবাদ, আর কিছু লাগবে না") and is_dismissal("ঠিক আছে, বিদায়", s)
    assert wake_greeting(s, "জার্ভিস তুমি আছো") == bangla_greeting("sir") and wake_greeting(s).startswith("Yes, sir")


def test_bangla_sentences_split_for_speech() -> None:
    splitter = SentenceSplitter()
    assert splitter.feed("আমি ক্রোম খুলে দিয়েছি। এখন আর কী করতে পারি? ") == ["আমি ক্রোম খুলে দিয়েছি।",
                                                                           "এখন আর কী করতে পারি?"]


def test_prompt_answers_in_the_users_language() -> None:
    prompt = build_system_prompt(assistant_name="Jarvis", now=NOW, reply_language="bn")
    assert "reply in Bangla" in prompt and "Bengali script" in prompt
    assert "reply in Bangla (Bengali script)." not in build_system_prompt(assistant_name="Jarvis", now=NOW)


def test_bangla_direct_command_answers_in_bangla() -> None:
    conv, calls, fake = _conversation()
    (reply,) = _send(conv, "ক্রোম আর নোটপ্যাড খোলো")
    assert calls == [("open", "chrome"), ("open", "notepad")]
    assert reply.text == "Chrome খুলে দিয়েছি। Notepad খুলে দিয়েছি।"
    assert conv.reply_language == "bn"


def test_settings_languages() -> None:
    s = Settings(voice_language="auto", voice_languages="EN, bn")
    assert s.allowed_languages == ("en", "bn")
    assert Settings().allowed_languages == ("en",)
    with pytest.raises(Exception):
        Settings(voice_languages="english!")


# ------------------------------------------------------------------ language detection limited to en/bn


class LangWhisper(FakeWhisper):
    """Detects Hindi first (accented Bangla often looks like it); Bangla when told to."""

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        lang = kwargs.get("language")
        if lang is None:
            info = SimpleNamespace(language="hi", all_language_probs=[("hi", 0.5), ("bn", 0.4), ("en", 0.1)])
            return iter([seg("क्रोम खोलो")]), info
        return iter([seg("ক্রোম খোলো" if lang == "bn" else "Chrome")]), SimpleNamespace(language=lang)


def test_auto_language_is_limited_to_the_users_languages(tmp_path: Path) -> None:
    model = {}

    def factory(name, **kw):
        model["m"] = LangWhisper([], **kw)
        return model["m"]

    stt = FasterWhisperSTT("large-v3-turbo", download_root=tmp_path, model_factory=factory, hotwords="Jarvis, Chrome",
                           allowed_languages=("en", "bn"))
    result = stt.transcribe(speech(), None)
    assert result.text == "ক্রোম খোলো" and result.language == "bn"
    calls = model["m"].calls
    assert calls[0]["language"] is None and calls[1]["language"] == "bn"
    assert calls[1]["hotwords"] is None  # English hints are not used for Bangla


def test_choose_language() -> None:
    assert choose_language(SimpleNamespace(language="en"), ("en", "bn")) == "en"
    assert choose_language(SimpleNamespace(language="ur", all_language_probs=[("ur", .6), ("en", .3)]), ("en", "bn")) == "en"
    assert choose_language(SimpleNamespace(language="xx"), ("en", "bn")) == "en"


# ------------------------------------------------------------------ neural voice


class Offline(TTSEngine):
    name = "offline"

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def load(self) -> None:
        pass

    def synthesize(self, text: str, speed: float = 1.0) -> AudioClip:
        self.spoken.append(text)
        return AudioClip(np.ones(100, np.float32), 22050)


def test_neural_voice_picks_the_voice_by_language() -> None:
    used: list[tuple[str, str, str, str]] = []

    def mp3(text, voice, rate, pitch):
        used.append((text, voice, rate, pitch))
        return b"mp3"

    tts = NeuralTTS("en-GB-RyanNeural", "bn-BD-PradeepNeural", synthesize_mp3=mp3,
                    decoder=lambda data: np.ones(2400, np.float32))
    tts.load()
    assert tts.synthesize("Done, sir!").samples.size == 2400
    tts.synthesize("ক্রোম খুলে দিয়েছি।")
    assert [u[1] for u in used] == ["en-GB-RyanNeural", "bn-BD-PradeepNeural"]
    assert used[0][2:] == ("+4%", "+4Hz")  # brighter for an exclamation


def test_neural_voice_falls_back_offline() -> None:
    def down(*args):
        raise OSError("no internet")

    offline = Offline()
    tts = NeuralTTS("en-GB-RyanNeural", "bn-BD-PradeepNeural", fallback=offline, synthesize_mp3=down)
    tts.load()
    assert tts.synthesize("Hello there.").sample_rate == 22050 and offline.spoken == ["Hello there."]
    assert tts.synthesize("হ্যালো।").samples.size == 0  # no offline Bangla voice: shown, not spoken
    assert offline.spoken == ["Hello there."]


def test_prosody() -> None:
    assert prosody("Sorry, I couldn't do that.") == ("-6%", "-3Hz")
    assert prosody("All set.", speed=1.1) == ("+10%", "+0Hz")


def test_create_engine_neural(tmp_path: Path, monkeypatch) -> None:
    from app.tts import manager

    monkeypatch.setattr(manager, "create_engine", manager.create_engine)
    engine, problem = manager.create_engine(Settings(tts_engine="neural"), tmp_path)
    assert problem is None and engine.name == "neural"


# ------------------------------------------------------------------ organize desktop


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(Settings(), paths=SimpleNamespace(data_dir=tmp_path / "data"))


def test_organize_and_undo(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    for name in ("photo.jpg", "report.pdf", "song.mp3", "setup.exe", "notes.txt", "weird.xyz", "Chrome.lnk"):
        (desktop / name).write_text(name)
    (desktop / "Projects").mkdir()
    ctx = _ctx(tmp_path)
    result = organize_desktop(OrganizeArgs(), ctx, desktop=desktop)
    assert result.ok and result.data["moved"] == 6
    root = desktop / "Organized Desktop"
    assert (root / "Images" / "photo.jpg").exists() and (root / "Documents" / "notes.txt").exists()
    assert (root / "Others" / "weird.xyz").exists() and (desktop / "Chrome.lnk").exists() and (desktop / "Projects").is_dir()
    assert "Say 'undo organize'" in result.message
    again = organize_desktop(OrganizeArgs(), ctx, desktop=desktop)
    assert again.data["moved"] == 0 and "already tidy" in again.message
    undone = organize_desktop(OrganizeArgs(undo=True), ctx, desktop=desktop)
    assert undone.message == "Put 6 files back on your Desktop."
    assert (desktop / "photo.jpg").read_text() == "photo.jpg" and not root.exists()
    with pytest.raises(ToolError, match="no organize to undo"):
        organize_desktop(OrganizeArgs(undo=True), ctx, desktop=desktop)


def test_organize_keeps_both_files_with_the_same_name(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    (desktop / "Organized Desktop" / "Images").mkdir(parents=True)
    (desktop / "Organized Desktop" / "Images" / "a.png").write_text("old")
    (desktop / "a.png").write_text("new")
    organize_desktop(OrganizeArgs(), _ctx(tmp_path), desktop=desktop)
    assert (desktop / "Organized Desktop" / "Images" / "a (2).png").read_text() == "new"


# ------------------------------------------------------------------ recycle bin


def test_recycle_bin_is_dangerous_and_always_asks(monkeypatch) -> None:
    from app.tools.catalog import build_default_registry
    from app.security.permissions import RiskLevel

    tool = build_default_registry().get("empty_recycle_bin")
    assert tool.level == RiskLevel.DANGEROUS and tool.direct_without_confirmation is None
    monkeypatch.setattr(winapi, "recycle_bin_info", lambda: (31, 1288490188))
    assert "31 item(s), 1.2 GB" in tool.confirmation_text(NoArgs(), ToolContext(Settings()))
    emptied: list[int] = []
    monkeypatch.setattr(winapi, "empty_recycle_bin", lambda: emptied.append(1))
    assert empty_recycle_bin(NoArgs(), ToolContext(Settings())).message == \
        "Emptied the Recycle Bin: 31 item(s), 1.2 GB freed."
    monkeypatch.setattr(winapi, "recycle_bin_info", lambda: (0, 0))
    assert empty_recycle_bin(NoArgs(), ToolContext(Settings())).message == "The Recycle Bin is already empty."
    assert emptied == [1]


# ------------------------------------------------------------------ news

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Big match tonight - Daily Star</title><link>https://x/1</link><source>Daily Star</source></item>
<item><title>Rain expected in Dhaka - Prothom Alo</title><link>https://x/2</link><source>Prothom Alo</source></item>
<item><title>New phone launched</title><link>https://x/3</link></item>
<item><title>Fourth story</title></item>
</channel></rss>"""


def test_news(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=RSS)

    monkeypatch.setattr(housekeeping, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(housekeeping, "_NEWS_CACHE", {})
    result = asyncio.run(get_news(NewsArgs(language="bn", topic="cricket"), ToolContext(Settings())))
    assert result.message.startswith("সেরা খবরগুলো: 1. Big match tonight (Daily Star). 2. Rain expected in Dhaka")
    assert len(result.data["headlines"]) == 4
    assert seen[0].url.path == "/rss/search" and seen[0].url.params["q"] == "cricket"
    assert seen[0].url.params["ceid"] == "BD:bn"


def test_parse_rss_errors() -> None:
    with pytest.raises(ToolError):
        parse_rss("not xml", 5)
    assert parse_rss("<rss><channel></channel></rss>", 5) == []


# ------------------------------------------------------------------ speed fixes


def test_thinking_only_models_never_show_their_reasoning() -> None:
    from app.ai.ollama import ThinkFilter, strip_think

    f = ThinkFilter(hidden_open=True)
    pieces = ["Okay, the user said Google Chrome Club. ", "Let me think...", "</thi", "nk>\n\nOpened Chrome", " for you."]
    assert "".join(f.feed(p) for p in pieces) + f.flush() == "Opened Chrome for you."
    f = ThinkFilter(hidden_open=True)
    assert f.feed("Hello there.") + f.flush() == "Hello there."  # a normal answer is not lost
    assert strip_think("long reasoning</think>\nAnswer") == "Answer"
    plain = ThinkFilter()
    assert plain.feed("Hi ") == "Hi "  # models without thinking stream immediately


@pytest.mark.parametrize(("heard", "command"), [
    ("Google Chrome Club", "open Google Chrome"), ("Chrome kolo", "open Chrome"),
    ("WhatsApp bondho koro", "close WhatsApp"), ("what is a book club", None),
])
def test_misheard_bangla_verbs(heard: str, command: str | None) -> None:
    from app.agent.language import misheard_bangla_command

    assert misheard_bangla_command(heard) == command


def test_misheard_command_runs_directly_and_answers_in_bangla() -> None:
    conv, calls, fake = _conversation()
    (reply,) = _send(conv, "Google Chrome Club")
    assert calls == [("open", "Google Chrome")] and reply.text == "Google Chrome খুলে দিয়েছি।"
