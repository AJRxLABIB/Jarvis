"""Phase 13: whole-codebase security and contract checks.

These read the source (AST) and the tool registry instead of running features,
so they catch a regression anywhere in ``app/``: a new ``shell=True``, a new
place that starts processes, a tool without strict arguments, a setting that
isn't documented, and so on.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent.tool_router import ALWAYS, KEYWORDS
from app.config.settings import ENV_NAMES, Settings
from app.security.permissions import RiskLevel
from app.tools.base import NoArgs, ToolArgs
from app.tools.catalog import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SOURCES = sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)

# The only modules allowed to start processes, and why.
PROCESS_MODULES = {
    "app/security/command_executor.py": "allow-listed developer commands (argument lists, shell=False)",
    "app/tools/developer.py": "opening a folder in VS Code (fixed argument list)",
    "app/tools/system.py": "nvidia-smi for GPU statistics (fixed argument list)",
    "app/tools/winapi.py": "shutdown.exe with fixed arguments; os.startfile for validated paths/URLs",
    "app/vision/ocr.py": "tesseract.exe with fixed arguments",
    "app/mcp/client.py": "MCP servers the user configured in config/mcp.json (argument list, shell=False)",
}
PROCESS_CALLS = {"subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
                 "subprocess.check_output", "asyncio.create_subprocess_exec", "os.startfile"}
FORBIDDEN_CALLS = {"os.system", "os.popen", "os.execv", "os.execve", "os.execl", "os.execlp", "os.execvp",
                   "os.spawnl", "os.spawnv", "os.spawnve", "os.posix_spawn", "subprocess.getoutput",
                   "subprocess.getstatusoutput", "asyncio.create_subprocess_shell", "pty.spawn",
                   "yaml.load", "pickle.loads", "pickle.load", "marshal.loads"}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_IMPORTS = {"pickle", "marshal", "shelve", "cPickle"}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------- processes


def test_sources_found() -> None:
    assert len(SOURCES) > 60


@pytest.mark.parametrize("path", SOURCES, ids=rel)
def test_no_shell_true_anywhere(path: Path) -> None:
    for call in calls(path):
        for keyword in call.keywords:
            if keyword.arg == "shell":
                assert isinstance(keyword.value, ast.Constant) and keyword.value.value is False, \
                    f"{rel(path)}:{call.lineno} passes shell={ast.unparse(keyword.value)}"


@pytest.mark.parametrize("path", SOURCES, ids=rel)
def test_no_dangerous_calls(path: Path) -> None:
    for call in calls(path):
        name = dotted(call.func)
        assert name not in FORBIDDEN_CALLS, f"{rel(path)}:{call.lineno} calls {name}"
        if isinstance(call.func, ast.Name):
            assert call.func.id not in FORBIDDEN_BUILTINS, f"{rel(path)}:{call.lineno} calls {call.func.id}()"
        for keyword in call.keywords:
            if keyword.arg == "verify":
                assert not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False), \
                    f"{rel(path)}:{call.lineno} turns off TLS verification"


@pytest.mark.parametrize("path", SOURCES, ids=rel)
def test_no_unsafe_deserialisation_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]
        assert not set(names) & FORBIDDEN_IMPORTS, f"{rel(path)}:{node.lineno} imports {names}"


def test_processes_start_only_in_reviewed_modules() -> None:
    found: dict[str, list[int]] = {}
    for path in SOURCES:
        for call in calls(path):
            if dotted(call.func) in PROCESS_CALLS:
                found.setdefault(rel(path), []).append(call.lineno)
    assert set(found) <= set(PROCESS_MODULES), f"new process-starting code: {set(found) - set(PROCESS_MODULES)}"


def test_process_commands_are_argument_lists() -> None:
    """The program and its arguments are never one string (which a shell or parser could split)."""
    for name in PROCESS_MODULES:
        for call in calls(ROOT / name):
            func = dotted(call.func)
            if func not in PROCESS_CALLS or func == "os.startfile" or not call.args:
                continue
            first = call.args[0]
            assert not isinstance(first, (ast.JoinedStr, ast.BinOp)) and not (
                isinstance(first, ast.Constant) and isinstance(first.value, str)), \
                f"{name}:{call.lineno} passes the command as a string"
            if func.startswith("subprocess."):
                assert any(k.arg == "shell" for k in call.keywords), f"{name}:{call.lineno} must say shell=False"


def test_model_output_never_reaches_a_process_directly() -> None:
    """The conversation/agent layer never starts processes; only tools behind the executor do."""
    for path in (APP / "agent").rglob("*.py"):
        for call in calls(path):
            assert dotted(call.func) not in PROCESS_CALLS, f"{rel(path)}:{call.lineno}"
    for path in (APP / "ai").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "subprocess" not in text and "startfile" not in text, rel(path)


# ------------------------------------------------------------------- tools


REGISTRY = build_default_registry()


@pytest.mark.parametrize("tool", REGISTRY.tools, ids=lambda t: t.name)
def test_tool_contract(tool) -> None:
    assert re.fullmatch(r"[a-z][a-z0-9_]{2,40}", tool.name)
    assert len(tool.description) >= 20, "the model needs a real description"
    assert issubclass(tool.args_model, ToolArgs), "arguments must use the strict ToolArgs base"
    assert tool.args_model.model_config.get("extra") == "forbid"
    assert tool.level != RiskLevel.CRITICAL, "critical tools are blocked; don't register them"
    assert 0 < tool.timeout <= 900
    assert tool.category in KEYWORDS or tool.category in ALWAYS, f"router has no keywords for {tool.category}"
    schema = tool.args_model.model_json_schema()
    json.dumps(schema)
    assert schema.get("type") == "object"
    for field_name, field in tool.args_model.model_fields.items():
        assert field.description or tool.args_model is NoArgs, f"{tool.name}.{field_name} has no description"


def test_nothing_is_unrestricted_by_accident() -> None:
    """Tools that change things beyond the screen must at least be MODERATE by default or by argument."""
    must_ask = {"end_process", "power_control", "run_command", "delete_item", "move_item", "forget_everything",
                "schedule_task", "save_workflow"}
    for name in must_ask:
        tool = REGISTRY.get(name)
        assert tool is not None, name
        assert tool.level >= RiskLevel.MODERATE or tool.risk is not None, name


def test_argument_models_reject_unknown_fields() -> None:
    for tool in REGISTRY.tools:
        with pytest.raises(ValueError):
            tool.args_model.model_validate({"__jarvis_injected__": "rm -rf /"})


# ---------------------------------------------------------------- settings


def test_every_setting_is_documented_in_env_example() -> None:
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"),
                                re.M))
    missing = {env for env in ENV_NAMES.values() if env not in documented}
    assert not missing, f"add to .env.example: {sorted(missing)}"


def test_env_example_loads_and_matches_defaults_where_given() -> None:
    from app.config.settings import load_settings

    loaded = load_settings(ROOT / ".env.example", environ={})
    assert isinstance(loaded, BaseModel)
    assert loaded.screen_capture_enabled is False and loaded.offline_mode == "auto"


def test_settings_have_no_secret_fields_yet() -> None:
    """No API keys/tokens are stored in settings today; if one is added it must be masked in public_dict."""
    masked = Settings().public_dict()
    for name in Settings.model_fields:
        if any(marker in name for marker in ("key", "token", "secret", "password")):
            assert masked[name] in ("", None, "[REDACTED]"), name


def test_privacy_features_default_to_off_or_asking() -> None:
    s = Settings()
    assert not s.screen_capture_enabled and not s.start_with_windows and s.offline_mode == "auto"
    from app.config.files import PermissionsConfig

    permissions = PermissionsConfig()
    assert permissions.confirm_moderate and permissions.block_critical
