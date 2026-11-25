"""Screen-grounding trial: can a vision model point at the right control? Measured against ground truth from Windows itself.

    python run.py --ground-trial qwen2.5vl:7b        (any vision model already installed in Ollama; nothing is downloaded)

Method: take the foreground window's named, visible controls from UI Automation (name + rectangle = the ground truth), take one screenshot of that
window, and for each of up to ``MAX_TARGETS`` controls ask the model "where is <name>?" A reply counts as a HIT when the point it gives lies inside
that control's rectangle. Reported: hits / targets, replies that had no usable coordinates, median time per answer.

Honest limits: models answer in different coordinate conventions - pixels of the image, 0-1 fractions and 0-1000 grids are understood, anything else
counts as "no coordinates" and lowers the score through format, not ability. One window is a small sample. The result says nothing about clicking
safely: it only measures pointing. This trial clicks nothing.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_TARGETS = 12
PROMPT = "This is a screenshot of a window, {w} by {h} pixels. Where is the control named \"{name}\"? Answer with the pixel coordinates of its centre as (x, y) and nothing else."


@dataclass
class Target:
    name: str
    rect: tuple[int, int, int, int]          # left, top, right, bottom in SCREENSHOT pixels


@dataclass
class Trial:
    model: str
    targets: int = 0
    hits: int = 0
    hits_refined: int = 0
    refined_tried: bool = False
    no_coordinates: int = 0
    errors: list[str] = field(default_factory=list)
    times_ms: list[int] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        return self.hits / self.targets if self.targets else None


def parse_point(reply: str, w: int, h: int) -> tuple[float, float] | None:
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if len(nums) < 2:
        return None
    if len(nums) >= 4 and re.search(r"box|bbox|\[\s*\d", text):       # a box x1,y1,x2,y2 -> its centre
        x, y = (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    else:
        x, y = nums[0], nums[1]
    if 0 <= x <= 1 and 0 <= y <= 1:                                   # fractions
        return x * w, y * h
    if (x > w or y > h) and x <= 1000 and y <= 1000:                  # a 0-1000 grid
        return x / 1000 * w, y / 1000 * h
    if 0 <= x <= w and 0 <= y <= h:
        return x, y
    return None


ZOOM_SPAN = 0.22        # the zoom crop is this share of the window's width / height around the first guess (at least MIN_CROP px)
MIN_CROP = 240
ZOOM_PROMPT = "This is a zoomed crop of a window, {w} by {h} pixels. Where is the control named \"{name}\"? Answer with the pixel coordinates of its centre as (x, y) and nothing else."


def crop_box(point: tuple[float, float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The crop (left, top, right, bottom) around a first guess: ``ZOOM_SPAN`` of the window, at least ``MIN_CROP`` px, kept inside the image."""
    w, h = size
    cw, ch = min(w, max(MIN_CROP, int(w * ZOOM_SPAN))), min(h, max(MIN_CROP, int(h * ZOOM_SPAN)))
    left = int(min(max(0, point[0] - cw / 2), w - cw))
    top = int(min(max(0, point[1] - ch / 2), h - ch))
    return left, top, left + cw, top + ch


def map_back(point_in_crop: tuple[float, float], box: tuple[int, int, int, int]) -> tuple[float, float]:
    return point_in_crop[0] + box[0], point_in_crop[1] + box[1]


def pick_targets(controls: list[Target], limit: int = MAX_TARGETS) -> list[Target]:
    """Named controls with a real size, each name once (an ambiguous name has no single right answer)."""
    counts: dict[str, int] = {}
    for c in controls:
        counts[c.name.strip().lower()] = counts.get(c.name.strip().lower(), 0) + 1
    ok = [c for c in controls if c.name.strip() and counts[c.name.strip().lower()] == 1 and c.rect[2] - c.rect[0] >= 8 and c.rect[3] - c.rect[1] >= 8]
    return ok[:limit]


