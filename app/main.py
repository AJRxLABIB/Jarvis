"""Application entry point and bootstrap sequence.

Available so far: foundation (settings, logging, config files, database),
``--doctor`` diagnostics, local AI through Ollama (``--models``, ``--chat``) and
voice (``--voice``, ``--devices``, ``--mic-test``, ``--say``, ``--setup-voice``).
The desktop interface is added in Phase 9.
"""

from __future__ import annotations

import argparse
import os
import importlib.util
import platform
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from app import __app_name__, __version__
from app.config.files import ConfigBundle, load_config_bundle
from app.config.settings import ConfigError, Settings, load_settings
from app.memory.database import LATEST_SCHEMA_VERSION, Database, StorageError
from app.utils.logging_setup import (
    get_logger,
    install_exception_hooks,
    log_event,
    setup_logging,
    teardown_logging,
)
from app.utils.paths import AppPaths, default_root, is_frozen

if TYPE_CHECKING:
    from app.ai.manager import AIStatus

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_STORAGE_ERROR = 3
EXIT_OS_ERROR = 4

# (import name, purpose, phase that adds it) - reported by --doctor only.
OPTIONAL_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("playwright", "Browser automation", "Phase 10"),
    ("PIL", "Screen capture (Pillow)", "Phase 12"),
)


@dataclass
class AppContext:
    settings: Settings
    paths: AppPaths
    config: ConfigBundle
    database: Database
    log_file: Path


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail" | "info"
    detail: str


def _repair_forgotten_setup(paths: AppPaths, settings) -> list[str]:
    """.env says "never set up" but setup was done (data/setup_state.json): write SETUP_DONE and the remembered answers
    back instead of showing the setup wizard again. Returns the names written."""
    from app.core import setup_state

    try:
        from app.config.env_writer import update_env_file
        from app.config.settings import _read_env_file

        current = _read_env_file(paths.env_file) if paths.env_file.is_file() else {}
        if settings.setup_done:
            if not setup_state.is_done(paths.data_dir):        # set up before this record existed: start it now
                setup_state.remember(paths.data_dir, {k: v for k, v in current.items() if k in setup_state.KEEP})
            return []
        if not setup_state.is_done(paths.data_dir):
            return []
        fixes = setup_state.repairs(current, paths.data_dir)
        if fixes:
            update_env_file(paths.env_file, fixes)
        return sorted(fixes)
    except Exception:
        return []


def bootstrap(root: Path | None = None, environ: Mapping[str, str] | None = None) -> AppContext:
    """Load settings, prepare folders, start logging, load config, open the database.

    Raises ConfigError, StorageError or OSError with user-readable messages.
    """
    root = Path(root or default_root()).resolve()
    migrated: list[str] = []
    try:
        from app.config.migrate import migrate_env_file

        migrated = migrate_env_file(root / ".env")  # upgrade old settings files (a .env.bak copy is kept)
        try:
            from app.config.env_writer import encrypt_plain_secrets

            encrypt_plain_secrets(root / ".env")  # tokens typed into .env by hand -> encrypted (Windows)
        except Exception:
            pass
    except OSError:
        pass
    shadowed: list[str] = []
    if environ is None and os.environ.get("JARVIS_ENV_WINS", "").strip().lower() not in ("1", "true", "yes"):
        # A terminal (VS Code's python.terminal.useEnvFile) or the previous Jarvis this one was restarted from can carry an OLDER copy of
        # .env in its environment, which would silently beat every later edit to the file. For names written in .env, the file wins.
        # (JARVIS_ENV_WINS=1 restores "environment first"; a variable that is not in .env still applies as before.)
        try:
            from app.config.settings import env_overrides

            shadowed = env_overrides(root / ".env")
            for name in shadowed:
                os.environ.pop(name, None)
        except Exception:
            shadowed = []
    settings = load_settings(root / ".env", environ)
    try:
        from app.utils import gpu_pref

        gpu_pref.apply(settings.gpu_preference)          # before any speech / model library loads
    except Exception:
        pass
    paths = AppPaths.build(root, settings.data_dir)
    paths.ensure_dirs()
    setup_repaired = _repair_forgotten_setup(paths, settings)
    if setup_repaired:
        settings = load_settings(root / ".env", environ)

    log_file = setup_logging(
        paths.logs_dir,
        settings.effective_log_level,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        console=settings.debug,
    )
    log = get_logger("main")
    log_event(
        log,
        "startup",
        version=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        frozen=is_frozen(),
        root=str(paths.root),
        data_dir=str(paths.data_dir),
    )
    log_event(log, "settings_loaded", settings=settings.public_dict())
    if migrated:
        log_event(log, "settings_upgraded", migrations=migrated)
    if shadowed:
        log_event(log, "stale_environment_ignored", names=shadowed)
    if setup_repaired:   # .env had been replaced by a fresh copy: the one-time setup was restored, not asked again
        log_event(log, "setup_restored", keys=setup_repaired)

    config = load_config_bundle(paths.config_dir)
    log_event(
        log,
        "config_loaded",
        applications=len(config.apps.applications),
        dev_commands=len(config.permissions.allowed_dev_commands),
        hotkeys=len(config.shortcuts.hotkeys),
    )

    database = Database(paths.db_path)
    applied = database.initialize()
    log_event(
        log,
        "database_ready",
        path=str(paths.db_path),
        schema_version=database.schema_version(),
        migrations_applied=applied,
    )
    from app.core.runs import RunStore

    interrupted = RunStore(database).mark_interrupted()  # requests cut off by a crash or a hard exit
    if interrupted:
        log_event(log, "runs_interrupted", count=interrupted)
    from app.core.missions import MissionStore

    from app.memory.manager import MemoryManager

    expired = MemoryManager(database).purge_expired()  # "remember for today ..." ends here
    if expired:
        log_event(log, "memories_expired", count=expired)
    paused = MissionStore(database).mark_interrupted()  # "continue my mission" picks them up
    if paused:
        log_event(log, "missions_interrupted", count=paused)
    context = AppContext(settings=settings, paths=paths, config=config, database=database, log_file=log_file)
    from app.utils import egress

    egress.install(lambda: context.settings.forced_offline,        # OFFLINE_MODE=on: nothing leaves this computer / your network
                   lambda host: log_event(log, "egress_blocked", host=host))
    return context


# ------------------------------------------------------------------ diagnostics


def _python_check() -> Check:
    version = platform.python_version()
    if sys.version_info >= (3, 12):
        return Check("Python", "ok", version)
    if sys.version_info >= (3, 11):
        return Check("Python", "warn", f"{version} (3.12+ recommended)")
    return Check("Python", "fail", f"{version} (3.11+ required)")


def _os_check() -> Check:
    if sys.platform != "win32":
        return Check("Operating system", "warn", f"{platform.system()} - Windows-only features will be unavailable")
    build = sys.getwindowsversion().build
    if build >= 22000:
        return Check("Operating system", "ok", f"Windows 11 (build {build})")
    return Check("Operating system", "warn", f"Windows build {build} - Windows 11 is the supported target")


def _writable_check(directory: Path) -> Check:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-", delete=True) as handle:
            handle.write(b"ok")
        return Check("Data folder", "ok", f"{directory} (writable)")
    except OSError as exc:
        return Check("Data folder", "fail", f"{directory} is not writable: {exc}")


def _ai_checks(ctx: AppContext, status: "AIStatus | None") -> list[Check]:
    s = ctx.settings
    if status is None:
        return [Check("AI provider", "info", f"{s.ai_provider} at {s.ollama_host} (not checked)")]
    if not status.reachable:
        return [Check("Ollama", "warn", f"{status.message} ({s.ollama_host})")]
    checks = [Check("Ollama", "ok", f"running, version {status.version or 'unknown'} at {s.ollama_host}")]
    if status.selected is None:
        checks.append(Check("AI model", "warn", status.message or "no model selected"))
    else:
        tools = {True: "supports tools", False: "NO tool support", None: "tool support unknown"}
        state = "ok" if status.selected.supports_tools is not False else "warn"
        checks.append(
            Check(
                "AI model", state,
                f"{status.selected.name} ({tools[status.selected.supports_tools]}; "
                f"selected by {status.selection_source}; {len(status.models)} installed)",
            )
        )
    return checks


