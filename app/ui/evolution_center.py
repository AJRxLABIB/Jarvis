"""Evolution Center: Jarvis's improvement proposals - why, evidence, diff, test report - and what's applied.

Every button sends an ordinary command ("test proposal 3") through the same path as your voice, so the
executor's checks and confirmations apply exactly as they do for a spoken request. Nothing here writes code.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

from app.evolution.proposals import ProposalStore


def details_text(proposal) -> str:
    lines = [proposal.summary(), f"Created: {proposal.created_at}"]
    if proposal.applied_at:
        lines.append(f"Applied: {proposal.applied_at}")
    if proposal.reason:
        lines += ["", "Why:", proposal.reason]
    evidence = {k: v for k, v in proposal.evidence.items() if k != "key"}
    if evidence:
        lines += ["", "Evidence:", *(f"  {k}: {v}" for k, v in evidence.items())]
    for edit in proposal.edits:
        lines += ["", f"--- {edit.path}", "- " + edit.find.replace("\n", "\n- ") if edit.find else "(new file)",
                  "+ " + edit.replace.replace("\n", "\n+ ")]
    report = proposal.report
    if report:
        tests = report.get("tests", {})
        lines += ["", f"Test report: {'PASSED' if report.get('passed') else 'FAILED'}",
                  f"  tests: {tests.get('passed', 0)} passed, {tests.get('failed', 0)} failed, "
                  f"{tests.get('errors', 0)} errors (before: {report.get('baseline') or 'unknown'})",
                  f"  linter: {'clean' if report.get('lint_ok') else 'problems'}"]
        lines += [f"  - {r}" for r in report.get("reasons", [])]
        if report.get("test_output"):
            lines += ["", "Test output (end):", report["test_output"]]
    return "\n".join(lines)


class EvolutionCenter(QDialog):
    def __init__(self, database, send_command, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Evolution Center")
        self.setMinimumSize(820, 520)
        self.store, self.send = ProposalStore(database), send_command
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self.show_selected())
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.details)
        split.setSizes([300, 520])
        note = QLabel("Proposals are only ideas until they pass their tests on a copy of Jarvis and you approve them. "
                      "The security core can't be changed here. Applied changes take effect after a rebuild.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        buttons = QHBoxLayout()
        for label, verb in (("Find improvements", None), ("Test", "test"), ("Apply", "apply"), ("Reject", "reject"),
                            ("Roll back", "roll back")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, v=verb: self.command(v))
            buttons.addWidget(button)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for proposal in self.store.list(limit=100):
            item = QListWidgetItem(proposal.summary())
            item.setData(256, proposal.id)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.details.setPlainText("No proposals yet. Press 'Find improvements' or say 'improve yourself'.")

    def selected_id(self) -> int | None:
        item = self.list.currentItem()
        return int(item.data(256)) if item is not None else None

    def show_selected(self) -> None:
        proposal_id = self.selected_id()
        if proposal_id is not None:
            self.details.setPlainText(details_text(self.store.get(proposal_id)))

    def command(self, verb: str | None) -> None:
        if verb is None:
            self.send("improve yourself")
        else:
            proposal_id = self.selected_id()
            if proposal_id is None:
                return
            self.send(f"roll back proposal {proposal_id}" if verb == "roll back" else f"{verb} proposal {proposal_id}")
        self.accept()  # the answer (and any confirmation) appears in the main window
