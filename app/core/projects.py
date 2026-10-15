"""Project awareness: a profile of each project Jarvis works in, read from the project's own files.

Profile: languages / frameworks, dependencies (names only), entry points, how to run it, how to test it, how to
build it, the database it seems to use, git (branch, remote, uncommitted changes) and your own notes ("known
problems", added by voice). Nothing is guessed by the AI; every field says where it came from by being read from
a named file. Notes are kept in data\\projects.json; the rest is re-read each time (it can't go stale).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.world import project_stack
from app.tools.base import ToolError

NOTES_FILE = "projects.json"
SKIP_DIRS = frozenset({".git", "node_modules", "vendor", ".venv", "venv", "env", "__pycache__", "dist", "build",
                       ".idea", ".vscode", ".next", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".php", ".html", ".htm", ".css", ".scss", ".json", ".md", ".txt", ".go",
    ".rs", ".java", ".kt", ".cs", ".c", ".h", ".cpp", ".hpp", ".sql", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".env.example", ".xml", ".vue", ".svelte", ".dart", ".rb", ".sh", ".ps1", ".bat", ".gradle", ".twig", ".blade.php",
})


@dataclass
class Profile:
    name: str
    folder: str
    stack: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    run: str = ""
    test: str = ""
    build: str = ""
    database: str = ""
    git: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    lint: str = ""
    types: str = ""

    def describe(self) -> str:
        parts = [f"{self.name} ({', '.join(self.stack) or 'unknown stack'})"]
        for label, value in (("run", self.run), ("test", self.test), ("lint", self.lint), ("types", self.types),
                             ("build", self.build), ("database", self.database)):
            if value:
                parts.append(f"{label}: {value}")
        if self.entry_points:
            parts.append("entry: " + ", ".join(self.entry_points[:3]))
        if self.git:
            git = f"git {self.git.get('branch', '?')}"
            if self.git.get("changes"):
                git += f", {self.git['changes']} uncommitted change(s)"
            parts.append(git)
        if self.notes:
            parts.append("known problems: " + "; ".join(self.notes[:3]))
        return ". ".join(parts) + "."


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _dependencies(folder: Path) -> list[str]:
    deps: list[str] = []
    package = _read_json(folder / "package.json")
    for key in ("dependencies", "devDependencies"):
        if isinstance(package.get(key), dict):
            deps += list(package[key])
    requirements = folder / "requirements.txt"
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
            name = re.split(r"[<>=!~;\[ ]", line.strip(), maxsplit=1)[0]
            if name and not name.startswith(("#", "-")):
                deps.append(name)
    composer = _read_json(folder / "composer.json")
    if isinstance(composer.get("require"), dict):
        deps += [d for d in composer["require"] if d != "php"]
    gomod = folder / "go.mod"
    if gomod.is_file():
        deps += re.findall(r"^\s*([\w./-]+\.[\w./-]+)\s+v", gomod.read_text(encoding="utf-8", errors="ignore"), re.M)
    return sorted(set(deps))[:40]


def _entry_points(folder: Path) -> list[str]:
    candidates = ("run.py", "main.py", "app.py", "manage.py", "index.php", "index.js", "server.js", "src/main.ts",
                  "src/index.ts", "src/main.py", "app/main.py", "main.go", "src/main.rs", "lib/main.dart")
    return [c for c in candidates if (folder / c).is_file()]


def _test_command(folder: Path) -> str:
    scripts = _read_json(folder / "package.json").get("scripts") or {}
    if isinstance(scripts, dict) and "test" in scripts:
        return "npm test"
    if (folder / "tests").is_dir() or (folder / "pytest.ini").is_file() or \
            "[tool.pytest" in _text(folder / "pyproject.toml"):
        return "python -m pytest -q"
    if (folder / "go.mod").is_file():
        return "go test ./..."
    if (folder / "Cargo.toml").is_file():
        return "cargo test"
    if (folder / "artisan").is_file():
        return "php artisan test"
    return ""


def _build_command(folder: Path) -> str:
    scripts = _read_json(folder / "package.json").get("scripts") or {}
    if isinstance(scripts, dict) and "build" in scripts:
        return "npm run build"
    if (folder / "packaging" / "build.ps1").is_file():
        return r"powershell -File packaging\build.ps1"
    if (folder / "go.mod").is_file():
        return "go build ./..."
    if (folder / "Cargo.toml").is_file():
        return "cargo build"
    return ""


def _lint_command(folder: Path) -> str:
    """The project's own linter - only one it is actually configured for, never a guess."""
    scripts = _read_json(folder / "package.json").get("scripts") or {}
    if isinstance(scripts, dict) and "lint" in scripts:
        return "npm run lint"
    pyproject = _text(folder / "pyproject.toml")
    if (folder / "ruff.toml").is_file() or (folder / ".ruff.toml").is_file() or "[tool.ruff" in pyproject:
        return "python -m ruff check ."
    if (folder / ".flake8").is_file() or "[flake8]" in _text(folder / "setup.cfg") or (folder / "tox.ini").is_file() \
            and "[flake8]" in _text(folder / "tox.ini"):
        return "python -m flake8"
    if (folder / "go.mod").is_file():
        return "go vet ./..."
    return ""


