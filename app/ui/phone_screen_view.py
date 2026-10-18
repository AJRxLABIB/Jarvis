"""The PC's window onto a phone that shares its screen (JARVIS Mobile > Control > Share screen).

Click = tap, hold = long press, drag = swipe; Back / Home / Recents buttons; type text into the focused field.
Input is only carried out when the phone has switched control on (its accessibility service + "Allow the PC to
tap"); otherwise this is view-only and says so. Stop ends sharing on the phone. Frames are never saved.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

LONG_PRESS = 0.6          # seconds held = long press
SWIPE_MIN = 0.03          # fraction of the screen moved = a swipe, not a tap


def classify(start: tuple[float, float], end: tuple[float, float], held: float) -> dict:
    """What a mouse gesture on the picture means (coordinates 0..1)."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    if (dx * dx + dy * dy) ** 0.5 >= SWIPE_MIN:
        return {"kind": "swipe", "x": start[0], "y": start[1], "x2": end[0], "y2": end[1],
                "ms": int(max(120, min(1500, held * 1000)))}
    return {"kind": "tap", "x": start[0], "y": start[1], "long": held >= LONG_PRESS}


class ScreenCanvas(QWidget):
    gesture = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(270, 480)
        self.image: QImage | None = None
        self._press: tuple[tuple[float, float], float] | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Phone screen")

    def set_jpeg(self, data: bytes) -> None:
        image = QImage.fromData(data, "JPEG")
        if not image.isNull():
            self.image = image
            self.update()

    def picture_rect(self) -> QRectF:
        if self.image is None or self.image.width() == 0:
            return QRectF()
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def to_phone(self, point: QPointF) -> tuple[float, float] | None:
        rect = self.picture_rect()
        if rect.isEmpty() or not rect.contains(point):
            return None
        return ((point.x() - rect.x()) / rect.width(), (point.y() - rect.y()) / rect.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(self.picture_rect(), self.image)
        else:
            p.setPen(Qt.GlobalColor.gray)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No picture yet")
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        spot = self.to_phone(event.position())
        self._press = (spot, time.monotonic()) if spot is not None else None

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press is None:
            return
        start, at = self._press
        self._press = None
        end = self.to_phone(event.position()) or start
        self.gesture.emit(classify(start, end, time.monotonic() - at))


class PhoneScreenWindow(QDialog):
    def __init__(self, hub, parent=None, ask_to_share=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phone screen")
        self.hub = hub
        self._shown_at = 0.0
        self.devices = QComboBox()
        self.devices.setAccessibleName("Sharing phone")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.canvas = ScreenCanvas()
        self.canvas.gesture.connect(self._gesture)
        buttons = QHBoxLayout()
        for label, kind in (("Back", "back"), ("Home", "home"), ("Recents", "recents")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=kind: self._send(k))
            buttons.addWidget(b)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Type into the phone's focused field, Enter to send")
        self.text.returnPressed.connect(self._type)
        self.ask_to_share = ask_to_share         # callable() -> str: the phone is asked to start sharing (it still asks you)
        ask = QPushButton("Ask the phone to share")
        ask.setToolTip("Your phone shows Android's 'Start now?' - you tap it there. Needs JARVIS Mobile 1.2")
        ask.clicked.connect(self.request_share)
        ask.setEnabled(ask_to_share is not None)
        stop = QPushButton("Stop sharing")
        stop.clicked.connect(self.stop_sharing)
        top = QHBoxLayout()
        top.addWidget(self.devices, 1)
        top.addWidget(ask)
        top.addWidget(stop)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.text)
        self.resize(420, 820)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(120)
        self.refresh()

    def current(self) -> str:
        return self.devices.currentData() or ""

    def refresh(self) -> None:
        active = self.hub.active()
        ids = [s.device_id for s in active]
        if ids != [self.devices.itemData(i) for i in range(self.devices.count())]:
            keep = self.current()
            self.devices.clear()
            for s in active:
                self.devices.addItem(s.name, s.device_id)
            if keep in ids:
                self.devices.setCurrentIndex(ids.index(keep))
        share = self.hub.latest(self.current()) if self.current() else None
        if share is None or not share.frame:
            if getattr(self, "_asked", False):
                return
            self.status.setText("Waiting for a phone. On the phone: JARVIS Mobile > Control > Share screen with PC "
                                "(Android asks you each time), or press 'Ask the phone to share'. No such button on "
                                "the phone? Its app is older than 1.2: rebuild and install it (packaging\\"
                                "build_android.ps1, then MENU > Mobile devices > Install JARVIS Mobile).")
            self.canvas.image = None
            self.canvas.update()
            return
        if share.at != self._shown_at:
            self._shown_at = share.at
            self.canvas.set_jpeg(share.frame)
        self.status.setText(f"{share.name}: sharing. " + ("Click to tap, hold for a long press, drag to swipe."
                            if share.control else "View only - to tap from here, switch on 'JARVIS control' in the "
                            "phone's Accessibility settings and 'Allow the PC to tap' in the app."))

    def _send(self, kind: str, **values) -> bool:
        device = self.current()
        if not device:
            return False
        ok = self.hub.send_input(device, kind, **values)
        if not ok:
            self.status.setText("The phone hasn't allowed control - view only.")
        return ok

    def _gesture(self, gesture: dict) -> None:
        kind = gesture.pop("kind")
        self._send(kind, **gesture)

    def _type(self) -> None:
        text = self.text.text()
        if text and self._send("text", text=text):
            self.text.clear()

    def request_share(self) -> None:
        if self.ask_to_share is None:
            return
        try:
            self.status.setText(self.ask_to_share())
        except Exception as exc:
            self.status.setText(f"Couldn't ask the phone: {exc}")
        self._asked = True

    def stop_sharing(self) -> None:
        if self.current():
            self.hub.stop(self.current())
        self.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)
