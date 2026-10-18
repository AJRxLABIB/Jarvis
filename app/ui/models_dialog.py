"""AI and Models workspace: the installed local models, what each can do on THIS PC, how they behaved here, your
benchmark results, and which model does which job - with a recommendation built only from those facts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.ai import roles

COLUMNS = ("Model", "Size", "Fits RAM", "Tools", "Vision", "Thinking", "Embedding", "Benchmark", "Calls / fails",
           "Avg reply", "Tool calls OK")


def _yes(value) -> str:
    return "yes" if value is True else ("no" if value is False else "?")


class ModelsDialog(QDialog):
    def __init__(self, controller, parent: QWidget | None = None, *, models=None, evidence=None, health=None,
                 total_ram=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI and models")
        self.setMinimumSize(980, 560)
        self.controller = controller
        self._inputs = (models, evidence, health, total_ram)
        intro = QLabel("Everything runs on this PC through Ollama. Capabilities come from Ollama, behaviour from this "
                       "PC's own record, scores from YOUR benchmark (python run.py --eval-quality). A model's name is "
                       "never used to judge it.")
        intro.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Installed models")
        self.roles_label = QLabel()
        self.roles_label.setWordWrap(True)
        self.roles_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        use = QPushButton("Use selected as main model")
        use.setAccessibleName("Use the selected model as the main model")
        use.clicked.connect(self.use_selected)
        apply = QPushButton("Apply recommended roles...")
        apply.setToolTip("Saves the recommended models for each job (asks first)")
        apply.clicked.connect(self.apply_recommended)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        for b in (use, apply, refresh):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("Jobs (role -> model)"))
        layout.addWidget(self.roles_label)
        layout.addLayout(buttons)
        self.picks: list[roles.Pick] = []
        self.refresh()

    # ---- data
    def _gather(self):
        models, evidence, health, total_ram = self._inputs
        c = self.controller
        if models is None:
            status = getattr(c, "last_status", None)
            models = list(getattr(status, "models", ()) or ())
        if evidence is None:
            from app.ai import switching

            evidence = switching.load_evidence(c.ctx.paths.data_dir) if getattr(c.ctx, "paths", None) else {}
        if health is None:
            ai = getattr(c, "ai", None)
            health = ai.health.summary() if ai is not None and hasattr(ai, "health") else {}
        if total_ram is None:
            from app.ai.health import total_memory_bytes

            total_ram = total_memory_bytes()
        return models, evidence, health, total_ram

    def current_roles(self) -> dict[str, str]:
        s = self.controller.ctx.settings
        return {"general": s.ai_model, "fast": s.ai_fast_model, "smart": s.ai_smart_model,
                "coding": getattr(s, "ai_coding_model", ""), "vision": s.vision_model, "embedding": s.embedding_model}

    def refresh(self) -> None:
        models, evidence, health, total_ram = self._gather()
        self.facts = roles.facts(models, evidence, health, total_ram)
        self.picks = roles.recommend(self.facts)
        t = self.table
        t.setRowCount(len(self.facts))
        for r, f in enumerate(self.facts):
            values = (f.name, f"{f.size_gb} GB" if f.size_gb is not None else "?",
                      "no" if f.fits is False else ("yes" if f.fits else "?"), _yes(f.tools), _yes(f.vision),
                      _yes(f.thinking), _yes(f.embedding_only),
                      f"{f.score:.0%}" if f.score is not None else "not run", f"{f.calls} / {f.failures}",
                      f"{f.avg_seconds:.1f} s" if f.avg_seconds else "-",
                      f"{f.tool_ok_rate:.0%}" if f.tool_ok_rate is not None else "-")
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if c == 2 and value == "no":
                    item.setToolTip("Bigger than this PC's memory: it can't load here")
                t.setItem(r, c, item)
        current = self.current_roles()
        lines = []
        for p in self.picks:
            now = current.get(p.role) or "(not set)"
            rec = "already set" if roles.same_model(p.model, current.get(p.role)) else (p.model or "keep as is")
            lines.append(f"<b>{p.role}</b>: now {now} &rarr; {rec} <span style='color:#8aa0b0'>({p.reason})</span>")
        if not self.facts:
            lines.insert(0, "No models listed - is Ollama running? (Refresh after starting it.)")
        self.roles_label.setText("<br>".join(lines))

    # ---- actions
    def selected_model(self) -> str | None:
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 and self.table.item(row, 0) else None

    def _save(self, changes: dict[str, str]) -> bool:
        from app.config.env_writer import update_env_file
        from app.config.settings import ENV_NAMES

        c = self.controller
        saved = update_env_file(c.ctx.paths.env_file, changes)
        fields = {name for name, env in ENV_NAMES.items() if env in changes}
        if hasattr(c, "apply_settings"):
            c.apply_settings(saved, fields)
        self.refresh()
        return True

    def use_selected(self) -> bool:
        name = self.selected_model()
        fact = next((f for f in getattr(self, "facts", []) if f.name == name), None)
        if fact is None:
            return False
        if fact.embedding_only or fact.tools is False:
            QMessageBox.warning(self, "AI and models", f"{name} can't chat with tools, so it can't be the main model.")
            return False
        return self._save({"AI_MODEL": name})

    def apply_recommended(self, confirm: bool = True) -> bool:
        changes = roles.changes(self.picks, self.current_roles())
        if not changes:
            QMessageBox.information(self, "AI and models", "Nothing to change - the recommendation matches your setup "
                                                           "(or needs a benchmark first).")
            return False
        text = "\n".join(f"{k} = {v}" for k, v in changes.items())
        if confirm and QMessageBox.question(self, "AI and models", f"Save these?\n\n{text}") != \
                QMessageBox.StandardButton.Yes:
            return False
        return self._save(changes)
