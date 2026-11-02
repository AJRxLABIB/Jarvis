"""Audio output switching: routing decisions from fake device lists, the voice phrases, and the tool with a fake Windows environment.
The Windows COM calls (app/tools/audio_win.py) are not run here; `audio_output list` on your PC shows what they see."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core import audio_route as AR
from app.core.audio_route import Device

REALTEK_SPK = Device("spk", "Speakers (Realtek(R) Audio)", form_factor=1, enumerator="HDAUDIO", is_default=False)
REALTEK_HP = Device("hp", "Headphones (Realtek(R) Audio)", form_factor=3, enumerator="HDAUDIO", is_default=True)
BT_JBL = Device("jbl", "JBL Flip 6 Stereo", form_factor=1, enumerator="BTHENUM")
BT_HF = Device("jblhf", "JBL Flip 6 Hands-Free AG Audio", form_factor=5, enumerator="BTHHFENUM")
USB_SPK = Device("usb", "USB Speakers", form_factor=1, enumerator="USB")
HDMI = Device("hdmi", "NVIDIA High Definition Audio (LG monitor)", form_factor=9, enumerator="HDAUDIO")


def test_classification():
    assert REALTEK_SPK.kind == AR.SPEAKERS and REALTEK_HP.kind == AR.HEADPHONES and BT_JBL.kind == AR.BLUETOOTH
    assert USB_SPK.kind == AR.USB and HDMI.kind == AR.DISPLAY and BT_HF.hands_free
    assert Device("x", "JBL Boombox", enumerator="USB").kind == AR.USB          # a known (non-Bluetooth) enumerator beats a brand-name guess
    assert Device("y", "Sony WH-1000XM5").kind == AR.BLUETOOTH                  # no enumerator: the name is the best guess


def test_parse_target():
    assert AR.parse_target("the speaker")[0] == "speakers" and AR.parse_target("bluetooth speaker")[0] == "bluetooth"
    assert AR.parse_target("connected speaker")[0] == "connected" and AR.parse_target("my headset")[0] == "headphones"
    assert AR.parse_target("JBL") == ("any", "jbl")


def test_headphones_to_speakers_and_loud():
    d = AR.decide([REALTEK_SPK, REALTEK_HP], "speakers")
    assert d.device is REALTEK_SPK and not d.fallback and d.raise_volume_to == AR.SPEAKER_MIN_PERCENT
    assert AR.decide([REALTEK_SPK, REALTEK_HP], "speakers", loud=True).raise_volume_to == AR.LOUD_PERCENT


def test_speakers_never_picks_bluetooth_while_a_normal_speaker_exists():
    assert AR.decide([BT_JBL, REALTEK_SPK, USB_SPK], "speakers").device is REALTEK_SPK


def test_bluetooth_prefers_stereo_over_hands_free_and_falls_back_to_pc_speakers():
    assert AR.decide([REALTEK_SPK, BT_HF, BT_JBL], "bluetooth").device is BT_JBL
    d = AR.decide([REALTEK_SPK, REALTEK_HP], "bluetooth")
    assert d.device is REALTEK_SPK and d.fallback and "No Bluetooth" in d.message and "PC speakers" in d.message


def test_connected_speaker_order_and_fallback():
    assert AR.decide([REALTEK_SPK, USB_SPK, HDMI, BT_JBL], "connected").device is BT_JBL
    assert AR.decide([REALTEK_SPK, USB_SPK, HDMI], "connected").device is USB_SPK
    assert AR.decide([REALTEK_SPK, HDMI], "connected").device is HDMI
    assert AR.decide([REALTEK_SPK], "connected").fallback


def test_named_device_and_inactive_devices_are_ignored():
    assert AR.decide([REALTEK_SPK, BT_JBL], "any", "jbl").device is BT_JBL
    gone = Device("jbl", "JBL Flip 6 Stereo", enumerator="BTHENUM", active=False)
    d = AR.decide([REALTEK_SPK, gone], "bluetooth")
    assert d.device is REALTEK_SPK and d.fallback


def test_already_on_it_and_nothing_available():
    on = Device("spk", "Speakers (Realtek(R) Audio)", form_factor=1, is_default=True)
    d = AR.decide([on], "speakers")
    assert d.already and "already" in d.message
    assert AR.decide([], "speakers").device is None
    assert AR.decide([HDMI], "bluetooth").device is None             # a monitor is not "the PC speakers": change nothing and say so


def test_no_pc_speakers_and_no_bluetooth_changes_nothing():
    d = AR.decide([Device("m", "Microphone array", form_factor=4)], "bluetooth")
    assert d.device is None and "changed nothing" in d.message


# ---- voice phrases -------------------------------------------------------------------------------------------------------------------------
def test_voice_phrases():
    from app.agent import shortcuts

    cases = {
        "put it on speaker": ("speaker", False), "put it on the speaker loudly": ("speaker", True), "play it on speakers": ("speakers", False),
        "switch to speakers": ("speakers", False), "use the speaker": ("speaker", False), "put it on bluetooth": ("bluetooth", False),
        "put it on the bluetooth speaker": ("bluetooth speaker", False), "switch to headphones": ("headphones", False),
        "put it on connected speaker": ("connected speaker", False), "move the sound to my headset": ("headset", False),
        "play the music on the laptop speakers": ("laptop speakers", False), "put it on bluetooth please": ("bluetooth", False),
    }
    for text, (dev, loud) in cases.items():
        call = shortcuts.match_shortcut(text, lambda n: None)
        assert call and call.name == "audio_output" and call.arguments["action"] == "switch", text
        assert call.arguments["target"].lower().endswith(dev) and call.arguments["loud"] is loud, (text, call.arguments)
    assert shortcuts.match_shortcut("switch audio to JBL", lambda n: None).arguments["target"] == "JBL"
    assert shortcuts.match_shortcut("which output am I using", lambda n: None).arguments == {"action": "status"}
    assert shortcuts.match_shortcut("list sound outputs", lambda n: None).arguments == {"action": "list"}
    for other in ("put it on repeat", "play it on youtube", "play music", "put it on the table", "switch to chrome"):
        call = shortcuts.match_shortcut(other, lambda n: None)
        assert call is None or call.name != "audio_output", other


# ---- the tool, with a fake Windows --------------------------------------------------------------------------------------------------------
class FakeAudio:
    def __init__(self, devices, *, stuck=False, volume=20):
        self.devs, self.stuck, self.vol, self.calls = list(devices), stuck, volume, []

    def devices(self):
        return list(self.devs)

    def default_id(self):
        return next((d.id for d in self.devs if d.is_default), "")

    def set_default(self, device_id):
        self.calls.append(("default", device_id))
        if not self.stuck:
            self.devs = [Device(d.id, d.name, d.form_factor, d.enumerator, d.active, d.id == device_id) for d in self.devs]

    def volume(self, device_id):
        return self.vol

    def set_volume(self, device_id, percent):
        self.calls.append(("volume", percent))
        self.vol = percent


def run_tool(env, **args):
    from app.tools import audio_output_tool as T

    T.ENV_FACTORY = lambda: env
    try:
        return asyncio.run(T.audio_output(T.AudioArgs(**args), SimpleNamespace()))
    finally:
        T.ENV_FACTORY = None


def test_tool_switches_verifies_and_raises_volume():
    env = FakeAudio([REALTEK_SPK, REALTEK_HP], volume=15)
    r = run_tool(env, action="switch", target="speakers", loud=True)
    assert r.ok and ("default", "spk") in env.calls and ("volume", 70) in env.calls and "raised from 15% to 70%" in r.message
    assert env.default_id() == "spk"


def test_tool_reports_when_windows_did_not_switch():
    env = FakeAudio([REALTEK_SPK, REALTEK_HP], stuck=True)
    r = run_tool(env, action="switch", target="speakers")
    assert not r.ok and r.error == "not_verified"


def test_tool_fallback_message_and_status_and_list():
    env = FakeAudio([REALTEK_SPK, REALTEK_HP])
    r = run_tool(env, action="switch", target="bluetooth")
    assert r.ok and r.data["fallback"] and "PC speakers" in r.message
    assert "Speakers" in run_tool(env, action="status").message
    assert "[headphones]" in run_tool(FakeAudio([REALTEK_SPK, REALTEK_HP]), action="list").message


def test_tool_does_not_touch_volume_when_already_loud_enough():
    env = FakeAudio([REALTEK_SPK, REALTEK_HP], volume=90)
    run_tool(env, action="switch", target="speakers", loud=True)
    assert not [c for c in env.calls if c[0] == "volume"]


def test_windows_side_compiles_and_refuses_off_windows():
    import ast
    from pathlib import Path

    ast.parse((Path(__file__).resolve().parents[1] / "app" / "tools" / "audio_win.py").read_text(encoding="utf-8"))
    from app.tools import audio_output_tool as T

    if not T.winapi.is_windows():
        with pytest.raises(Exception):
            asyncio.run(T.audio_output(T.AudioArgs(action="list"), SimpleNamespace()))
