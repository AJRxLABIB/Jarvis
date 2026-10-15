from __future__ import annotations

import json
import logging
from pathlib import Path

from app.utils.logging_setup import get_logger, log_event, setup_logging, teardown_logging


def _records(log_file: Path) -> list[dict]:
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_json_lines_with_structured_fields(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path, "INFO")
    log_event(get_logger("test"), "tool_call", tool="open_application", arguments={"application": "chrome"})
    teardown_logging()
    (record,) = _records(log_file)
    assert record["logger"] == "jarvis.test"
    assert record["msg"] == "tool_call"
    assert record["fields"]["tool"] == "open_application"
    assert record["fields"]["arguments"] == {"application": "chrome"}


def test_secrets_never_reach_the_file(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path, "DEBUG")
    log = get_logger("test")
    log.info("connecting with api_key=%s", "sk-proj-SUPERSECRETVALUE123456")
    log_event(log, "login", password="hunter2", headers={"Authorization": "Bearer abcdefghijkl1234"})
    try:
        raise RuntimeError("failed: token=tok_abcdef123456")
    except RuntimeError:
        log.exception("boom")
    teardown_logging()
    content = log_file.read_text(encoding="utf-8")
    for secret in ("SUPERSECRETVALUE", "hunter2", "abcdefghijkl1234", "tok_abcdef123456"):
        assert secret not in content


def test_reserved_field_names_do_not_crash(tmp_path: Path) -> None:
    log_file = setup_logging(tmp_path, "INFO")
    log_event(get_logger("test"), "evt", name="x", msg="y")
    teardown_logging()
    fields = _records(log_file)[0]["fields"]
    assert fields["field_name"] == "x" and fields["field_msg"] == "y"


def test_setup_is_idempotent_and_level_respected(tmp_path: Path) -> None:
    setup_logging(tmp_path, "WARNING")
    log_file = setup_logging(tmp_path, "WARNING")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_jarvis_handler", False)]
    assert len(ours) == 1
    get_logger("test").info("hidden")
    get_logger("test").warning("shown")
    teardown_logging()
    assert [r["msg"] for r in _records(log_file)] == ["shown"]
