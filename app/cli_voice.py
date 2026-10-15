"""Terminal voice commands: --devices, --mic-test, --say, --setup-voice, --voice.

Push-to-talk in the terminal: press Enter (or Space) to talk, Esc to stop the
assistant speaking, Q to quit. The GUI (Phase 9) replaces this with hotkeys.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from app.agent.conversation import ConversationManager
from app.cli import create_ai_manager, create_tool_executor, describe_execution, print_status
from app.security.confirmations import PendingConfirmer
from app.memory.conversations import ConversationStore
from app.tts.manager import TTSManager
from app.tts.piper import download_voice, is_voice_installed
from app.tts.base import TTSError
from app.utils.async_runner import AsyncRunner
from app.voice.audio import AudioError, list_devices
from app.voice.microphone import Microphone
from app.voice.pipeline import VoicePipeline
from app.voice.speech_to_text import FasterWhisperSTT, STTError

if TYPE_CHECKING:
    from app.main import AppContext

KEY_TALK, KEY_STOP, KEY_QUIT, KEY_OTHER = "talk", "stop", "quit", "other"


# ------------------------------------------------------------------ keyboard


class KeyReader:
    """Single-key input on Windows (msvcrt); line input elsewhere."""

    def __init__(self) -> None:
        try:
            import msvcrt

            self._msvcrt = msvcrt
        except ImportError:
            self._msvcrt = None

    @property
    def single_key(self) -> bool:
        return self._msvcrt is not None

    def _translate(self, ch: str) -> str:
        if ch in ("\r", "\n", " "):
            return KEY_TALK
        if ch == "\x1b":
            return KEY_STOP
        if ch.lower() == "q":
            return KEY_QUIT
        return KEY_OTHER

    def _read_msvcrt(self) -> str:
        ch = self._msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow/function keys send two codes
            self._msvcrt.getwch()
            return KEY_OTHER
        if ch == "\x03":
            raise KeyboardInterrupt
        return self._translate(ch)

    def wait_char(self) -> str:
        """One raw character (Windows) or one typed line (elsewhere)."""
        if self._msvcrt is not None:
            ch = self._msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                self._msvcrt.getwch()
                return ""
            if ch == "\x03":
                raise KeyboardInterrupt
            return ch
        return input().strip()[:1]

    def wait(self) -> str:
        if self._msvcrt is not None:
            return self._read_msvcrt()
        line = input().strip().lower()
        return {"": KEY_TALK, "q": KEY_QUIT, "s": KEY_STOP}.get(line, KEY_OTHER)

    def poll(self, timeout: float = 0.05) -> str | None:
        """A key pressed within ``timeout`` seconds, or None (never blocks longer)."""
        if self._msvcrt is None:
            time.sleep(timeout)
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._msvcrt.kbhit():
                return self._read_msvcrt()
            time.sleep(0.01)
        return None


# ------------------------------------------------------------------ builders


def whisper_dir(ctx: "AppContext") -> Path:
    return ctx.paths.models_dir / "whisper"


def piper_dir(ctx: "AppContext") -> Path:
    return ctx.paths.models_dir / "piper"


def wake_dir(ctx: "AppContext") -> Path:
    return ctx.paths.models_dir / "wakeword"


def _stt_offline(s) -> bool:
    """Use only downloaded speech models when offline mode is on or there's no internet."""
    from app.utils import network

    return s.forced_offline or (s.offline_mode == "auto" and not network.MONITOR.online())


def create_stt(ctx: "AppContext", *, offline: bool | None = None, background_upgrade: bool = True) -> FasterWhisperSTT:
    from app.voice.stt_select import stt_candidates

    s = ctx.settings
    candidates = stt_candidates(s)
    first = candidates[0]
    return FasterWhisperSTT(
        first.model, download_root=whisper_dir(ctx), device=first.device, compute_type=first.compute_type,
        beam_size=s.stt_beam_size, offline=_stt_offline(s) if offline is None else offline,
        hotwords=recognition_hints(s.wake_word, s.assistant_name, vocabulary(ctx)),
        candidates=candidates, background_upgrade=background_upgrade,
        allowed_languages=s.allowed_languages if s.voice_language == "auto" else None,
        second_pass=s.stt_second_pass,
        bangla_model=s.stt_bangla_model,
    )


