"""Phase 5: repository understanding (symbols, imports, impact) and safer git commits."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import codeindex
from app.tools.developer import _changed_paths, looks_secret
from tests.test_developer import FakeExecutor, call, make


def build_project(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "billing.py").write_text(
        "def total(items):\n    return sum(items)\n\n\nclass Invoice:\n    def pay(self):\n        return total([1])\n")
    (root / "app" / "orders.py").write_text("from app.billing import total\n\n\ndef checkout(x):\n    return total(x)\n")
    (root / "app" / "views.py").write_text("from app import orders\n\n\ndef page():\n    return orders.checkout([2])\n")
    (root / "tests" / "test_billing.py").write_text("from app.billing import total\n\n\ndef test_total():\n    assert total([1]) == 1\n")
    (root / "web.js").write_text("import { helper } from './util'\nexport function render() { return helper() }\n")
    (root / "util.js").write_text("export function helper() { return 1 }\n")
    return root


def test_symbols(tmp_path: Path) -> None:
    root = build_project(tmp_path / "p")
    names = {(s.file, s.kind, s.name) for s in codeindex.project_symbols(root)}
    assert ("app/billing.py", "function", "total") in names and ("app/billing.py", "class", "Invoice") in names
    assert ("web.js", "function", "render") in names and ("util.js", "function", "helper") in names


def test_import_graph_and_dependents(tmp_path: Path) -> None:
    root = build_project(tmp_path / "p")
    graph = codeindex.import_graph(root)
    assert "app/billing.py" in graph["app/orders.py"] and "app/orders.py" in graph["app/views.py"]
    assert graph["web.js"] == {"util.js"}
    assert codeindex.dependents(graph, "app/billing.py") == {"app/orders.py": 1, "tests/test_billing.py": 1,
                                                             "app/views.py": 2}


def test_impact_of_a_symbol_lists_definition_references_dependents_and_tests(tmp_path: Path) -> None:
    root = build_project(tmp_path / "p")
    found = codeindex.impact(root, "total")
    assert found.kind == "symbol" and [(d.file, d.line) for d in found.definitions] == [("app/billing.py", 1)]
    assert any(r.startswith("app/orders.py:") for r in found.references)
    assert set(found.dependents) == {"app/orders.py", "tests/test_billing.py", "app/views.py"}
    assert "tests/test_billing.py" in found.tests
    assert "check them" in codeindex.describe(found)


def test_impact_of_a_file_and_of_something_unknown(tmp_path: Path) -> None:
    root = build_project(tmp_path / "p")
    found = codeindex.impact(root, "app/billing.py")
    assert found.kind == "file" and "tests/test_billing.py" in found.tests
    unknown = codeindex.impact(root, "nonexistent_thing")
    assert unknown.kind == "unknown" and "can't find" in codeindex.describe(unknown)
    assert codeindex.impact(root, "orders.checkout").kind in ("unknown", "symbol")  # never crashes on odd input


def test_syntax_errors_and_huge_files_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "bad.py").write_text("def broken(:\n")
    assert codeindex.project_symbols(root) == []


def test_code_impact_tool(tmp_path: Path) -> None:
    ex, ctx, _, project = make(tmp_path)
    build_project(project)
    result = call(ex, "code_impact", target="total").result
    assert result.ok and "app/billing.py:1" in result.message and result.data["tests"] == ["tests/test_billing.py"]


@pytest.mark.parametrize(("path", "secret"), [(".env", True), ("config/.env", True), (".env.example", False),
                                              ("server.pem", True), ("android/signing/x.jks", True), ("id_rsa", True),
                                              ("src/app.py", False), ("keys.md", False), ("Credentials.json", True)])
def test_looks_secret(path: str, secret: bool) -> None:
    assert looks_secret(path) is secret


def test_changed_paths() -> None:
    assert _changed_paths([" M app/a.py", "?? .env", "R  old.py -> new.py", '?? "my file.txt"']) == [
        "app/a.py", ".env", "new.py", "my file.txt"]


class Recorder(FakeExecutor):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status
        self.staged = ""

    def run(self, argv, cwd, timeout=120):
        from app.security.command_executor import CommandOutput

        self.calls.append(("run", tuple(argv), Path(cwd)))
        argv = list(argv)
        out = "ok"
        if argv[1:3] == ["status", "--short"]:
            out = self.status
        elif argv[1:3] == ["diff", "--cached"]:
            out = self.staged
        elif argv[1] == "commit":
            out = "[main abc1234] msg"
        return CommandOutput(tuple(argv), 0, out)


def test_commit_never_stages_secrets(tmp_path: Path) -> None:
    rec = Recorder(" M app/a.py\n?? .env\n?? server.pem")
    rec.staged = "app/a.py"
    ex, ctx, confirmer, _ = make(tmp_path, executor=rec)
    result = call(ex, "git_commit", message="fix a").result
    argvs = [c[1] for c in rec.calls]
    assert result.ok and ("git", "reset", "-q", "--", ".env") in argvs and ("git", "reset", "-q", "--", "server.pem") in argvs
    assert "Committed 1 file(s)" in result.message and ".env" in result.message  # reported, not hidden
    assert ".env" in confirmer.requests[0].summary and "NOT including" in confirmer.requests[0].summary


def test_commit_of_only_secrets_commits_nothing(tmp_path: Path) -> None:
    rec = Recorder("?? .env")
    rec.staged = ""
    ex, *_ = make(tmp_path, executor=rec)
    result = call(ex, "git_commit", message="oops").result
    assert result.ok and "Nothing to commit except files that look like secrets" in result.message
    assert not any(c[1][1] == "commit" for c in rec.calls)


def test_push_question_names_branch_and_remote(tmp_path: Path) -> None:
    class Push(Recorder):
        def run(self, argv, cwd, timeout=120):
            from app.security.command_executor import CommandOutput

            if list(argv)[1:3] == ["rev-parse", "--abbrev-ref"]:
                return CommandOutput(tuple(argv), 0, "main\n")
            if list(argv)[1:3] == ["remote", "get-url"]:
                return CommandOutput(tuple(argv), 0, "git@github.com:me/x.git\n")
            return super().run(argv, cwd, timeout)

    rec = Push(" M a.py")
    rec.staged = "a.py"
    ex, ctx, confirmer, _ = make(tmp_path, executor=rec)
    call(ex, "git_commit", message="fix a", push=True)
    question = confirmer.requests[0].summary
    assert "branch 'main'" in question and "git@github.com:me/x.git" in question and "never forced" in question


@pytest.mark.parametrize("path", ["app/core/agents.py", "app/core/orchestrator.py", "app/core/missions.py",
                                  "app/core/taskstate.py", "app/memory/database.py", "app/tools/developer.py"])
def test_self_improvement_cannot_touch_the_task_engines_trust_boundaries(path: str) -> None:
    from app.evolution.proposals import PROTECTED

    assert any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED)


# ------------------------------------------------------------------ reviewing a change before committing

from app.core import diffreview  # noqa: E402

SAMPLE = """diff --git a/app/billing.py b/app/billing.py
--- a/app/billing.py
+++ b/app/billing.py
@@ -1,2 +1,3 @@
-def total(items):
+def total(items, tax=0):
+    return sum(items) + tax
diff --git a/tests/test_billing.py b/tests/test_billing.py
--- a/tests/test_billing.py
+++ b/tests/test_billing.py
@@ -1,9 +1,3 @@
-def test_total():
-    assert total([1]) == 1
-def test_tax():
-    assert total([1], 2) == 3
+@pytest.mark.skip
+def test_new():
+    pass
diff --git a/config.py b/config.py
new file mode 100644
--- /dev/null
+++ b/config.py
@@ -0,0 +1,2 @@
+API_TOKEN = "sk_live_abcdefghijklmnop1234567890"
+x = 1
diff --git a/.env b/.env
new file mode 100644
--- /dev/null
+++ b/.env
@@ -0,0 +1 @@
+A=1
"""


def test_diff_review_flags_what_a_reviewer_would() -> None:
    review = diffreview.analyse_diff(SAMPLE)
    by = {f.path: f for f in review.files}
    assert by["app/billing.py"].added == 2 and by["app/billing.py"].removed == 1 and not by["app/billing.py"].is_test
    assert by["tests/test_billing.py"].tests_removed == 2 and by["tests/test_billing.py"].tests_added == 1
    flags = " | ".join(review.flags)
    assert "tests removed in tests/test_billing.py: 2 removed, 1 added" in flags
    assert "test(s) skipped" in flags and "look like a secret" in flags and ".env is the kind of file" in flags
    assert "sk_live_abcdefghijklmnop1234567890" not in flags  # the secret itself is never repeated
    assert not review.clean


def test_a_clean_diff_says_so_without_overclaiming() -> None:
    clean = diffreview.analyse_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    text = diffreview.describe(clean)
    assert clean.clean and "Nothing risky stands out" in text and "not proof" in text
    assert diffreview.describe(diffreview.analyse_diff("")) == "There are no changes to review."


def test_review_changes_tool(tmp_path: Path) -> None:
    class Diff(FakeExecutor):
        def run(self, argv, cwd, timeout=120):
            from app.security.command_executor import CommandOutput

            self.calls.append(("run", tuple(argv), Path(cwd)))
            argv = list(argv)
            out = SAMPLE if argv[1] == "diff" else "?? .env.local\n?? notes.txt" if argv[1] == "status" else ""
            return CommandOutput(tuple(argv), 0, out)

    ex, ctx, _, _ = make(tmp_path, executor=Diff())
    result = call(ex, "review_changes").result
    assert result.ok and not result.data["clean"] and "Look at this before committing" in result.message
    assert any(".env.local is untracked" in f for f in result.data["flags"])
    assert result.data["untracked"] == [".env.local", "notes.txt"]


# ------------------------------------------------------------------ the project's own lint / type checks


def test_lint_and_type_commands_come_only_from_what_the_project_configures(tmp_path: Path) -> None:
    from app.core import projects

    bare = tmp_path / "bare"
    bare.mkdir()
    assert projects._lint_command(bare) == "" and projects._types_command(bare) == ""  # never a guess
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n")
    assert projects._lint_command(py) == "python -m ruff check ." and projects._types_command(py) == "python -m mypy"
    js = tmp_path / "js"
    js.mkdir()
    (js / "package.json").write_text('{"scripts": {"lint": "eslint .", "typecheck": "tsc --noEmit", "test": "jest"}}')
    assert projects._lint_command(js) == "npm run lint" and projects._types_command(js) == "npm run typecheck"
    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n")
    assert projects._lint_command(go) == "go vet ./..."


def test_check_project_runs_the_lint_command_and_says_when_there_is_none(tmp_path: Path) -> None:
    ex, ctx, _, project = make(tmp_path)
    (project / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    result = call(ex, "check_project", what="lint").result
    assert result.ok and result.data["command"] == "python -m ruff check ."
    assert call(ex, "check_project", what="types").result.error == "unknown_command"  # nothing configured: no guess
    assert not call(ex, "check_project", what="coverage").result.ok  # not an allowed kind of check


def test_the_lint_and_type_commands_are_allowed_without_asking() -> None:
    from app.config.files import default_permissions
    from app.security.command_policy import CommandPolicy

    allowed = {tuple(c.argv) for c in default_permissions().allowed_dev_commands}
    for argv in (("python", "-m", "ruff", "check"), ("python", "-m", "mypy"), ("npm", "run", "lint")):
        assert argv in allowed
    assert CommandPolicy is not None
