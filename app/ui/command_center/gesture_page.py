"""Gesture Control page: six live panels (camera, hand tracking, virtual mouse, mappings, performance - measured only - and safety), a skeleton
preview (landmarks only: the camera image itself is never drawn or kept), a calibration wizard and a no-control tutorial.
All decisions live in app/gesture/dashboard.py (unit-tested); this file only draws them. Not exercised by automated tests (needs Qt + a display)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.gesture import dashboard as dash
from app.gesture.service import SERVICE


class SkeletonPreview(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAccessibleName("Hand skeleton preview")
        self.hands: list = []
        self.area: tuple[float, float, float, float] | None = None
        self.caption = "Preview is off. Start practice mode to see your hand."

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 24, 30))
        w, h = self.width(), self.height()
        if self.area:                       # the calibrated active area (mirrored like the preview)
            left, top, right, bottom = self.area
            p.setPen(QPen(QColor(90, 160, 255), 1, Qt.DashLine))
            p.drawRect(int((1 - right) * w), int(top * h), int((right - left) * w), int((bottom - top) * h))
        points = []
        for n, (pts, bones) in enumerate(dash.skeletons(self.hands, w, h)):
            p.setPen(QPen(QColor(120, 230, 160) if n == 0 else QColor(255, 190, 90), 2))      # green = pointer hand, amber = second hand
            for a, b2 in bones:
                p.drawLine(QPointF(*a), QPointF(*b2))
            p.setPen(QPen(QColor(255, 255, 255), 5))
            for x, y in pts:
                p.drawPoint(x, y)
            points += pts
        if not points:
            p.setPen(QColor(170, 175, 185))
            p.drawText(self.rect().adjusted(8, 8, -8, -8), Qt.AlignCenter | Qt.TextWordWrap, self.caption)


BADGES = {"INDEX_POINT": "☝  Pointing - move your fingertip", "PINCH": "🤏  Pinch - click", "OPEN_PALM": "✋  Open palm - hold to pause",
          "TWO_FINGER_SCROLL": "✌  Scrolling - move up / down", "FIST": "✊  Fist", "NONE": "No hand seen", "UNKNOWN": "Hand seen - try a gesture"}
HINTS = {"DISABLED": "Press Start to control the mouse with your hand, or Practice to try it without clicking anything.",
         "INITIALIZING": "Starting the camera and the hand detector...", "READY": "Almost ready...",
         "ACTIVE": "Control is ON. Point with your index finger to move, pinch to click, two fingers up/down to scroll, open palm to pause.",
         "PAUSED": "Paused. Press Resume (or Start) to continue.", "TRACKING_LOST": "I lost your hand. Bring it back into the camera view.",
         "ERROR": "Something went wrong - see the details below or press Check setup.", "SHUTTING_DOWN": "Stopping..."}


class GesturePage(QWidget):
    frame_in = Signal(object, object)

    def __init__(self, run_tool: Callable[[str, dict, Callable[[str], None]], None], settings_provider: Callable[[], object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.run_tool, self.settings_provider = run_tool, settings_provider
        self.wizard: dash.CalibrationWizard | None = None
        self.tutorial: dash.Tutorial | None = None
        title = QLabel("Gesture control")
        title.setObjectName("h1")
        self.status = QLabel("")
        self.status.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.status.setAccessibleName("Gesture status")
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setAccessibleName("What to do now")
        self.badge = QLabel("")
        self.badge.setStyleSheet("font-size: 15px;")
        self.badge.setAccessibleName("Current gesture")
        self.preview = SkeletonPreview()
        self.preview.setMinimumSize(480, 360)
        self.coach = QLabel("")
        self.coach.setWordWrap(True)
        self.coach.setStyleSheet("font-size: 14px;")
        self.coach.setAccessibleName("Gesture coach")
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setAccessibleName("Gesture message")
        self.boxes: dict[str, QLabel] = {}
        self.details = QWidget()
        grid = QGridLayout(self.details)
        for i, name in enumerate(("Camera", "Hand tracking", "Virtual mouse", "Mappings", "Performance", "Safety")):
            box, label = QGroupBox(name), QLabel("")
            label.setWordWrap(True)
            label.setAccessibleName(name)
            inner = QVBoxLayout(box)
            inner.addWidget(label)
            self.boxes[name] = label
            grid.addWidget(box, i // 3, i % 3)
        self.details.setVisible(False)

        def make(label: str, tip: str, slot, name: str = "") -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setAccessibleName(name or label)
            b.setMinimumHeight(34)
            b.clicked.connect(lambda _=False, s=slot: s())
            return b

        self.start = make("▶  Start", "Control the mouse with your hand (asks first)", lambda: self._start())
        self.stop = make("■  Stop", "Release the mouse, turn gesture control off and let go of the camera", self._stop, "Stop gesture control")
        self.stop.setObjectName("danger")
        self.toggle = make("Pause", "Pause or resume control (an open palm held for a second also pauses)", self._toggle_pause)
        main = QHBoxLayout()
        for b in (self.start, self.toggle, self.stop):
            main.addWidget(b)
        main.addStretch(1)
        learn = QHBoxLayout()
        for label, tip, slot in (("Practice", "See your gestures without clicking anything", self._practice),
                                 ("Tutorial", "Learn each gesture step by step (nothing is clicked)", self._tutorial),
                                 ("Calibrate", "Choose which part of the camera view covers your screen", self._calibrate),
                                 ("Check setup", "Is mediapipe, the model and the camera ready?", lambda: self._tool({"action": "diagnostic"})),
                                 ("Show details", "Camera, tracking, mouse, performance and safety panels", self._toggle_details)):
            b = make(label, tip, slot)
            if label == "Show details":
                self.details_button = b
            learn.addWidget(b)
        learn.addStretch(1)
        from app.gesture import profiles as _profiles

        self.profile = QComboBox()
        for _name, _p in _profiles.PROFILES.items():
            self.profile.addItem(_p.label, _name)
        self.profile.setToolTip("A ready-made set of options for what you are doing. Picking one changes the options on this page; "
                                "it never turns on anything that runs a JARVIS action.")
        self.profile.setAccessibleName("Gesture profile")
        self.profile.activated.connect(lambda _=0: self._pick_profile())
        self.profile_note = QLabel("")
        self.profile_note.setWordWrap(True)
        self.reach = QComboBox()
        self.reach.addItems(["small", "medium", "large"])
        self.reach.setCurrentText("medium")
        self.reach.setToolTip("How far your hand travels to cross the screen: small = little movement, large = big movement")
        self.reach.setAccessibleName("Hand movement")
        self.reach.activated.connect(lambda _=0: self._tool({"action": "settings", "setting": "reach", "value": self.reach.currentText()}))
        self.smooth = QComboBox()
        self.smooth.addItems(["smoother", "balanced", "quicker"])
        self.smooth.setCurrentText("balanced")
        self.smooth.setToolTip("Smoother = steadier pointer, a little slower. Quicker = faster response, a little more shake")
        self.smooth.setAccessibleName("Smoothness")
        self.smooth.activated.connect(lambda _=0: self._tool({"action": "settings", "setting": "smooth", "value": self.smooth.currentText()}))
        self.drag = QCheckBox("Drag (hold a pinch)")
        self.drag.setToolTip("Holding a pinch for half a second grabs; letting go drops")
        self.drag.toggled.connect(lambda on: self._tool({"action": "settings", "setting": "drag", "value": "on" if on else "off"}))
        self.right = QCheckBox("Right click (thumb + middle finger)")
        self.right.toggled.connect(lambda on: self._tool({"action": "settings", "setting": "right_click", "value": "on" if on else "off"}))
        self.swipe = QComboBox()
        self.swipe.addItems(["off", "browser", "desktops", "slides"])
        self.swipe.setToolTip("What a quick sideways swipe of an open palm does: browser = back / forward, desktops = next / previous desktop, slides = arrow keys")
        self.swipe.setAccessibleName("Swipe action")
        self.swipe.activated.connect(lambda _=0: self._tool({"action": "settings", "setting": "swipe", "value": self.swipe.currentText()}))
        self.zoom = QCheckBox("Zoom (pinch with both hands)")
        self.zoom.setChecked(True)
        self.zoom.toggled.connect(lambda on: self._tool({"action": "settings", "setting": "zoom", "value": "on" if on else "off"}))
        self.dwell = QCheckBox("Dwell click (hold still)")
        self.dwell.setToolTip("Hold your fingertip still for about a second to click, without pinching")
        self.dwell.toggled.connect(lambda on: self._tool({"action": "settings", "setting": "dwell_click", "value": "on" if on else "off"}))
        options = QHBoxLayout()
        for label, widget in (("Profile:", self.profile), ("Hand movement:", self.reach), ("Smoothness:", self.smooth), ("Swipe:", self.swipe)):
            options.addWidget(QLabel(label))
            options.addWidget(widget)
        options.addStretch(1)
        more = QHBoxLayout()
        more.addWidget(self.profile_note)
        for box in (self.drag, self.right, self.zoom, self.dwell):
            more.addWidget(box)
        more.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.preview, 3)
        side = QVBoxLayout()
        side.addWidget(self.badge)
        side.addWidget(self.hint)
        side.addWidget(self.coach)
        side.addWidget(self.message)
        side.addStretch(1)
        top.addLayout(side, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self.status)
        layout.addLayout(main)
        layout.addLayout(top, 1)
        layout.addLayout(options)
        layout.addLayout(more)
        layout.addLayout(learn)
        layout.addWidget(self.details)
        self.frame_in.connect(self._on_frame)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self._hook()
        self.refresh()

    # ----------------------------------------------------------------- actions
    def _say(self, text: str) -> None:
        self.message.setText(text)

    def _tool(self, args: dict) -> None:
        self.run_tool("gesture_control", args, self._say)

    def _start(self) -> None:
        self.wizard = self.tutorial = None
        self._hook()
        self._tool({"action": "enable"})

    def _stop(self) -> None:
        SERVICE.emergency_stop()
        SERVICE.disable("stopped with the Stop button")
        self.wizard = self.tutorial = None
        self.coach.setText("")
        self._say("Gesture control is off. The mouse is released and the camera is free.")

    def _toggle_pause(self) -> None:
        self._tool({"action": "resume" if SERVICE.state == "PAUSED" else "pause"})

    def _toggle_details(self) -> None:
        show = not self.details.isVisible()
        self.details.setVisible(show)
        self.details_button.setText("Hide details" if show else "Show details")

    def _hook(self) -> None:
        SERVICE.on_frame = lambda hand, frame: self.frame_in.emit(hand, frame)   # worker thread: the signal hops to the UI thread

    def _practice(self) -> None:
        self.wizard = self.tutorial = None
        self._hook()
        self._tool({"action": "practice"})

    def _tutorial(self) -> None:
        self.tutorial = dash.Tutorial()
        self.wizard = None
        self._hook()
        self._tool({"action": "practice"})
        self.coach.setText(self.tutorial.prompt)

    def _calibrate(self) -> None:
        self.wizard = dash.CalibrationWizard()
        self.tutorial = None
        self._hook()
        self._tool({"action": "practice"})
        self.coach.setText(self.wizard.message)

    def _on_frame(self, hand, frame) -> None:
        import time

        now = time.monotonic()
        self.preview.hands = list(getattr(frame, "hands", None) or ([hand] if hand is not None else []))
        self.preview.caption = "No hand seen. Hold your hand up in front of the camera."
        self.preview.area = tuple(SERVICE.settings.calibration)
        badge = BADGES.get(frame.gesture, frame.gesture)
        if any(e[0] == "click" for e in frame.events):
            badge = "✔  Click!"
        elif any(e[0] == "right_click" for e in frame.events):
            badge = "✔  Right click!"
        elif any(e[0] == "zoom" for e in frame.events):
            badge = "🔍  Zoom in" if sum(e[1] for e in frame.events if e[0] == "zoom") > 0 else "🔍  Zoom out"
        elif any(e[0] == "swipe" for e in frame.events):
            badge = "↔  Swipe " + next(e[1] for e in frame.events if e[0] == "swipe")
        elif len(self.preview.hands) == 2:
            badge += "   (two hands)"
        self.badge.setText(badge)
        if self.wizard is not None:
            self.wizard.feed(hand, now)
            self.coach.setText(f"{self.wizard.message}  ({int(self.wizard.progress(now) * 100)}%)")
            if self.wizard.done and self.wizard.result:
                c = self.wizard.result
                ok = SERVICE.calibrate((c.left, c.top), (c.right, c.bottom))
                self._say("Calibration saved and applied. Press Start to use it." if ok else "That area was refused; try again.")
                self.wizard = None
                self.coach.setText("")
        elif self.tutorial is not None:
            self.tutorial.feed(frame)
            self.coach.setText(self.tutorial.prompt)
        self.preview.update()

    def _pick_profile(self) -> None:
        self._tool({"action": "profile", "profile": self.profile.currentData()})      # the tool runs off-thread; refresh() shows the result

    def _sync_options(self) -> None:
        """Show the service's real settings (a profile, a voice command or the app may have changed them)."""
        st = SERVICE.settings
        pairs = ((self.reach, st.reach if st.reach in ("small", "medium", "large") else None), (self.smooth, st.smooth), (self.swipe, st.swipe))
        for combo, value in pairs:
            if value is not None and combo.currentText() != value:
                combo.blockSignals(True)
                combo.setCurrentText(value)
                combo.blockSignals(False)
        for box, value in ((self.drag, st.drag), (self.right, st.right_click), (self.zoom, st.zoom), (self.dwell, st.dwell_click)):
            if box.isChecked() != bool(value):
                box.blockSignals(True)
                box.setChecked(bool(value))
                box.blockSignals(False)
        index = self.profile.findData(SERVICE.profile)
        if index >= 0 and self.profile.currentIndex() != index and not self.profile.view().isVisible():
            self.profile.blockSignals(True)
            self.profile.setCurrentIndex(index)
            self.profile.blockSignals(False)
        suggested = SERVICE.suggested
        self.profile_note.setText(f"The app in front suggests the '{suggested}' profile." if suggested else "")

    def refresh(self) -> None:
        from app.gesture.detector import availability

        self._sync_options()

        s = self.settings_provider()
        try:
            ready = availability(s.paths.models_dir) if getattr(s, "paths", None) else (False, "not checked")
        except Exception as exc:
            ready = (False, str(exc))
        allowed = bool(getattr(getattr(s, "settings", s), "gesture_control", False))
        status = SERVICE.status()
        data = dash.panels(status, ready=ready, allowed=allowed, settings=vars(SERVICE.settings))
        for name, lines in data.items():
            self.boxes[name].setText("\n".join(lines))
        state = status["state"]
        word = {"ACTIVE": "ON - controlling the mouse", "PAUSED": "PAUSED", "DISABLED": "OFF", "ERROR": "PROBLEM", "TRACKING_LOST": "ON - hand not visible",
                "INITIALIZING": "Starting...", "READY": "Starting..."}.get(state, state)
        if status["practice"] and status["running"]:
            word = "PRACTICE - nothing is clicked"
        self.status.setText(f"Gesture control: {word}")
        self.hint.setText(HINTS.get(state, "") if not status["problem"] else f"Problem: {status['problem']}")
        self.toggle.setText("Resume" if state == "PAUSED" and not status["practice"] else "Pause")
        self.toggle.setEnabled(status["running"] and not status["practice"])
        self.stop.setEnabled(status["running"])
        self.start.setEnabled(not (status["running"] and not status["practice"]))
        if not SERVICE.running:
            self.preview.hands = []
            self.preview.caption = "Preview is off. Press Start or Practice to see your hand."
            self.badge.setText("")
            self.preview.update()
