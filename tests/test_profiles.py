from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import profiles as prof
from app.config.groups import GROUPS
from app.config.settings import SECRET_FIELDS, Settings
from app.memory.database import Database
from app.memory.settings_store import SettingsStore
from app.tools.base import ToolContext, ToolError
from app.tools.profiles_tool import ProfileArgs, _question, _risk, settings_profile


@pytest.fixture()
def ctx(tmp_path: Path):
    db = Database(tmp_path / "j.db")
    db.initialize()
    applied: list[tuple[str, str]] = []
    c = ToolContext(Settings(), database=db)
    c.apply_setting = lambda f, v: applied.append((f, v))
    c.applied = applied
    return c


def run(ctx, **kw):
    return settings_profile(ProfileArgs(**kw), ctx)


def test_profiles_can_never_contain_secrets_or_anything_that_guards_the_pc() -> None:
    forbidden = set(SECRET_FIELDS) | set(GROUPS["security"]) | set(GROUPS["devices_and_remote"]) | set(GROUPS["extensions"]) \
        | set(GROUPS["developer"]) | set(GROUPS["diagnostics"]) | {"document_folders", "data_dir", "microphone_device", "output_device"}
    assert not forbidden & set(prof.PROFILE_FIELDS)
    for values in prof.BUILTIN.values():
        assert set(values) <= set(prof.PROFILE_FIELDS)
        prof.validate(values)                                   # every built-in value is accepted by the real settings model


def test_every_profile_field_exists_and_round_trips_through_the_settings_model() -> None:
    settings = Settings()
    captured = prof.capture(settings)
    assert set(captured) == set(prof.PROFILE_FIELDS)
    prof.validate(captured)


def test_clean_drops_disallowed_fields_instead_of_applying_them() -> None:
    cleaned = prof.clean({"tts_enabled": False, "github_token": "x", "confirm_power_actions": False, "remote_enabled": True,
                          "direct_commands": True, "screen_blocked_windows": ""})
    assert cleaned == {"tts_enabled": "false"}


def test_save_validates_and_rejects_bad_values_and_names(ctx) -> None:
    store = prof.ProfileStore(SettingsStore(ctx.database))
    with pytest.raises(prof.ProfileError):
        store.save("x", {"tts_volume": "9000"})
    with pytest.raises(prof.ProfileError):
        store.save("../etc", {"tts_enabled": "false"})
    with pytest.raises(prof.ProfileError):
        store.save("empty", {"github_token": "x"})
    assert store.save("My Desk", {"tts_enabled": "false"}) == "my_desk" and "my_desk" in store.names()


def test_list_show_and_builtins(ctx) -> None:
    assert {"meeting", "night", "performance"} <= set(run(ctx, action="list").data["profiles"])
    shown = run(ctx, action="show", name="meeting")
    assert shown.data["settings"]["tts_enabled"] == "false"
    with pytest.raises(ToolError):
        run(ctx, action="show", name="nope")


def test_apply_changes_only_what_differs_and_undo_restores_it(ctx) -> None:
    result = run(ctx, action="apply", name="meeting")
    changed = dict(ctx.applied)
    assert changed["tts_enabled"] == "false" and "proactive_level" in changed
    assert "start_fullscreen" in changed
    assert all(f in prof.PROFILE_FIELDS for f in changed)
    assert result.ok and "Changed" in result.message
    ctx.applied.clear()
    ctx.settings = Settings(tts_enabled=False, proactive_level="minimal")        # what the window would now report
    run(ctx, action="undo")
    undone = dict(ctx.applied)
    assert undone["tts_enabled"] == "true" and undone["proactive_level"] == "normal"


def test_applying_what_is_already_set_does_nothing(ctx) -> None:
    ctx.settings = Settings(**{k: v for k, v in prof.BUILTIN["night"].items()})
    result = run(ctx, action="apply", name="night")
    assert "nothing to change" in result.message and ctx.applied == []


def test_the_confirmation_shows_exactly_what_will_change_and_flags_wider_privacy(ctx) -> None:
    store = prof.ProfileStore(SettingsStore(ctx.database))
    store.save("watcher", {"screen_capture_enabled": "true", "tts_volume": "0.5"})
    text = _question(ProfileArgs(action="apply", name="watcher"), ctx)
    assert "screen capture enabled: false -> true" in text and "tts volume: 1.0 -> 0.5" in text
    assert "see more" in text and "screen capture enabled" in text.split("see more")[1]


def test_risk_levels_and_undo_without_history(ctx) -> None:
    assert _risk(ProfileArgs(action="list"), ctx).name == "SAFE" and _risk(ProfileArgs(action="apply", name="x"), ctx).name == "MODERATE"
    with pytest.raises(ToolError):
        run(ctx, action="undo")


def test_builtin_cannot_be_deleted_but_can_be_overridden_and_custom_ones_can(ctx) -> None:
    with pytest.raises(ToolError):
        run(ctx, action="delete", name="meeting")
    run(ctx, action="save", name="meeting")                      # my own version of "meeting"
    run(ctx, action="delete", name="meeting")
    run(ctx, action="save", name="mine")
    run(ctx, action="delete", name="mine")
    assert "mine" not in prof.ProfileStore(SettingsStore(ctx.database)).names()


def test_export_import_round_trip_drops_tampered_fields(ctx) -> None:
    store = prof.ProfileStore(SettingsStore(ctx.database))
    store.save("calm", {"tts_enabled": "false"})
    exported = json.loads(store.export("calm"))
    exported["settings"].update({"github_token": "stolen", "confirm_power_actions": "false"})
    name = store.import_text(json.dumps(exported), "copy")
    assert store.get(name) == {"tts_enabled": "false"}
    with pytest.raises(prof.ProfileError):
        store.import_text("not json")
