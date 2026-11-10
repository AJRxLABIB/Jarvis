"""The setup wizard is a one-time thing: a replaced .env must not bring it back."""

from __future__ import annotations

import json
from pathlib import Path

from app.config.env_template import default_env_text
from app.core import setup_state
from app.main import bootstrap



def test_record_and_repairs(tmp_path: Path) -> None:
    assert setup_state.repairs({"SETUP_DONE": "false"}, tmp_path) == {}            # never set up: ask
    setup_state.remember(tmp_path, {"USER_ADDRESS": "Sir", "HOME_LOCATION": "Dhaka", "GITHUB_TOKEN": "secret",
                                    "START_WITH_WINDOWS": "true"})
    data = json.loads((tmp_path / setup_state.FILE_NAME).read_text(encoding="utf-8"))
    assert "GITHUB_TOKEN" not in data["answers"] and data["done"] and data["finished"]   # no secrets kept
    fixes = setup_state.repairs({"SETUP_DONE": "false", "USER_ADDRESS": "", "START_WITH_WINDOWS": "false"}, tmp_path)
    assert fixes == {"SETUP_DONE": "true", "USER_ADDRESS": "Sir", "HOME_LOCATION": "Dhaka", "START_WITH_WINDOWS": "true"}
    assert setup_state.repairs({"SETUP_DONE": "true"}, tmp_path) == {}             # nothing to repair
    setup_state.remember(tmp_path, finished=False)                                  # a later cancel keeps answers
    assert setup_state.read(tmp_path)["answers"]["USER_ADDRESS"] == "Sir" and setup_state.read(tmp_path)["finished"]


def test_a_replaced_env_is_repaired_at_start(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SETUP_DONE=true\nUSER_ADDRESS=Sir\nHOME_LOCATION=Dhaka\n", encoding="utf-8")
    ctx = bootstrap(tmp_path, environ={})
    assert ctx.settings.setup_done and setup_state.is_done(ctx.paths.data_dir)     # record started from .env
    ctx.database.close() if hasattr(ctx.database, "close") else None
    env.write_text(default_env_text().replace("\n", "\r\n"), encoding="utf-8")  # .env reset to defaults
    ctx = bootstrap(tmp_path, environ={})
    assert ctx.settings.setup_done and ctx.settings.user_address == "Sir" and ctx.settings.home_location == "Dhaka"
    assert "SETUP_DONE=true" in env.read_text(encoding="utf-8")


def test_a_true_first_run_still_asks(tmp_path: Path) -> None:
    ctx = bootstrap(tmp_path, environ={})
    assert not ctx.settings.setup_done and not setup_state.is_done(ctx.paths.data_dir)
