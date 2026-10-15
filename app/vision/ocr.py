"""Text recognition (OCR) on captured images, fully local.

Engines:
* ``windows``: the OCR built into Windows 10/11 (Windows.Media.Ocr), used
  through the pywinrt packages. Nothing to install besides pip packages; it
  reads the languages installed in Windows.
* ``tesseract``: an installed Tesseract (e.g. the UB Mannheim build), run as a
  fixed program with an argument list (never through a shell).

``auto`` uses Windows OCR and falls back to Tesseract.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.tools.base import ToolError

if TYPE_CHECKING:
    from PIL.Image import Image

    from app.config.settings import Settings

OCR_ENGINES: tuple[str, ...] = ("auto", "windows", "tesseract")
SMALL_IMAGE_SIDE = 1800  # smaller images are enlarged: UI text is often too small for OCR at 100%
TESSERACT_TIMEOUT = 90.0
_TESSERACT_LANGS = {"en": "eng", "bn": "ben", "hi": "hin", "ar": "ara", "fr": "fra", "de": "deu", "es": "spa",
                    "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn", "ko": "kor", "zh": "chi_sim", "ur": "urd",
                    "tr": "tur", "nl": "nld", "pl": "pol"}
_TESSERACT_LANG_RE = re.compile(r"^[a-z_]{3,20}(\+[a-z_]{3,20}){0,3}$")
_WINDOWS_OCR_HELP = (
    "Add an OCR language in Windows: Settings > Time & language > Language & region > your language > "
    "Language options > 'Optical character recognition'.")


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: tuple[int, int, int, int] | None = None  # left, top, width, height in the captured image
    words: tuple[tuple[str, tuple[int, int, int, int]], ...] = ()  # each word with its box (Windows OCR)


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...]
    engine: str
    language: str

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class OcrEngine(ABC):
    name = "base"

    @abstractmethod
    def problem(self) -> str | None:
        """Why this engine can't run here, or None when it can."""

    @abstractmethod
    async def recognize(self, image: "Image") -> OcrResult:
        """Recognise the text in an RGB image."""


def enlarge_factor(image: "Image", limit: int) -> float:
    """How much to scale the image: enlarge small ones (up to 2x), shrink ones over the engine limit."""
    longest = max(image.size)
    if longest > limit:
        return limit / longest
    if longest < SMALL_IMAGE_SIDE:
        return min(2.0, SMALL_IMAGE_SIDE / longest, limit / longest)
    return 1.0


def _resize(image: "Image", factor: float) -> "Image":
    if abs(factor - 1.0) < 0.01:
        return image
    from PIL import Image as PILImage

    size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
    return image.resize(size, PILImage.Resampling.LANCZOS if factor < 1 else PILImage.Resampling.BICUBIC)


# ------------------------------------------------------------------- Windows


def load_qt_runtime_first() -> None:
    """Make sure Qt's C++ runtime DLL is the one this process uses.

    winrt-runtime ships an older msvcp140.dll than PySide6. Windows loads a DLL
    name only once per process, so if winrt loads first, importing Qt later
    crashes (access violation). Qt's runtime is newer and also works for winrt,
    so load it before winrt. Does nothing when Qt is already loaded or not installed.
    """
    if sys.platform != "win32" or "PySide6.QtCore" in sys.modules:
        return
    try:
        import PySide6.QtCore  # noqa: F401
    except ImportError:
        pass


class WindowsOcr(OcrEngine):
    name = "windows"

    def __init__(self, language: str = "") -> None:
        self.language = language

    def _modules(self) -> tuple[Any, Any, Any, Any]:
        load_qt_runtime_first()
        import winrt.windows.globalization as globalization
        import winrt.windows.graphics.imaging as imaging
        import winrt.windows.media.ocr as ocr
        import winrt.windows.storage.streams as streams

        return ocr, globalization, imaging, streams

    def languages(self) -> list[str]:
        ocr, *_ = self._modules()
        return [language.language_tag for language in ocr.OcrEngine.available_recognizer_languages]

    def _engine(self) -> Any:
        ocr, globalization, _, _ = self._modules()
        if self.language:
            language = globalization.Language(self.language)
            if not ocr.OcrEngine.is_language_supported(language):
                available = ", ".join(self.languages()) or "none"
                raise ToolError(f"Windows OCR has no '{self.language}' language installed (installed: {available}). "
                                + _WINDOWS_OCR_HELP, "ocr_unavailable")
            engine = ocr.OcrEngine.try_create_from_language(language)
        else:
            engine = ocr.OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise ToolError("Windows OCR has no language for your Windows display languages. " + _WINDOWS_OCR_HELP,
                            "ocr_unavailable")
        return engine

    def problem(self) -> str | None:
        if sys.platform != "win32":
            return "Windows OCR only works on Windows."
        try:
            self._modules()
        except ImportError:
            return "the Windows OCR packages are not installed (run 'pip install -r requirements.txt')."
        try:
            self._engine()
        except ToolError as exc:
            return str(exc)
        except Exception as exc:  # WinRT errors
            return f"Windows OCR failed to start: {exc}"
        return None

    async def recognize(self, image: "Image") -> OcrResult:
        ocr, _, imaging, streams = self._modules()
        engine = self._engine()
        factor = enlarge_factor(image, int(ocr.OcrEngine.max_image_dimension))
        prepared = _resize(image, factor).convert("RGBA")
        writer = streams.DataWriter()
        writer.write_bytes(prepared.tobytes("raw", "BGRA"))
        bitmap = imaging.SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), imaging.BitmapPixelFormat.BGRA8, prepared.width, prepared.height)
        result = await engine.recognize_async(bitmap)
        lines = []
        for line in result.lines:
            text = " ".join(line.text.split())
            if not text:
                continue
            rects = [word.bounding_rect for word in line.words]
            box = None
            if rects:
                left = min(r.x for r in rects)
                top = min(r.y for r in rects)
                right = max(r.x + r.width for r in rects)
                bottom = max(r.y + r.height for r in rects)
                box = (round(left / factor), round(top / factor), round((right - left) / factor),
                       round((bottom - top) / factor))
            words = tuple((w.text, (round(w.bounding_rect.x / factor), round(w.bounding_rect.y / factor),
                                     round(w.bounding_rect.width / factor), round(w.bounding_rect.height / factor)))
                          for w in line.words)
            lines.append(OcrLine(text, box, words))
        return OcrResult(tuple(lines), self.name, engine.recognizer_language.language_tag)


