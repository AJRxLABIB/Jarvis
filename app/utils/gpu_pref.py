"""Which GPU JARVIS uses on a laptop with two: GPU_PREFERENCE = auto | integrated | nvidia.

``integrated`` (e.g. Intel UHD instead of the RTX): 
* CUDA_VISIBLE_DEVICES=-1 in this process, before speech / voice libraries load, so nothing here uses the NVIDIA card; Ollama started by
  JARVIS inherits it. OLLAMA_VULKAN=1 is also passed so a recent Ollama may use the Intel GPU through Vulkan (unverified, version dependent).
* Windows' own per-app graphics preference (Settings > System > Display > Graphics) is set to "Power saving" for the program that runs
  JARVIS, so its windows / OpenGL render on the Intel GPU. Written to HKCU, no admin needed, persistent.
Honest limit: CUDA is NVIDIA-only. Speech recognition and models do not run on an Intel iGPU through CUDA; with the NVIDIA card hidden they
run on the CPU (or via Vulkan for Ollama if supported). An Ollama started by the tray app (not by JARVIS) is not affected by this process.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, MutableMapping

REG_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
VALUE = {"integrated": "GpuPreference=1;", "nvidia": "GpuPreference=2;"}   # 1 = power saving (integrated), 2 = high performance


def _write_registry(exe: str, value: str | None) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, exe)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, value)


def programs() -> list[str]:
    """The executables whose graphics preference matters: the Python / packaged program that runs JARVIS (and its windowed twin)."""
    exe = os.path.abspath(sys.executable)
    names = {exe}
    if exe.lower().endswith("python.exe"):
        names.add(exe[:-10] + "pythonw.exe")
    return sorted(names)


def apply(preference: str, *, environ: MutableMapping[str, str] | None = None, windows: bool | None = None,
          registry: Callable[[str, str | None], None] | None = None) -> list[str]:
    """Apply the preference; returns what was done (words for the log / status). Never raises."""
    env = os.environ if environ is None else environ
    on_windows = (sys.platform == "win32") if windows is None else windows
    pref = (preference or "auto").strip().lower()
    done: list[str] = []
    if pref == "integrated":
        env["CUDA_VISIBLE_DEVICES"] = "-1"
        env.setdefault("OLLAMA_VULKAN", "1")
        done.append("NVIDIA GPU hidden from JARVIS (speech and models run on the CPU, or Intel via Vulkan where Ollama supports it)")
    elif pref in ("auto", "nvidia") and env.get("CUDA_VISIBLE_DEVICES") == "-1" and env.get("JARVIS_HID_CUDA") == "1":
        env.pop("CUDA_VISIBLE_DEVICES", None)
    if pref == "integrated":
        env["JARVIS_HID_CUDA"] = "1"
    if on_windows:
        write = registry or _write_registry
        try:
            for exe in programs():
                write(exe, VALUE.get(pref))                     # auto -> remove our entry: Windows decides again
            done.append({"integrated": "Windows graphics preference set to Power saving (Intel) for JARVIS",
                         "nvidia": "Windows graphics preference set to High performance (NVIDIA) for JARVIS"}.get(pref, "Windows graphics preference cleared"))
        except Exception as exc:                                # registry blocked: the env part still holds
            done.append(f"could not write the Windows graphics preference ({type(exc).__name__})")
    return done