def _types_command(folder: Path) -> str:
    scripts = _read_json(folder / "package.json").get("scripts") or {}
    if isinstance(scripts, dict) and "typecheck" in scripts:
        return "npm run typecheck"
    if (folder / "mypy.ini").is_file() or "[tool.mypy" in _text(folder / "pyproject.toml") \
            or "[mypy]" in _text(folder / "setup.cfg"):
        return "python -m mypy"
    return ""


def _text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _database(folder: Path, deps: list[str]) -> str:
    lowered = {d.lower() for d in deps}
    for names, label in ((("mysql", "mysql2", "mysqlclient", "pymysql"), "MySQL"),
                         (("pg", "psycopg2", "psycopg", "postgres"), "PostgreSQL"),
                         (("mongodb", "mongoose", "pymongo"), "MongoDB"),
                         (("sqlite3", "better-sqlite3", "sqlalchemy"), "SQLite/SQL")):
        if lowered & set(names):
            return label
    env = _text(folder / ".env.example") + _text(folder / "config.php") + _text(folder / "config" / "database.php")
    if re.search(r"mysqli?_connect|DB_CONNECTION\s*=\s*mysql|mysql:host", env, re.I):
        return "MySQL"
    if any(folder.glob("*.db")) or any(folder.glob("*.sqlite")):
        return "SQLite"
    return ""


def _git(folder: Path, ctx) -> dict[str, Any]:
    if not (folder / ".git").exists():
        return {}
    from app.tools.developer import _executor

    info: dict[str, Any] = {}
    try:
        result = _executor(ctx).run(["git", "status", "--short", "--branch"], folder, timeout=15)
        if result.returncode == 0:
            lines = result.output.splitlines()
            if lines and lines[0].startswith("## "):
                info["branch"] = lines[0][3:].split("...")[0]
            info["changes"] = sum(1 for line in lines[1:] if line.strip())
        remote = _executor(ctx).run(["git", "remote", "get-url", "origin"], folder, timeout=10)
        if remote.returncode == 0 and remote.output.strip():
            info["remote"] = re.sub(r"//[^@/]+@", "//", remote.output.strip().splitlines()[0])  # never credentials
    except (ToolError, OSError):
        pass
    return info


def _notes_file(ctx) -> Path | None:
    return ctx.paths.data_dir / NOTES_FILE if getattr(ctx, "paths", None) is not None else None


def notes_for(folder: Path, ctx) -> list[str]:
    path = _notes_file(ctx)
    data = _read_json(path) if path is not None else {}
    notes = data.get(str(folder).lower(), [])
    return [str(n) for n in notes][:20] if isinstance(notes, list) else []


def add_note(folder: Path, note: str, ctx) -> list[str]:
    path = _notes_file(ctx)
    if path is None:
        raise ToolError("There's no data folder to keep project notes in.", "no_data_dir")
    data = _read_json(path)
    key = str(folder).lower()
    notes = [n for n in data.get(key, []) if isinstance(n, str)]
    notes.append(" ".join(note.split())[:200])
    data[key] = notes[-20:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data[key]


def profile(folder: Path, ctx) -> Profile:
    from app.tools.developer import _xampp_url, project_command

    try:
        run = project_command(folder)
    except ToolError:
        run = _xampp_url(folder) or ""
    deps = _dependencies(folder)
    return Profile(folder.name, str(folder), project_stack(folder), deps, _entry_points(folder), run,
                   _test_command(folder), _build_command(folder), _database(folder, deps), _git(folder, ctx),
                   notes_for(folder, ctx), _lint_command(folder), _types_command(folder))


def known_project_folders(ctx) -> list[Path]:
    from app.core.world import _projects

    return [Path(e.attrs["folder"]) for e in _projects(ctx)]


def project_of(path: Path, ctx) -> Path:
    """The known project a file belongs to - code tools only work inside projects you told Jarvis about."""
    resolved = path.resolve()
    for folder in sorted(known_project_folders(ctx), key=lambda p: -len(str(p))):
        root = folder.resolve()
        if resolved == root or root in resolved.parents:
            return root
    raise ToolError(f"{path} isn't inside a project I know. Set DEV_PROJECT_DIR, say 'my project folder is ...', "
                    "or run a command there once to trust it.", "not_a_project")


def code_files(root: Path, limit: int = 5000):
    count = 0
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix.lower() in CODE_EXTENSIONS or entry.name.endswith(".blade.php"):
                yield entry
                count += 1
                if count >= limit:
                    return
