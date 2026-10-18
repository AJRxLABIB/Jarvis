"""My life log: journal, habits (tick grid + streaks), expenses (chart + CSV export) and calendar conflicts.
Everything here is on this PC only; the same things work by voice through the journal / habits / expenses /
calendar_conflicts tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from app.core.finance import Finance
from app.core.habit_tracker import HabitTracker
from app.core.journal import MOODS, Journal


class BarChart(QWidget):
    """Plain bar chart: [(label, value)] - months, or categories."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.bars: list[tuple[str, float]] = []
        self.limits: dict[str, float] = {}
        self.setMinimumHeight(170)
        self.setAccessibleName(title)

    def set_bars(self, bars: list[tuple[str, float]], limits: dict[str, float] | None = None) -> None:
        self.bars, self.limits = list(bars), dict(limits or {})
        self.setToolTip("; ".join(f"{k}: {v:,.0f}" for k, v in self.bars))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#9fb4c4"))
        p.drawText(QRectF(8, 2, self.width() - 16, 18), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.bars or not any(v for _, v in self.bars):
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nothing recorded yet.")
            return
        top, bottom = 42.0, self.height() - 22.0
        peak = max([v for _, v in self.bars] + list(self.limits.values())) or 1.0
        slot = (self.width() - 16) / len(self.bars)
        for i, (label, value) in enumerate(self.bars):
            x = 8 + i * slot + slot * 0.18
            w = slot * 0.64
            h = (bottom - top) * value / peak
            limit = self.limits.get(label)
            colour = QColor("#ff6b6b") if limit and value >= limit else QColor("#ffcc66") if limit and \
                value >= 0.8 * limit else QColor("#35d6ff")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(x, bottom - h, w, h), 3, 3)
            if limit:
                y = bottom - (bottom - top) * limit / peak
                p.setPen(QPen(QColor(255, 255, 255, 150), 1, Qt.PenStyle.DashLine))
                p.drawLine(int(x - 3), int(y), int(x + w + 3), int(y))
            p.setPen(QColor("#d8ecf7"))
            p.drawText(QRectF(x - 10, bottom - h - 16, w + 20, 14), Qt.AlignmentFlag.AlignCenter, f"{value:,.0f}")
            p.setPen(QColor("#8aa0b0"))
            p.drawText(QRectF(8 + i * slot, bottom + 3, slot, 16), Qt.AlignmentFlag.AlignCenter, label[:10])


