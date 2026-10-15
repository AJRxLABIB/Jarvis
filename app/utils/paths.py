"""Filesystem locations used by the application.

Development (running from source): everything lives inside the project folder.
Packaged (JARVIS.exe): writable files live in %LOCALAPPDATA%\\JarvisAssistant,
because Program Files is not writable for normal users.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "JarvisAssistant"


def is_frozen() -> bool:
    """True when running as a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """Folder that contains ``run.py`` when running from source."""
    return Path(__file__).resolve().parents[2]


def default_root() -> Path:
    """Writable application root for the current run mode."""
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    return project_root()


@dataclass(frozen=True)
class AppPaths:
    root: Path
    env_file: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    models_dir: Path
    db_path: Path

    @classmethod
    def build(cls, root: Path, data_dir: Path | None = None) -> "AppPaths":
        root = Path(root).resolve()
        data = Path(data_dir).resolve() if data_dir else root / "data"
        return cls(
            root=root,
            env_file=root / ".env",
            config_dir=root / "config",
            data_dir=data,
            logs_dir=data / "logs",
            models_dir=data / "models",
            db_path=data / "assistant.db",
        )

    def ensure_dirs(self) -> None:
        """Create writable folders. Raises OSError with the offending path."""
        for directory in (self.config_dir, self.data_dir, self.logs_dir, self.models_dir):
            directory.mkdir(parents=True, exist_ok=True)
