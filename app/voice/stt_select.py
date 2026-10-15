"""Choosing the speech model and where it runs.

``STT_MODEL=auto`` / ``STT_DEVICE=auto`` pick the most accurate setup the PC can run fast:

* NVIDIA GPU with working CUDA libraries -> ``large-v3-turbo`` on the GPU (float16):
  near large-v3 accuracy, well under a second for a spoken command.
* otherwise -> ``small.en`` on the CPU (int8), much more accurate than ``base.en``.

Every choice has fallbacks (GPU -> CPU, big model -> smaller model), so voice keeps
working when a library or a download is missing. The NVIDIA libraries can come from
the ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` pip packages (requirements-gpu.txt);
their DLL folders are registered here because Windows doesn't search site-packages.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.utils.logging_setup import get_logger, log_event

if TYPE_CHECKING:
    from app.config.settings import Settings

_log = get_logger("stt")
GPU_MODEL = "large-v3-turbo"
CPU_MODEL = "small.en"
CPU_MODEL_MULTILINGUAL = "small"
LAST_RESORT = "base.en"
LAST_RESORT_MULTILINGUAL = "base"

_registered = False
_cuda: bool | None = None
_lock = threading.Lock()


@dataclass(frozen=True)
class ModelChoice:
    model: str
    device: str  # cpu | cuda
    compute_type: str

    def describe(self) -> str:
        return f"{self.model} on {'GPU' if self.device == 'cuda' else 'CPU'}"


def _nvidia_roots() -> list[Path]:
    roots: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)  # the packaged app
    if bundle:
        roots.append(Path(bundle) / "nvidia")
    for entry in sys.path:
        if entry and entry.lower().endswith("site-packages"):
            roots.append(Path(entry) / "nvidia")
    return roots


def register_cuda_libraries() -> list[str]:
    """Make the pip-installed NVIDIA DLLs (cuBLAS, cuDNN) findable. Returns the folders added."""
    global _registered
    added: list[str] = []
    with _lock:
        if _registered:
            return added
        _registered = True
        for root in _nvidia_roots():
            if not root.is_dir():
                continue
            for folder in sorted(root.glob("*/bin")) + sorted(root.glob("*/lib")):
                if not folder.is_dir():
                    continue
                path = str(folder)
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(path)
                    except OSError:
                        continue
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
                added.append(path)
    if added:
        log_event(_log, "cuda_libraries_registered", folders=len(added))
    return added


def cuda_available(*, refresh: bool = False) -> bool:
    """True if CTranslate2 sees an NVIDIA GPU that supports float16. (Whether cuBLAS/cuDNN load is only
    known when the model runs; the recognizer falls back to the CPU if they don't.)"""
    global _cuda
    if _cuda is not None and not refresh:
        return _cuda
    register_cuda_libraries()
    try:
        import ctranslate2

        _cuda = ctranslate2.get_cuda_device_count() > 0 and "float16" in ctranslate2.get_supported_compute_types("cuda")
    except Exception:
        _cuda = False
    if _cuda and not cuda_libraries_present():
        # A GPU but no cuBLAS/cuDNN: don't pick (or download) the GPU model; it could not run.
        log_event(_log, "cuda_libraries_missing")
        _cuda = False
    return _cuda


_CUDA_DLLS = {"nt": ("cublas64_12.dll", "cudnn64_9.dll"), "posix": ("libcublas.so.12", "libcudnn.so.9")}


def cuda_libraries_present(loader=None) -> bool:
    """True if the CUDA 12 cuBLAS and cuDNN 9 libraries can be loaded (pip packages or a CUDA install)."""
    import ctypes

    load = loader or (ctypes.WinDLL if os.name == "nt" else ctypes.CDLL)
    for name in _CUDA_DLLS.get(os.name, ()):
        try:
            load(name)
        except OSError:
            return False
    return True


def _compute(device: str, requested: str) -> str:
    if requested not in ("", "auto"):
        return requested
    # int8_float16 on the GPU: about half the memory of float16 at practically the same accuracy, which
    # leaves room for the AI model on a 4-6 GB GPU (otherwise Ollama spills layers to the CPU: very slow)
    return "int8_float16" if device == "cuda" else "int8"


def stt_candidates(settings: "Settings", *, gpu: bool | None = None) -> list[ModelChoice]:
    """Setups to try, best first."""
    gpu = cuda_available() if gpu is None else gpu
    english = settings.voice_language == "en"
    model, device, compute = settings.stt_model, settings.stt_device, settings.stt_compute_type
    cpu_model = CPU_MODEL if english else CPU_MODEL_MULTILINGUAL
    last = LAST_RESORT if english else LAST_RESORT_MULTILINGUAL
    out: list[ModelChoice] = []
    if model == "auto":
        if device in ("auto", "cuda") and (gpu or device == "cuda"):
            out.append(ModelChoice(GPU_MODEL, "cuda", _compute("cuda", compute)))
        # the CPU is always the fallback
        out.append(ModelChoice(cpu_model, "cpu", _compute("cpu", compute if device == "cpu" else "auto")))
        out.append(ModelChoice(last, "cpu", "int8"))
    else:
        if device == "cuda" or (device == "auto" and gpu):
            out.append(ModelChoice(model, "cuda", _compute("cuda", compute)))
        out.append(ModelChoice(model, "cpu", _compute("cpu", compute if device == "cpu" else "auto")))
        if device != "cpu" and not model.startswith(("tiny", "base")) and not Path(model).is_absolute():
            out.append(ModelChoice(cpu_model, "cpu", "int8"))
    unique: list[ModelChoice] = []
    for choice in out:
        if choice not in unique:
            unique.append(choice)
    return unique


def is_cuda_error(exc: BaseException) -> bool:
    detail = str(exc).lower()
    return any(word in detail for word in ("cublas", "cudnn", "cuda", "libcu", "cufft", "nvrtc", "gpu"))
