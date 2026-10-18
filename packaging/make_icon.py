"""Draws the app icon (Jarvis's three-ring logo, the same as the taskbar and the header) into packaging/jarvis.ico.

No image files are needed: run `python packaging/make_icon.py` (build.ps1 does it for you).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIZES = (256, 128, 64, 48, 32, 24, 16)


def main(out: Path = HERE / "jarvis.ico") -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(HERE.parent))
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication

    from app.ui.app_icon import draw_logo

    _app = QApplication.instance() or QApplication([])
    images = []
    for size in SIZES:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        draw_logo(size).save(buffer, "PNG")
        images.append(Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA"))
    images[0].save(out, sizes=[(i.width, i.height) for i in images], append_images=images[1:])
    images[0].save(out.with_suffix(".png"))
    return out


if __name__ == "__main__":
    print(main())