def _voice_checks(ctx: AppContext) -> list[Check]:
    from app.tts.piper import is_voice_installed
    from app.voice.audio import AudioError, list_devices

    s = ctx.settings
    checks: list[Check] = []
    try:
        devices = list_devices()
        mic = next((d.name for d in devices if d.is_default_input), None)
        speaker = next((d.name for d in devices if d.is_default_output), None)
        if not s.microphone_enabled:
            checks.append(Check("Microphone", "info", "turned off (MICROPHONE_ENABLED=false)"))
        elif mic or s.microphone_device:
            checks.append(Check("Microphone", "ok", s.microphone_device or f"{mic} (Windows default)"))
        else:
            checks.append(Check("Microphone", "warn", "no microphone found"))
        checks.append(
            Check("Speakers", "ok" if speaker or s.output_device else "warn", s.output_device or speaker or "none found")
        )
    except AudioError as exc:
        checks.append(Check("Audio", "warn", str(exc)))

    whisper_dir = ctx.paths.models_dir / "whisper"
    try:
        from faster_whisper.utils import download_model

        from app.voice.stt_select import cuda_available, stt_candidates

        choices = stt_candidates(s)
        ready = []
        for choice in choices:
            try:
                download_model(choice.model, local_files_only=True, cache_dir=str(whisper_dir))
                ready.append(choice)
            except Exception:
                pass
        gpu = "NVIDIA GPU found" if cuda_available() else "no NVIDIA GPU (CPU)"
        if ready and ready[0] == choices[0]:
            checks.append(Check("Speech recognition", "ok", f"{choices[0].describe()} (downloaded; {gpu})"))
        elif ready:
            checks.append(Check("Speech recognition", "warn",
                                f"using {ready[0].describe()}; {choices[0].describe()} downloads automatically "
                                f"when online ({gpu})"))
        else:
            checks.append(Check("Speech recognition", "warn", f"model '{choices[0].model}' not downloaded - run --setup-voice"))
    except ImportError:
        checks.append(Check("Speech recognition", "warn", "faster-whisper not installed"))

    if not s.tts_enabled:
        checks.append(Check("Text-to-speech", "info", "turned off (TTS_ENABLED=false)"))
    elif s.tts_engine != "sapi" and is_voice_installed(ctx.paths.models_dir / "piper", s.tts_voice):
        checks.append(Check("Text-to-speech", "ok", f"Piper ({s.tts_voice})"))
    elif s.tts_engine != "piper" and sys.platform == "win32" and importlib.util.find_spec("win32com"):
        note = "" if s.tts_engine == "sapi" else " - run --setup-voice for the Piper voice"
        checks.append(Check("Text-to-speech", "ok", f"Windows voice{note}"))
    else:
        checks.append(Check("Text-to-speech", "warn", f"Piper voice '{s.tts_voice}' not downloaded - run --setup-voice"))
    return checks


def collect_checks(
    ctx: AppContext, ai_status: "AIStatus | None" = None, *, include_voice: bool = False
) -> list[Check]:
    checks = [
        _python_check(),
        _os_check(),
        Check("Architecture", "ok" if sys.maxsize > 2**32 else "fail", platform.machine() or "unknown"),
        _writable_check(ctx.paths.data_dir),
    ]

    integrity = ctx.database.integrity_check()
    version = ctx.database.schema_version()
    if integrity == "ok" and version == LATEST_SCHEMA_VERSION:
        checks.append(Check("Database", "ok", f"{ctx.paths.db_path} (schema v{version})"))
    else:
        checks.append(Check("Database", "fail", f"integrity: {integrity}; schema v{version}"))

    cfg = ctx.config
    checks.append(
        Check(
            "Config files",
            "ok",
            f"{len(cfg.apps.applications)} apps, {len(cfg.permissions.allowed_dev_commands)} dev commands, "
            f"{len(cfg.shortcuts.hotkeys)} hotkeys",
        )
    )
    checks.extend(_ai_checks(ctx, ai_status))
    if include_voice:
        checks.extend(_voice_checks(ctx))
    checks.append(Check("Log file", "info", str(ctx.log_file)))

    for module, purpose, phase in OPTIONAL_COMPONENTS:
        installed = importlib.util.find_spec(module) is not None
        checks.append(Check(purpose, "info", "installed" if installed else f"not installed yet ({phase})"))
    checks.append(_screen_check(ctx.settings))
    checks.append(_hands_free_check(ctx))
    return checks


def _hands_free_check(ctx: AppContext) -> Check:
    s = ctx.settings
    state = "on" if s.hands_free else "off (HANDS_FREE=true, or the tray menu)"
    try:
        from app.voice.hands_free import resolve_engine
        from app.voice.wake_word import missing_models

        engine = resolve_engine(s)
        if engine == "openwakeword" and missing_models(ctx.paths.models_dir / "wakeword", s.wake_word,
                                                       s.wake_word_model):
            return Check("Hands-free", "warn" if s.hands_free else "info",
                         f"{state}; wake-word model not downloaded (python run.py --setup-voice)")
    except Exception as exc:
        return Check("Hands-free", "warn" if s.hands_free else "info", f"{state}; {exc}")
    return Check("Hands-free", "info", f"{state}; wake phrase \"{s.wake_word}\" via {engine}")


def _screen_check(settings: Settings) -> Check:
    """Which OCR engine would read the screen (nothing is captured here)."""
    state = "on" if settings.screen_capture_enabled else "off (opt-in: SCREEN_CAPTURE_ENABLED=true)"
    try:
        from app.vision.ocr import create_ocr

        engine = create_ocr(settings)
    except Exception as exc:
        return Check("Screen capture", "info", f"{state}; text recognition unavailable: {exc}")
    return Check("Screen capture", "info", f"{state}; text recognition: {engine.name}")


def _make_jarvis_model(ctx: AppContext, base_name: str | None) -> int:
    """jarvis-cli --make-jarvis-model [--model BASE]: one model called "jarvis", then (if you agree) remove the rest."""
    from app.ai import jarvis_model
    from app.cli import create_ai_manager
    from app.config.env_writer import update_env_file
    from app.utils.async_runner import AsyncRunner

    ai = create_ai_manager(ctx)
    runner = AsyncRunner("jarvis-model").start()
    s = ctx.settings
    try:
        models = runner.run(ai.refresh_models(), timeout=30)
        base = next((m for m in models if base_name and m.name.lower() == base_name.lower()), None) if base_name \
            else jarvis_model.choose_base(models, s.ai_model)
        if base is None:
            print(f"The model '{base_name}' is not installed: run  ollama pull {base_name}  first." if base_name
                  else "No chat model is installed: run  ollama pull qwen3:4b-instruct  first.")
            return EXIT_CHECK_FAILED
        print(f"Making the model \"jarvis\" from {base.name}...")
        runner.run(jarvis_model.build(ai, base.name, s), timeout=900)
        update_env_file(ctx.paths.env_file, {"AI_MODEL": jarvis_model.JARVIS_MODEL})
        print("Done: Jarvis now uses the model \"jarvis\" (AI_MODEL=jarvis).")
        models = runner.run(ai.refresh_models(), timeout=30)
        plan = jarvis_model.cleanup_plan(models, keep_names=(s.ai_fast_model, s.ai_smart_model, s.vision_model,
                                                            s.embedding_model))
        if not plan.delete:
            print("No other chat models to remove.")
            return EXIT_OK
        print("\nThese models are no longer needed (\"jarvis\" keeps what it uses from them):")
        for name in plan.delete:
            print(f"   {name}")
        print("Kept: " + ", ".join(plan.keep))
        if input("Delete them now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was deleted.")
            return EXIT_OK
        removed = runner.run(jarvis_model.delete_models(ai, plan.delete), timeout=600)
        print(f"Deleted {len(removed)} model(s).")
        return EXIT_OK
    finally:
        try:
            runner.run(ai.aclose(), timeout=10)
        except Exception:
            pass


