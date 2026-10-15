from __future__ import annotations

from pathlib import Path

import pytest

from app.main import EXIT_CONFIG_ERROR, EXIT_OK, bootstrap, collect_checks, main


def test_bootstrap_creates_everything(tmp_path: Path) -> None:
    ctx = bootstrap(tmp_path, environ={})
    assert (tmp_path / "config" / "apps.json").is_file()
    assert (tmp_path / "data" / "assistant.db").is_file()
    assert ctx.log_file == tmp_path / "data" / "logs" / "jarvis.log"
    assert ctx.log_file.read_text(encoding="utf-8").count('"event": "startup"') == 1


def test_custom_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "elsewhere"
    ctx = bootstrap(tmp_path / "root", environ={"JARVIS_DATA_DIR": str(data)})
    assert ctx.paths.db_path == data / "assistant.db"
    assert ctx.paths.db_path.is_file()


def test_required_checks_pass(tmp_path: Path) -> None:
    checks = {c.name: c for c in collect_checks(bootstrap(tmp_path, environ={}))}
    for name in ("Data folder", "Database", "Config files", "Architecture"):
        assert checks[name].status == "ok", checks[name]


def test_main_info(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "--info"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Database" in out and "Log file" in out


def test_gui_that_cannot_load_is_reported_not_crashed(tmp_path: Path, capsys, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "app.ui.app":
            raise ImportError("libEGL.so.1: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: object() if name == "PySide6" else None)
    code = main(["--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code != EXIT_OK and "desktop app could not start" in err and "--chat" in err


def test_main_doctor_prints_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")  # nothing listens there
    main(["--root", str(tmp_path), "--doctor"])
    out = capsys.readouterr().out
    assert "Database" in out and "Config files" in out
    assert "[WARN] Ollama" in out and "not running" in out


def test_main_reports_config_error_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".env").write_text("AI_PROVIDER=skynet\n", encoding="utf-8")
    assert main(["--root", str(tmp_path)]) == EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "AI_PROVIDER" in err and "Traceback" not in err


def test_windowed_exe_shows_errors_in_a_message_box(tmp_path: Path, monkeypatch) -> None:
    """JARVIS.exe has no console (stdout/stderr are None): errors must be shown, and prints must not crash."""
    import sys

    from app import main as main_module

    boxes: list[str] = []
    monkeypatch.setattr(main_module, "show_message_box", lambda title, text: boxes.append(text))
    monkeypatch.setattr(main_module, "_WINDOWED", False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["--root", str(tmp_path), "--info"]) == EXIT_OK  # prints go nowhere instead of crashing
    (tmp_path / ".env").write_text("TTS_SPEED=fast\n", encoding="utf-8")
    assert main(["--root", str(tmp_path), "--info"]) == EXIT_CONFIG_ERROR
    assert boxes and "TTS_SPEED" in boxes[0]


def test_minimized_flag_is_accepted(tmp_path: Path, monkeypatch) -> None:
    from app.main import build_parser
    from app.utils import autostart

    assert build_parser().parse_args(["--minimized"]).minimized
    assert "--minimized" not in autostart.launch_command()  # at sign-in the interface is shown (START_MINIMIZED decides)


def test_packaged_app_keeps_data_in_local_app_data(tmp_path: Path, monkeypatch) -> None:
    """JARVIS.exe (PyInstaller sets sys.frozen): data goes to %LOCALAPPDATA%\\JarvisAssistant, not Program Files."""
    import sys

    from app.utils import autostart
    from app.utils.paths import default_root

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Users\me\AppData\Local\Programs\Jarvis\JARVIS.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_root() == tmp_path / "JarvisAssistant"
    assert autostart.launch_command() == r'"C:\Users\me\AppData\Local\Programs\Jarvis\JARVIS.exe"'
    ctx = bootstrap(default_root(), environ={})
    assert ctx.paths.env_file.parent == tmp_path / "JarvisAssistant" and ctx.paths.models_dir.is_dir()


def test_packaging_files_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    assert 'name="JARVIS", console=False' in spec and 'name="jarvis-cli", console=True' in spec
    compile(spec, "jarvis.spec", "exec")  # valid Python
    install = (root / "packaging" / "install.ps1").read_text(encoding="utf-8")
    assert "Programs\\Jarvis" in install and "JarvisAssistant" in install and "--minimized" not in install
    for script in ("build.ps1", "install.ps1", "uninstall.ps1", "make_icon.py", "make_version_info.py"):
        assert (root / "packaging" / script).is_file(), script