def run(model: str, controls: list[Target], size: tuple[int, int], ask: Callable[[str], str], timer: Callable[[], float] = time.perf_counter,
        zoom: Callable[[tuple[int, int, int, int], str], str] | None = None) -> Trial:
    """``zoom(box, prompt)`` asks the model about the CROP; when given, a second answer is measured next to the first (two-step: point, zoom, point again)."""
    trial = Trial(model)
    trial.refined_tried = zoom is not None
    w, h = size
    for t in pick_targets(controls):
        trial.targets += 1
        t0 = timer()
        try:
            reply = ask(PROMPT.format(w=w, h=h, name=t.name))
        except Exception as exc:
            trial.errors.append(f"{t.name}: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        trial.times_ms.append(int((timer() - t0) * 1000))
        point = parse_point(reply, w, h)
        if point is None:
            trial.no_coordinates += 1
            trial.misses.append(f"{t.name}: no coordinates in the reply")
        elif t.rect[0] <= point[0] <= t.rect[2] and t.rect[1] <= point[1] <= t.rect[3]:
            trial.hits += 1
        else:
            trial.misses.append(f"{t.name}: pointed at ({point[0]:.0f}, {point[1]:.0f}), control is at {t.rect}")
        if zoom is not None and point is not None:
            box = crop_box(point, size)
            try:
                second = parse_point(zoom(box, ZOOM_PROMPT.format(w=box[2] - box[0], h=box[3] - box[1], name=t.name)), box[2] - box[0], box[3] - box[1])
            except Exception as exc:
                trial.errors.append(f"{t.name} (zoom): {type(exc).__name__}: {str(exc)[:60]}")
                continue
            if second is not None:
                x, y = map_back(second, box)
                trial.hits_refined += int(t.rect[0] <= x <= t.rect[2] and t.rect[1] <= y <= t.rect[3])
    return trial


def report(t: Trial) -> str:
    if not t.targets:
        return "No usable controls were found in the foreground window (UI Automation gave none with a unique name). Open a normal program window, e.g. Notepad or Settings, and run the trial again."
    lines = [f"Grounding trial - {t.model}", f"  hit: {t.hits} of {t.targets} controls ({t.rate:.0%})",
             f"  replies without usable coordinates: {t.no_coordinates}", f"  errors: {len(t.errors)}"]
    if t.refined_tried:
        lines.insert(2, f"  with zoom (point, crop around it, point again): {t.hits_refined} of {t.targets} controls ({t.hits_refined / t.targets:.0%})")
    if t.times_ms:
        lines.append(f"  median time per answer: {int(statistics.median(t.times_ms))} ms")
    lines += [f"  miss - {m}" for m in t.misses[:6]] + [f"  error - {e}" for e in t.errors[:3]]
    lines.append("  One window is a small sample; this measures pointing only, and nothing was clicked.")
    return "\n".join(lines)


def collect_windows() -> tuple[list[Target], Any, tuple[int, int]]:
    """Real readers (Windows only): the foreground window's controls + one screenshot of it. Returns (targets, PNG bytes, size)."""
    import io

    from PIL import ImageGrab

    from app.tools import winapi
    from app.tools.browser_tabs import _automation
    from app.tools.ui_elements import MAX_ELEMENTS, PRESSABLE, PROP_CONTROL_TYPE, SCOPE_DESCENDANTS

    found = _automation()
    if found is None:
        raise RuntimeError("UI Automation is not available on this PC")
    uia, _ = found
    hwnd = winapi.foreground_window()
    root = uia.ElementFromHandle(hwnd)
    wr = root.CurrentBoundingRectangle
    left, top, right, bottom = int(wr.left), int(wr.top), int(wr.right), int(wr.bottom)
    conds = [uia.CreatePropertyCondition(PROP_CONTROL_TYPE, cid) for cid in PRESSABLE]
    cond = conds[0]
    for extra in conds[1:]:
        cond = uia.CreateOrCondition(cond, extra)
    items = root.FindAll(SCOPE_DESCENDANTS, cond)
    targets = []
    for i in range(min(items.Length, MAX_ELEMENTS)):
        el = items.GetElement(i)
        if el.CurrentIsOffscreen:
            continue
        r = el.CurrentBoundingRectangle
        targets.append(Target(str(el.CurrentName or ""), (int(r.left) - left, int(r.top) - top, int(r.right) - left, int(r.bottom) - top)))
    image = ImageGrab.grab(bbox=(left, top, right, bottom))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return targets, buf.getvalue(), image.size
