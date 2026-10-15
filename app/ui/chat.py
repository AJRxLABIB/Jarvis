"""Conversation view with streamed assistant replies. All text is escaped:
nothing the model or a tool writes can inject markup or links."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser, QWidget

# role -> (tag, tag colour, text colour)
_ROLE_STYLE = {
    "user": ("YOU", "#38bdf8", "#e8f6ff"),
    "assistant": ("", "#f5b642", "#d6e6f5"),
    "tool": ("⚙", "#4d6a86", "#6f8ba6"),
    "notice": ("▲", "#fbbf24", "#fcd98a"),
    "error": ("✕", "#f87171", "#fca5a5"),
}
_MONO = "'Cascadia Mono','Consolas',monospace"


class ChatView(QTextBrowser):
    def __init__(self, assistant_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant_name = assistant_name
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self._streaming = False
        self._pending = False
        self._plain: list[tuple[str, str]] = []  # (role, text) - for tests and export

    def _block(self, role: str, text: str) -> str:
        tag, tag_color, color = _ROLE_STYLE.get(role, ("", "#a5f3fc", "#d6e6f5"))
        name = self.assistant_name.upper() if role == "assistant" else tag
        stamp = datetime.now().strftime("%H:%M")
        safe = html.escape(text).replace("\n", "<br>")
        head = f'<span style="color:#35516b;font-family:{_MONO};font-size:7.5pt">{stamp}</span>&nbsp;&nbsp;'
        if role in ("user", "assistant"):
            head += (f'<span style="color:{tag_color};font-family:{_MONO};font-size:8pt;font-weight:700;'
                     f'letter-spacing:2px">{html.escape(name)} &#8250;</span><br>')
            size, family = "10.5pt", "'Segoe UI',sans-serif"
        else:
            head += f'<span style="color:{tag_color};font-family:{_MONO};font-size:8pt">{html.escape(name)}</span> '
            size, family = "8.5pt", _MONO
        gap = "14px" if role == "assistant" else "6px"
        return (f'<p style="margin:4px 0 {gap} 0">{head}<span style="color:{color};font-size:{size};'
                f'font-family:{family}">{safe}</span></p>')

    def add_message(self, role: str, text: str) -> None:
        pending = self._pending
        self.end_stream()
        self._pending = pending and role == "tool"  # the reply continues after a tool line
        self._plain.append((role, text))
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertHtml(self._block(role, text))
        self.insertHtml("<br>")
        self._scroll_to_end()

    def begin_stream(self) -> None:
        """A reply is coming. The name is written only when its first text arrives, so a
        reply that starts with a tool call doesn't leave an empty 'Jarvis' line."""
        self.end_stream()
        self._pending = True

    def _open_stream(self) -> None:
        self._pending = False
        self._plain.append(("assistant", ""))
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertHtml(self._block("assistant", "").replace("</span></p>", "</span>"))
        self._streaming = True

    def append_stream(self, delta: str) -> None:
        if not self._streaming:
            if not delta.strip():
                return
            self._open_stream()
        role, text = self._plain[-1]
        self._plain[-1] = (role, text + delta)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(delta)  # plain text: never interpreted as HTML
        self._scroll_to_end()

    def end_stream(self) -> None:
        self._pending = False
        if self._streaming:
            self._streaming = False
            self.moveCursor(QTextCursor.MoveOperation.End)
            self.insertHtml("<br><br>")

    def finish_reply(self, text: str) -> None:
        """End of a reply. If nothing was streamed (e.g. an error), show ``text`` instead."""
        if not self._streaming and text:
            self.add_message("assistant", text)
        self.end_stream()

    @property
    def messages(self) -> list[tuple[str, str]]:
        return list(self._plain)

    def _scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
