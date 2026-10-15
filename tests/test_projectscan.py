from __future__ import annotations

from pathlib import Path

from app.core import projectscan


def make(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / "app.py").write_text(
        "import subprocess, yaml, pickle\n"
        "# eval(fake) in a comment is ignored\n"
        "API_TOKEN = 'abcd1234efgh5678ijkl'\n"
        "def run(x):\n    return eval(x)\n"
        "subprocess.run(x, shell=True)\n"
        "data = yaml.load(open('f'))\n"
        "ok = yaml.load(open('f'), Loader=yaml.SafeLoader)\n")
    (tmp_path / "web.js").write_text("el.innerHTML = userText;\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("eval('1+1')\npassword = 'hunter2hunter2'\n")
    (tmp_path / "requirements.txt").write_text("requests\nflask>=2\n# comment\n")
    return tmp_path


def test_finds_the_common_problems_and_never_prints_the_secret(tmp_path: Path) -> None:
    found = projectscan.scan(make(tmp_path))
    text = " ".join(f.line_text() for f in found)
    assert any(f.file == ".env" and f.severity == "high" for f in found)  # not in .gitignore
    assert any("secret written into the code" in f.message and f.file == "app.py" for f in found)
    assert any("eval()" in f.message and f.line == 5 for f in found)
    assert any("shell=True" in f.message for f in found) and any("innerHTML" in f.message for f in found)
    assert sum(1 for f in found if "yaml.load" in f.message) == 1  # the SafeLoader one is fine
    assert any(f.file == "requirements.txt" and "requests" in f.message for f in found)
    assert not any("flask" in f.message for f in found)
    assert "abcd1234efgh5678ijkl" not in text and "hunter2" not in text
    assert not any(f.file.startswith("tests/") for f in found)  # tests do these on purpose
    assert [f.severity for f in found] == sorted((f.severity for f in found), key=["high", "medium", "low"].index)


def test_a_gitignored_env_is_fine_and_a_clean_project_says_so(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / "main.py").write_text("print('hello')\n")
    found = projectscan.scan(tmp_path)
    assert found == [] and "checklist" in projectscan.describe(found)


def test_describe_summarises_counts(tmp_path: Path) -> None:
    text = projectscan.describe(projectscan.scan(make(tmp_path)))
    assert "high" in text and "more" in text