COMMON_APP_WORDS = (
    "WhatsApp", "Chrome", "Google Chrome", "VS Code", "Visual Studio Code", "Spotify", "Discord", "Telegram",
    "Notepad", "File Explorer", "YouTube", "Gmail", "GitHub", "Zoom", "Teams", "Outlook", "Word", "Excel",
    "PowerPoint", "Edge", "Firefox", "Task Manager", "Settings", "Downloads", "Documents", "Desktop", "git",
    "Lock my PC", "Instagram", "Facebook", "Messenger", "Phone Link", "Calculator", "Paint", "Snipping Tool",
    "Command Prompt", "PowerShell", "Terminal", "Control Panel", "Photos", "Camera", "Microsoft Store",
)
MAX_HINT_WORDS = 70  # the hint becomes part of Whisper's prompt; keep it short


def recognition_hints(wake_word: str, assistant_name: str, extra: tuple[str, ...] | list[str] = ()) -> str:
    """Words the recognizer should expect: wake phrase, assistant name, app names.
    Without them small models hear "close WhatsApp" as "close bar set"."""
    words: list[str] = []
    for phrase in (wake_word.title(), assistant_name, *extra):
        phrase = " ".join(phrase.split())
        if phrase and phrase.lower() not in {w.lower() for w in words}:
            words.append(phrase)
    text, count = [], 0
    for phrase in words:
        size = len(phrase.split())
        if count + size > MAX_HINT_WORDS:
            break
        text.append(phrase)
        count += size
    return ", ".join(text)


def vocabulary(ctx: "AppContext") -> list[str]:
    """Hints for the recognizer: your own words first (CUSTOM_VOCABULARY, your corrections), then apps."""
    custom = [w.strip() for w in ctx.settings.custom_vocabulary.replace(";", ",").split(",") if w.strip()]
    try:
        from app.voice.corrections import Corrections

        custom += Corrections(ctx.paths.data_dir).vocabulary()
    except Exception:
        pass
    names = [entry.display_name for entry in ctx.config.apps.applications.values() if entry.enabled]
    return [*custom, *personal_words(ctx), *names, *COMMON_APP_WORDS]


def personal_words(ctx: "AppContext") -> list[str]:
    """Names from your own use - they're what speech recognition gets wrong most: your nicknames, the apps
    you open most, your contacts and your workflows. Each source is optional; a failure just skips it."""
    words: list[str] = []
    try:
        from app.agent.aliases import AliasStore

        for key, alias in AliasStore(ctx.paths.data_dir).items().items():
            words.append(key)
            if alias.get("kind") == "app":
                words.append(str(alias.get("target", "")))
    except Exception:
        pass
    database = getattr(ctx, "database", None)
    if database is not None:
        try:
            with database.connect() as conn:
                rows = conn.execute("SELECT name FROM usage_events GROUP BY name ORDER BY COUNT(*) DESC LIMIT 12")
                words += [r[0] for r in rows]
        except Exception:
            pass
        try:
            from app.automation.tasks import AutomationStore

            words += [task.name for task in AutomationStore(database).all()][:8]
        except Exception:
            pass
    try:
        from types import SimpleNamespace

        from app.tools.phone import load_contacts

        words += list(load_contacts(SimpleNamespace(paths=ctx.paths)))[:15]  # type: ignore[arg-type]
    except Exception:
        pass
    seen: set[str] = set()
    return [w for w in (" ".join(str(w).split()) for w in words)
            if w and len(w) <= 40 and not (w.lower() in seen or seen.add(w.lower()))]


def create_tts(ctx: "AppContext") -> TTSManager:
    return TTSManager(ctx.settings, piper_dir(ctx))


def _write(out: TextIO, text: str) -> None:
    out.write(text)
    out.flush()


# ------------------------------------------------------------------ commands


