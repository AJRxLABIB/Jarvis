"""Draws the app icon (an original glowing orb, same as the tray icon) into packaging/jarvis.ico.

No image files are needed: run `python packaging/make_icon.py` (build.ps1 does it for you).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)
CORE = (56, 189, 248)  # the orb's idle colour


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def draw(size: int = 256) -> Image.Image:
    scale = 4  # draw large, then shrink: smooth edges
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([big * 0.04, big * 0.04, big * 0.96, big * 0.96], fill=(*CORE, 110))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(big * 0.04)))
    orb = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw_orb = ImageDraw.Draw(orb)
    steps = 64
    inner, outer = (255, 255, 255), (8, 47, 73)
    for i in range(steps):
        t = i / (steps - 1)
        radius = big * 0.42 * (1 - t) + big * 0.02
        cx, cy = big * (0.5 - 0.08 * t), big * (0.5 - 0.08 * t)  # highlight towards the top left
        colour = _mix(outer, CORE, min(1.0, t * 1.6)) if t < 0.62 else _mix(CORE, inner, (t - 0.62) / 0.38)
        draw_orb.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(*colour, 255))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse([big * 0.08, big * 0.08, big * 0.92, big * 0.92], fill=255)
    image.paste(orb, (0, 0), mask)
    ring = ImageDraw.Draw(image)
    ring.ellipse([big * 0.08, big * 0.08, big * 0.92, big * 0.92], outline=(*CORE, 255), width=max(2, big // 64))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main(out: Path = HERE / "jarvis.ico") -> Path:
    draw(256).save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    draw(256).save(out.with_suffix(".png"))
    return out


if __name__ == "__main__":
    print(main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "jarvis.ico"))
