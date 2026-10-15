"""Mobile devices: pair JARVIS Mobile (QR, 5 minutes), trust / restrict / revoke phones, their permissions and
security events. Everything here happens only in the window - a phone or a voice can't trust itself."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from app.remote.mobile import LABELS, PERMISSIONS


def device_line(device) -> str:
    seen = (device.last_seen or "never")[:16].replace("T", " ")
    return f"{device.name}  [{device.trust}]  {device.platform} {device.os_version}  last seen {seen}"


def describe(device) -> str:
    key = ("hardware-backed" if device.key_info.get("hardware") else "software") + \
        (", needs fingerprint/PIN to use" if device.key_info.get("user_auth") else "")
    auth = (f"protected key set ({device.auth_fingerprint})" if device.auth_public_key
            else "no protected key yet (phone must finish setup before it can control the PC)")
    return (f"Name: {device.name}\nTrust: {device.trust}\nFingerprint: {device.fingerprint}\n"
            f"Key: {key} (as reported by the app)\nProtected key: {auth}\nApp: {device.app_version or '?'}\n"
            f"Paired: {device.paired_at[:16].replace('T', ' ')}\n"
            + (f"Revoked: {device.revoked_at[:16].replace('T', ' ')}\n" if device.revoked_at else ""))


class MobileDevicesDialog(QDialog):
    def __init__(self, registry, pairing_link, parent: QWidget | None = None, sessions=None, app_info=None,
                 apk_link: str | None = None, install_qr: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mobile devices")
        self.setMinimumSize(760, 640)
        self.registry, self.pairing_link = registry, pairing_link
        self.sessions, self.app_info, self.apk_link, self.install_qr = sessions, app_info, apk_link, install_qr
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self.show_selected())
        self.details = QLabel()
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setWordWrap(True)
        self.checks: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        for i, permission in enumerate(PERMISSIONS):
            box = QCheckBox(f"{permission}: {LABELS[permission]}")
            if permission in ("files", "admin"):
                box.setEnabled(False)  # not built yet - never shown as working
            box.toggled.connect(lambda _on, p=permission: self.permission_changed(p))
            self.checks[permission] = box
            grid.addWidget(box, i // 2, i % 2)
        buttons = QHBoxLayout()
        for label, action in (("Trust", lambda: self.trust("trusted")),
                              ("Restrict (read only)", lambda: self.trust("restricted")),
                              ("Revoke", self.revoke), ("Rename", self.rename)):
            button = QPushButton(label)
            button.clicked.connect(action)
            buttons.addWidget(button)
        self.pairing = QLabel()
        self.pairing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.events = QListWidget()
        self.events.setMinimumHeight(80)

        # everything scrolls, so the important buttons are always reachable on any screen size
        inner = QWidget()
        content = QVBoxLayout(inner)
        content.addWidget(QLabel("Phones with JARVIS Mobile. Each has its own key; the private key never leaves the "
                                 "phone. A new phone can do nothing until you press Trust here."))
        content.addWidget(self.list)
        content.addWidget(self.details)
        content.addLayout(grid)
        self.sessions_list = QListWidget()
        self.sessions_list.setMaximumHeight(90)
        end_session = QPushButton("End selected session")
        end_session.clicked.connect(self.end_session)
        content.addWidget(QLabel("Active remote-control sessions"))
        content.addWidget(self.sessions_list)
        content.addWidget(end_session, alignment=Qt.AlignmentFlag.AlignRight)
        content.addWidget(self._install_box())
        content.addWidget(self.pairing)
        content.addWidget(QLabel("Security events"))
        content.addWidget(self.events)
        self.list.setMinimumHeight(90)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        # a prominent, always-visible Pair button at the very top, plus the per-device actions
        pair_now = QPushButton("Pair a new phone")
        pair_now.setMinimumHeight(40)
        pair_now.clicked.connect(self.pair)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addLayout(buttons)
        bottom.addStretch(1)
        bottom.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(pair_now)
        layout.addWidget(scroll, 1)
        layout.addLayout(bottom)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._updating = False
        self.refresh()

    def _install_box(self) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        info = self.app_info or {}
        if not info.get("available"):
            col.addWidget(QLabel(info.get("message", "The JARVIS Mobile app isn't built yet. Run "
                                 "packaging\\build_android.ps1 on the PC, then rebuild Jarvis.")))
            return box
        col.addWidget(QLabel(f"Install JARVIS Mobile  -  version {info.get('version', '?')}  -  "
                             f"{round(info.get('size', 0) / 1024)} KB"))
        sha = QLabel("SHA-256: " + info.get("sha256", "?"))
        sha.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sha.setWordWrap(True)
        col.addWidget(sha)
        if self.apk_link:
            link = QLabel(f'On the phone open <a href="{self.apk_link}">{self.apk_link}</a> to download the APK, '
                          "then allow install from this source.")
            link.setOpenExternalLinks(True)
            link.setWordWrap(True)
            col.addWidget(link)
            if self.install_qr:
                from app.ui.phone_setup import qr_pixmap

                pixmap = qr_pixmap(self.apk_link, 200)
                if pixmap is not None:
                    label = QLabel()
                    label.setPixmap(pixmap)
                    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    col.addWidget(label)
        return box

    def end_session(self) -> None:
        item = self.sessions_list.currentItem()
        if item is None or self.sessions is None:
            return
        self.sessions.end(item.data(256), "ended from the PC")
        self.refresh()

    def refresh(self) -> None:
        current = self.selected()
        self.list.clear()
        for device in self.registry.all():
            item = QListWidgetItem(device_line(device))
            item.setData(256, device.id)
            self.list.addItem(item)
            if current is not None and device.id == current.id:
                self.list.setCurrentItem(item)
        if getattr(self, "sessions_list", None) is not None and self.sessions is not None:
            self.sessions_list.clear()
            for session in self.sessions.active():
                dev = self.registry.get(session.device_id)
                line = (f"{(dev.name if dev else session.device_id)}  {session.network}  since "
                        f"{session.started_at[11:16]}  ({', '.join(session.scopes)})")
                item = QListWidgetItem(line)
                item.setData(256, session.id)
                self.sessions_list.addItem(item)
        self.events.clear()
        for event in self.registry.security_events(40):
            self.events.addItem(f"{event['at'][:16].replace('T', ' ')}  {event['kind']}  {event['detail']}")
        self.show_selected()

    def selected(self):
        item = self.list.currentItem() if hasattr(self, "list") else None
        return self.registry.get(item.data(256)) if item is not None else None

    def show_selected(self) -> None:
        device = self.selected()
        self._updating = True
        self.details.setText(describe(device) if device else "No phone selected.")
        for permission, box in self.checks.items():
            box.setChecked(bool(device) and permission in device.permissions)
            if permission not in ("files", "admin"):
                box.setEnabled(bool(device) and device.trust in ("trusted", "restricted"))
        self._updating = False

    def permission_changed(self, permission: str) -> None:
        device = self.selected()
        if self._updating or device is None:
            return
        chosen = [p for p, box in self.checks.items() if box.isChecked()]
        self.registry.set_permissions(device.id, chosen)
        self.refresh()

    def trust(self, state: str) -> None:
        device = self.selected()
        if device is None:
            return
        if state == "trusted":
            answer = QMessageBox.question(self, "Trust phone", f"Trust {device.name}?\n\nFingerprint:\n"
                                          f"{device.fingerprint}\n\nCheck the JARVIS Mobile app shows the SAME "
                                          "fingerprint.")
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.registry.set_trust(device.id, state)
        except ValueError as exc:
            QMessageBox.information(self, "Mobile devices", str(exc))
        self.refresh()

    def revoke(self) -> None:
        device = self.selected()
        if device is None:
            return
        if QMessageBox.question(self, "Revoke", f"Revoke {device.name}? It can't do anything any more (every "
                                "request it sends is refused); to use it again it must pair again.") \
                == QMessageBox.StandardButton.Yes:
            self.registry.set_trust(device.id, "revoked")
            if self.sessions is not None:
                self.sessions.end_device(device.id, "revoked")
            self.refresh()

    def rename(self) -> None:
        device = self.selected()
        if device is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=device.name)
        if ok and name.strip():
            self.registry.rename(device.id, name)
            self.refresh()

    def pair(self) -> None:
        from app.ui.phone_setup import qr_pixmap

        link = self.pairing_link()
        if link is None:
            QMessageBox.information(self, "Pair", "Turn on the phone remote first (Settings -> Devices & remote).")
            return
        url, self._left = link
        pixmap = qr_pixmap(url, 240)
        if pixmap is not None:
            self.pairing.setPixmap(pixmap)
        self.pairing.setToolTip(url)
        self._tick()
        self._timer.start(1000)

    def _tick(self) -> None:
        left = self.registry.pairing_left()
        if left <= 0:
            self._timer.stop()
            self.pairing.clear()
            self.pairing.setText("Pairing code expired or used. Press 'Pair a new phone' for a new one.")
            self.refresh()
            return
        self.setWindowTitle(f"Mobile devices - pairing expires in {left // 60:02d}:{left % 60:02d}")