def run_devices(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    try:
        devices = list_devices()
    except AudioError as exc:
        _write(out, f"{exc}\n")
        return 1
    for kind, attr in (("Microphones", "max_input_channels"), ("Speakers", "max_output_channels")):
        _write(out, f"{kind}:\n")
        for d in devices:
            if getattr(d, attr) < 1:
                continue
            default = d.is_default_input if attr == "max_input_channels" else d.is_default_output
            _write(out, f"  {'*' if default else ' '} [{d.index:>3}] {d.name}  ({d.host_api})\n")
    _write(out, "\n* = Windows default. Set MICROPHONE_DEVICE / OUTPUT_DEVICE in .env to an index or part of a name.\n")
    return 0


def run_say(ctx: "AppContext", text: str, out: TextIO = sys.stdout) -> int:
    tts = create_tts(ctx)
    try:
        tts.wait_ready(60)
        if not tts.available:
            _write(out, f"{tts.problem}\n")
            return 1
        _write(out, f"Speaking with {tts.engine.description}...\n")
        tts.speak(text)
        tts.wait_until_idle(120)
        if tts.problem:
            _write(out, f"{tts.problem}\n")
            return 1
        return 0
    finally:
        tts.close()


def run_mic_test(ctx: "AppContext", out: TextIO = sys.stdout, seconds: float = 4.0) -> int:
    mic = Microphone(ctx.settings.microphone_device, enabled=ctx.settings.microphone_enabled)
    _write(out, f"Speak normally for {seconds:.0f} seconds, starting now.\nRecording... ")
    try:
        report, recording = mic.level_test(seconds)
    except AudioError as exc:
        _write(out, f"\n{exc}\n")
        return 1
    _write(out, "done.\n")
    _write(out, f"Peak {report.peak_db:.1f} dBFS, average {report.average_db:.1f} dBFS. {report.verdict}\n")
    if report.peak_db < -50:
        return 1
    stt = create_stt(ctx)
    _write(out, "Transcribing (the first run loads the model)...\n")
    try:
        transcript = stt.transcribe(recording.audio, ctx.settings.voice_language)
    except STTError as exc:
        _write(out, f"{exc}\n")
        return 1
    _write(out, f"Heard: \"{transcript.text or '(nothing)'}\"  confidence {transcript.confidence:.2f}, "
                f"{transcript.elapsed_ms} ms ({getattr(stt, 'description', 'speech recognition')})\n")
    return 0


def run_setup_voice(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    s = ctx.settings
    if s.forced_offline:
        _write(out, "Offline mode is on. Set OFFLINE_MODE=auto to download voice models.\n")
        return 1
    ok = True
    from app.voice.stt_select import stt_candidates

    choices = stt_candidates(s)
    for number, choice in enumerate(choices[:2], 1):  # the best setup, and its CPU fallback
        label = "1/3 Speech recognition" if number == 1 else "    fallback"
        _write(out, f"{label}: {choice.describe()} (downloaded once, then used offline)...\n")
        try:
            started = time.perf_counter()
            one = FasterWhisperSTT(choice.model, download_root=whisper_dir(ctx), device=choice.device,
                                   compute_type=choice.compute_type, offline=False)
            one.load()
            _write(out, f"    ready ({time.perf_counter() - started:.0f} s)\n")
        except STTError as exc:
            if number == 1 and len(choices) > 1:
                _write(out, f"    {exc}\n    (the fallback below is used instead)\n")
            else:
                ok = False
                _write(out, f"    {exc}\n")

    _write(out, f"2/3 Piper voice '{s.tts_voice}'...\n")
    if is_voice_installed(piper_dir(ctx), s.tts_voice):
        _write(out, "    already installed\n")
    else:
        last = {"pct": -1}

        def progress(name: str, done: int, total: int | None) -> None:
            if total:
                pct = done * 100 // total
                if pct >= last["pct"] + 10:
                    last["pct"] = pct
                    _write(out, f"    {name}: {pct}%\n")

        try:
            download_voice(s.tts_voice, piper_dir(ctx), progress=progress)
            _write(out, "    installed\n")
        except TTSError as exc:
            ok = False
            _write(out, f"    {exc}\n")
    from app.voice.hands_free import resolve_engine
    from app.voice.wake_word import WakeWordUnavailableError, download_models, missing_models

    _write(out, f"3/3 Wake-word model for \"{s.wake_word}\" (hands-free listening)...\n")
    if resolve_engine(s) != "openwakeword":
        _write(out, "    not needed (this wake phrase is spotted with speech recognition)\n")
    else:
        try:
            if not missing_models(wake_dir(ctx), s.wake_word, s.wake_word_model):
                _write(out, "    already installed\n")
            else:
                download_models(wake_dir(ctx), s.wake_word, s.wake_word_model)
                _write(out, "    installed (checksums verified)\n")
        except WakeWordUnavailableError as exc:
            ok = False
            _write(out, f"    {exc}\n")
    if "bn" in s.allowed_languages:
        from app.tts import bangla

        _write(out, "4/4 Offline Bangla voice (about 108 MB, spoken without internet)...\n")
        try:
            bangla.download(ctx.paths.models_dir)
            _write(out, "    installed\n")
        except TTSError as exc:
            ok = False
            _write(out, f"    {exc}\n")
    if ok:
        _write(out, "Voice setup complete. Try: python run.py --say \"Hello, I am ready.\"\n")
    return 0 if ok else 1


def run_voice(ctx: "AppContext", out: TextIO = sys.stdout) -> int:
    s = ctx.settings
    runner = AsyncRunner().start()
    ai = create_ai_manager(ctx)
    confirmer = PendingConfirmer()
    executor = create_tool_executor(
        ctx, confirmer, ai=ai,
        on_screen_capture=lambda active: active and _write(out, "\n  [SCREEN CAPTURE ACTIVE]\n"))
    conversation = ConversationManager(
        ai, s, ConversationStore(ctx.database), tools=executor,
        on_tool=lambda e: _write(out, f"\n  {describe_execution(e)}\n"), spoken=True,
    )
    mic = Microphone(s.microphone_device, enabled=s.microphone_enabled)
    stt = create_stt(ctx)
    tts = create_tts(ctx)
    from app.voice.vad import create_vad

    pipeline = VoicePipeline(s, mic, stt, tts, conversation, vad_factory=lambda: create_vad(s.vad_engine))
    keys = KeyReader()
    try:
        status = runner.run(ai.status())
        print_status(status, out)
        if not status.reachable or status.selected is None:
            return 1
        tts.start()
        _write(out, "Loading speech recognition...\n")
        try:
            stt.load()
        except STTError as exc:
            _write(out, f"{exc}\n")
            return 1
        _write(out, f"Speech recognition: {getattr(stt, 'description', 'ready')}\n")
        tts.wait_ready(60)
        _write(out, f"Voice output: {tts.engine.description if tts.engine else 'unavailable - ' + str(tts.problem)}\n")
        from app.automation.scheduler import Scheduler
        from app.automation.tasks import AutomationStore
        from app.tools.automation import seed_default_workflows

        def notify(title: str, message: str) -> None:
            _write(out, f"\n[reminder] {title}: {message}\n")
            tts.speak(message)

        executor.context.notify = notify
        seed_default_workflows(executor.context)
        scheduler = Scheduler(AutomationStore(ctx.database), executor, notify)
        executor.context.scheduler = scheduler

        async def start_scheduler() -> None:
            scheduler.start()

        runner.run(start_scheduler(), timeout=10)
        if keys.single_key:
            _write(out, "\nPress ENTER or SPACE to talk, ESC to stop speaking, Q to quit.\n")
        else:
            _write(out, "\nPress ENTER to talk, type s+ENTER to stop speaking, q+ENTER to quit.\n")

        while True:
            key = keys.wait()
            if key == KEY_QUIT:
                break
            if key == KEY_STOP:
                tts.stop()
                continue
            if key != KEY_TALK:
                continue
            tts.stop()
            _write(out, "[microphone on] Listening... (any key stops)\n")
            stop = threading.Event()
            holder: list = []
            worker = threading.Thread(target=lambda: holder.append(pipeline.listen(stop)), daemon=True)
            worker.start()
            while worker.is_alive():
                if keys.poll(0.05) is not None:
                    stop.set()
            worker.join()
            _write(out, "[microphone off]\n")
            result = holder[0]
            if result.transcript is not None and result.transcript.text:
                _write(out, f"You: {result.transcript.text}\n")
            if not result.ok:
                _write(out, f"{s.assistant_name}: {result.message}\n\n")
                if result.status in ("wake_only", "low_confidence"):
                    tts.speak(result.message)
                continue

            _write(out, f"{s.assistant_name}: ")
            future = runner.submit(pipeline.respond(result.text, on_text=lambda d: _write(out, d)))
            cancelled = False
            while not future.done():
                request = confirmer.pending()
                if request is not None:
                    tts.stop()
                    _write(out, f"\n[confirm - {request.level_name}] You said: \"{result.transcript.text if result.transcript else result.text}\"\n"
                                f"  {request.summary}  Press Y to confirm, any other key cancels: ")
                    tts.speak(request.summary)
                    answer = keys.wait_char().lower() == "y"
                    tts.stop()
                    _write(out, "yes\n" if answer else "cancelled\n")
                    confirmer.resolve(answer)
                    continue
                if keys.poll(0.05) in (KEY_STOP, KEY_QUIT):
                    future.cancel()
                    tts.stop()
                    cancelled = True
                    break
            if cancelled:
                _write(out, " [stopped]\n\n")
                continue
            reply = future.result()
            if not reply.ok:
                _write(out, reply.text)
            if getattr(reply, "checklist", ""):
                _write(out, "\n\n" + reply.checklist)
            _write(out, "\n\n")
            while tts.is_speaking:
                if keys.poll(0.05) == KEY_STOP:
                    tts.stop()
                    break
        return 0
    except KeyboardInterrupt:
        _write(out, "\n")
        return 0
    finally:
        tts.close()
        for closing in (executor.context.aclose(), ai.aclose()):
            try:
                runner.run(closing, timeout=10)
            except Exception:
                pass
        runner.stop()
