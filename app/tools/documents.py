"""Writing documents: the result of a task ("compare laptops and make a document") as a Word file.

Files go to Documents\\Jarvis (a new name every time: nothing is ever overwritten). Markdown-style
input is understood: "# Heading", "## Subheading", "- bullet", "| a | b |" tables, blank lines
between paragraphs. .docx is written directly (no Office needed); .md and .txt are also possible.
"""

from __future__ import annotations

import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from pydantic import Field

from app.security.permissions import RiskLevel
from app.security.validator import check_component
from app.tools import winapi
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                  '<Default Extension="xml" ContentType="application/xml"/>'
                  '<Override PartName="/word/document.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                  '<Override PartName="/word/styles.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                  '</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
         'officeDocument" Target="word/document.xml"/></Relationships>')
_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
             'Target="styles.xml"/></Relationships>')
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_STYLES = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {_W}>'
           '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Nirmala UI"/>'
           '<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>'
           '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="40"/>'
           '</w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:spacing '
           'w:before="240" w:after="80"/></w:pPr><w:rPr><w:b/><w:sz w:val="30"/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr><w:spacing '
           'w:before="200" w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
           '</w:styles>')
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _runs(text: str) -> str:
    parts, out = _BOLD.split(text), []
    for i, part in enumerate(parts):
        if part:
            bold = "<w:rPr><w:b/></w:rPr>" if i % 2 else ""
            out.append(f'<w:r>{bold}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _paragraph(text: str, style: str | None = None, bullet: bool = False) -> str:
    props = f'<w:pStyle w:val="{style}"/>' if style else ""
    if bullet:
        props += '<w:ind w:left="360" w:hanging="240"/>'
        text = "• " + text
    return f"<w:p>{'<w:pPr>' + props + '</w:pPr>' if props else ''}{_runs(text)}</w:p>"


def _table(rows: list[list[str]]) -> str:
    border = ('<w:tblBorders>' + "".join(f'<w:{side} w:val="single" w:sz="4" w:color="999999"/>'
                                           for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
              + '</w:tblBorders>')
    body = []
    for index, row in enumerate(rows):
        cells = "".join(f'<w:tc><w:p>{_runs(f"**{c}**" if index == 0 else c)}</w:p></w:tc>' for c in row)
        body.append(f"<w:tr>{cells}</w:tr>")
    return f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>{border}</w:tblPr>{"".join(body)}</w:tbl>'


def markdown_to_body(title: str, text: str) -> str:
    out = [_paragraph(title, "Title")]
    table: list[list[str]] = []
    for raw in text.splitlines() + [""]:
        line = raw.rstrip()
        if line.strip().startswith("|") and line.strip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                table.append(cells)
            continue
        if table:
            width = max(len(r) for r in table)
            out.append(_table([r + [""] * (width - len(r)) for r in table]))
            table = []
        if not line.strip():
            continue
        if line.startswith("## "):
            out.append(_paragraph(line[3:], "Heading2"))
        elif line.startswith("# "):
            out.append(_paragraph(line[2:], "Heading1"))
        elif re.match(r"^\s*[-*•]\s+", line):
            out.append(_paragraph(re.sub(r"^\s*[-*•]\s+", "", line), bullet=True))
        else:
            out.append(_paragraph(line))
    return "".join(out)


def write_docx(path: Path, title: str, text: str) -> None:
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W}><w:body>'
                f'{markdown_to_body(title, text)}<w:sectPr/></w:body></w:document>')
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _STYLES)
        archive.writestr("word/document.xml", document)


def output_folder() -> Path:
    base = winapi.known_folder("DOCUMENTS") if winapi.is_windows() else None
    base = base or Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
    return base / "Jarvis"


def _free_path(folder: Path, name: str, ext: str) -> Path:
    path = folder / f"{name}{ext}"
    number = 2
    while path.exists():
        path = folder / f"{name} ({number}){ext}"
        number += 1
    return path


class DocumentArgs(ToolArgs):
    title: str = Field(min_length=1, max_length=120, description="Document title (also the file name)")
    content: str = Field(min_length=1, max_length=60000,
                         description="The document text in simple markdown: # headings, - bullets, | tables |")
    format: Literal["docx", "md", "txt"] = Field(default="docx", description="docx (Word) unless asked otherwise")
    open_after: bool = Field(default=True, description="Open the document when it's written")


def write_document(args: DocumentArgs, ctx: ToolContext, folder: Path | None = None) -> ToolResult:
    folder = folder or output_folder()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", args.title).strip(" .") or "Document"
    name = check_component(f"{safe[:80]} {datetime.now():%Y-%m-%d}")
    path = _free_path(folder, name, "." + args.format)
    if args.format == "docx":
        write_docx(path, args.title, args.content)
    else:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(f"# {args.title}\n\n{args.content}\n")
    if args.open_after and winapi.is_windows():
        try:
            winapi.start_file(str(path))
        except ToolError:
            pass
    return ToolResult.success(f"Saved '{path.name}' in {folder}.", path=str(path))


TOOLS: tuple[Tool, ...] = (
    Tool("write_document", "Save a document (Word .docx by default) with the results of a task: headings, bullet "
         "points and tables written in simple markdown. Saved in Documents\\Jarvis; never overwrites.",
         write_document, DocumentArgs, RiskLevel.MODERATE, category="files", timeout=30,
         confirmation=lambda a, _: f"Save the document '{a.title}' in Documents\\Jarvis?",
         direct_without_confirmation=lambda a, _: True),
)
