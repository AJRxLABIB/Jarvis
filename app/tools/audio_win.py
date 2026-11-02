"""Windows side of audio output switching (pycaw + comtypes). NOT run by the automated tests: it needs real devices.

Listing uses the documented MMDevice API. Changing the default playback device has NO documented API: Windows' own Settings app and every audio
switcher use the undocumented ``IPolicyConfig`` COM interface (same one as SoundSwitch / AudioDeviceCmdlets), called here through comtypes.
The result is always read back from Windows (``default_id()``) and reported as it is, not as intended."""

from __future__ import annotations

import ctypes
from ctypes import POINTER, c_int, c_longlong, c_void_p

from app.core.audio_route import Device
from app.tools import winapi
from app.tools.base import ToolError
from app.utils.logging_setup import get_logger

_log = get_logger("audio_route")
E_RENDER, E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS = 0, 0, 1, 2
_FRIENDLY = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 14"
_DESCRIPTION = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 2"
_ENUMERATOR = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 24"
_FORM_FACTOR = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"


def _policy_config():
    import comtypes
    import comtypes.client
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    LPCWSTR = ctypes.c_wchar_p

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetMixFormat", (["in"], LPCWSTR, "n"), (["out"], POINTER(c_void_p), "f")),
            COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], LPCWSTR, "n"), (["in"], c_int, "d"), (["out"], POINTER(c_void_p), "f")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], LPCWSTR, "n")),
            COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], LPCWSTR, "n"), (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], LPCWSTR, "n"), (["in"], c_int, "d"), (["out"], POINTER(c_longlong), "a"),
                      (["out"], POINTER(c_longlong), "b")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], LPCWSTR, "n"), (["in"], POINTER(c_longlong), "a")),
            COMMETHOD([], HRESULT, "GetShareMode", (["in"], LPCWSTR, "n"), (["out"], POINTER(c_void_p), "m")),
            COMMETHOD([], HRESULT, "SetShareMode", (["in"], LPCWSTR, "n"), (["in"], c_void_p, "m")),
            COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], LPCWSTR, "n"), (["in"], c_void_p, "k"), (["out"], POINTER(c_void_p), "v")),
            COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], LPCWSTR, "n"), (["in"], c_void_p, "k"), (["in"], c_void_p, "v")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPCWSTR, "id"), (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], LPCWSTR, "n"), (["in"], c_int, "v")),
        )

    comtypes.CoInitialize()
    # CPolicyConfigClient pairs with this IPolicyConfig IID. ({294935CE-...} is CPolicyConfigVistaClient, a different interface: using it here
    # made Windows answer "No such interface supported" and the switch never happened.)
    return comtypes.client.CreateObject("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}", interface=IPolicyConfig)


class WindowsAudio:
    """devices() / default_id() / set_default(id) / volume(id) / set_volume(id, percent)."""

    def __init__(self) -> None:
        winapi.require_windows()

    # ---- reading ---------------------------------------------------------------------------------------------------------------------------
    def _enumerator(self):
        import comtypes
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator

        comtypes.CoInitialize()
        return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)

    def default_id(self) -> str:
        try:
            return self._enumerator().GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA).GetId()
        except Exception:
            return ""

    def devices(self) -> list[Device]:
        from pycaw.constants import DEVICE_STATE
        from pycaw.utils import AudioUtilities

        enum = self._enumerator()
        default = self.default_id()
        coll = enum.EnumAudioEndpoints(E_RENDER, DEVICE_STATE.ACTIVE.value)
        out: list[Device] = []
        for i in range(coll.GetCount()):
            try:
                dev = AudioUtilities.CreateDevice(coll.Item(i))
            except Exception:
                _log.warning("could not read an audio endpoint", exc_info=True)
                continue
            props = dev.properties
            name = str(props.get(_FRIENDLY) or props.get(_DESCRIPTION) or dev.id)
            form = props.get(_FORM_FACTOR)
            out.append(Device(id=dev.id, name=name, form_factor=int(form) if isinstance(form, int) else 10,
                              enumerator=str(props.get(_ENUMERATOR) or ""), active=True, is_default=dev.id == default))
        return out

    # ---- changing --------------------------------------------------------------------------------------------------------------------------
    def set_default(self, device_id: str) -> None:
        try:
            config = _policy_config()
            for role in (E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS):      # everything: music, system sounds and calls
                config.SetDefaultEndpoint(device_id, role)
        except Exception as exc:
            raise ToolError(f"Windows refused to change the default sound output ({type(exc).__name__}: {str(exc)[:120]}).", "switch_failed") from exc

    def _volume_iface(self, device_id: str):
        from pycaw.utils import AudioUtilities

        dev = self._enumerator().GetDevice(device_id)
        return AudioUtilities.CreateDevice(dev).EndpointVolume

    def volume(self, device_id: str) -> int:
        return int(round(self._volume_iface(device_id).GetMasterVolumeLevelScalar() * 100))

    def set_volume(self, device_id: str, percent: int) -> None:
        vol = self._volume_iface(device_id)
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
