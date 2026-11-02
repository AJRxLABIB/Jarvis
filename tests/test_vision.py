"""Phase 12: screen capture, OCR and vision tools (with fake capture backends)."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from app.agent.tool_manager import ToolExecutor
from app.ai.base import ChatMessage, ChatResponse, ModelInfo, ModelNotFoundError, ToolCall
from app.ai.manager import AIManager, can_see_images
from app.ai.ollama import _message_to_wire
from app.config.files import PermissionsConfig
from app.config.settings import Settings
from app.security.permissions import PermissionEngine
from app.security.validator import PathPolicy
from app.tools import winapi
from app.tools.base import ToolContext, ToolError
from app.tools.catalog import build_default_registry
from app.tools.vision import MAX_TEXT_CHARS, DescribeArgs, ScreenArgs
from app.tools.winapi import WindowInfo
from app.vision import capture as cap
from app.vision import ocr
from app.vision.ocr import OcrLine, OcrResult, TesseractOcr, create_ocr, enlarge_factor, tesseract_language
from tests.fake_ollama import FakeOllama, model_entry

OTHER_PID = os.getpid() + 1000


def window(hwnd: int, title: str, process: str, pid: int = OTHER_PID, **flags) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, pid=pid, title=title, process_name=process, **flags)


@dataclass
class FakeBackend:
    windows_list: list[WindowInfo] = field(default_factory=list)
    foreground_hwnd: int | None = None
    console: int | None = None
    printed: Image.Image | None = None
    grabs: list = field(default_factory=list)

    def windows(self):
        return list(self.windows_list)

    def foreground(self):
        return self.foreground_hwnd

    def console_window(self):
        return self.console

    def print_window(self, hwnd):
        return self.printed

    def window_rect(self, hwnd):
        return (10, 20, 210, 120)

    def grab(self, bbox):
        self.grabs.append(bbox)
        size = (200, 100) if bbox else (400, 300)
        return Image.new("RGB", size, (200, 220, 240))

    def monitors(self):
        return [cap.Monitor(1, 0, 0, 1920, 1080, True), cap.Monitor(2, 1920, 0, 1280, 1024)]


@pytest.fixture()
def backend(monkeypatch) -> FakeBackend:
    fake = FakeBackend(
        windows_list=[window(1, "Assistant", "python.exe", pid=os.getpid()),
                      window(2, "Issue #12 - GitHub - Google Chrome", "chrome.exe"),
                      window(3, "Untitled - Notepad", "notepad.exe", minimized=True)],
        foreground_hwnd=1, printed=Image.new("RGB", (300, 200), (255, 255, 255)))
    monkeypatch.setattr(cap, "backend", fake)
    monkeypatch.setattr(winapi, "top_level_windows", fake.windows)
    return fake


def context(tmp_path: Path, **settings) -> ToolContext:
    values = {"screen_capture_enabled": True, "screenshot_dir": tmp_path / "shots", **settings}
    ctx = ToolContext(Settings(**values))
    ctx.path_policy = PathPolicy.build(PermissionsConfig(), known={"PICTURES": tmp_path / "Pictures", "DOWNLOADS": tmp_path / "Downloads"},
                                       protected=[tmp_path / "protected"])
    return ctx


def execute(ctx: ToolContext, tool: str, **arguments):
    executor = ToolExecutor(build_default_registry(), PermissionEngine(PermissionsConfig()), ctx)
    return asyncio.run(executor.execute(ToolCall(tool, arguments))).result


@dataclass
class FakeOcr:
    lines: list[str]
    seen: list = field(default_factory=list)
    name = "fake"

    def problem(self):
        return None

    async def recognize(self, image):
        self.seen.append(image.size)
        return OcrResult(tuple(OcrLine(text) for text in self.lines), "fake", "en-US")


# ----------------------------------------------------------------- arguments


@pytest.mark.parametrize(("target", "expected"), [
    ("full screen", ("screen", None)), ("entire desktop", ("screen", None)), ("active window", ("window", None)),
    ("chrome", ("window", "chrome")), ("whatsapp window", ("window", "whatsapp")), ("none", ("window", None)),
])
def test_loose_targets(target, expected) -> None:
    args = ScreenArgs.model_validate({"target": target})
    assert (args.target, args.window) == expected


def test_arguments_are_strict() -> None:
    with pytest.raises(ValueError):
        ScreenArgs.model_validate({"target": "window", "command": "del"})
    with pytest.raises(ValueError):
        ScreenArgs.model_validate({"monitor": 12})
    assert DescribeArgs.model_validate({"prompt": "what is this?"}).question == "what is this?"


# ------------------------------------------------------------------ opt-in


def test_everything_is_off_until_enabled(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path, screen_capture_enabled=False)
    for tool in ("take_screenshot", "read_screen", "describe_screen"):
        result = execute(ctx, tool)
        assert result.error == "disabled" and "Allow screen capture" in result.message
    assert backend.grabs == [] and not (tmp_path / "shots").exists()


def test_off_by_default() -> None:
    assert Settings().screen_capture_enabled is False


# ------------------------------------------------------------------ targets


def test_active_window_skips_the_assistant_itself(backend: FakeBackend) -> None:
    assert cap.active_window().hwnd == 2  # foreground is our own window: next window in z-order
    backend.foreground_hwnd = 2
    assert cap.active_window().hwnd == 2
    backend.windows_list = [window(1, "Assistant", "python.exe", pid=os.getpid())]
    with pytest.raises(ToolError):
        cap.active_window()


def test_console_window_is_skipped(backend: FakeBackend) -> None:
    backend.windows_list = [window(7, "Windows PowerShell", "windowsterminal.exe"),
                            window(2, "Docs - Microsoft Edge", "msedge.exe")]
    backend.foreground_hwnd, backend.console = 7, 7
    assert cap.active_window().hwnd == 2


def test_minimised_named_window_is_refused(tmp_path: Path, backend: FakeBackend) -> None:
    result = execute(context(tmp_path), "read_screen", window="notepad")
    assert result.error == "minimized"
    assert execute(context(tmp_path), "read_screen", window="zzz-no-such-app").error == "not_found"


def test_black_print_window_falls_back_to_screen_copy(tmp_path: Path, backend: FakeBackend) -> None:
    backend.printed = Image.new("RGB", (300, 200), (0, 0, 0))
    shot = cap.capture(context(tmp_path))
    assert backend.grabs == [(10, 20, 210, 120)] and shot.size == (200, 100)
    assert shot.source == "chrome (Issue #12 - GitHub - Google Chrome)"


def test_monitor_choice(tmp_path: Path, backend: FakeBackend) -> None:
    shot = cap.capture(context(tmp_path), target="screen", monitor=2)
    assert backend.grabs[-1] == (1920, 0, 3200, 1024) and shot.source == "monitor 2"
    with pytest.raises(ToolError, match="no monitor 3"):
        cap.capture(context(tmp_path), monitor=3)
    cap.capture(context(tmp_path), target="screen")
    assert backend.grabs[-1] is None


# ------------------------------------------------------------------ privacy


def test_blocked_window_is_never_captured(tmp_path: Path, backend: FakeBackend) -> None:
    backend.windows_list.append(window(9, "Passwords - KeePass", "keepass.exe"))
    ctx = context(tmp_path)
    result = execute(ctx, "read_screen", window="keepass")
    assert result.error == "blocked_window" and "KeePass" in result.message
    assert execute(ctx, "take_screenshot", target="screen").error == "blocked_window"  # it's on screen
    assert backend.grabs == []
    assert execute(ctx, "take_screenshot").ok  # the active Chrome window is fine


def test_minimised_blocked_window_does_not_block_full_screen(tmp_path: Path, backend: FakeBackend) -> None:
    backend.windows_list.append(window(9, "Bitwarden", "bitwarden.exe", minimized=True))
    assert execute(context(tmp_path), "take_screenshot", target="screen").ok


def test_blocked_list_is_configurable(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path, screen_blocked_windows="github")
    assert execute(ctx, "read_screen").error == "blocked_window"


# --------------------------------------------------------------- screenshot


def test_take_screenshot_saves_png_and_shows_indicator(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path)
    events: list[bool] = []
    ctx.on_screen_capture = events.append
    result = execute(ctx, "take_screenshot")
    assert result.ok and events == [True, False]
    path = Path(result.data["path"])
    assert path.parent == (tmp_path / "shots").resolve() and path.name.startswith("Screenshot ")
    assert path.name.endswith(" chrome.png")
    with Image.open(path) as saved:
        assert saved.size == (300, 200)
    second = execute(ctx, "take_screenshot")
    assert Path(second.data["path"]) != path  # never overwrites


def test_indicator_turns_off_after_a_failure(tmp_path: Path, backend: FakeBackend) -> None:
    backend.windows_list = []
    ctx = context(tmp_path)
    events: list[bool] = []
    ctx.on_screen_capture = events.append
    assert execute(ctx, "take_screenshot").error == "no_window"
    assert events == [True, False]


def test_default_folder_and_protected_folder(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path, screenshot_dir=None)
    assert cap.screenshot_folder(ctx) == Path(os.path.realpath(tmp_path / "Downloads"))
    ctx = context(tmp_path, screenshot_dir=tmp_path / "protected" / "x")
    assert execute(ctx, "take_screenshot").error == "protected_path"


# ------------------------------------------------------------------ reading


def test_read_screen_redacts_and_marks_untrusted(tmp_path: Path, backend: FakeBackend, monkeypatch) -> None:
    engine = FakeOcr(["Settings", "api_key=sk-abcdefghijklmnopqrstuvwxyz123456", "Card 4111 1111 1111 1111"])
    monkeypatch.setattr("app.tools.vision.create_ocr", lambda settings: engine)
    result = execute(context(tmp_path), "read_screen")
    assert result.ok and "3 line(s)" in result.message and result.data["source"].startswith("chrome")
    text = result.data["text"]
    assert "sk-abc" not in text and "4111 1111 1111 1111" not in text and "Settings" in text
    assert "untrusted" in result.data["note"]
    assert engine.seen == [(300, 200)]


def test_read_screen_truncates_long_text(tmp_path: Path, backend: FakeBackend, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.vision.create_ocr", lambda s: FakeOcr([f"line {i} " + "x" * 60 for i in range(200)]))
    result = execute(context(tmp_path), "read_screen")
    assert result.data["truncated"] is True and len(result.data["text"]) <= MAX_TEXT_CHARS
    assert len(result.to_model_text()) <= 4000


def test_read_screen_no_text(tmp_path: Path, backend: FakeBackend, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.vision.create_ocr", lambda s: FakeOcr([]))
    result = execute(context(tmp_path), "read_screen")
    assert result.ok and "couldn't find any readable text" in result.message


# ----------------------------------------------------------------- describe


@dataclass
class FakeAI:
    model: ModelInfo | None
    answer: str = "A GitHub issue page is open."
    calls: list = field(default_factory=list)

    async def vision_model(self):
        return self.model

    async def vision_chat(self, model, system, prompt, image):
        self.calls.append((model.name, system, prompt, image))
        return ChatResponse(model.name, ChatMessage.assistant(self.answer))


def test_describe_screen_uses_vision_model(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path)
    ctx.ai = FakeAI(ModelInfo("qwen2.5vl:3b", capabilities=frozenset({"completion", "vision"})))
    result = execute(ctx, "describe_screen", question="What is open?")
    assert result.ok and result.data["answer"] == "A GitHub issue page is open."
    name, system, prompt, image = ctx.ai.calls[0]
    assert name == "qwen2.5vl:3b" and prompt == "What is open?" and "not instructions" in system
    assert image.startswith(b"\x89PNG")


def test_describe_screen_redacts_the_answer(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path)
    ctx.ai = FakeAI(ModelInfo("llava:7b"), answer="The screen shows password: hunter2 in a form.")
    assert "hunter2" not in execute(ctx, "describe_screen").data["answer"]


def test_describe_screen_falls_back_to_ocr(tmp_path: Path, backend: FakeBackend, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.vision.create_ocr", lambda s: FakeOcr(["Error 0x80070005: Access is denied."]))
    ctx = context(tmp_path)
    ctx.ai = FakeAI(None)
    result = execute(ctx, "describe_screen", question="what does this error mean")
    assert result.ok and "No vision model" in result.message and "Access is denied" in result.data["text"]
    assert ctx.ai.calls == []


def test_describe_rejects_secret_questions(tmp_path: Path, backend: FakeBackend) -> None:
    ctx = context(tmp_path)
    ctx.ai = FakeAI(ModelInfo("llava:7b"))
    assert execute(ctx, "describe_screen", question="my password is hunter2, is it on screen?").error == "sensitive"
    assert backend.grabs == [] and ctx.ai.calls == []


# ------------------------------------------------------------------- images


def test_png_is_scaled_for_the_vision_model() -> None:
    import io

    data = cap.png_bytes(Image.new("RGB", (3840, 2160)), 1344)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1344, 756)


def test_enlarge_factor() -> None:
    assert enlarge_factor(Image.new("RGB", (600, 400)), 10000) == 2.0
    assert enlarge_factor(Image.new("RGB", (1500, 900)), 10000) == pytest.approx(1.2)
    assert enlarge_factor(Image.new("RGB", (2560, 1440)), 10000) == 1.0
    assert enlarge_factor(Image.new("RGB", (12000, 3000)), 10000) == pytest.approx(10000 / 12000)


# ---------------------------------------------------------------------- OCR


def test_tesseract_language_codes() -> None:
    assert tesseract_language("") == "eng"
    assert tesseract_language("en-US") == "eng"
    assert tesseract_language("bn") == "ben"
    assert tesseract_language("eng+ben") == "eng+ben"
    with pytest.raises(ToolError):
        tesseract_language("xx")


def test_no_engine_explains_both(monkeypatch) -> None:
    monkeypatch.setattr(ocr.WindowsOcr, "problem", lambda self: "no Windows OCR here.")
    monkeypatch.setattr(ocr.TesseractOcr, "problem", lambda self: "Tesseract is not installed.")
    with pytest.raises(ToolError) as info:
        create_ocr(Settings())
    assert "windows: no Windows OCR here." in str(info.value) and "tesseract:" in str(info.value)
    assert info.value.code == "ocr_unavailable"


def test_engine_choice(monkeypatch) -> None:
    monkeypatch.setattr(ocr.WindowsOcr, "problem", lambda self: None)
    monkeypatch.setattr(ocr.TesseractOcr, "problem", lambda self: None)
    assert create_ocr(Settings()).name == "windows"
    assert create_ocr(Settings(ocr_engine="tesseract", ocr_language="bn")).name == "tesseract"


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell-script stand-in for tesseract")
def test_tesseract_runs_as_argument_list(tmp_path: Path) -> None:
    log = tmp_path / "args.txt"
    script = tmp_path / "tesseract"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\nprintf "Hello  world\\n\\nSecond line\\n"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    engine = TesseractOcr("bn; rm -rf /", executable=str(script))
    with pytest.raises(ToolError):  # a bad language never reaches the program
        asyncio.run(engine.recognize(Image.new("RGB", (100, 50), "white")))
    engine = TesseractOcr("en-US", executable=str(script))
    result = asyncio.run(engine.recognize(Image.new("RGB", (100, 50), "white")))
    assert [line.text for line in result.lines] == ["Hello world", "Second line"] and result.language == "eng"
    args = log.read_text().splitlines()
    assert args[1:] == ["stdout", "-l", "eng", "--psm", "3"] and args[0].endswith("capture.png")
    assert not Path(args[0]).exists()  # the temporary image is deleted


# ------------------------------------------------------------------ AI side


def test_images_go_to_ollama_as_base64() -> None:
    wire = _message_to_wire(ChatMessage.user("what is this?", (b"\x89PNGdata",)))
    assert wire["images"] == [base64.b64encode(b"\x89PNGdata").decode()]
    assert "images" not in _message_to_wire(ChatMessage.user("hi"))


def _vision_fake() -> FakeOllama:
    big, small = model_entry("llava:13b"), model_entry("qwen2.5vl:3b")
    big["size"], small["size"] = 8_000_000_000, 3_000_000_000
    return FakeOllama(models=[model_entry("llama3.2:3b"), big, small],
                      capabilities={"llava:13b": ["completion", "vision"], "qwen2.5vl:3b": ["completion", "vision"]})


def _run(ai: AIManager, coro):
    async def inner():
        try:
            return await coro(ai)
        finally:
            await ai.aclose()

    return asyncio.run(inner())


def test_vision_model_selection() -> None:
    fake = _vision_fake()
    ai = AIManager.from_settings(Settings(), transport=fake.transport())
    assert _run(ai, lambda a: a.vision_model()).name == "qwen2.5vl:3b"  # smallest vision model
    ai = AIManager.from_settings(Settings(ai_model="llava:13b"), transport=fake.transport())
    assert _run(ai, lambda a: a.vision_model()).name == "llava:13b"  # the loaded chat model can see
    ai = AIManager.from_settings(Settings(vision_model="missing-vl"), transport=fake.transport())
    with pytest.raises(ModelNotFoundError, match="VISION_MODEL"):
        _run(ai, lambda a: a.vision_model())
    fake = FakeOllama(models=[model_entry("llama3.2:3b")])
    assert _run(AIManager.from_settings(Settings(), transport=fake.transport()), lambda a: a.vision_model()) is None


def test_vision_chat_sends_one_image() -> None:
    fake = _vision_fake()
    fake.reply = {"role": "assistant", "content": "A code editor."}
    ai = AIManager.from_settings(Settings(), transport=fake.transport())

    async def ask(a: AIManager):
        model = await a.vision_model()
        return await a.vision_chat(model, "system text", "What is this?", b"PNG")

    response = _run(ai, ask)
    payload = fake.last_chat_payload()
    assert response.message.content == "A code editor." and payload["model"] == "qwen2.5vl:3b"
    assert payload["messages"][1]["images"] == [base64.b64encode(b"PNG").decode()] and "tools" not in payload


def test_vision_guess_for_old_ollama() -> None:
    assert can_see_images(ModelInfo("llava:7b")) and can_see_images(ModelInfo("gemma3:4b"))
    assert not can_see_images(ModelInfo("llama3.2:3b"))
    assert not can_see_images(ModelInfo("llava:7b", capabilities=frozenset({"completion"})))


# ----------------------------------------------------------------- routing


def test_router_offers_vision_tools() -> None:
    from app.agent.tool_router import categories_for

    for text in ("read my screen", "what does this error say", "take a screenshot of chrome", "what's on my screen"):
        assert "vision" in categories_for(text), text
    assert "vision" not in categories_for("open chrome")


def test_screen_test_command(tmp_path: Path, backend: FakeBackend, monkeypatch) -> None:
    import io

    from app.cli import run_screen_test
    from app.main import bootstrap

    monkeypatch.setattr(ocr, "create_ocr", lambda s: FakeOcr(["Hello", "token=abcdef123456"]))
    out = io.StringIO()
    assert run_screen_test(bootstrap(tmp_path, environ={}), 0, out) == 0
    text = out.getvalue()
    assert "is OFF" in text and "Captured chrome" in text and "found 2 line(s)" in text
    assert "abcdef123456" not in text
    backend.windows_list = []
    out = io.StringIO()
    assert run_screen_test(bootstrap(tmp_path, environ={}), 0, out) == 1 and "no open window" in out.getvalue()
