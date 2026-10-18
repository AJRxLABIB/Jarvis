"""The workflow studio's node canvas: your steps as boxes joined by arrows.

A view of the real workflow (see app/automation/canvas.py): boxes sit in numbered slots, arrows always mean "then" (or
carry the next step's condition). Click selects (the step table follows), drag a box to another slot to reorder, arrow
keys move the selection, Ctrl+Left/Right move the selected step, Delete removes it, F2-free: add notes and conditions
with the studio's buttons. A canvas cannot be read by a screen reader the way the Steps table can, so the table stays
the accessible equivalent and is always one tab away.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
)

from app.automation import canvas as geo
from app.automation.tasks import Step
from app.ui.styles.tokens import COLOR

RISK_COLOUR = {"safe": COLOR["ok"], "moderate": COLOR["gold"], "dangerous": COLOR["danger"], "blocked": COLOR["danger"]}


class NodeItem(QGraphicsRectItem):
    def __init__(self, index: int, step: Step, risk: str, asks: bool, canvas: "WorkflowCanvas") -> None:
        super().__init__(0, 0, geo.NODE_W, geo.NODE_H)
        self.index, self.canvas = index, canvas
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(2)
        colour = QColor(RISK_COLOUR.get(risk, COLOR["accent"]))
        pen = QPen(colour, 2)
        if step.continue_on_error:
            pen.setStyle(Qt.PenStyle.DashLine)            # dashed = "carries on if this step fails"
        self.setPen(pen)
        self.setBrush(QBrush(QColor(COLOR["panel"])))
        self.setToolTip(f"Step {index + 1}: {step.describe()}\nrisk: {risk}{', asks first' if asks else ''}"
                        f"{', continues on error' if step.continue_on_error else ''}")
        title = QGraphicsSimpleTextItem(f"{index + 1}. {geo.node_title(step)}", self)
        title.setBrush(QBrush(QColor(COLOR["text_strong"])))
        title.setPos(8, 6)
        detail = QGraphicsSimpleTextItem(self._fit(geo.node_detail(step)), self)
        detail.setBrush(QBrush(QColor(COLOR["dim"])))
        detail.setPos(8, 28)
        badge = QGraphicsSimpleTextItem(risk + (" - asks first" if asks else ""), self)
        badge.setBrush(QBrush(colour))
        badge.setPos(8, geo.NODE_H - 22)
        self._home = QPointF(*geo.position(index))
        self.setPos(self._home)

    @staticmethod
    def _fit(text: str) -> str:
        return text if len(text) <= 26 else text[:25] + "…"

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = self.isSelected()
        painter.setPen(QPen(QColor(COLOR["accent"]), 3) if selected else self.pen())
        painter.setBrush(self.brush())
        painter.drawRoundedRect(self.rect(), 8, 8)

    def mouseReleaseEvent(self, event) -> None:       # noqa: N802 (Qt API)
        super().mouseReleaseEvent(event)
        centre = self.pos() + QPointF(geo.NODE_W / 2, geo.NODE_H / 2)
        target = geo.slot_for(centre.x(), centre.y(), self.canvas.count)
        self.setPos(self._home)                        # snap back; the studio redraws if the order really changed
        if target != self.index:
            self.canvas.moved.emit(self.index, target)


class WorkflowCanvas(QGraphicsView):
    selected = Signal(int)           # a node was chosen (index)
    moved = Signal(int, int)         # a node was dropped on another slot (from, to)
    nudged = Signal(int, str)        # Ctrl+Left/Right: (index, "up" | "down")
    removed = Signal(int)            # Delete

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(QBrush(QColor(COLOR["bg"])))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAccessibleName("Workflow canvas (the Steps table has the same content)")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.nodes: list[NodeItem] = []
        self.count = 0
        self.current = -1
        self._zoomed = False
        self._scene.selectionChanged.connect(self._selection_changed)

    def show_steps(self, steps: list[Step], reports: list, select: int = -1) -> None:
        self._scene.blockSignals(True)
        self._scene.clear()
        self.nodes = []
        self.count = len(steps)
        for i, step in enumerate(steps):
            report = reports[i] if i < len(reports) else None
            node = NodeItem(i, step, report.risk if report else "safe", bool(report and report.asks), self)
            self._scene.addItem(node)
            self.nodes.append(node)
        for i in range(1, len(steps)):
            self._arrow(i - 1, i, geo.edge_label(steps[i]))
        width, height = geo.scene_size(len(steps))
        self._scene.setSceneRect(QRectF(-20, -20, width + 40, height + 40))
        self._scene.blockSignals(False)
        self.select(select)

    def _arrow(self, a: int, b: int, label: str) -> None:
        ax, ay = geo.position(a)
        bx, by = geo.position(b)
        path = QPainterPath()
        if by == ay:                                    # same row: straight to the left edge of the next box
            start, end = QPointF(ax + geo.NODE_W, ay + geo.NODE_H / 2), QPointF(bx, by + geo.NODE_H / 2)
            path.moveTo(start)
            path.lineTo(end)
            head = [QPointF(end.x() - 9, end.y() - 5), end, QPointF(end.x() - 9, end.y() + 5)]
            mid = QPointF((start.x() + end.x()) / 2 - 18, start.y() - 18)
        else:                                           # wrapped: down from the end of one row to the start of the next
            start, end = QPointF(ax + geo.NODE_W / 2, ay + geo.NODE_H), QPointF(bx + geo.NODE_W / 2, by)
            mid_y = ay + geo.NODE_H + geo.GAP_Y / 2
            path.moveTo(start)
            path.lineTo(start.x(), mid_y)
            path.lineTo(end.x(), mid_y)
            path.lineTo(end)
            head = [QPointF(end.x() - 5, end.y() - 9), end, QPointF(end.x() + 5, end.y() - 9)]
            mid = QPointF(min(start.x(), end.x()) + 6, mid_y - 18)
        line = QGraphicsPathItem(path)
        line.setPen(QPen(QColor(COLOR["panel_edge"]), 2))
        line.setZValue(1)
        self._scene.addItem(line)
        tip = QPainterPath()
        tip.moveTo(head[0])
        tip.lineTo(head[1])
        tip.lineTo(head[2])
        arrow_head = QGraphicsPathItem(tip)
        arrow_head.setPen(QPen(QColor(COLOR["accent"]), 2))
        arrow_head.setZValue(1)
        self._scene.addItem(arrow_head)
        text = QGraphicsSimpleTextItem(label)
        text.setBrush(QBrush(QColor(COLOR["gold"] if label != "then" else COLOR["dim"])))
        text.setPos(mid)
        self._scene.addItem(text)

    def select(self, index: int) -> None:
        self._scene.blockSignals(True)
        self._scene.clearSelection()
        if 0 <= index < len(self.nodes):
            self.nodes[index].setSelected(True)
        self.current = index if 0 <= index < len(self.nodes) else -1
        self._scene.blockSignals(False)
        self._scene.update()

    def _selection_changed(self) -> None:
        picked = [n.index for n in self.nodes if n.isSelected()]
        if picked:
            self.current = picked[0]
            self.selected.emit(self.current)

    def resizeEvent(self, event) -> None:            # noqa: N802 (Qt API)
        if event is not None:
            super().resizeEvent(event)
        if not self._zoomed:
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            if self.transform().m11() > 1.0:           # never blow a small workflow up past 100 %
                self.resetTransform()

    def wheelEvent(self, event) -> None:             # noqa: N802 (Qt API)
        """Ctrl + wheel zooms (10 % to 300 %); the plain wheel scrolls."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            now = self.transform().m11() * factor
            if 0.1 <= now <= 3.0:
                self.scale(factor, factor)
                self._zoomed = True
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:   # noqa: N802 (Qt API)
        key, ctrl = event.key(), bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_0 and ctrl:               # Ctrl+0: fit to the window again
            self._zoomed = False
            self.resizeEvent(None)
            return
        if not self.nodes:
            return super().keyPressEvent(event)
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if ctrl and self.current >= 0:
                self.nudged.emit(self.current, "up" if key == Qt.Key.Key_Left else "down")
            else:
                step = -1 if key == Qt.Key.Key_Left else 1
                target = min(max(self.current + step, 0), len(self.nodes) - 1)
                self.select(target)
                self.selected.emit(target)
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            target = min(max(self.current + (-geo.PER_ROW if key == Qt.Key.Key_Up else geo.PER_ROW), 0), len(self.nodes) - 1)
            self.select(target)
            self.selected.emit(target)
            return
        if key == Qt.Key.Key_Delete and self.current >= 0:
            self.removed.emit(self.current)
            return
        super().keyPressEvent(event)
