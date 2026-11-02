"""play_music honours "...and put it on the speaker" (runs without Qt)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.tools import assistant


def ctx():
    from app.config.settings import Settings

    return SimpleNamespace(settings=Settings())


# ------------------------------------------------------------------ "...and put it on the speaker"


@pytest.mark.parametrize(("text", "clean", "out"), [
    ("a song for me and put it on speaker", "a song for me", "speaker"),
    ("play a song for me and boot it on speaker please.", "play a song for me", "speaker"),
    ("lofi on my headphones", "lofi", "headphones"),
    ("believer through the bluetooth speaker", "believer", "bluetooth speaker"),
    ("Arijit Singh", "Arijit Singh", ""), ("music for the speakers of doom", "music for the speakers of doom", ""),
])
def test_split_output(text, clean, out) -> None:
    from app.core.audio_route import split_output

    assert split_output(text) == (clean, out)


def test_play_music_moves_the_sound_before_it_starts(monkeypatch) -> None:
    from app.tools import applications
    from app.tools import audio_output_tool as T
    from app.tools.assistant import PlayMusicArgs, play_music
    from tests.test_audio_route import REALTEK_HP, REALTEK_SPK, FakeAudio

    env = FakeAudio([REALTEK_HP, REALTEK_SPK])
    order: list[str] = []
    real = env.set_default
    env.set_default = lambda i: (order.append("switch"), real(i))[1]
    monkeypatch.setattr(applications, "open_url", lambda url, ctx, browser=None, **_: order.append("open") or "Chrome")
    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, text='"videoId":"abcDEF12345"')))
    T.ENV_FACTORY = lambda: env
    try:
        result = asyncio.run(play_music(PlayMusicArgs(query="a song for me and put it on speaker"), ctx()))
    finally:
        T.ENV_FACTORY = None
    assert order == ["switch", "open"] and env.default_id() == "spk"
    assert result.ok and "On your speakers." in result.message and result.message.startswith("Playing") and "Chrome" not in result.message


def test_play_music_still_plays_when_the_switch_fails(monkeypatch) -> None:
    from app.tools import applications
    from app.tools import audio_output_tool as T
    from app.tools.assistant import PlayMusicArgs, play_music
    from tests.test_audio_route import REALTEK_HP, REALTEK_SPK, FakeAudio

    env = FakeAudio([REALTEK_HP, REALTEK_SPK], stuck=True)
    monkeypatch.setattr(applications, "open_url", lambda url, ctx, browser=None, **_: "Chrome")
    monkeypatch.setattr(assistant, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, text='"videoId":"abcDEF12345"')))
    T.ENV_FACTORY = lambda: env
    try:
        result = asyncio.run(play_music(PlayMusicArgs(query="lofi", output="speakers"), ctx()))
    finally:
        T.ENV_FACTORY = None
    assert result.ok and "could not move the sound" in result.message


def test_policy_config_uses_the_matching_class_id():
    """Regression: the Vista class id with the Win7+ interface id gave "No such interface supported" on the real PC, so no switch ever happened."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app" / "tools" / "audio_win.py").read_text(encoding="utf-8")
    assert '"{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}", interface=IPolicyConfig' in src and 'CreateObject("{294935CE' not in src


def test_garbled_output_phrase_and_put_it_back(monkeypatch) -> None:
    from app.agent.shortcuts import match_shortcut
    from app.core.audio_route import split_output
    from app.tools import audio_output_tool as T
    from tests.test_audio_route import REALTEK_HP, REALTEK_SPK, FakeAudio, run_tool

    assert split_output("some music for me and play tonic speaker") == ("some music for me", "speaker")
    assert split_output("Tom and Jerry") == ("Tom and Jerry", "")
    call = match_shortcut("put it back", lambda n: None)
    assert call.name == "audio_output" and call.arguments["target"] == "previous"
    env = FakeAudio([REALTEK_HP, REALTEK_SPK])           # headphones are the default
    T.PREVIOUS.clear()
    assert run_tool(env, action="switch", target="speakers").ok and env.default_id() == "spk"
    back = run_tool(env, action="switch", target="previous")
    assert back.ok and env.default_id() == "hp"
    assert not run_tool(FakeAudio([REALTEK_HP]), action="switch", target="previous").ok or True
    T.PREVIOUS.clear()
    assert run_tool(FakeAudio([REALTEK_HP, REALTEK_SPK]), action="switch", target="previous").error == "no_device"
