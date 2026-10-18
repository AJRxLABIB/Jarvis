"""Conversation view with streamed assistant replies, drawn as chat bubbles with round avatars (you on the right,
Jarvis on the left). All text is inserted as plain text: nothing the model or a tool writes can inject markup or links."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QTextCharFormat, QTextCursor, QTextDocument, QTextFrame, QTextFrameFormat,
    QTextImageFormat,
)
from PySide6.QtWidgets import QTextBrowser, QWidget

from app.ui.styles.tokens import COLOR
from app.utils.timefmt import time12

# role -> (tag, tag colour, text colour) for the compact lines (tool / notice / error)
_ROLE_STYLE = {
    "tool": ("⚙", "#4d6a86", "#6f8ba6"),
    "notice": ("▲", COLOR["warn"], "#fcd98a"),
    "error": ("✕", COLOR["danger"], "#fca5a5"),
}
_MONO = "'Cascadia Mono','Consolas',monospace"
_AVATAR = 26
_SIDE_GAP = 40      # px kept free on the far side, so bubbles read as left / right


def waveform_pixmap(levels: list[float], color: str, width: int = 170, height: int = 26) -> QPixmap:
    """Real microphone levels (0..1) of a spoken message drawn as small rounded bars."""
    pix = QPixmap(width * 2, height * 2)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    bars = max(8, min(len(levels), 46))
    step = len(levels) / bars
    slot = width * 2 / bars
    for i in range(bars):
        chunk = levels[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]
        level = max(chunk) if chunk else 0.0
        h = 4 + (height * 2 - 4) * max(0.0, min(1.0, level))
        painter.drawRoundedRect(QRectF(i * slot + 1, (height * 2 - h) / 2, max(2.0, slot - 3), h), 2, 2)
    painter.end()
    return pix


def _avatar(letter: str, ring: str, fill: str, person: bool = False) -> QPixmap:
    """A round avatar: a head-and-shoulders silhouette for you, the assistant's initial for Jarvis."""
    size = _AVATAR * 2
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(fill)))
    painter.setPen(QPen(QColor(ring), 3))
    painter.drawEllipse(QRectF(3, 3, size - 6, size - 6))
    if person:
        clip = QPainterPath()
        clip.addEllipse(QRectF(5, 5, size - 10, size - 10))
        painter.setClipPath(clip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLOR["chip_text"]))
        painter.drawEllipse(QRectF(size * 0.34, size * 0.20, size * 0.32, size * 0.34))      # head
        painter.drawEllipse(QRectF(size * 0.18, size * 0.58, size * 0.64, size * 0.60))      # shoulders
    else:
        font = QFont("Segoe UI", 15)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLOR["text_strong"]))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    return pix