class PersonalDialog(QDialog):
    def __init__(self, ctx, parent: QWidget | None = None, *, today: date | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("My life log")
        self.setMinimumSize(780, 560)
        self.ctx = ctx
        self.today = today or datetime.now().date()
        self.journal, self.tracker, self.finance = Journal(ctx.database), HabitTracker(ctx.database), Finance(ctx.database)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._journal_page(), "Journal")
        self.tabs.addTab(self._habits_page(), "Habits")
        self.tabs.addTab(self._expenses_page(), "Expenses")
        self.tabs.addTab(self._schedule_page(), "Schedule")
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.refresh_journal()
        self.refresh_habits()
        self.refresh_expenses()

    # ------------------------------------------------------------ journal
    def _journal_page(self) -> QWidget:
        page = QWidget()
        self.entry = QPlainTextEdit()
        self.entry.setPlaceholderText("Write something down... (or say 'note to self: ...')")
        self.entry.setMaximumHeight(110)
        self.mood = QComboBox()
        self.mood.addItem("no mood", "")
        for mood in MOODS:
            self.mood.addItem(mood, mood)
        add = QPushButton("Add entry")
        add.clicked.connect(self.add_entry)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Search the journal")
        self.find.textChanged.connect(self.refresh_journal)
        self.entries = QListWidget()
        self.entries.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.mood)
        row.addStretch(1)
        row.addWidget(add)
        layout = QVBoxLayout(page)
        layout.addWidget(self.entry)
        layout.addLayout(row)
        layout.addWidget(self.find)
        layout.addWidget(self.entries, 1)
        return page

    def add_entry(self) -> None:
        text = self.entry.toPlainText().strip()
        if not text:
            return
        self.journal.add(text, mood=self.mood.currentData() or "")
        self.entry.clear()
        self.refresh_journal()

    def refresh_journal(self) -> None:
        words = self.find.text().strip()
        rows = self.journal.search(words, 100) if words else self.journal.recent(100)
        self.entries.clear()
        for r in rows:
            mark = " 🎙" if r["source"] == "voice" else ""
            mood = f" · {r['mood']}" if r["mood"] else ""
            self.entries.addItem(f"{r['when']}{mood}{mark}\n{r['text']}")
        if not rows:
            self.entries.addItem("No entries yet." if not words else "Nothing matches.")

    # ------------------------------------------------------------ habits
    DAYS = 14

    def _habits_page(self) -> QWidget:
        page = QWidget()
        self.habit_table = QTableWidget()
        self.habit_table.cellClicked.connect(self._toggle_cell)
        self.habit_summary = QLabel()
        self.habit_summary.setWordWrap(True)
        add, remove = QPushButton("Add habit..."), QPushButton("Stop tracking")
        add.clicked.connect(self.add_habit)
        remove.clicked.connect(self.remove_habit)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Click a day to tick it (or untick it). Last 14 days, today on the right."))
        layout.addWidget(self.habit_table, 1)
        layout.addWidget(self.habit_summary)
        layout.addLayout(row)
        return page

    def refresh_habits(self) -> None:
        grid = self.tracker.grid(self.today, self.DAYS)
        habits = {h.name: h for h in self.tracker.habits(self.today)}
        days = [self.today - timedelta(days=k) for k in range(self.DAYS - 1, -1, -1)]
        t = self.habit_table
        t.clear()
        t.setRowCount(len(grid))
        t.setColumnCount(self.DAYS + 1)
        t.setHorizontalHeaderLabels([d.strftime("%a\n%d") for d in days] + ["streak"])
        t.setVerticalHeaderLabels(list(grid))
        for r, (name, marks) in enumerate(grid.items()):
            for c, done in enumerate(marks):
                cell = QTableWidgetItem("✔" if done else "")
                cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if done:
                    cell.setBackground(QColor(53, 214, 255, 70))
                t.setItem(r, c, cell)
            h = habits.get(name)
            streak = QTableWidgetItem(f"{h.streak} (best {h.best})" if h else "")
            streak.setFlags(Qt.ItemFlag.ItemIsEnabled)
            t.setItem(r, self.DAYS, streak)
        t.resizeColumnsToContents()
        left = [h.name for h in habits.values() if not h.done_today]
        self.habit_summary.setText("No habits yet - add one, or say 'track the habit drink water'." if not habits else
                                   "All done today." if not left else "Still to do today: " + ", ".join(left))

    def _toggle_cell(self, row: int, column: int) -> None:
        if column >= self.DAYS:
            return
        name = self.habit_table.verticalHeaderItem(row).text()
        day = self.today - timedelta(days=self.DAYS - 1 - column)
        done = self.habit_table.item(row, column).text() == ""
        self.tracker.check(name, day, done=done)
        self.refresh_habits()

    def add_habit(self) -> None:
        name, ok = QInputDialog.getText(self, "Add habit", "Habit (e.g. drink water, exercise, read 20 pages):")
        if ok and name.strip():
            try:
                self.tracker.add(name)
            except ValueError as exc:
                QMessageBox.warning(self, "Habits", str(exc))
            self.refresh_habits()

    def remove_habit(self) -> None:
        row = self.habit_table.currentRow()
        if row < 0:
            return
        name = self.habit_table.verticalHeaderItem(row).text()
        if QMessageBox.question(self, "Habits", f"Stop tracking '{name}'? Its history is kept.") == \
                QMessageBox.StandardButton.Yes:
            self.tracker.remove(name)
            self.refresh_habits()

    # ------------------------------------------------------------ expenses
    def _expenses_page(self) -> QWidget:
        page = QWidget()
        self.month_chart = BarChart("Last 6 months")
        self.category_chart = BarChart("This month by category (dashed line = budget)")
        self.expense_summary = QLabel()
        self.expense_summary.setWordWrap(True)
        export, export_all = QPushButton("Export this month (CSV)..."), QPushButton("Export everything (CSV)...")
        export.clicked.connect(lambda: self.export_expenses(False))
        export_all.clicked.connect(lambda: self.export_expenses(True))
        row = QHBoxLayout()
        row.addWidget(export)
        row.addWidget(export_all)
        row.addStretch(1)
        layout = QVBoxLayout(page)
        layout.addWidget(self.month_chart, 1)
        layout.addWidget(self.category_chart, 1)
        layout.addWidget(self.expense_summary)
        layout.addLayout(row)
        return page

    def refresh_expenses(self) -> None:
        self.month_chart.set_bars(self.finance.monthly_totals(6))
        report = self.finance.report()
        cats = sorted(report.by_category.items(), key=lambda kv: -kv[1])
        for name in report.budgets:
            if name not in report.by_category:
                cats.append((name, 0.0))
        self.category_chart.set_bars(cats, report.budgets)
        warn = report.warnings()
        self.expense_summary.setText(f"{report.month}: {report.total:,.0f} in {report.count} expense(s)."
                                     + (" " + "; ".join(warn) + "." if warn else "")
                                     + "  Add by voice: 'I spent 450 on lunch'.")

    def export_expenses(self, everything: bool) -> str | None:
        base = self.ctx.paths.data_dir / "exports" if getattr(self.ctx, "paths", None) else None
        name = f"expenses-{'all' if everything else self.today.strftime('%Y-%m')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export expenses", str(base / name) if base else name,
                                              "CSV files (*.csv)")
        if not path:
            return None
        count = self.finance.export_csv(path, all_months=everything)
        QMessageBox.information(self, "Expenses", f"Exported {count} expense(s) to {path}.")
        return path

    # ------------------------------------------------------------ schedule
    def _schedule_page(self) -> QWidget:
        page = QWidget()
        self.range = QComboBox()
        for label, value in (("This week", "week"), ("Today", "today"), ("Tomorrow", "tomorrow")):
            self.range.addItem(label, value)
        check = QPushButton("Check for conflicts")
        check.clicked.connect(self.check_conflicts)
        self.conflict_text = QLabel("Reads your Outlook calendar and shows appointments that overlap.")
        self.conflict_text.setWordWrap(True)
        self.conflicts = QListWidget()
        row = QHBoxLayout()
        row.addWidget(self.range)
        row.addWidget(check)
        row.addStretch(1)
        layout = QVBoxLayout(page)
        layout.addLayout(row)
        layout.addWidget(self.conflict_text)
        layout.addWidget(self.conflicts, 1)
        return page

    def check_conflicts(self) -> None:
        from app.tools.base import ToolError
        from app.tools.personal_tools import ConflictArgs, calendar_conflicts

        self.conflicts.clear()
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            result = calendar_conflicts(ConflictArgs(day=self.range.currentData()), self.ctx)
            self.conflict_text.setText(result.message.split(":")[0] + "." if result.data.get("conflicts")
                                       else result.message)
            self.conflicts.addItems(result.data.get("conflicts", []))
        except ToolError as exc:
            self.conflict_text.setText(str(exc))
        except Exception as exc:
            self.conflict_text.setText(f"I couldn't read the calendar: {exc}")
        finally:
            self.unsetCursor()