def _self_improve(ctx: AppContext) -> int:
    from app.agent.improve_job import improve_now
    from app.tools.catalog import build_default_registry
    from app.utils.async_runner import AsyncRunner

    print("Testing set-ups on your real requests (this uses the AI for a few minutes)...")
    runner = AsyncRunner("improve").start()
    try:
        outcome = runner.run(improve_now(ctx.settings, ctx.paths, ctx.database, build_default_registry()),
                             timeout=3600)
    except Exception as exc:
        print(f"Couldn't run: {exc} (is Ollama running?)")
        return EXIT_CHECK_FAILED
    finally:
        runner.stop()
    for row in outcome.scores:
        print(f"  {row['label']:11} {row['base']} @ {row['temperature']}: right tool {row['accuracy']:.0%}, "
              f"broken calls {row['malformed']}, {row['avg_seconds']} s")
    print(outcome.message + (" Restart Jarvis to use it." if outcome.changed else ""))
    return EXIT_OK


def _model_roles(ctx: AppContext, apply: bool) -> int:
    """Which installed model for which job, from capabilities + this PC's memory + your benchmark results."""
    from app.ai import roles, switching
    from app.ai.health import total_memory_bytes
    from app.cli import create_ai_manager
    from app.utils.async_runner import AsyncRunner

    runner = AsyncRunner("roles").start()
    try:
        ai = create_ai_manager(ctx)
        try:
            models = runner.run(ai.refresh_models(), timeout=60)
        except Exception as exc:
            print(f"Couldn't reach Ollama ({exc}). Start Ollama and try again.")
            return EXIT_CHECK_FAILED
        found = roles.facts(models, switching.load_evidence(ctx.paths.data_dir), ai.health.summary(),
                            total_memory_bytes())
        picks = roles.recommend(found)
        s = ctx.settings
        current = {"general": s.ai_model, "fast": s.ai_fast_model, "smart": s.ai_smart_model,
                   "coding": getattr(s, "ai_coding_model", ""), "vision": s.vision_model, "embedding": s.embedding_model}
        print(roles.report(found, picks, current))
        changes = roles.changes(picks, current)
        if apply and changes:
            from app.config.env_writer import update_env_file

            print("\nWill set: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
            if input("Apply these to .env? [y/N] ").strip().lower() == "y":
                update_env_file(ctx.paths.env_file, changes)
                print("Saved. Restart Jarvis (python run.py --restart) to use them.")
        elif apply:
            print("\nNothing to change.")
        return EXIT_OK
    finally:
        runner.stop()


def _longmem_eval(ctx: AppContext, file: str, with_model: bool) -> int:
    """`--longmem-eval [FILE] [--full]` (app/longmem_eval.py). Retrieval needs no model; --full asks the configured chat model."""
    from app import longmem_eval as LM

    if file:
        path = Path(file).expanduser()
        if not path.is_file():
            print(f"No such file: {path}")
            return EXIT_CHECK_FAILED
        try:
            cases = LM.load(path)
        except (ValueError, OSError) as exc:
            print(f"Could not read {path.name}: {exc}")
            return EXIT_CHECK_FAILED
        source = f"{path.name}"
        if not cases:
            print("That file has no questions in the LongMemEval layout.")
            return EXIT_CHECK_FAILED
    else:
        cases, source = LM.builtin(), "built-in cases"
    ask = None
    runner = manager = None
    if with_model:
        from app.ai.base import ChatMessage
        from app.ai.manager import AIManager
        from app.utils.async_runner import AsyncRunner

        runner = AsyncRunner("longmem").start()
        manager = AIManager.from_settings(ctx.settings)

        def ask(question: str, context: str) -> str:
            reply = runner.run(manager.chat([ChatMessage.system(LM.SYSTEM),
                                             ChatMessage.user(f"{context}\n\nQuestion: {question}")]), timeout=180)
            return str(reply.message.content or "")
    try:
        print(LM.report(LM.run(cases, ask), source, with_answers=with_model))
        return EXIT_OK
    finally:
        if runner is not None:
            try:
                runner.run(manager.aclose(), timeout=10)
            finally:
                runner.stop()


def _ground_trial(ctx: AppContext, model: str) -> int:
    """`--ground-trial MODEL`: the screen-grounding trial (app/validation/grounding_trial.py). Uses a model that is ALREADY installed; downloads nothing."""
    import time as _t

    from app.ai.base import ModelInfo
    from app.ai.manager import AIManager
    from app.utils.async_runner import AsyncRunner
    from app.validation import grounding_trial as GT

    print("Switch to the window to test (e.g. Notepad or Settings). Capturing in 5 seconds...")
    _t.sleep(5)
    try:
        controls, png, size = GT.collect_windows()
    except Exception as exc:
        print(f"Could not read the window: {exc}")
        return EXIT_CHECK_FAILED
    runner = AsyncRunner("ground").start()
    manager = AIManager.from_settings(ctx.settings)
    try:
        installed = [m.name for m in runner.run(manager.models(), timeout=30)]
        if model not in installed:
            print(f"'{model}' is not installed. Installed: {', '.join(installed) or 'none'}. Nothing was downloaded.")
            return EXIT_CHECK_FAILED

        def ask(prompt: str) -> str:
            reply = runner.run(manager.vision_chat(ModelInfo(model), "You locate controls in screenshots.", prompt, png), timeout=180)
            return str(getattr(getattr(reply, "message", reply), "content", "") or getattr(reply, "content", "") or "")

        print(GT.report(GT.run(model, controls, size, ask)))
        return EXIT_OK
    finally:
        try:
            runner.run(manager.aclose(), timeout=10)
        finally:
            runner.stop()


def _compare_models(ctx: AppContext, names: str | None, switch: bool) -> int:
    from app import model_compare
    from app.ai import jarvis_model
    from app.ai.manager import AIManager
    from app.config.env_writer import update_env_file
    from app.utils.async_runner import AsyncRunner

    s = ctx.settings
    runner = AsyncRunner("compare").start()
    try:
        results = runner.run(model_compare.compare_installed(s, names), timeout=3600)
        if not results:
            return EXIT_CHECK_FAILED
        winner = model_compare.pick_winner(results)
        if switch and winner is not None:
            print(f"Making \"jarvis\" from {winner.model}...")
            probe = AIManager.from_settings(s)
            try:
                runner.run(jarvis_model.build(probe, winner.model, s), timeout=900)
            finally:
                runner.run(probe.aclose(), timeout=10)
            update_env_file(ctx.paths.env_file, {"AI_MODEL": jarvis_model.JARVIS_MODEL})
            print("Done: Jarvis now uses the winner (AI_MODEL=jarvis).")
        elif winner is not None:
            print(f"To use it:  jarvis-cli --make-jarvis-model --model {winner.model}")
        return EXIT_OK
    finally:
        runner.stop()


def _database_backup(ctx: AppContext, restore: Path | None) -> int:
    from app.memory import backup

    folder = ctx.paths.data_dir / "backups"
    try:
        if restore is None:
            path = backup.create_backup(ctx.paths.db_path, folder)
            print(f"Backed up the database to {path}")
            outcome = backup.restore_test(path)
            print(outcome.message())
            if not outcome.ok:
                return EXIT_STORAGE_ERROR
        else:
            kept = backup.restore_backup(ctx.paths.db_path, restore, folder)
            print(f"Restored the database from {restore}. The previous one is kept as {kept}.")
    except StorageError as exc:
        print(f"Not done: {exc}")
        return EXIT_STORAGE_ERROR
    return EXIT_OK


def _health_report(ctx: AppContext) -> int:
    """Self-test (no microphone), benchmark (if recorded), a short Bangla score, speed, mistakes -> one report."""
    from app import health
    from app.cli import create_ai_manager
    from app.cli_voice import create_stt, create_tts
    from app.utils.async_runner import AsyncRunner

    runner = AsyncRunner("health").start()
    ai = create_ai_manager(ctx)
    tts = create_tts(ctx)
    try:
        tts.wait_ready(60)
        stt = create_stt(ctx, background_upgrade=False)
        path, summary = health.collect(ctx, ai, stt, tts.engine, lambda coro: runner.run(coro, timeout=900),
                                       out=lambda line: print(line, flush=True))
        print("\nHealth report:")
        for line in summary:
            print(f"  {line}")
        print(f"\nFull report: {path}")
        return EXIT_OK
    finally:
        tts.close()
        try:
            runner.run(ai.aclose(), timeout=10)
        except Exception:
            pass


def _always_on(ctx: AppContext, enable: bool) -> int:
    """Personal-assistant mode: start with Windows, straight into the tray (no window), listening hands-free."""
    from app.config.env_writer import update_env_file
    from app.utils import autostart

    flag = "true" if enable else "false"
    update_env_file(ctx.paths.env_file, {"START_WITH_WINDOWS": flag, "START_MINIMIZED": flag, "HANDS_FREE": flag})
    try:
        autostart.set_enabled(enable)
    except OSError as exc:
        print(f"Settings saved, but Windows start-up could not be changed: {exc}")
        return EXIT_OS_ERROR
    if enable:
        print("Always-on is ON: Jarvis starts when you sign in, stays in the system tray (no window) and listens for "
              f"\"{ctx.settings.wake_word.title()}\". Open it from the tray icon or the activation hotkey; closing the "
              "window only hides it. Start it now with: python run.py --minimized\nUndo: python run.py --always-on-off")
    else:
        print("Always-on is OFF (no start with Windows, normal window start, hands-free off).")
    return EXIT_OK


def _eval_quality(ctx: AppContext, model: str | None) -> int:
    from datetime import datetime

    from app import quality_eval
    from app.ai.base import ChatMessage
    from app.ai.ollama import strip_think
    from app.ai.prompts import build_system_prompt
    from app.cli import create_ai_manager
    from app.utils.async_runner import AsyncRunner

    if model:  # --model must really change what answers (the fast/smart models would otherwise take over)
        from app.ai.manager import AIManager
        from app.memory.settings_store import SettingsStore

        pinned = ctx.settings.model_copy(update={"ai_model": model, "ai_fast_model": "", "ai_smart_model": "",
                                                 "ai_coding_model": ""})
        ai = AIManager.from_settings(pinned, SettingsStore(ctx.database))
    else:
        ai = create_ai_manager(ctx)
    runner = AsyncRunner("quality").start()
    system = build_system_prompt(assistant_name=ctx.settings.assistant_name, now=datetime.now(), reply_language="en",
                                 spoken=True)
    used = {"model": model or ctx.settings.ai_model or "?"}

    async def ask(question: str) -> str:
        reply = await ai.chat([ChatMessage.system(system), ChatMessage.user(question)])
        used["model"] = reply.model
        return strip_think(reply.message.content or "")

    from app.agent.tool_router import select_specs
    from app.tools.catalog import build_default_registry

    registry = build_default_registry()

    async def ask_tools(question: str) -> list[tuple[str, dict]]:
        """What the model would call, given the tools the router would really offer for this request."""
        specs = select_specs(registry, question)
        reply = await ai.chat([ChatMessage.system(system), ChatMessage.user(question)], tools=specs)
        return [(call.name, dict(call.arguments)) for call in reply.message.tool_calls]

    questions = quality_eval.load_questions(ctx.paths.data_dir) + [dict(i) for i in quality_eval.TOOL_ITEMS]
    print(f"Answer quality (dataset {quality_eval.DATASET_VERSION}, {len(questions)} questions)...\n")
    sample, usage = quality_eval.make_sampler()
    try:
        # a cold model can take a minute to load; that is a start-up cost, not a wrong answer, so load it first
        print("Loading the model (not scored)...")
        try:
            runner.run(ask("Say OK."), timeout=600)
        except Exception as exc:
            print(f"  warm-up failed ({type(exc).__name__}); scoring anyway.")
        scores = runner.run(quality_eval.evaluate(ask, questions, retries=1, ask_tools=ask_tools, sample=sample),
                            timeout=3600)
    finally:
        try:
            runner.run(ai.aclose(), timeout=10)
        except Exception:
            pass
    for s in scores:
        print(f"{'OK  ' if s.ok else 'MISS'} {s.question[:60]!r:64} {s.reason}")
        if not s.ok and s.answer:
            print(f"       it said: {' '.join(s.answer.split())[:300]}")
    before = quality_eval.previous(ctx.paths.data_dir, used["model"])
    result = quality_eval.save(ctx.paths.data_dir, used["model"], scores, usage())
    print(f"\n{used['model']}: {result['right']}/{result['questions']} right ({result['score']}%)")
    for line in quality_eval.report_lines(result):
        print(line)
    env = result["environment"]
    print(f"  measured on: {env['os']}, {env['cpus']} CPUs, {env['ram_gb']} GB RAM, GPU: {env['gpu']}; "
          f"CPU/memory while answering: {result['usage'] or 'not measured'}")
    for line in quality_eval.compare(before, result):
        print(line)
    return EXIT_OK


def _benchmark_models(ctx: AppContext, priority: str) -> int:
    """Measure every installed chat model on THIS PC (speed, load time, GPU share) and recommend one for ``priority``."""
    from app import quality_eval
    from app.ai import modelbench as MB
    from app.ai.base import ChatMessage
    from app.ai.health import could_ever_fit, total_memory_bytes
    from app.ai.manager import AIManager
    from app.ai.switching import load_evidence
    from app.cli import create_ai_manager
    from app.memory.settings_store import SettingsStore
    from app.utils.async_runner import AsyncRunner

    base = create_ai_manager(ctx)
    runner = AsyncRunner("modelbench").start()
    data_dir = ctx.paths.data_dir
    try:
        models = [m for m in runner.run(base.models(), timeout=30)
                  if m.can_chat and not m.is_embedding_only and could_ever_fit(m.size_bytes, total_memory_bytes())]
    except Exception as exc:
        print(f"Could not list the installed models ({type(exc).__name__}). Is Ollama running?")
        return EXIT_CHECK_FAILED
    if not models:
        print("No installed chat model found. Pull one with Ollama first.")
        return EXIT_CHECK_FAILED
    print(f"Measuring {len(models)} model(s) on this PC (each is unloaded first so the load time is real)...\n")
    gpu = quality_eval._read_gpu()
    env = quality_eval.environment() | ({"gpu": f"{gpu['total_mb']:.0f} MB total"} if gpu else {})
    for info in models:
        pinned = ctx.settings.model_copy(update={"ai_model": info.name, "ai_fast_model": "", "ai_smart_model": "",
                                                 "ai_coding_model": ""})
        ai = AIManager.from_settings(pinned, SettingsStore(ctx.database))

        async def chat(model: str, prompt: str, ai=ai):
            return await ai.chat([ChatMessage.user(prompt)])

        freed = runner.run(MB.unload_all(ai.provider), timeout=60)
        if freed:
            print(f"  (unloaded {', '.join(freed)} from GPU memory first so {info.name} gets the whole GPU)")
        before = quality_eval._read_gpu()
        if before:
            print(f"  GPU memory in use by other programs before {info.name} loads: {before['used_mb']:.0f} of "
                  f"{before['total_mb']:.0f} MB (Windows, browser, games and Jarvis's own window count)")
        try:
            speed = runner.run(MB.measure(chat, info.name, unload=ai.provider.unload,
                                          gpu_share=getattr(ai.provider, "gpu_share", None),
                                          read_gpu=quality_eval._read_gpu, environment=env), timeout=900)
        except Exception as exc:
            print(f"  {info.name}: failed ({type(exc).__name__}: {str(exc)[:80]})")
            continue
        finally:
            try:
                runner.run(ai.provider.unload(info.name), timeout=30)       # release GPU memory before the next one
                runner.run(ai.aclose(), timeout=10)
            except Exception:
                pass
        MB.save(data_dir, speed)
        print(f"  {info.name}: {speed.tokens_per_s or '-'} tok/s, load {speed.load_ms if speed.load_ms is not None else '-'} ms"
              + (f", {speed.gpu_share:.0%} on GPU" if speed.gpu_share is not None else "")
              + (f"  [{'; '.join(speed.errors[:2])}]" if speed.errors else ""))
    table = MB.rows(models, load_evidence(data_dir), MB.load_history(data_dir))
    vram_gb = gpu["total_mb"] / 1024 if gpu else None
    print("\n" + MB.table_text(table, vram_gb))
    rec = MB.recommend(table, priority, vram_gb)
    print(f"\nRecommendation for '{priority}': {rec.model or 'none yet'} - {rec.reason}")
    for line in rec.caveats:
        print("  note: " + line)
    if rec.unranked:
        print("  no quality benchmark yet (so not recommendable): " + ", ".join(rec.unranked)
              + "\n  run: python run.py --eval-quality --model <name>")
    print("\nNothing was changed. To use a model, set it in Settings (AI_MODEL / AI_FAST_MODEL / AI_SMART_MODEL).")
    return EXIT_OK


def _eval_bangla(ctx: AppContext, model: str | None) -> int:
    from datetime import datetime

    from app import eval_bangla
    from app.ai.base import ChatMessage
    from app.ai.ollama import strip_think
    from app.ai.prompts import build_system_prompt
    from app.cli import create_ai_manager
    from app.utils.async_runner import AsyncRunner

    settings = ctx.settings.model_copy(update={"ai_model": model}) if model else ctx.settings
    ai = create_ai_manager(ctx) if not model else None
    if ai is None:
        from app.ai.manager import AIManager

        ai = AIManager.from_settings(settings)
    runner = AsyncRunner("eval").start()
    system = build_system_prompt(assistant_name=settings.assistant_name, now=datetime.now(), reply_language="bn",
                                 spoken=True)

    async def ask(question: str) -> str:
        reply = await ai.chat([ChatMessage.system(system), ChatMessage.user(question)])
        ask.model = reply.model
        return strip_think(reply.message.content or "")

    ask.model = model or settings.ai_model or "?"

    async def run() -> list:
        return await eval_bangla.evaluate(ask)

    print(f"Bangla answer quality ({len(eval_bangla.QUESTIONS)} questions)...\n")
    try:
        scores = runner.run(run(), timeout=1800)
    finally:
        try:
            runner.run(ai.aclose(), timeout=10)
        except Exception:
            pass
    before = [r for r in eval_bangla.previous(ctx.paths.data_dir) if r.get("model") == ask.model]
    result = eval_bangla.save(ctx.paths.data_dir, ask.model, scores)
    print(f"\n{ask.model}: {result['fully_right']}/{result['questions']} fully right, score {result['score']}%, "
          f"{result['avg_ms']} ms per answer")
    if before:
        last = before[-1]
        print(f"Last time with {ask.model} ({last['at'][:16].replace('T', ' ')}): score {last['score']}% "
              f"-> {result['score'] - last['score']:+.1f} points")
    return EXIT_OK


def _selftest(ctx: AppContext) -> int:
    from app.cli import create_ai_manager
    from app.cli_voice import create_stt, create_tts
    from app.selftest import Parts, run_selftest, summary
    from app.voice.microphone import Microphone

    from app.utils.async_runner import AsyncRunner

    runner = AsyncRunner("selftest").start()  # one event loop for all AI calls (the connection is reused)
    ai = create_ai_manager(ctx)
    tts = create_tts(ctx)
    tts.wait_ready(60)
    parts = Parts(ctx.settings, ai=ai, stt=create_stt(ctx, background_upgrade=False), tts_engine=tts.engine,
                  microphone=Microphone(ctx.settings.microphone_device, enabled=ctx.settings.microphone_enabled),
                  models_dir=ctx.paths.models_dir, run_async=lambda coro: runner.run(coro, timeout=180),
                  before_microphone=lambda: print("   ● Microphone test: say something now (2 seconds)...", flush=True))
    print("Self-test...")
    results = run_selftest(parts, on_result=lambda r: print(r.line(), flush=True))
    print(summary(results))
    try:
        runner.run(ai.aclose(), timeout=10)
    except Exception:
        pass
    tts.close()
    return EXIT_CHECK_FAILED if any(r.status == "fail" for r in results) else EXIT_OK


def _setup_hand_model(ctx) -> int:
    from app.gesture import detector

    ok, why = detector.availability(ctx.paths.models_dir)
    if ok:
        print("Hand tracking is already set up.")
        return 0
    if "mediapipe package" in why:
        print("Missing: " + why.split(";")[0] + ".")
    print(f"Downloading the hand model once from {detector.MODEL_URL} ...")
    try:
        path = detector.download_model(ctx.paths.models_dir)
    except Exception as exc:
        print(f"Could not download it ({exc}). Nothing was changed.")
        return 1
    print(f"Saved {path}. Detection itself never uses the network.")
    return 0


def _validate(ctx: AppContext, area: str, *, launch: bool, full: bool) -> int:
    """Hardware validation (app/validation): real checks on this PC, saved to data\\validation. Pending is never reported as passed."""
    from app.validation import report as vr

    if area == "status":
        print(vr.status_text(ctx.paths.data_dir))
        return EXIT_OK
    reports = []
    for name in (("windows", "voice", "phone", "gesture") if area == "all" else (area,)):
        print(f"\n=== {name} ===", flush=True)
        if name == "windows":
            from app.validation import windows_checks as wc

            rep = wc.run(wc.RealEnv(), launch=launch)
        elif name == "voice":
            from app.cli_voice import create_tts
            from app.validation import voice_checks as vc

            tts = create_tts(ctx)
            try:
                tts.start()
                tts.wait_ready(20)
            except Exception:
                tts = None
            rep = vc.run(vc.RealVoiceEnv(ctx, tts), ctx.paths.data_dir)
        elif name == "gesture":
            from app.validation import gesture_checks as gc

            rep = gc.run(gc.RealGestureEnv(ctx))
        else:
            from app.validation import phone_checks as pc

            rep = pc.run(pc.RealPhoneEnv(ctx), full=full)
        print(rep.describe())
        print(f"Saved: {vr.save(ctx.paths.data_dir, rep)}")
        reports.append(rep)
    return EXIT_CHECK_FAILED if any(r.verdict == "FAILED" for r in reports) else EXIT_OK


def _benchmark(ctx: AppContext, *, record: bool, train: bool = False) -> int:
    from app import benchmark
    from app.cli_voice import create_stt
    from app.tools.applications import known_application
    from app.tools.base import ToolContext
    from app.voice.microphone import Microphone

    if record:
        import time as _time

        from app.voice.vad import create_vad

        mic = Microphone(ctx.settings.microphone_device, enabled=ctx.settings.microphone_enabled)

        def prompt(say: str, number: int, total: int) -> None:
            print(f"\n[{number}/{total}] Say:  {benchmark.spoken_hint(say)}", flush=True)

        def recorder():
            _time.sleep(0.6)  # read the sentence first; the noise level is measured before you speak
            print("   ● Speak now...", flush=True)
            # a longer pause before stopping: short commands ("Volume up") were being cut off
            recording = mic.record(silence_timeout=1.6, start_timeout=6, max_seconds=10,
                                   vad=create_vad(ctx.settings.vad_engine))
            audio = recording.audio
            if not benchmark.audio_ok(audio):
                # The voice detector didn't notice you (quiet speech): record a fixed window instead,
                # so there is always something to learn from, and cut it down to the speech.
                print("   ● I didn't catch the start. Speak now (4 seconds)...", flush=True)
                recording = mic.record(fixed_seconds=4.0)
                audio = benchmark.trim_silence(recording.audio)
            print(f"   {audio.size / 16000:.1f} s recorded, loudest {recording.peak_db:.0f} dB", flush=True)
            advice = benchmark.level_advice(recording.peak_db)
            if advice:
                print(advice, flush=True)
            return audio

        def retry(say: str) -> None:
            print(f"   I didn't hear that clearly. Please say it again:  {say}", flush=True)

        count = benchmark.record(ctx.paths.data_dir, recorder, prompt, retry)
        print(f"\nRecorded {count} command(s) into {benchmark.folder(ctx.paths.data_dir)}. Now run --benchmark.")
        return EXIT_OK
    stt = create_stt(ctx, background_upgrade=False)
    tool_ctx = ToolContext(ctx.settings, ctx.paths, ctx.database, ctx.config)
    print("Speech recognition: loading...", flush=True)
    stt.load()
    print(f"Speech recognition: {stt.description}\n")
    language = None if ctx.settings.voice_language == "auto" else ctx.settings.voice_language
    if train:
        print("Learning how you say your commands...")
        result = benchmark.train(ctx.paths.data_dir, lambda audio: stt.transcribe(audio, language),
                                 lambda name: known_application(name, tool_ctx))
        return EXIT_OK if result else EXIT_CHECK_FAILED
    result = benchmark.run(ctx.paths.data_dir, lambda audio: stt.transcribe(audio, language),
                           lambda name: known_application(name, tool_ctx))
    return EXIT_OK if result else EXIT_CHECK_FAILED


def print_checks(checks: Sequence[Check], stream: TextIO) -> None:
    labels = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[ -- ]"}
    width = max(len(c.name) for c in checks)
    for check in checks:
        stream.write(f"{labels[check.status]} {check.name.ljust(width)}  {check.detail}\n")


# -------------------------------------------------------------------------- CLI


_WINDOWED = False  # JARVIS.exe (no console): errors are shown in a message box instead


def _configure_console() -> None:
    global _WINDOWED
    # The windowed JARVIS.exe has no console: sys.stdout/stderr are None and any print would crash.
    if sys.stdout is None or sys.stderr is None:
        _WINDOWED = True
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the process
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    # Windows consoles may use a legacy code page; never crash on output.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def show_message_box(title: str, message: str) -> None:
    """A plain Windows message box (works before Qt is loaded, and without it)."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass


def _error(message: str) -> None:
    if _WINDOWED:
        show_message_box(__app_name__, message.rstrip())
    elif sys.stderr is not None:
        sys.stderr.write(message.rstrip() + "\n")


def _restart(ctx, minimized: bool) -> int:
    from app.automation.background import instance_lock_path
    from app.core import relaunch

    ok, message = relaunch.cli_restart(ctx.paths.data_dir, instance_lock_path(), minimized=minimized)
    print(message)
    return EXIT_OK if ok else EXIT_CHECK_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description=f"{__app_name__} {__version__}")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--doctor", action="store_true", help="run diagnostics and exit")
    mode.add_argument("--selftest", action="store_true", help="try every feature for real and report pass/fail")
    mode.add_argument("--record-benchmark", action="store_true", help="record your voice commands for --benchmark")
    mode.add_argument("--benchmark", action="store_true", help="measure recognition and command matching on them")
    mode.add_argument("--train-voice", action="store_true", help="learn how you pronounce your commands (from them)")
    mode.add_argument("--eval-bangla", action="store_true", help="score the AI's Bangla answers on a fixed question set")
    mode.add_argument("--self-improve", action="store_true",
                      help="test better AI set-ups on your real requests; keep one only if it scores better")
    mode.add_argument("--export-training", action="store_true",
                      help="write your good requests as chat JSONL (data/exports) for an optional fine-tune")
    mode.add_argument("--export-speech-dataset", metavar="LANG", default=None,
                      help="write your corrected voice recordings (SPEECH_DATASET) for scripts/finetune_whisper.py")
    mode.add_argument("--model-roles", action="store_true",
                      help="which installed model for which job (facts + your benchmarks); add --apply to save")
    mode.add_argument("--compare-models", nargs="?", const="", metavar="MODELS",
                      help="score installed models (or a comma-separated list) on the Bangla questions")
    mode.add_argument("--memory-eval", action="store_true",
                      help="check the memory rules (updates, history, forgetting, expiry, privacy) on a throw-away database")
    mode.add_argument("--longmem-eval", nargs="?", const="", metavar="FILE",
                      help="long-term memory retrieval check in the LongMemEval format (built-in cases, or a dataset file); add --full to also ask the model")
    mode.add_argument("--ground-trial", metavar="MODEL",
                      help="measure how well an installed vision model points at controls of the foreground window (clicks nothing)")
    mode.add_argument("--health-report", action="store_true",
                      help="self-test + benchmark + Bangla score + speed + mistakes, in one report")
    mode.add_argument("--make-jarvis-model", action="store_true",
                      help="make one model called 'jarvis' from the best installed one (or --model), remove the rest")
    mode.add_argument("--backup-db", action="store_true", help="save a verified copy of the database (data\\backups)")
    mode.add_argument("--restore-db", type=Path, metavar="BACKUP", help="replace the database with a backup (close Jarvis first)")
    mode.add_argument("--test-restore", nargs="?", const="newest", metavar="BACKUP",
                      help="prove a backup can be restored: open it in a throw-away copy and check it (default: the newest)")
    mode.add_argument("--eval-quality", action="store_true",
                      help="score the AI's answers on a reference set (includes 'admit you don't know' questions)")
    mode.add_argument("--validate", choices=("windows", "voice", "phone", "gesture", "all", "status"), metavar="AREA",
                      help="run checks on THIS PC's real hardware and record the result: windows | voice | phone | gesture | all | status")
    mode.add_argument("--benchmark-models", action="store_true",
                      help="measure every installed model on this PC (speed, load time, GPU share) and recommend one for "
                           "--priority; changes nothing")
    mode.add_argument("--perf", action="store_true",
                      help="check that written commands are understood and time the main internal steps")
    mode.add_argument("--lab", action="store_true",
                      help="reliability lab: permissions, task recovery, phone replay protection, memory and commands "
                           "checked with harmless test tools (nothing on your PC or phone is touched)")
    mode.add_argument("--rollback-update", action="store_true",
                      help="restore the database from just before the last update (close Jarvis first; the program "
                           "itself is not reinstalled)")
    mode.add_argument("--audit-accessibility", action="store_true",
                      help="check the app's colours against WCAG contrast (control names are checked by the test-suite)")
    mode.add_argument("--export-tokens", action="store_true",
                      help="print the UI design tokens (colours, fonts, spacing) as JSON")
    mode.add_argument("--background", action="store_true",
                      help="run scheduled automations without the window (no voice, no approvals; see docs/STATUS.md)")
    mode.add_argument("--restart", action="store_true", help="restart the running Jarvis (or start it if it isn't running)")
    mode.add_argument("--info", action="store_true", help="print paths and settings instead of opening the app")
    mode.add_argument("--models", action="store_true", help="list installed Ollama models and exit")
    mode.add_argument("--chat", action="store_true", help="text chat with the assistant in this terminal")
    mode.add_argument("--voice", action="store_true", help="push-to-talk voice chat in this terminal")
    mode.add_argument("--tools", action="store_true", help="list the tools the assistant can use")
    mode.add_argument("--windows", action="store_true", help="diagnostic: list open windows as the assistant sees them")
    mode.add_argument("--browser-profiles", action="store_true", help="diagnostic: list Chrome/Edge/Brave profiles")
    mode.add_argument("--find-app", metavar="NAME", help="diagnostic: show how an application name is found")
    mode.add_argument("--memory", nargs="?", const="", metavar="QUERY", help="list (or search) saved memories")
    mode.add_argument("--forget", type=int, metavar="ID", help="delete one saved memory")
    mode.add_argument("--screen-test", nargs="?", type=int, const=3, metavar="SECONDS",
                      help="diagnostic: after a countdown, capture the active window and read its text")
    mode.add_argument("--devices", action="store_true", help="list microphones and speakers")
    mode.add_argument("--mic-test", action="store_true", help="test the microphone level and speech recognition")
    mode.add_argument("--wake-doctor", action="store_true",
                      help="say the wake phrase a few times, see what was heard, and teach Jarvis your spelling of its name")
    mode.add_argument("--voice-doctor", action="store_true",
                      help="measure EVERY microphone while you speak and say which to use / what to fix when Jarvis can't hear you")
    mode.add_argument("--always-on", action="store_true",
                      help="run as a background personal assistant: start with Windows, live in the tray, listen hands-free")
    mode.add_argument("--always-on-off", action="store_true", help="undo --always-on")
    mode.add_argument("--say", metavar="TEXT", help="speak TEXT with the configured voice")
    mode.add_argument("--setup-hand-model", action="store_true", help="one-time, on request: download the local hand-landmark model for gesture control")
    mode.add_argument("--setup-voice", action="store_true", help="download the speech and voice models")
    parser.add_argument("--launch", action="store_true", help="with --validate windows: open Notepad to test real windows, then close it")
    parser.add_argument("--full", action="store_true", help="with --validate phone: also ring the phone and test reconnecting")
    parser.add_argument("--priority", choices=("balanced", "speed", "quality", "low_resource"), default="balanced",
                        help="with --benchmark-models: what to optimise for")
    parser.add_argument("--root", type=Path, default=None, help="use a different application folder")
    parser.add_argument("--model", default=None, help="with --eval-bangla: the model to test; with --make-jarvis-model: the base")
    parser.add_argument("--restart-after", type=int, default=None, metavar="PID", help=argparse.SUPPRESS)  # wait for PID to exit first
    parser.add_argument("--minimized", action="store_true", help="start the desktop app in the tray")
    parser.add_argument("--intro-child", action="store_true", help=argparse.SUPPRESS)  # the start-up intro process
    parser.add_argument("--intro-title", default="J.A.R.V.I.S.", help=argparse.SUPPRESS)
    parser.add_argument("--intro-subtitle", default="", help=argparse.SUPPRESS)
    parser.add_argument("--intro-owner", default="", help=argparse.SUPPRESS)
    parser.add_argument("--stats-child", action="store_true", help=argparse.SUPPRESS)  # the dashboard's process lister
    parser.add_argument("--mcp-server-token", action="store_true", help="create (or replace) the token for the local read-only MCP server and show it once")
    parser.add_argument("--write-env", action="store_true", help="create .env with every setting at its default (an existing .env is never overwritten)")
    parser.add_argument("--a2a-server-token", action="store_true", help="create (or replace) the token for the local read-only A2A agent server and show it once")
    parser.add_argument("--a2a-server-revoke", action="store_true", help="delete the local A2A server token (it stops accepting every agent)")
    parser.add_argument("--mcp-server-revoke", action="store_true", help="delete the local MCP server token (it stops accepting every client)")
    parser.add_argument("--capabilities", action="store_true",
                        help="print what works offline / on the local network / with internet, and every tool's risk")
    parser.add_argument("--reset-gl-guard", action="store_true",
                        help="undo the automatic graphics step-down after a GPU-driver crash at start-up")
    parser.add_argument("--apply", action="store_true", help="with --model-roles: offer to save the recommendation")
    parser.add_argument("--switch", action="store_true", help="with --compare-models: make 'jarvis' from the winner")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    if args.restart_after:                       # started by "restart yourself": let the old Jarvis finish exiting first
        from app.core import relaunch

        relaunch.wait_for_exit(args.restart_after)
    if args.capabilities:  # no settings or database needed: the registered tools + the reviewed feature table
        from app.core.capabilities import markdown
        from app.tools.catalog import build_default_registry

        print(markdown(build_default_registry()))
        return EXIT_OK
    if args.mcp_server_token or args.mcp_server_revoke:   # the token of the local read-only MCP server: only a person at the PC can create it
        from app.mcp import server as mcp_server

        ctx = bootstrap(args.root)
        if args.mcp_server_revoke:
            print("Token deleted." if mcp_server.revoke_token(ctx.paths.data_dir) else "There was no token.")
            return EXIT_OK
        print("Token (shown once, keep it secret): " + mcp_server.create_token(ctx.paths.data_dir)
              + "\nSet MCP_SERVER_ENABLED=true and restart Jarvis; clients call http://127.0.0.1:%d/mcp with 'Authorization: Bearer <token>'." % ctx.settings.mcp_server_port)
        return EXIT_OK
    if args.write_env:
        from app.config.env_template import write_default_env

        target = Path(args.root or default_root()).resolve() / ".env"
        print(f"Created {target}." if write_default_env(target) else f"{target} already exists; it was not changed.")
        return EXIT_OK
    if args.a2a_server_token or args.a2a_server_revoke:   # the token of the local read-only A2A server: only a person at the PC can create it
        from app.core import a2a_server

        ctx = bootstrap(args.root)
        if args.a2a_server_revoke:
            print("Token deleted." if a2a_server.revoke_token(ctx.paths.data_dir) else "There was no token.")
            return EXIT_OK
        print("Token (shown once, keep it secret): " + a2a_server.create_token(ctx.paths.data_dir)
              + "\nSet A2A_SERVER_ENABLED=true and restart Jarvis; the agent card is http://127.0.0.1:%d/.well-known/agent-card.json and calls go to /a2a with 'Authorization: Bearer <token>'." % ctx.settings.a2a_server_port)
        return EXIT_OK
    if args.stats_child:  # lists processes for the dashboard in its own process (never stalls the animation)
        from app.ui.stats_process import run_stats_child

        return run_stats_child()
    if args.intro_child:  # the intro runs on its own: no settings, no database, just the animation
        from app.ui.intro_process import run_intro_child

        return run_intro_child(args.intro_title, args.intro_subtitle, args.intro_owner)
    install_exception_hooks()
    exit_code = EXIT_OK
    try:
        try:
            ctx = bootstrap(args.root)
        except ConfigError as exc:
            _error(f"Configuration problem:\n{exc}\n\nFix the value above and start again. "
                   "Deleting a file in the config folder restores its defaults.")
            get_logger("main").error("Configuration error", extra={"event": "config_error", "detail": str(exc)})
            return EXIT_CONFIG_ERROR
        except StorageError as exc:
            _error(f"Storage problem: {exc}")
            get_logger("main").error("Storage error", extra={"event": "storage_error", "detail": str(exc)})
            return EXIT_STORAGE_ERROR
        except OSError as exc:
            _error(f"Could not prepare the application folders: {exc}")
            return EXIT_OS_ERROR

        if args.selftest:
            exit_code = _selftest(ctx)
        elif args.eval_bangla:
            exit_code = _eval_bangla(ctx, getattr(args, "model", None))
        elif args.self_improve:
            exit_code = _self_improve(ctx)
        elif args.export_training:
            from app.agent.self_improve import export_training

            from app.tools.catalog import build_default_registry

            path = ctx.paths.data_dir / "exports" / "training.jsonl"
            count = export_training(ctx.database, path, registry=build_default_registry())
            print(f"Wrote {count} examples to {path} (redacted). Optional fine-tune on a GPU PC: "
                  f"python scripts/finetune_llm.py {path}")
        elif args.export_speech_dataset:
            from app.voice.speech_dataset import SpeechDataset

            target = ctx.paths.data_dir / "exports" / f"speech_{args.export_speech_dataset}"
            count = SpeechDataset(ctx.paths.data_dir).export(args.export_speech_dataset, target)
            print(f"Exported {count} corrected recordings to {target}." if count else
                  "Nothing to export yet: turn SPEECH_DATASET on and correct me ('no, I said ...') when I mishear.")
            if count:
                print(f"Next (GPU PC): python scripts/finetune_whisper.py {target}")
        elif args.reset_gl_guard:
            from app.ui.core3d import gl_guard

            gl_guard.reset(ctx.paths.data_dir)
            print("Graphics restored: the intro, fades, quality and the 3D core follow your settings again.")
        elif args.model_roles:
            exit_code = _model_roles(ctx, args.apply)
        elif args.compare_models is not None:
            exit_code = _compare_models(ctx, args.compare_models or None, args.switch)
        elif args.memory_eval:
            from app import memory_eval

            results = memory_eval.run()
            print(memory_eval.report(results))
            exit_code = EXIT_OK if all(r.passed for r in results) else EXIT_CHECK_FAILED
        elif args.longmem_eval is not None:
            exit_code = _longmem_eval(ctx, args.longmem_eval, args.full)
        elif args.ground_trial:
            exit_code = _ground_trial(ctx, args.ground_trial)
        elif args.health_report:
            exit_code = _health_report(ctx)
        elif args.restart:
            exit_code = _restart(ctx, args.minimized)
        elif args.always_on or args.always_on_off:
            exit_code = _always_on(ctx, args.always_on)
        elif args.benchmark_models:
            exit_code = _benchmark_models(ctx, args.priority)
        elif args.eval_quality:
            exit_code = _eval_quality(ctx, getattr(args, "model", None))
        elif args.perf:
            from app.core import selfbench

            result = selfbench.run(ctx.paths.data_dir)
            exit_code = EXIT_OK if not result["suite"]["misses"] else EXIT_CHECK_FAILED
        elif args.lab:
            from app.core import reliability_lab

            lab = reliability_lab.run()
            summary = lab.summary()
            notes = reliability_lab.compare(reliability_lab.last_run(ctx.paths.data_dir), summary)
            print("\n".join(reliability_lab.report_lines(lab, notes)))
            reliability_lab.save(ctx.paths.data_dir, summary)
            exit_code = EXIT_OK if summary["passed"] == summary["cases"] and not notes else EXIT_CHECK_FAILED
        elif args.rollback_update:
            from app.core import update_guard

            try:
                done = update_guard.rollback(ctx.paths)
            except StorageError as exc:
                _error(str(exc))
                exit_code = EXIT_STORAGE_ERROR
            else:
                print(f"Database restored from {done['restored_from']}.\nThe database from before the rollback was kept as "
                      f"{done['previous_database_kept_as']}.\n{done['next_step']}")
        elif args.test_restore:
            from app.memory import backup

            found = backup.list_backups(ctx.paths.data_dir / "backups")
            target = found[0] if args.test_restore == "newest" and found else Path(args.test_restore)
            if args.test_restore == "newest" and not found:
                print("There is no backup yet. Run: python run.py --backup-db")
                exit_code = EXIT_STORAGE_ERROR
            else:
                outcome = backup.restore_test(target)
                print(outcome.message())
                exit_code = EXIT_OK if outcome.ok else EXIT_STORAGE_ERROR
        elif args.audit_accessibility:
            from app.core import a11y

            findings = a11y.audit_theme()
            print(a11y.report(findings))
            exit_code = EXIT_OK if not findings else EXIT_CHECK_FAILED
        elif args.export_tokens:
            import json as _json

            from app.ui.styles.tokens import as_dict

            print(_json.dumps(as_dict(), indent=2))
            exit_code = EXIT_OK
        elif args.background:
            from app.automation.background import run_background

            exit_code = run_background(ctx, __version__)
        elif args.backup_db or args.restore_db:
            exit_code = _database_backup(ctx, args.restore_db)
        elif args.make_jarvis_model:
            exit_code = _make_jarvis_model(ctx, getattr(args, "model", None))
        elif args.validate:
            exit_code = _validate(ctx, args.validate, launch=args.launch, full=args.full)
        elif args.record_benchmark or args.benchmark or args.train_voice:
            exit_code = _benchmark(ctx, record=args.record_benchmark, train=args.train_voice)
        elif args.doctor:
            from app.cli import probe_ai

            checks = collect_checks(ctx, probe_ai(ctx), include_voice=True)
            print_checks(checks, sys.stdout)
            exit_code = EXIT_CHECK_FAILED if any(c.status == "fail" for c in checks) else EXIT_OK
        elif args.models:
            from app.cli import run_models

            exit_code = run_models(ctx)
        elif args.tools:
            from app.cli import run_tools

            exit_code = run_tools(ctx)
        elif args.windows:
            from app.cli import run_windows

            exit_code = run_windows(ctx)
        elif args.memory is not None:
            from app.cli import run_memory

            exit_code = run_memory(ctx, args.memory or None)
        elif args.forget is not None:
            from app.cli import run_forget

            exit_code = run_forget(ctx, args.forget)
        elif args.screen_test is not None:
            from app.cli import run_screen_test

            exit_code = run_screen_test(ctx, args.screen_test)
        elif args.browser_profiles:
            from app.cli import run_browser_profiles

            exit_code = run_browser_profiles(ctx)
        elif args.find_app is not None:
            from app.cli import run_find_app

            exit_code = run_find_app(ctx, args.find_app)
        elif args.setup_hand_model:
            exit_code = _setup_hand_model(ctx)
        elif args.chat:
            from app.cli import run_chat

            exit_code = run_chat(ctx)
        elif args.voice or args.devices or args.mic_test or args.voice_doctor or args.wake_doctor or args.say is not None or args.setup_voice:
            from app import cli_voice

            if args.voice:
                exit_code = cli_voice.run_voice(ctx)
            elif args.devices:
                exit_code = cli_voice.run_devices(ctx)
            elif args.mic_test:
                exit_code = cli_voice.run_mic_test(ctx)
            elif args.voice_doctor:
                exit_code = cli_voice.run_voice_doctor(ctx)
            elif args.wake_doctor:
                exit_code = cli_voice.run_wake_doctor(ctx)
            elif args.setup_voice:
                exit_code = cli_voice.run_setup_voice(ctx)
            else:
                exit_code = cli_voice.run_say(ctx, args.say)
        elif not args.info and importlib.util.find_spec("PySide6") is not None:
            try:
                from app.ui.app import run_gui
            except (ImportError, OSError) as exc:  # Qt present but cannot load (missing system libraries)
                get_logger("main").error("desktop app unavailable", exc_info=True)
                _error(f"The desktop app could not start: {exc}\n"
                       "Reinstall it with 'pip install --force-reinstall PySide6', or use the terminal modes "
                       "('python run.py --chat' / '--voice').")
                exit_code = EXIT_OS_ERROR
            else:
                try:
                    exit_code = run_gui(ctx, [sys.argv[0], *(["--minimized"] if args.minimized else [])])
                except Exception as exc:  # never disappear silently
                    get_logger("main").critical("desktop app crashed", exc_info=True)
                    _error(f"{__app_name__} stopped because of an unexpected error: {exc}\n\n"
                           f"Details are in {ctx.log_file}")
                    exit_code = EXIT_OS_ERROR
        else:
            s = ctx.settings
            print(f"{__app_name__} {__version__}")
            print(f"  Root      : {ctx.paths.root}")
            print(f"  Database  : {ctx.paths.db_path} (schema v{ctx.database.schema_version()})")
            print(f"  Log file  : {ctx.log_file}")
            print(f"  AI        : {s.ai_provider} at {s.ollama_host} (model: {s.ai_model or 'auto-detect'})")
            print("Try:  python run.py (desktop app) | --doctor | --chat | --voice   (--help for all)")
            if importlib.util.find_spec("PySide6") is None:
                print("The desktop app needs PySide6: pip install -r requirements.txt")
        log_event(get_logger("main"), "shutdown", exit_code=exit_code)
        return exit_code
    finally:
        teardown_logging()
