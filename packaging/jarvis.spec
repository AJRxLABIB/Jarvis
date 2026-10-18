# PyInstaller build recipe for JARVIS.exe. Run packaging\build.ps1 (it calls this).
#
# Output: dist\JARVIS\ (a folder, not a single file: starts faster and antivirus software trusts it more)
#   JARVIS.exe      the desktop app (no console window)
#   jarvis-cli.exe  the same program for terminal commands: --doctor, --setup-voice, --chat, --voice ...
# Settings, memories, logs and downloaded models live in %LOCALAPPDATA%\JarvisAssistant (not in this folder).
# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH is set by PyInstaller)
sys.path.insert(0, ROOT)
from app import __version__  # noqa: E402

ICON = os.path.join(SPECPATH, "jarvis.ico")  # noqa: F821
VERSION_FILE = os.path.join(SPECPATH, "version_info.txt")  # noqa: F821


def optional(collect, package, **kwargs):
    """Collect from a package only if it is installed (Windows-only packages when building elsewhere)."""
    try:
        return collect(package, **kwargs)
    except Exception:
        return []


datas = []
binaries = []
hiddenimports = collect_submodules("app")  # tools are imported lazily in places; include every module

# Speech output (Piper): espeak-ng phoneme data and its native library.
datas += optional(collect_data_files, "piper")
binaries += optional(collect_dynamic_libs, "piper")
# Speech recognition (faster-whisper): the Silero VAD model it ships, and CTranslate2's DLLs.
datas += optional(collect_data_files, "faster_whisper")
binaries += optional(collect_dynamic_libs, "ctranslate2")
# NVIDIA GPU speech recognition (only if requirements-gpu.txt is installed): cuBLAS and cuDNN DLLs,
# kept at nvidia/<lib>/bin so app/voice/stt_select.py finds them.
binaries += optional(collect_dynamic_libs, "nvidia.cublas")
binaries += optional(collect_dynamic_libs, "nvidia.cudnn")
# Windows OCR (pywinrt): every projection module plus its bundled runtime DLL.
hiddenimports += optional(collect_submodules, "winrt")
binaries += optional(collect_dynamic_libs, "winrt")
# Browser automation (Playwright): its driver. It drives your installed Edge/Chrome, so no browser is bundled.
datas += optional(collect_data_files, "playwright")
# Audio device access (PortAudio DLL) and the volume API.
datas += optional(collect_data_files, "_sounddevice_data")
binaries += optional(collect_dynamic_libs, "_sounddevice_data")
hiddenimports += ["pycaw.pycaw", "comtypes.stream", "win32timezone"]
hiddenimports += ["comtypes.client", "win32com.client", "pythoncom"]  # tab reuse (UI Automation), brightness (WMI)
# Neural voices (edge-tts, imported lazily) and its certificate bundle.
hiddenimports += optional(collect_submodules, "edge_tts")
hiddenimports += optional(collect_submodules, "qrcode")  # phone setup QR code (imported lazily)
datas += optional(collect_data_files, "certifi")
# The Jarvis Wake Android app (built by packaging\build_android.ps1), offered to your phone by the phone remote.
_WAKE_APK = os.path.join(ROOT, "app", "remote", "assets", "JarvisWake.apk")
if os.path.isfile(_WAKE_APK):
    datas += [(_WAKE_APK, os.path.join("app", "remote", "assets"))]
# Offline Bangla voice (sherpa-onnx) and presence detection (OpenCV's face model).
binaries += optional(collect_dynamic_libs, "sherpa_onnx")
hiddenimports += optional(collect_submodules, "sherpa_onnx")
datas += optional(collect_data_files, "cv2")
# the 3D core: moderngl + its OpenGL context helpers
hiddenimports += optional(collect_submodules, "moderngl") + optional(collect_submodules, "glcontext")
binaries += optional(collect_dynamic_libs, "moderngl") + optional(collect_dynamic_libs, "glcontext")

excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "_pytest", "tests", "PyQt5", "PyQt6",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
    "sklearn", "scipy", "pandas", "torch", "tensorflow",
]

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    exclude_binaries=True, debug=False, strip=False, upx=False,
    icon=ICON if os.path.exists(ICON) else None,
    version=VERSION_FILE if os.path.exists(VERSION_FILE) and sys.platform == "win32" else None,
)
gui = EXE(pyz, a.scripts, [], name="JARVIS", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="jarvis-cli", console=True, **common)

coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="JARVIS")
