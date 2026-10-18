"""Device manager: every device Jarvis knows in one place - this PC and other computers, paired phones, and
smart-home devices on your network - with what each can really do, its last known state, and controls to check,
add, remove or revoke. Nothing here is "controllable" just because it is visible on the network."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

COLUMNS = ("Device", "Kind", "Connection", "Can do", "Last seen / state")


class AddLocalDeviceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.core.local_home import KINDS

        self.setWindowTitle("Add a device on this network")
        self.name = QLineEdit()
        self.name.setPlaceholderText("desk lamp")
        self.kind = QComboBox()
        self.kind.addItems(KINDS)
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.50")
        self.channel = QSpinBox()
        self.channel.setRange(0, 7)
        self.confirm = QCheckBox("Ask before switching it (recommended for plugs: they can power anything)")
        self.confirm.setChecked(True)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Type", self.kind)
        form.addRow("Address (on this network)", self.address)
        form.addRow("Channel", self.channel)
        form.addRow(self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        note = QLabel("Shelly (Gen 1 / Gen 2+), Tasmota and WLED devices without a password. Jarvis talks to them "
                      "directly - nothing goes through the internet.")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def device(self):
        from app.core.local_home import LocalDevice

        return LocalDevice(self.name.text().strip(), self.kind.currentText(), self.address.text().strip(),
                           self.channel.value(), self.confirm.isChecked())


class DeviceManagerDialog(QDialog):
    def __init__(self, controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device manager")
        self.setMinimumSize(940, 540)
        self.controller = controller
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Devices")
        self.info = QLabel()
        self.info.setWordWrap(True)
        buttons = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        for key, text, slot, tip in (
                ("check", "Check now", self.check_selected, "Ask the selected device for its real state"),
                ("add", "Add network device...", self.add_local, "A Shelly / Tasmota / WLED device on your network"),
                ("remove", "Remove", self.remove_selected, "Forget the selected computer or network device"),
                ("revoke", "Revoke phone", self.revoke_selected, "The phone can't control this PC any more"),
                ("phones", "Phone settings...", self.open_phones, "Pairing, permissions and sessions of phones"),
                ("refresh", "Refresh", self.refresh, "")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setAccessibleName(text.rstrip("."))
            button.clicked.connect(lambda _=False, s=slot: s())
            buttons.addWidget(button)
            self.buttons[key] = button
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Each device lists only what it has shown it can do. Phones and computers need your "
                                "pairing; network devices only what you add."))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.info)
        layout.addLayout(buttons)
        self.rows: list[tuple[str, object]] = []      # (section, object) per table row
        self.refresh()

    # ---- sources
    def _ctx(self):
        return self.controller.ctx

    def _computers(self) -> list:
        db = getattr(self._ctx(), "database", None)
        if db is None:
            return []
        from app.core.devices import DeviceRegistry

        try:
            return DeviceRegistry(db).all()
        except Exception:
            return []

    def _phones(self) -> list:
        registry = self.controller.mobile_registry() if hasattr(self.controller, "mobile_registry") else None
        return [d for d in (registry.all() if registry is not None else []) if d.trust != "revoked"]

    def _local(self) -> list:
        paths = getattr(self._ctx(), "paths", None)
        if paths is None:
            return []
        from app.core.local_home import LocalHome

        return LocalHome(paths.config_dir).devices()

    def refresh(self) -> None:
        rows: list[tuple[str, object, tuple[str, ...]]] = []
        for d in self._computers():
            caps = ", ".join(f"{c.name.replace('_', ' ')} ({c.status})" for c in d.capabilities) or "-"
            rows.append(("computer", d, (d.name + (" (this PC)" if d.is_self else ""), d.kind,
                                         "this PC" if d.is_self else (d.address or d.mac or "-"), caps,
                                         (d.last_seen or "never")[:16].replace("T", " ") + f" · {d.last_state}")))
        phone_state = {}
        db = getattr(self._ctx(), "database", None)
        if db is not None:
            from app.remote.phone_link import PhoneLink

            link = PhoneLink(db)
            for p in self._phones():
                try:
                    phone_state[p.id] = link.state(p.id)
                except Exception:
                    phone_state[p.id] = {}
        for p in self._phones():
            state = phone_state.get(p.id, {})
            battery = f" · battery {state['battery']}%" if isinstance(state.get("battery"), int | float) else ""
            control = "PC may control it" if state.get("control") else "PC control off (phone setting)"
            rows.append(("phone", p, (p.name, f"phone ({p.platform})", f"paired · {p.trust}",
                                      ", ".join(p.permissions) + f"; {control}",
                                      (state.get("seen") or p.last_seen or "never")[:16].replace("T", " ") + battery)))
        for d in self._local():
            can = "on / off" + (", brightness" if d.kind == "wled" else "") + ("; asks first" if d.confirm else "")
            rows.append(("local", d, (d.name, d.kind, f"local network · {d.address}", can, "press Check now")))
        t = self.table
        t.setRowCount(len(rows))
        for r, (_, _, values) in enumerate(rows):
            for c, value in enumerate(values):
                t.setItem(r, c, QTableWidgetItem(value))
        self.rows = [(section, obj) for section, obj, _ in rows]
        self.info.setText(f"{sum(1 for s, _ in self.rows if s == 'computer')} computer(s), "
                          f"{sum(1 for s, _ in self.rows if s == 'phone')} phone(s), "
                          f"{sum(1 for s, _ in self.rows if s == 'local')} network device(s).")

    def selected(self) -> tuple[str, object] | tuple[None, None]:
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else (None, None)

    # ---- actions
    def check_selected(self) -> str:
        section, obj = self.selected()
        if section != "local":
            text = "Select a network device to ask it for its state." if section is None else (
                "Computers are checked with 'is my desktop on'; phones report when they check in.")
            self.info.setText(text)
            return text
        from app.core.local_home import LocalDeviceError, read_state

        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            state = read_state(obj)
            text = f"{obj.name}: {'on' if state['on'] else 'off'}" + (
                f", brightness {round(state['brightness'] / 2.55)}%" if state.get("brightness") is not None else "") + \
                " (reported by the device)."
            row = self.table.currentRow()
            self.table.setItem(row, 4, QTableWidgetItem("on" if state["on"] else "off"))
        except LocalDeviceError as exc:
            text = f"{obj.name}: {exc}"
        finally:
            self.unsetCursor()
        self.info.setText(text)
        return text

    def add_local(self, device=None) -> bool:
        from app.core.local_home import LocalDeviceError, LocalHome

        if device is None:
            dialog = AddLocalDeviceDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            device = dialog.device()
        try:
            LocalHome(self._ctx().paths.config_dir).add(device)
        except LocalDeviceError as exc:
            QMessageBox.warning(self, "Device manager", str(exc))
            return False
        self.refresh()
        return True

    def _ask(self, text: str) -> bool:
        return QMessageBox.question(self, "Device manager", text) == QMessageBox.StandardButton.Yes

    def remove_selected(self, confirm: bool = True) -> bool:
        section, obj = self.selected()
        if section == "local":
            if confirm and not self._ask(f"Forget {obj.name}? (The device itself isn't changed.)"):
                return False
            from app.core.local_home import LocalHome

            LocalHome(self._ctx().paths.config_dir).remove(obj.name)
        elif section == "computer" and not obj.is_self:
            if confirm and not self._ask(f"Forget the computer {obj.name}?"):
                return False
            from app.core.devices import DeviceRegistry

            DeviceRegistry(self._ctx().database).remove(obj)
        else:
            self.info.setText("Select a network device or another computer to remove (phones: Revoke).")
            return False
        self.refresh()
        return True

    def revoke_selected(self, confirm: bool = True) -> bool:
        section, obj = self.selected()
        if section != "phone":
            self.info.setText("Select a phone to revoke.")
            return False
        if confirm and not self._ask(f"Revoke {obj.name}? It can't control this PC until you pair it again."):
            return False
        self.controller.mobile_registry().set_trust(obj.id, "revoked")
        self.refresh()
        return True

    def open_phones(self) -> None:
        window = self.parent()
        if window is not None and hasattr(window, "open_mobile_devices"):
            window.open_mobile_devices()
            self.refresh()