# ----------------------------------------------------------------- Tesseract


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            for sub in ("Tesseract-OCR", os.path.join("Programs", "Tesseract-OCR")):
                candidate = Path(base) / sub / "tesseract.exe"
                if candidate.is_file():
                    return str(candidate)
    return None


def tesseract_language(tag: str) -> str:
    """'' -> eng, 'en-US' -> eng, 'bn' -> ben; Tesseract codes ('eng+ben') pass through."""
    tag = tag.strip().lower()
    if not tag:
        return "eng"
    if _TESSERACT_LANG_RE.match(tag) and len(tag.split("+")[0]) >= 3:
        return tag
    code = _TESSERACT_LANGS.get(tag.split("-")[0])
    if code is None:
        raise ToolError(f"I don't know the Tesseract name for OCR_LANGUAGE={tag}; use a code such as 'eng'.",
                        "ocr_unavailable")
    return code


class TesseractOcr(OcrEngine):
    name = "tesseract"

    def __init__(self, language: str = "", executable: str | None = None) -> None:
        self.language = language
        self.executable = executable

    def _exe(self) -> str | None:
        return self.executable or find_tesseract()

    def problem(self) -> str | None:
        if self._exe() is None:
            return ("Tesseract is not installed (get the Windows installer from github.com/UB-Mannheim/tesseract, "
                    "or add tesseract.exe to PATH).")
        try:
            tesseract_language(self.language)
        except ToolError as exc:
            return str(exc)
        return None

    async def recognize(self, image: "Image") -> OcrResult:
        exe = self._exe()
        if exe is None:
            raise ToolError(self.problem() or "Tesseract is not installed.", "ocr_unavailable")
        language = tesseract_language(self.language)
        with tempfile.TemporaryDirectory(prefix="jarvis-ocr-") as folder:
            path = Path(folder) / "capture.png"
            _resize(image, enlarge_factor(image, 10000)).save(path, format="PNG")
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            process = await asyncio.create_subprocess_exec(
                exe, str(path), "stdout", "-l", language, "--psm", "3",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=flags)
            try:
                out, err = await asyncio.wait_for(process.communicate(), TESSERACT_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise ToolError("Tesseract took too long.", "timeout") from None
        if process.returncode != 0:
            detail = err.decode("utf-8", "replace").strip().splitlines()
            raise ToolError(f"Tesseract failed: {detail[-1] if detail else 'exit code ' + str(process.returncode)}",
                            "ocr_failed")
        lines = [" ".join(line.split()) for line in out.decode("utf-8", "replace").splitlines()]
        return OcrResult(tuple(OcrLine(line) for line in lines if line), self.name, language)


# ------------------------------------------------------------------- factory


def create_ocr(settings: "Settings") -> OcrEngine:
    """The configured engine, or the first working one for 'auto'."""
    candidates: list[OcrEngine]
    if settings.ocr_engine == "windows":
        candidates = [WindowsOcr(settings.ocr_language)]
    elif settings.ocr_engine == "tesseract":
        candidates = [TesseractOcr(settings.ocr_language)]
    else:
        candidates = [WindowsOcr(settings.ocr_language), TesseractOcr(settings.ocr_language)]
    problems = []
    for engine in candidates:
        problem = engine.problem()
        if problem is None:
            return engine
        problems.append(f"{engine.name}: {problem}")
    raise ToolError("Text recognition isn't available. " + " ".join(problems), "ocr_unavailable")
