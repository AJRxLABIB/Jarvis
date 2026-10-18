"""Master prompt 3, stage F: a real microphone mute (nothing listens) and an honest TRANSCRIBING state."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def test_mute_stops_everything_and_blocks_listening(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from tests.test_gui import make_controller

    QApplication.instance() or QApplication([])
    controller, *_ = make_controller(tmp_path)
    states, notices = [], []
    controller.mic_muted_changed.connect(states.append)
    controller.notice.connect(notices.append)
    stopped = []
    controller.stop_hands_free = lambda: stopped.append("hands_free")
    assert "muted" in controller.set_mic_muted(True).lower() and controller.mic_muted and states == [True]
    assert stopped == ["hands_free"]
    controller.start_listening()
    assert "muted" in notices[-1] and not controller._listening                 # push-to-talk refuses
    assert controller.start_hands_free() is False                              # hands-free refuses
    assert "already muted" in controller.set_mic_muted(True)
    controller.set_mic_muted(False)
    assert not controller.mic_muted and states == [True, False]


def test_voice_phrase_mutes() -> None:
    from app.agent.shortcuts import match_shortcut

    call = match_shortcut("mute the microphone", lambda name: None)
    assert call is not None and call.name == "jarvis_control" and call.arguments == {"action": "mute_microphone"}


def test_transcribing_is_reported_only_for_commands() -> None:
    from types import SimpleNamespace

    from app.voice.hands_free import HandsFreeListener

    seen = []
    listener = HandsFreeListener.__new__(HandsFreeListener)
    listener.on_transcribing = seen.append
    listener.settings = SimpleNamespace(voice_language="en")
    listener.stt = SimpleNamespace(transcribe=lambda audio, lang: "open chrome")
    listener.state = "command"
    assert listener._transcribe(np.zeros(10)) == "open chrome" and seen == [True, False]
    listener.state = "waiting"                                               # wake-word listening: not a command
    listener._transcribe(np.zeros(10))
    assert seen == [True, False]
