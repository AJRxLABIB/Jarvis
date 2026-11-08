"""Read-only summaries of Word / Excel / PowerPoint files (.docx .xlsx .pptx) without Office installed and without new dependencies.

These formats are zip files of XML. This module reads a few parts and reports structure: headings / word count (Word), sheets with sizes and formula counts
(Excel), slide titles and notes (PowerPoint), plus the document properties. It never opens the file in Office, never runs macros (.docm / .xlsm are
refused), and treats the content as untrusted data: text is only counted or shortened, never followed. Zip bombs are limited by member count and the
size of each part read.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_MEMBERS = 5000
MAX_PART = 25 * 1024 * 1024
KINDS = {".docx": "word", ".xlsx": "excel", ".pptx": "powerpoint"}
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "dc": "http://purl.org/dc/elements/1.1/",
      "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties", "dcterms": "http://purl.org/dc/terms/"}


class OfficeError(ValueError):
    pass


def _read(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise OfficeError(f"{name} is too large to read safely.")
    return ET.fromstring(z.read(name))


def _props(z: zipfile.ZipFile) -> dict[str, str]:
    root = _read(z, "docProps/core.xml")
    if root is None:
        return {}
    out = {}
    for tag, label in (("dc:title", "title"), ("dc:creator", "author"), ("dcterms:modified", "modified")):
        el = root.find(tag, NS)
        if el is not None and el.text:
            out[label] = el.text.strip()[:120]
    return out


def _word(z: zipfile.ZipFile) -> dict:
    root = _read(z, "word/document.xml")
    if root is None:
        raise OfficeError("This does not look like a Word document.")
    words, headings, paras = 0, [], 0
    for p in root.iter(f"{{{NS['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{NS['w']}}}t"))
        if text.strip():
            paras += 1
            words += len(text.split())
        style = p.find("w:pPr/w:pStyle", NS)
        val = style.get(f"{{{NS['w']}}}val", "") if style is not None else ""
        if re.match(r"(?i)^(heading|title)", val) and text.strip():
            headings.append(f"{val}: {text.strip()[:80]}")
    tables = len(list(root.iter(f"{{{NS['w']}}}tbl")))
    return {"paragraphs": paras, "words": words, "headings": headings[:30], "tables": tables,
            "comments": z.getinfo("word/comments.xml").file_size > 0 if "word/comments.xml" in z.namelist() else False,
            "images": sum(1 for n in z.namelist() if n.startswith("word/media/"))}


def _excel(z: zipfile.ZipFile) -> dict:
    book = _read(z, "xl/workbook.xml")
    if book is None:
        raise OfficeError("This does not look like an Excel workbook.")
    names = [s.get("name", "?") for s in book.iter(f"{{{NS['m']}}}sheet")]
    sheets = []
    for i, name in enumerate(names, 1):
        part = f"xl/worksheets/sheet{i}.xml"
        info = {"name": name[:60]}
        try:
            if part in z.namelist() and z.getinfo(part).file_size <= MAX_PART:
                raw = z.read(part).decode("utf-8", "replace")
                dim = re.search(r'<dimension ref="([^"]+)"', raw)
                info["range"] = dim.group(1) if dim else "empty"
                info["formulas"] = raw.count("<f>") + raw.count("<f ")
        except Exception:
            info["range"] = "unreadable"
        sheets.append(info)
    return {"sheets": sheets, "charts": sum(1 for n in z.namelist() if n.startswith("xl/charts/chart")),
            "tables": sum(1 for n in z.namelist() if n.startswith("xl/tables/table"))}


def _slides(z: zipfile.ZipFile) -> dict:
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    if not slides:
        raise OfficeError("This does not look like a PowerPoint file.")
    titles = []
    for n in slides[:60]:
        root = _read(z, n)
        texts = ["".join(t.text or "" for t in sp.iter(f"{{{NS['a']}}}t")).strip() for sp in root.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}sp")] if root is not None else []
        titles.append(next((t for t in texts if t), "(no text)")[:80])
    return {"slides": len(slides), "titles": titles, "notes": sum(1 for n in z.namelist() if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)),
            "images": sum(1 for n in z.namelist() if n.startswith("ppt/media/"))}


def summarize(path: Path) -> dict:
    kind = KINDS.get(path.suffix.lower())
    if kind is None:
        raise OfficeError("Only .docx, .xlsx and .pptx are read (macro files are refused).")
    try:
        with zipfile.ZipFile(path) as z:
            if len(z.namelist()) > MAX_MEMBERS:
                raise OfficeError("This file has too many parts to read safely.")
            if any(n.lower().endswith("vbaproject.bin") for n in z.namelist()):
                raise OfficeError("This file contains macros; I only read macro-free files.")
            body = {"word": _word, "excel": _excel, "powerpoint": _slides}[kind](z)
            return {"kind": kind, "name": path.name, "size_kb": round(path.stat().st_size / 1024), **_props(z), **body}
    except zipfile.BadZipFile as exc:
        raise OfficeError("This is not a valid Office file (it may be damaged or password-protected).") from exc
    except ET.ParseError as exc:
        raise OfficeError("A part of this file could not be read.") from exc


def render(info: dict) -> str:
    head = f"{info['name']} ({info['kind']}, {info['size_kb']} KB)" + (f", by {info['author']}" if info.get("author") else "") + (f", modified {info['modified'][:10]}" if info.get("modified") else "")
    if info["kind"] == "word":
        lines = [f"{info['words']} words in {info['paragraphs']} paragraphs, {info['tables']} table(s), {info['images']} image(s)" + (", has comments" if info["comments"] else "")]
        lines += [f"  {h}" for h in info["headings"][:12]]
    elif info["kind"] == "excel":
        lines = [f"{len(info['sheets'])} sheet(s), {info['charts']} chart(s), {info['tables']} table(s)"]
        lines += [f"  {s['name']}: {s.get('range', '?')}, {s.get('formulas', 0)} formula(s)" for s in info["sheets"][:15]]
    else:
        lines = [f"{info['slides']} slide(s), {info['notes']} with speaker notes, {info['images']} image(s)"]
        lines += [f"  {i}. {t}" for i, t in enumerate(info["titles"][:15], 1)]
    return "\n".join([head, *lines])
