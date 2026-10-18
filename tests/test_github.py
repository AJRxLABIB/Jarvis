from __future__ import annotations

import httpx
import pytest

from app.config.settings import Settings
from app.core import integrations
from app.tools import github
from app.tools.base import ToolContext, ToolError
from app.tools.github import GitHubArgs, github as run


def ctx(token: str = "ghp_secret") -> ToolContext:
    return ToolContext(Settings(github_token=token))


def serve(monkeypatch, handler):
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(handler))


def test_needs_a_token() -> None:
    with pytest.raises(ToolError) as err:
        run(GitHubArgs(action="notifications"), ctx(""))
    assert err.value.code == "not_configured"


def test_reads_only_with_get_and_sends_the_token(monkeypatch) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json=[{"repository": {"full_name": "me/app"}, "subject": {"type": "PullRequest",
                                                                                         "title": "Fix it"},
                                          "reason": "review_requested"}])

    serve(monkeypatch, handler)
    result = run(GitHubArgs(action="notifications"), ctx())
    assert "me/app: Fix it (review_requested)" in result.message
    assert seen == [("GET", "/notifications", "Bearer ghp_secret")]
    assert "ghp_secret" not in result.message and "untrusted" in result.data["note"]


def test_assigned_and_pull_requests(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/issues":
            return httpx.Response(200, json=[{"repository": {"full_name": "me/app"}, "number": 4, "title": "Bug",
                                              "pull_request": {}}])
        return httpx.Response(200, json=[{"number": 9, "title": "Add x", "user": {"login": "bob"}, "draft": True}])

    serve(monkeypatch, handler)
    assert "me/app#4 Bug (pull request)" in run(GitHubArgs(action="assigned"), ctx()).message
    assert "#9 Add x by bob (draft)" in run(GitHubArgs(action="pull_requests", repo="me/app"), ctx()).message


def test_bad_repo_is_rejected_before_any_request(monkeypatch) -> None:
    serve(monkeypatch, lambda r: pytest.fail("no request expected"))
    for bad in (None, "no-slash", "a/b/../c", "a b/c"):
        with pytest.raises(ToolError):
            run(GitHubArgs(action="pull_requests", repo=bad), ctx())


@pytest.mark.parametrize("status,remaining,code", [(401, "5", "auth"), (403, "0", "auth"), (404, "5", "not_found"),
                                                    (500, "5", "service_error")])
def test_errors_are_plain(monkeypatch, status, remaining, code) -> None:
    serve(monkeypatch, lambda r: httpx.Response(status, headers={"x-ratelimit-remaining": remaining}))
    with pytest.raises(ToolError) as err:
        run(GitHubArgs(action="notifications"), ctx())
    assert err.value.code == code


def test_integration_status() -> None:
    row = next(r for r in integrations.evaluate_all(ctx("")) if r["key"] == "github")
    assert row["state"] == "not_set_up"
    row = next(r for r in integrations.evaluate_all(ctx("x")) if r["key"] == "github")
    assert row["state"] == "ready" and "ghp" not in str(row)


def test_token_is_a_secret_setting() -> None:
    from app.config.settings import SECRET_FIELDS

    assert "github_token" in SECRET_FIELDS
