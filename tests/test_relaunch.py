from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.shortcuts import match_shortcut
from app.core import relaunch


def test_command_is_an_argument_list_that_waits_for_the_old_process() -> None:
    cmd = relaunch.relaunch_command(4242, minimized=True, frozen=False, executable="/x/python", root=Path("/app"))
    assert cmd[0].endswith("python") and cmd[1].endswith("run.py")
    assert cmd[2:] == ["--restart-after", "4242", "--minimized"]
    frozen = relaunch.relaunch_command(7, minimized=False, frozen=True, executable="/x/Jarvis.exe")
    assert frozen == ["/x/Jarvis.exe", "--restart-after", "7"]


@pytest.mark.parametrize("text", ["refresh", "Refresh.", "restart yourself", "restart Jarvis", "reload yourself",
                                  "please refresh yourself", "relaunch the assistant", "refresh now"])
def test_voice_phrases_restart_jarvis(text: str) -> None:
    call = match_shortcut(text, lambda n: None)
    assert call is not None and call.name == "jarvis_control" and call.arguments == {"action": "restart_jarvis"}


@pytest.mark.parametrize("text", ["restart my pc", "restart", "refresh the page", "restart Spotify"])
def test_other_restarts_are_not_hijacked(text: str) -> None:
    call = match_shortcut(text, lambda n: "Spotify" if n.lower() == "spotify" else None)
    assert call is None or call.arguments.get("action") != "restart_jarvis"


def test_wait_for_exit_returns_when_the_process_is_gone() -> None:
    assert relaunch.wait_for_exit(2 ** 22 + 12345, timeout=2.0) is True   # no such pid