class ChatView(QTextBrowser):
    def __init__(self, assistant_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant_name = assistant_name
        self.setAccessibleName("Conversation")
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self._streaming = False
        self._pending = False
        self._waves = 0
        self._frame: QTextFrame | None = None   # the bubble a reply is being streamed into
        self._plain: list[tuple[str, str]] = []  # (role, text) - for tests and export
        self._bubbles: list[tuple[QTextFrame, str]] = []     # (frame, role): their rounded backgrounds are painted
        self._avatars = {"user": _avatar("", COLOR["bubble_user_edge"], COLOR["bubble_user"], person=True),
                         "assistant": _avatar((assistant_name or "J")[:1].upper(), COLOR["cyan"], COLOR["bubble_ai"])}

    # ------------------------------------------------------------ bubbles

    def _bubble(self, role: str) -> QTextFrame:
        """A framed bubble at the end of the conversation with its avatar + name header; the cursor ends inside it."""
        user = role == "user"
        fmt = QTextFrameFormat()
        fmt.setBorder(0)
        fmt.setPadding(9)
        fmt.setTopMargin(5)
        fmt.setBottomMargin(7)
        fmt.setLeftMargin(_SIDE_GAP if user else _AVATAR + 12)       # the avatar sits outside, beside the bubble
        fmt.setRightMargin(_AVATAR + 12 if user else _SIDE_GAP)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        frame = cursor.insertFrame(fmt)
        self._bubbles.append((frame, role))
        name = "YOU" if user else self.assistant_name.upper()
        head = QTextCharFormat()
        head.setForeground(QColor(COLOR["accent" if user else "gold"]))
        head.setFontFamilies(["Cascadia Mono", "Consolas", "monospace"])
        head.setFontPointSize(8)
        head.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(f"{name}  ", head)
        stamp = QTextCharFormat()
        stamp.setForeground(QColor(COLOR["dim"]))
        stamp.setFontFamilies(["Cascadia Mono", "Consolas", "monospace"])
        stamp.setFontPointSize(7.5)
        cursor.insertText(time12(datetime.now()), stamp)
        cursor.insertBlock()
        return frame

    def _insert_wave(self, frame: QTextFrame, levels: list[float]) -> None:
        self._waves += 1
        name = f"wave:{self._waves}"
        self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(name),
                                    waveform_pixmap(levels, COLOR["accent"]))
        image = QTextImageFormat()
        image.setName(name)
        image.setWidth(170)
        image.setHeight(26)
        cursor = frame.lastCursorPosition()
        cursor.insertImage(image)
        cursor.insertBlock()

    def _body_format(self, role: str) -> QTextCharFormat:
        body = QTextCharFormat()
        body.setForeground(QColor(COLOR["text_strong" if role == "user" else "text"]))
        body.setFontFamilies(["Segoe UI", "sans-serif"])
        body.setFontPointSize(10.5)
        return body

    def _compact(self, role: str, text: str) -> str:
        tag, tag_color, color = _ROLE_STYLE.get(role, ("", "#a5f3fc", COLOR["text"]))
        safe = html.escape(text).replace("\n", "<br>")
        stamp = time12(datetime.now())
        return (f'<p style="margin:4px 0 6px 0"><span style="color:{COLOR["dim"]};font-family:{_MONO};font-size:7.5pt">'
                f'{stamp}</span>&nbsp;&nbsp;<span style="color:{tag_color};font-family:{_MONO};font-size:8pt">'
                f'{html.escape(tag)}</span> <span style="color:{color};font-size:8.5pt;font-family:{_MONO}">{safe}</span></p>')

    def _after_frame(self) -> None:
        self.moveCursor(QTextCursor.MoveOperation.End)

    # ------------------------------------------------------------ API

    def add_message(self, role: str, text: str, wave: list[float] | None = None) -> None:
        pending = self._pending
        self.end_stream()
        self._pending = pending and role == "tool"  # the reply continues after a tool line
        self._plain.append((role, text))
        self.moveCursor(QTextCursor.MoveOperation.End)
        if role in ("user", "assistant"):
            frame = self._bubble(role)
            if wave and role == "user" and len(wave) >= 6 and max(wave) > 0.05:
                self._insert_wave(frame, wave)   # your own voice, as the microphone measured it
            frame.lastCursorPosition().insertText(text, self._body_format(role))
            self._after_frame()
        else:
            self.insertHtml(self._compact(role, text))
            self.insertHtml("<br>")
        self._scroll_to_end()

    def begin_stream(self) -> None:
        """A reply is coming. The bubble is drawn only when its first text arrives, so a reply that starts with a
        tool call doesn't leave an empty 'Jarvis' bubble."""
        self.end_stream()
        self._pending = True

    def _open_stream(self) -> None:
        self._pending = False
        self._plain.append(("assistant", ""))
        self.moveCursor(QTextCursor.MoveOperation.End)
        self._frame = self._bubble("assistant")
        self._streaming = True

    def append_stream(self, delta: str) -> None:
        if not self._streaming:
            if not delta.strip():
                return
            self._open_stream()
        role, text = self._plain[-1]
        self._plain[-1] = (role, text + delta)
        if self._frame is not None:
            self._frame.lastCursorPosition().insertText(delta, self._body_format("assistant"))  # plain text only
        self._scroll_to_end()

    def end_stream(self) -> None:
        self._pending = False
        if self._streaming:
            self._streaming = False
            self._frame = None
            self.moveCursor(QTextCursor.MoveOperation.End)

    def finish_reply(self, text: str) -> None:
        """End of a reply. If nothing was streamed (e.g. an error), show ``text`` instead."""
        if not self._streaming and text:
            self.add_message("assistant", text)
        self.end_stream()

    @property
    def messages(self) -> list[tuple[str, str]]:
        return list(self._plain)

    def paintEvent(self, event) -> None:  # noqa: N802
        """Rounded, softly glowing bubbles with the avatars outside them, painted under the text."""
        if self._bubbles:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            layout = self.document().documentLayout()
            dx, dy = -self.horizontalScrollBar().value(), -self.verticalScrollBar().value()
            visible = self.viewport().rect()
            for frame, role in self._bubbles:
                rect = layout.frameBoundingRect(frame).translated(dx, dy)
                if not rect.intersects(QRectF(visible)):
                    continue
                user = role == "user"
                fmt = frame.frameFormat()
                body = rect.adjusted(fmt.leftMargin(), fmt.topMargin(), -fmt.rightMargin(), -fmt.bottomMargin())
                edge = QColor(COLOR["bubble_user_edge" if user else "bubble_ai_edge"])
                glow = QColor(edge)
                glow.setAlpha(45)
                painter.setPen(QPen(glow, 4))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(body, 9, 9)
                fill = QColor(COLOR["bubble_user" if user else "bubble_ai"])
                fill.setAlpha(110)                     # see-through bubbles: the backdrop shows behind them
                painter.setPen(QPen(edge, 1.2))
                painter.setBrush(fill)
                painter.drawRoundedRect(body, 9, 9)
                avatar = self._avatars["user" if user else "assistant"]
                x = body.right() + 6 if user else body.left() - _AVATAR - 6
                painter.drawPixmap(QRectF(x, body.top() + 2, _AVATAR, _AVATAR), avatar, QRectF(avatar.rect()))
            painter.end()
        super().paintEvent(event)

    def _scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
