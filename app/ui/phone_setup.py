"""Phone remote setup: a QR code to scan, the pairing code, and the steps (home Wi-Fi, home screen, away)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


def qr_matrix(text: str) -> list[list[bool]] | None:
    try:
        import qrcode
    except ImportError:
        return None
    code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def qr_pixmap(text: str, size: int = 220) -> QPixmap | None:
    matrix = qr_matrix(text)
    if not matrix:
        return None
    cells = len(matrix)
    scale = max(1, size // cells)
    image = QImage(cells * scale, cells * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(x * scale, y * scale, scale, scale, QColor("black"))
    painter.end()
    return QPixmap.fromImage(image)


STEPS = (
    "1. Phone and PC on the same Wi-Fi. Scan the code with the phone's camera (or type the address).\n"
    "2. The phone warns about the certificate once (it's Jarvis's own): Advanced -> Proceed. Check the fingerprint "
    "below matches.\n"
    "3. Type the pairing code on the page. The phone gets its own key; you won't need the code again.\n"
    "4. Keep it one tap away: Chrome menu (three dots) -> 'Add to Home screen' (iPhone Safari: Share -> 'Add to Home "
    "Screen'). It opens full screen like an app.\n"
    "5. The full Android app (wake AND control): scan the 'JARVIS Mobile app' code to install it (allow 'install "
    "unknown apps' for the browser once). Then pair it from Menu -> Mobile devices -> Pair a new phone (scan THAT "
    "QR with the phone's camera - it opens the app). This web page is the older, lighter remote; the app does more, "
    "including waking the PC when Wake-on-LAN is on.\n"
    "6. Away from home: install Tailscale (free) on the PC and the phone with the same account, turn on 'Phone "
    "remote also over my Tailscale network' in Settings, and use the second code.\n\n"
    "Honest limits: the page works while it's open - a phone stops web pages in the background, so it can't stay "
    "running all the time or wake you with alerts when closed. Jarvis must be running on the PC (Start with Windows "
    "keeps it on). Turning the PC on from the phone needs Wake-on-LAN and another device at home.")


class PhoneSetupDialog(QDialog):
    def __init__(self, url: str, away_url: str | None, code: str, fingerprint: str, paired: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phone remote")
        codes = QHBoxLayout()
        from app.remote import apk

        app_apk = url.rstrip("/") + "/mobile/app.apk" if apk.apk_path() is not None else None
        for label, address in (("At home (same Wi-Fi)", url), ("Away (Tailscale)", away_url),
                               ("JARVIS Mobile app (Android)", app_apk)):
            if not address:
                continue
            column = QVBoxLayout()
            picture = QLabel()
            pixmap = qr_pixmap(address)
            if pixmap is not None:
                picture.setPixmap(pixmap)
            else:
                picture.setText("(install the 'qrcode' package to show a QR code)")
            column.addWidget(QLabel(f"<b>{label}</b>"))
            column.addWidget(picture, alignment=Qt.AlignmentFlag.AlignCenter)
            address_label = QLabel(address)
            address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(address_label)
            codes.addLayout(column)
        facts = QLabel(f"<b>Pairing code: {code}</b> (works once, for 5 minutes) &nbsp; Paired phones: {paired}<br>"
                       + (f"Certificate fingerprint starts with: {fingerprint[:23]}" if fingerprint else
                          "Plain http: turn on 'Phone remote over https' in Settings to talk from the phone."))
        facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        steps = QLabel(STEPS)
        steps.setWordWrap(True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addLayout(codes)
        layout.addWidget(facts)
        layout.addWidget(steps)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.setMinimumWidth(620)
