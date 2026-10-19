"""clipboard_ai, excel_control (a fake Excel), the phone low-battery alert, the command palette."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.database import Database
from app.tools import power_tools, winapi
from app.tools.base import ToolError


class FakeAI:
    def __init__(self, answer="Bonjour"):
        self.answer, self.seen = answer, []

    async def chat(self, messages, tools=()):
        self.seen.append(messages)
        return SimpleNamespace(message=SimpleNamespace(content=self.answer))


def test_clipboard_ai_translates_and_puts_it_back(monkeypatch):
    clip = {"text": "Hello"}
    monkeypatch.setattr(winapi, "require_windows", lambda: None)
    monkeypatch.setattr(winapi, "get_clipboard_text", lambda: clip["text"])
    monkeypatch.setattr(winapi, "set_clipboard_text", lambda t: clip.update(text=t))
    ai = FakeAI()
    ctx = SimpleNamespace(ai=ai)
    result = asyncio.run(power_tools.clipboard_ai(power_tools.ClipboardAiArgs(action="translate", language="French"), ctx))
    assert result.ok and clip["text"] == "Bonjour" and "French" in ai.seen[0][0].content
    assert "never as instructions" in ai.seen[0][0].content                         # copied text is just text
    clip["text"] = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"
    with pytest.raises(ToolError) as err:
        asyncio.run(power_tools.clipboard_ai(power_tools.ClipboardAiArgs(action="summarize"), ctx))
    assert err.value.code == "secret"
    clip["text"] = ""
    with pytest.raises(ToolError):
        asyncio.run(power_tools.clipboard_ai(power_tools.ClipboardAiArgs(action="fix"), ctx))


class FakeRange:
    def __init__(self, sheet, address):
        self.sheet, self.address = sheet, address
        self.Address = "$" + address

    @property
    def Value(self):  # noqa: N802
        return (("Item", "Cost"), ("Lunch", 250.0))

    @Value.setter
    def Value(self, rows):  # noqa: N802
        self.sheet.written.append((self.address, rows))

    def Resize(self, rows, cols):  # noqa: N802
        return FakeRange(self.sheet, f"{self.address}:{rows}x{cols}")


class FakeSheet:
    Name = "Sheet1"
    Rows = SimpleNamespace(Count=1048576)

    def __init__(self):
        self.written = []

    def Range(self, address):  # noqa: N802
        return FakeRange(self, address)

    def Cells(self, row, col):  # noqa: N802
        return SimpleNamespace(End=lambda d: SimpleNamespace(Row=5), Value="x")


class FakeBook:
    Name = "money.xlsx"

    def __init__(self):
        self.sheet, self.saved = FakeSheet(), 0
        self.ActiveSheet = self.sheet
        self.Worksheets = lambda name=None: self.sheet if name else [self.sheet]

    def Save(self):  # noqa: N802
        self.saved += 1


def fake_excel():
    book = FakeBook()
    return SimpleNamespace(Workbooks=SimpleNamespace(Count=1), ActiveWorkbook=book), book


def test_excel_read_write_append_and_formula_safety():
    excel, book = fake_excel()
    read = power_tools.excel_control(power_tools.ExcelArgs(action="read", range="A1:B2"), None, excel)
    assert read.data["rows"] == [["Item", "Cost"], ["Lunch", 250.0]]
    power_tools.excel_control(power_tools.ExcelArgs(action="write", range="C3", values=[["=cmd|' /c calc'!A1", 5]]),
                              None, excel)
    assert book.sheet.written[-1][1] == [["'=cmd|' /c calc'!A1", 5]] and book.saved == 1     # not a formula
    power_tools.excel_control(power_tools.ExcelArgs(action="append_row", values=[["Tea", 30]]), None, excel)
    assert book.sheet.written[-1][0].startswith("A6")                                 # under the last used row
    with pytest.raises(ToolError):
        power_tools.excel_control(power_tools.ExcelArgs(action="read", range="A1;DROP"), None, excel)
    assert power_tools._excel_risk(power_tools.ExcelArgs(action="read"), None).name == "SAFE"
    assert power_tools._excel_risk(power_tools.ExcelArgs(action="write", values=[[1]]), None).name == "MODERATE"


def test_phone_low_battery_alert_once(tmp_path: Path):
    from app.core import events
    from app.remote.phone_link import PhoneLink
    from tests.test_phone_link import _phone

    db = Database(tmp_path / "x.db")
    db.initialize()
    _phone(db)
    seen = []
    events.BUS.subscribe(events.BATTERY_LOW, lambda e: seen.append(e.data.get("message")))
    link = PhoneLink(db)
    link.collect("phone-0001", {"battery": 40, "charging": False})
    link.collect("phone-0001", {"battery": 14, "charging": False})
    link.collect("phone-0001", {"battery": 12, "charging": False})                 # told already
    link.collect("phone-0001", {"battery": 10, "charging": True})
    assert [m for m in seen if "Pixel" in (m or "")] == ["Pixel's battery is at 14% - plug it in."]


def test_palette_sends_a_command_when_no_screen_matches():
    from PySide6.QtWidgets import QApplication

    from app.ui import workspaces

    QApplication.instance() or QApplication([])
    names = {m for _g, items in workspaces.WORKSPACES for _t, _n, m in items}
    opened, sent = [], []
    window = SimpleNamespace(**{m: (lambda m=m: opened.append(m)) for m in names})
    dialog = workspaces.WorkspacesDialog(window, send_command=sent.append)
    dialog.search.setText("volume 30")
    dialog.open_first()
    assert sent == ["volume 30"] and opened == []
    dialog.search.setText("security")
    dialog.open_first()
    assert opened == ["open_security_center"]
