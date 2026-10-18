from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.core import integrations
from app.memory.database import Database
from app.tools.base import ToolContext, ToolError
from app.tools.diagnostics_tools import IntegrationArgs, integrations_status


def make(tmp_path: Path, **settings) -> ToolContext:
    db = Database(tmp_path / "i.db")
    db.initialize()
    return ToolContext(Settings(**settings), database=db)


def serve(monkeypatch, handler) -> None:
    monkeypatch.setattr(integrations, "TRANSPORT", httpx.MockTransport(handler))


def row(ctx: ToolContext, key: str) -> dict:
    return next(r for r in integrations.evaluate_all(ctx) if r["key"] == key)


def test_unconfigured_service_is_not_tested_and_not_called(tmp_path: Path, monkeypatch) -> None:
    serve(monkeypatch, lambda r: pytest.fail("an unconfigured service must never be contacted"))
    ctx = make(tmp_path)
    result = integrations.run_smoke(ctx, "github")
    assert result["verdict"] in ("not_configured", "credentials_missing") and "not tested" in result["detail"]
    assert row(ctx, "github")["tested_at"] == ""                  # nothing was recorded as a test


@pytest.mark.parametrize("status,verdict", [(200, "smoke_passed"), (401, "auth_failed"), (403, "permission_denied"),
                                            (503, "degraded"), (404, "degraded")])
def test_github_verdicts_follow_the_response(tmp_path: Path, monkeypatch, status: int, verdict: str) -> None:
    seen = []
    serve(monkeypatch, lambda r: (seen.append((r.method, str(r.url))), httpx.Response(status, json={}))[1])
    ctx = make(tmp_path, github_token="ghp_secret_value")
    result = integrations.run_smoke(ctx, "github")
    assert result["verdict"] == verdict
    assert seen == [("GET", "https://api.github.com/user")]       # one read-only request
    stored = row(ctx, "github")
    assert stored["verdict"] == verdict and stored["tested_at"] and stored["end_to_end"] == "not tested"
    assert "ghp_secret_value" not in str(stored) and "ghp_secret_value" not in result["detail"]


def test_unreachable_and_timeout(tmp_path: Path, monkeypatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    serve(monkeypatch, boom)
    assert integrations.run_smoke(make(tmp_path, github_token="x"), "github")["verdict"] == "unreachable"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    serve(monkeypatch, slow)
    result = integrations.run_smoke(make(tmp_path / "t" if (tmp_path / "t").mkdir() is None else tmp_path, github_token="x"),
                                    "github")
    assert result["verdict"] == "unreachable" and "8 seconds" in result["detail"]


def test_home_assistant_sends_the_token_only_to_its_own_address(tmp_path: Path, monkeypatch) -> None:
    seen = []
    serve(monkeypatch, lambda r: (seen.append((str(r.url), r.headers["authorization"])), httpx.Response(200))[1])
    ctx = make(tmp_path, home_assistant_url="http://192.168.1.5:8123/", home_assistant_token="TOK")
    assert integrations.run_smoke(ctx, "home_assistant")["verdict"] == "smoke_passed"
    assert seen == [("http://192.168.1.5:8123/api/", "Bearer TOK")]


def test_ollama_model_missing_is_degraded_not_passed(tmp_path: Path, monkeypatch) -> None:
    serve(monkeypatch, lambda r: httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]}))
    ok = make(tmp_path, ai_model="qwen3:8b")
    assert integrations.run_smoke(ok, "ollama")["verdict"] == "smoke_passed"
    (tmp_path / "b").mkdir()
    bad = make(tmp_path / "b", ai_model="missing:1b")
    result = integrations.run_smoke(bad, "ollama")
    assert result["verdict"] == "degraded" and "not installed" in result["detail"]


def test_a_service_without_a_smoke_test_is_only_configured(tmp_path: Path) -> None:
    result = integrations.run_smoke(make(tmp_path, remote_enabled=False), "browser")
    assert result["verdict"] in ("configured", "credentials_missing") or "not tested" in result["detail"]
    assert row(make(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path), "browser")["verdict"] != "smoke_passed"


def test_a_crashing_check_is_a_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(integrations.SMOKE, "github", lambda ctx: 1 / 0)
    result = integrations.run_smoke(make(tmp_path, github_token="x"), "github")
    assert result["verdict"] == "degraded" and "ZeroDivisionError" in result["detail"]


def test_tool_lists_without_contacting_and_rejects_unknown(tmp_path: Path, monkeypatch) -> None:
    serve(monkeypatch, lambda r: pytest.fail("listing must not contact services"))
    ctx = make(tmp_path, github_token="x")
    text = integrations_status(IntegrationArgs(), ctx).message
    assert "No full end-to-end test has been run" in text
    with pytest.raises(ToolError):
        integrations_status(IntegrationArgs(test="nope"), ctx)


def test_passed_smoke_is_reported_as_smoke_not_end_to_end(tmp_path: Path, monkeypatch) -> None:
    serve(monkeypatch, lambda r: httpx.Response(200, json={}))
    ctx = make(tmp_path, github_token="x")
    message = integrations_status(IntegrationArgs(test="github"), ctx).message
    assert "smoke passed" in message and "not an end-to-end test" in message
    assert "GitHub" in integrations_status(IntegrationArgs(), ctx).message.split("smoke test:")[1].split(".")[0]
