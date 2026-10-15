from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.files import (
    AppsConfig,
    PermissionsConfig,
    ShortcutsConfig,
    default_apps,
    default_permissions,
    default_shortcuts,
    load_config_bundle,
    normalize_hotkey,
)
from app.config.settings import ConfigError

PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_missing_files_are_created_with_defaults(tmp_path: Path) -> None:
    bundle = load_config_bundle(tmp_path)
    for name in ("apps.json", "permissions.json", "shortcuts.json"):
        assert (tmp_path / name).is_file()
    assert bundle.apps == default_apps()
    assert load_config_bundle(tmp_path) == bundle  # round-trip


def test_shipped_config_files_are_valid_and_match_defaults() -> None:
    bundle = load_config_bundle(PROJECT_CONFIG)
    assert bundle.apps == default_apps()
    assert bundle.permissions == default_permissions()
    assert bundle.shortcuts == default_shortcuts()


def test_invalid_json_reports_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "apps.json").write_text('{\n  "version": 1,\n  oops\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config_bundle(tmp_path)
    assert "apps.json" in str(info.value) and "line 3" in str(info.value)


def test_alias_resolution() -> None:
    apps = default_apps()
    assert apps.resolve("Browser") == "chrome"
    assert apps.resolve("visual studio code") == "vscode"
    assert apps.resolve("  VS   Code ") == "vscode"
    assert apps.resolve("editor") == "vscode"
    assert apps.resolve("task manager") == "task_manager"
    assert apps.resolve("photoshop") is None


def test_disabled_apps_are_not_resolved() -> None:
    data = default_apps().model_dump(mode="json")
    data["applications"]["spotify"]["enabled"] = False
    assert AppsConfig.model_validate(data).resolve("music") is None


def test_duplicate_alias_across_apps_rejected() -> None:
    data = default_apps().model_dump(mode="json")
    data["applications"]["edge"]["aliases"].append("browser")
    with pytest.raises(Exception, match="browser"):
        AppsConfig.model_validate(data)


@pytest.mark.parametrize("value", ["chrome.exe & calc", "cmd.exe | x", "a;b", 'x"y'])
def test_shell_metacharacters_rejected_in_launch_values(value: str) -> None:
    data = default_apps().model_dump(mode="json")
    data["applications"]["notepad"]["launch"]["value"] = value
    with pytest.raises(Exception):
        AppsConfig.model_validate(data)


def test_app_paths_requires_bare_exe_name() -> None:
    data = default_apps().model_dump(mode="json")
    data["applications"]["chrome"]["launch"]["value"] = "C:\\x\\chrome.exe"
    with pytest.raises(Exception):
        AppsConfig.model_validate(data)


def test_unknown_fields_rejected(tmp_path: Path) -> None:
    data = default_shortcuts().model_dump(mode="json")
    data["surprise"] = True
    (tmp_path / "shortcuts.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="shortcuts.json"):
        load_config_bundle(tmp_path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Ctrl + Space", "ctrl+space"), ("shift+ctrl+J", "ctrl+shift+j"), ("Escape", "escape"), ("Win+Alt+F12", "alt+win+f12")],
)
def test_hotkey_normalization(raw: str, expected: str) -> None:
    assert normalize_hotkey(raw) == expected


@pytest.mark.parametrize("raw", ["ctrl+", "ctrl+shift", "hyper+x", "ctrl+ctrl+x", "ctrl+banana"])
def test_invalid_hotkeys(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_hotkey(raw)


def test_duplicate_and_missing_hotkeys_rejected() -> None:
    with pytest.raises(Exception, match="assigned to both"):
        ShortcutsConfig.model_validate(
            {"hotkeys": {"activate_assistant": "ctrl+space", "toggle_listening": "Ctrl+Space", "cancel": "escape"}}
        )
    with pytest.raises(Exception, match="missing"):
        ShortcutsConfig.model_validate({"hotkeys": {"activate_assistant": "ctrl+space"}})


def test_permission_defaults_are_safe() -> None:
    perms = default_permissions()
    assert perms.block_critical is True
    assert perms.confirm_moderate is True
    assert "lsass.exe" in perms.protected_processes
    assert all(cmd.argv[0] in {"git", "npm", "python"} for cmd in perms.allowed_dev_commands)


@pytest.mark.parametrize("root", ["relative\\path", "{DOCUMENTS}\\..\\..\\Windows", "{HOME}&calc"])
def test_invalid_safe_roots_rejected(root: str) -> None:
    with pytest.raises(Exception):
        PermissionsConfig.model_validate({"safe_roots": [root]})


def test_tool_level_out_of_range_rejected() -> None:
    with pytest.raises(Exception):
        PermissionsConfig.model_validate({"tool_levels": {"delete_file": 7}})


def test_app_names_forgive_small_differences() -> None:
    from app.config.files import default_apps

    apps = default_apps()
    assert apps.resolve("Visual Code") == "vscode"  # every word is in "Visual Studio Code"
    assert apps.resolve("note pad") == "notepad" and apps.resolve("spotfy") == "spotify"
    assert apps.resolve("task manger") == "task_manager"
    assert apps.resolve("photoshop") is None  # not configured: found through the Start Menu instead
