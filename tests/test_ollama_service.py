"""Starting the installed Ollama app when it isn't running."""

from __future__ import annotations

from pathlib import Path

from app.tools import winapi
from app.utils import ollama_service


def test_finds_the_installed_app(tmp_path: Path, monkeypatch) -> None:
    app = tmp_path / "Programs" / "Ollama" / "ollama app.exe"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    assert ollama_service.find_ollama_app() == app


def test_prefers_the_windowless_server(tmp_path: Path, monkeypatch) -> None:
    """`ollama serve` runs in the background; the Ollama app would open a window over Jarvis."""
    folder = tmp_path / "Programs" / "Ollama"
    folder.mkdir(parents=True)
    (folder / "ollama app.exe").write_bytes(b"exe")
    (folder / "ollama.exe").write_bytes(b"exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    programs, files = [], []
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(winapi, "start_program", lambda exe, args, **kw: programs.append((exe, args)))
    monkeypatch.setattr(winapi, "start_file", files.append)
    monkeypatch.setattr(ollama_service, "ollama_running", lambda: False)
    assert ollama_service.start_ollama()
    assert programs == [(str(folder / "ollama.exe"), ["serve"])] and files == []


def test_starts_only_when_installed_and_not_running(tmp_path: Path, monkeypatch) -> None:
    started = []
    monkeypatch.setattr(winapi, "is_windows", lambda: True)
    monkeypatch.setattr(winapi, "start_file", started.append)
    monkeypatch.setattr(ollama_service, "find_ollama_app", lambda: tmp_path / "ollama app.exe")
    monkeypatch.setattr(ollama_service, "find_ollama_server", lambda: None)  # only the app is there
    monkeypatch.setattr(ollama_service, "ollama_running", lambda: True)
    assert not ollama_service.start_ollama() and started == []  # already running
    monkeypatch.setattr(ollama_service, "ollama_running", lambda: False)
    assert ollama_service.start_ollama() and started == [str(tmp_path / "ollama app.exe")]
    monkeypatch.setattr(ollama_service, "find_ollama_app", lambda: None)
    assert not ollama_service.start_ollama()  # not installed: nothing to start
