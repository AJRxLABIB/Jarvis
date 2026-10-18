"""Knowledge-graph browser: pick a thing (project, file, decision, memory, goal...), see what it links to as a small
map around it, click a neighbour to walk there. Built from the same graph the "ask" box uses; read-only."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from app.ui.styles.tokens import COLOR

KIND_COLOURS = {"project": "#35d6ff", "file": "#8fb3c9", "technology": "#7cf0b0", "decision": "#ffcc66",
                "memory": "#c49bff", "goal": "#ff8fa3", "mission": "#ff9f5a", "request": "#9aa7b4"}
MAX_NEIGHBOURS = 18


def ego(graph, node_id: str, limit: int = MAX_NEIGHBOURS) -> list[tuple[str, str, float]]:
    """[(neighbour id, relation, angle)] around ``node_id``: one entry per neighbour (first relation wins), spread
    evenly on a circle, most-connected neighbours first. Pure - used by the view and the tests."""
    seen: dict[str, str] = {}
    for edge, other in graph.neighbours(node_id):
        seen.setdefault(other.id, edge.relation)
    degree = {nid: len(graph.neighbours(nid)) for nid in seen}
    ordered = sorted(seen, key=lambda nid: (-degree[nid], graph.nodes[nid].name.lower()))[:limit]
    count = max(len(ordered), 1)
    return [(nid, seen[nid], -math.pi / 2 + 2 * math.pi * i / count) for i, nid in enumerate(ordered)]


class GraphView(QWidget):
    """Paints the chosen node in the middle and its neighbours around it; click one to go there."""
    picked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.graph = None
        self.centre: str | None = None
        self._spots: list[tuple[QRectF, str]] = []
        self.setMinimumSize(360, 300)
        self.setMouseTracking(True)
        self.setAccessibleName("Knowledge map")

    def show_node(self, graph, node_id: str | None) -> None:
        self.graph, self.centre = graph, node_id
        self.update()

    def _bubble(self, painter: QPainter, centre: QPointF, text: str, kind: str, big: bool = False) -> QRectF:
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() + (2 if big else 0))
        font.setBold(big)
        painter.setFont(font)
        label = text if len(text) <= 28 else text[:26] + "…"
        width = painter.fontMetrics().horizontalAdvance(label) + 22
        height = painter.fontMetrics().height() + 12
        rect = QRectF(centre.x() - width / 2, centre.y() - height / 2, width, height)
        colour = QColor(KIND_COLOURS.get(kind, "#9aa7b4"))
        fill = QColor(colour)
        fill.setAlpha(60 if big else 38)
        painter.setPen(QPen(colour, 2 if big else 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, height / 2, height / 2)
        painter.setPen(QColor(COLOR["text_strong"]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        return rect

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(4, 10, 18))       # own dark backdrop: readable under any theme
        self._spots = []
        if self.graph is None or self.centre not in getattr(self.graph, "nodes", {}):
            painter.setPen(QColor("#7f93a3"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Pick something on the left.")
            return
        mid = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2 - 46
        around = ego(self.graph, self.centre)
        spots = []
        for nid, relation, angle in around:
            point = QPointF(mid.x() + radius * math.cos(angle), mid.y() + radius * math.sin(angle))
            painter.setPen(QPen(QColor(120, 180, 210, 110), 1.2))
            painter.drawLine(mid, point)
            painter.setPen(QColor("#7f93a3"))
            half = QPointF((mid.x() + point.x()) / 2, (mid.y() + point.y()) / 2)
            painter.drawText(QRectF(half.x() - 60, half.y() - 9, 120, 18), Qt.AlignmentFlag.AlignCenter,
                             relation.replace("_", " "))
            spots.append((point, nid))
        node = self.graph.nodes[self.centre]
        self._bubble(painter, mid, node.name, node.kind, big=True)
        for point, nid in spots:
            other = self.graph.nodes[nid]
            self._spots.append((self._bubble(painter, point, other.name, other.kind), nid))
        if not around:
            painter.setPen(QColor("#7f93a3"))
            painter.drawText(QRectF(0, mid.y() + 30, self.width(), 24), Qt.AlignmentFlag.AlignCenter,
                             "Nothing linked to this yet.")

    def mousePressEvent(self, event) -> None:
        for rect, nid in self._spots:
            if rect.contains(event.position()):
                self.picked.emit(nid)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        over = any(rect.contains(event.position()) for rect, _ in self._spots)
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)


class GraphBrowser(QWidget):
    def __init__(self, ctx, parent: QWidget | None = None, graph=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.graph = graph
        self.history: list[str] = []
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter, e.g. a project or technology")
        self.search.textChanged.connect(self._fill)
        self.kind = QComboBox()
        self.kind.addItem("Everything", "")
        for kind in KIND_COLOURS:
            self.kind.addItem(kind.title() + "s", kind)
        self.kind.currentIndexChanged.connect(self._fill)
        self.items = QListWidget()
        self.items.currentItemChanged.connect(lambda item, _old: item and self.go(item.data(Qt.ItemDataRole.UserRole)))
        self.view = GraphView()
        self.view.picked.connect(self.go)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        back = QPushButton("Back")
        back.clicked.connect(self.back)
        reload_ = QPushButton("Rebuild")
        reload_.setToolTip("Read projects, memories, goals and files again")
        reload_.clicked.connect(self.rebuild)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.search)
        lv.addWidget(self.kind)
        lv.addWidget(self.items, 1)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.view, 1)
        rv.addWidget(self.details)
        buttons = QHBoxLayout()
        buttons.addWidget(back)
        buttons.addStretch(1)
        buttons.addWidget(reload_)
        rv.addLayout(buttons)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        if self.graph is None:
            self.rebuild()
        else:
            self._fill()

    def rebuild(self) -> None:
        from app.core import knowledge_graph

        try:
            self.graph = knowledge_graph.build(self.ctx)
        except Exception as exc:
            from app.core.knowledge_graph import Graph

            self.graph = Graph()
            self.details.setText(f"I couldn't read everything: {exc}")
        self.history.clear()
        self._fill()

    def _fill(self) -> None:
        if self.graph is None:
            return
        wanted = self.search.text().strip()
        kind = self.kind.currentData() or None
        nodes = (self.graph.find(wanted, (kind,) if kind else None) if wanted else
                 sorted((n for n in self.graph.nodes.values() if kind is None or n.kind == kind),
                        key=lambda n: (n.kind, n.name.lower())))
        self.items.blockSignals(True)
        self.items.clear()
        for node in nodes[:500]:
            item = QListWidgetItem(f"{node.name}   ·  {node.kind}")
            item.setData(Qt.ItemDataRole.UserRole, node.id)
            item.setForeground(QColor(KIND_COLOURS.get(node.kind, "#9aa7b4")))
            self.items.addItem(item)
        self.items.blockSignals(False)
        if not nodes:
            self.view.show_node(self.graph, None)
            self.details.setText("Nothing matches." if wanted else "The graph is empty - tell me about your projects, "
                                 "or add memories and goals.")

    def go(self, node_id: str | None) -> None:
        if not node_id or self.graph is None or node_id not in self.graph.nodes:
            return
        if not self.history or self.history[-1] != node_id:
            self.history.append(node_id)
        node = self.graph.nodes[node_id]
        self.view.show_node(self.graph, node_id)
        extra = "; ".join(f"{k}: {v}" for k, v in list(node.attrs.items())[:4] if v not in ("", None, [], {}))
        links = len({o.id for _, o in self.graph.neighbours(node_id)})
        self.details.setText(f"<b>{node.name}</b> ({node.kind}) - {links} link(s)."
                             + (f"<br>{extra}" if extra else "") + (f"<br><i>from {node.source}</i>" if node.source else ""))

    def back(self) -> None:
        if len(self.history) > 1:
            self.history.pop()
            target = self.history.pop()
            self.go(target)
