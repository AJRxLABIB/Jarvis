"""File tools: zip / unzip, merge PDFs, shrink photos, convert images, Word to PDF.

Every result is a NEW file (or folder) next to the originals, with a name that doesn't exist yet - nothing
is overwritten or changed in place. Paths go through the same path policy as every file tool (a file in a
protected folder can't be read or written this way; outside your safe folders it asks first).

Unzipping is guarded: no member may leave the destination folder ("zip slip": absolute paths or ".."),
and archives that expand to more than MAX_UNZIP_BYTES or MAX_UNZIP_FILES are refused (zip bombs).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from app.utils.logging_setup import get_logger, log_event

_log = get_logger("tools.file_tools")
MAX_ITEMS = 50
MAX_UNZIP_BYTES = 2 * 1024 ** 3
MAX_UNZIP_FILES = 10_000
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def _policy(ctx: ToolContext):
    return ctx.get_path_policy()


def _existing(paths: list[str], ctx: ToolContext) -> list[Path]:
    return [_policy(ctx).resolve(p, must_exist=True) for p in paths]


def free_name(folder: Path, stem: str, suffix: str) -> Path:
    """'photos.zip', then 'photos (2).zip'... - never an existing name."""
    candidate = folder / f"{stem}{suffix}"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def _risk_for(paths: list[str], outputs_in: list[str], ctx: ToolContext) -> RiskLevel:
    try:
        level = RiskLevel.SAFE
        for path in _existing(paths, ctx):
            level = max(level, _policy(ctx).risk(path, "read"))
            level = max(level, _policy(ctx).risk(path.parent, "write"))
        return RiskLevel(level)
    except ToolError:
        return RiskLevel.DANGEROUS


def _clean_stem(name: str | None, fallback: str) -> str:
    text = "".join(ch for ch in (name or "").strip() if ch not in '<>:"/\\|?*').strip(" .")
    return text[:80] or fallback


# ------------------------------------------------------------------ zip / unzip


class ZipArgs(ToolArgs):
    paths: list[str] = Field(min_length=1, max_length=MAX_ITEMS, description="Files or folders to put in the zip")
    name: str | None = Field(default=None, max_length=80, description="Name for the zip (without .zip)")


def zip_items(args: ZipArgs, ctx: ToolContext) -> ToolResult:
    items = _existing(args.paths, ctx)
    target = free_name(items[0].parent, _clean_stem(args.name, items[0].stem if len(items) == 1 else "archive"), ".zip")
    count = 0
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            if item.is_dir():
                for file in sorted(item.rglob("*")):
                    if file.is_file() and file != target:
                        archive.write(file, Path(item.name) / file.relative_to(item))
                        count += 1
            else:
                archive.write(item, item.name)
                count += 1
    log_event(_log, "zipped", files=count)
    return ToolResult.success(f"Made {target.name} ({count} file(s)) in {target.parent}.", path=str(target), files=count)


class UnzipArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="The .zip file")


def safe_members(archive: zipfile.ZipFile, destination: Path) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_UNZIP_FILES:
        raise ToolError(f"That archive has more than {MAX_UNZIP_FILES} files; I won't unpack it.", "too_large")
    if sum(m.file_size for m in members) > MAX_UNZIP_BYTES:
        raise ToolError("That archive would unpack to more than 2 GB; I won't unpack it.", "too_large")
    root = destination.resolve()
    for member in members:
        name = member.filename.replace("\\", "/")
        target = (destination / name).resolve()
        if name.startswith("/") or ":" in name.split("/")[0] or ".." in name.split("/") or \
                not (target == root or root in target.parents):
            raise ToolError(f"The archive contains a file that would land outside its folder ({member.filename}); "
                            "I won't unpack it.", "unsafe_archive")
    return members


def extract_zip(args: UnzipArgs, ctx: ToolContext) -> ToolResult:
    source = _policy(ctx).resolve(args.path, must_exist=True)
    if not zipfile.is_zipfile(source):
        raise ToolError(f"{source.name} isn't a zip file.", "not_zip")
    destination = free_name(source.parent, source.stem, "")
    with zipfile.ZipFile(source) as archive:
        members = safe_members(archive, destination)
        destination.mkdir()
        archive.extractall(destination, members)
    files = sum(1 for m in members if not m.is_dir())
    return ToolResult.success(f"Unpacked {files} file(s) into {destination.name} in {destination.parent}.",
                              path=str(destination), files=files)


# ------------------------------------------------------------------ PDFs


class MergeArgs(ToolArgs):
    paths: list[str] = Field(min_length=2, max_length=MAX_ITEMS, description="PDF files, in order")
    name: str | None = Field(default=None, max_length=80, description="Name for the merged PDF (without .pdf)")


def merge_pdfs(args: MergeArgs, ctx: ToolContext) -> ToolResult:
    from pypdf import PdfReader, PdfWriter

    files = _existing(args.paths, ctx)
    not_pdf = [f.name for f in files if f.suffix.lower() != ".pdf"]
    if not_pdf:
        raise ToolError(f"These aren't PDFs: {', '.join(not_pdf)}.", "not_pdf")
    writer, pages = PdfWriter(), 0
    for file in files:
        reader = PdfReader(str(file))
        if reader.is_encrypted:
            raise ToolError(f"{file.name} is password-protected; I can't merge it.", "encrypted")
        for page in reader.pages:
            writer.add_page(page)
            pages += 1
    target = free_name(files[0].parent, _clean_stem(args.name, "merged"), ".pdf")
    with open(target, "xb") as handle:
        writer.write(handle)
    return ToolResult.success(f"Merged {len(files)} PDFs ({pages} pages) into {target.name}.", path=str(target),
                              pages=pages)


# ------------------------------------------------------------------ images


class ShrinkArgs(ToolArgs):
    paths: list[str] = Field(min_length=1, max_length=MAX_ITEMS, description="Photos (or a folder of photos)")
    max_side: int = Field(default=1920, ge=320, le=8000, description="Longest side in pixels")
    quality: int = Field(default=80, ge=40, le=95, description="JPEG quality")


def _images(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_TYPES)
        elif path.suffix.lower() in IMAGE_TYPES:
            found.append(path)
    return found[:500]


def shrink_images(args: ShrinkArgs, ctx: ToolContext) -> ToolResult:
    from PIL import Image, ImageOps

    images = _images(_existing(args.paths, ctx))
    if not images:
        raise ToolError("I didn't find any photos there.", "no_images")
    saved, before, after = [], 0, 0
    for image_path in images:
        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((args.max_side, args.max_side))
            target = free_name(image_path.parent, f"{image_path.stem}-small", ".jpg")
            image.convert("RGB").save(target, "JPEG", quality=args.quality, optimize=True)
        before += image_path.stat().st_size
        after += target.stat().st_size
        saved.append(target)
    saving = 100 - round(100 * after / before) if before else 0
    return ToolResult.success(f"Made {len(saved)} smaller cop{'y' if len(saved) == 1 else 'ies'} (about {saving}% smaller), "
                              f"named '...-small.jpg' next to the originals.", paths=[str(p) for p in saved[:50]],
                              path=str(saved[0]))


class ConvertImageArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="The image")
    to: Literal["png", "jpg", "webp"] = Field(description="The new format")


def convert_image(args: ConvertImageArgs, ctx: ToolContext) -> ToolResult:
    from PIL import Image

    source = _policy(ctx).resolve(args.path, must_exist=True)
    if source.suffix.lower() not in IMAGE_TYPES:
        raise ToolError(f"{source.name} isn't an image I can convert.", "not_image")
    target = free_name(source.parent, source.stem, f".{args.to}")
    with Image.open(source) as image:
        (image.convert("RGB") if args.to == "jpg" else image).save(target, "JPEG" if args.to == "jpg" else args.to.upper())
    return ToolResult.success(f"Saved {target.name} next to the original.", path=str(target))


# ------------------------------------------------------------------ Word -> PDF


class WordArgs(ToolArgs):
    path: str = Field(min_length=1, max_length=400, description="The Word document (.docx / .doc)")


def word_to_pdf(args: WordArgs, ctx: ToolContext) -> ToolResult:
    source = _policy(ctx).resolve(args.path, must_exist=True)
    if source.suffix.lower() not in (".docx", ".doc", ".rtf", ".odt"):
        raise ToolError(f"{source.name} isn't a Word document.", "not_word")
    target = free_name(source.parent, source.stem, ".pdf")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ToolError("Word to PDF needs Windows with Microsoft Word.", "unsupported") from exc
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
    except Exception as exc:
        raise ToolError("Microsoft Word isn't installed, so I can't convert it (you could open it and use "
                        "'Save as PDF').", "unsupported") from exc
    try:
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(source), ReadOnly=True, AddToRecentFiles=False)
        try:
            document.SaveAs2(str(target), FileFormat=17)  # wdFormatPDF
        finally:
            document.Close(False)
    finally:
        word.Quit()
    return ToolResult.success(f"Saved {target.name} next to the document.", path=str(target))


def _verify_new(args, result: ToolResult, ctx: ToolContext) -> tuple[bool, str]:
    path = result.data.get("path")
    ok = bool(path) and Path(str(path)).exists()
    return ok, f"{Path(str(path)).name if path else 'the result'} isn't there"


def _single(fn):
    return lambda a, c: _risk_for([a.path], [], c)


TOOLS: tuple[Tool, ...] = (
    Tool("zip_items", "Put files or folders into a new .zip next to them.", zip_items, ZipArgs, RiskLevel.SAFE,
         "files", timeout=300, risk=lambda a, c: _risk_for(a.paths, [], c), verify=_verify_new),
    Tool("extract_zip", "Unpack a .zip into a new folder next to it (unsafe archives are refused).", extract_zip,
         UnzipArgs, RiskLevel.SAFE, "files", timeout=300, risk=_single(None), verify=_verify_new),
    Tool("merge_pdfs", "Merge several PDFs (in the given order) into a new PDF.", merge_pdfs, MergeArgs,
         RiskLevel.SAFE, "files", timeout=180, risk=lambda a, c: _risk_for(a.paths, [], c), verify=_verify_new),
    Tool("shrink_images", "Make smaller copies of photos (for sending): '...-small.jpg' next to each original.",
         shrink_images, ShrinkArgs, RiskLevel.SAFE, "files", timeout=300, risk=lambda a, c: _risk_for(a.paths, [], c),
         verify=_verify_new),
    Tool("convert_image", "Convert an image to PNG, JPG or WebP (a new file next to it).", convert_image,
         ConvertImageArgs, RiskLevel.SAFE, "files", timeout=60, risk=_single(None), verify=_verify_new),
    Tool("word_to_pdf", "Convert a Word document to PDF (needs Microsoft Word installed).", word_to_pdf, WordArgs,
         RiskLevel.SAFE, "files", timeout=180, risk=_single(None), verify=_verify_new),
)
