"""An optional object detector for photos: boxes and labels from a YOLO model that YOU put on disk (``data/models/yolo/yolo.onnx``, nothing is downloaded).

Why next to the vision model: a vision-language model describes a scene in words and can invent things; a detector only reports the classes it was trained on, with a
confidence and a box, and says nothing about anything else. ``analyze_photo`` shows both, labelled apart, so a claim made by the language model that the detector did
not find stays visibly unconfirmed.

Supported output layout: YOLOv8 / YOLO11 style ``(1, 4 + classes, boxes)`` (or its transpose) with boxes as centre-x, centre-y, width, height in input pixels and no
separate objectness score. Other layouts are refused with a clear message rather than guessed. Class names default to the 80 COCO names (what the stock models use);
a ``labels.txt`` beside the model (one name per line) overrides them.

Pure numpy + PIL parts (letterbox, decoding, non-maximum suppression) are tested with a fake model; real model speed and accuracy are NOT verified here.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

COCO = tuple(n.replace("-", " ") for n in ("person bicycle car motorcycle airplane bus train truck boat traffic-light fire-hydrant stop-sign parking-meter bench bird cat dog horse sheep cow "
                                          "elephant bear zebra giraffe backpack umbrella handbag tie suitcase frisbee skis snowboard sports-ball kite baseball-bat baseball-glove skateboard "
                                          "surfboard tennis-racket bottle wine-glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot hot-dog pizza donut cake chair couch "
                                          "potted-plant bed dining-table toilet tv laptop mouse remote keyboard cell-phone microwave oven toaster sink refrigerator book clock vase scissors "
                                          "teddy-bear hair-drier toothbrush").split())
MODEL_DIR = ("models", "yolo")
MAX_DETECTIONS = 30


class DetectorError(Exception):
    """Safe to show."""


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    box: tuple[float, float, float, float]          # x1, y1, x2, y2 in the ORIGINAL image's pixels


def letterbox(image: Any, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    """PIL image -> (1, 3, size, size) float32 in 0..1, with grey padding; also the scale and (pad_x, pad_y) needed to map boxes back."""
    from PIL import Image

    w, h = image.size
    scale = min(size / w, size / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = image.convert("RGB").resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    pad = ((size - nw) / 2, (size - nh) / 2)
    canvas.paste(resized, (int(pad[0]), int(pad[1])))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    return arr.transpose(2, 0, 1)[None, ...], scale, (float(int(pad[0])), float(int(pad[1])))


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def decode(output: np.ndarray, labels: Sequence[str], scale: float, pad: tuple[float, float], image_size: tuple[int, int],
           min_score: float = 0.35, iou_limit: float = 0.5) -> list[Detection]:
    """YOLOv8-style output -> detections in original-image pixels, after per-class non-maximum suppression."""
    out = np.asarray(output)
    if out.ndim == 3:
        out = out[0]
    if out.ndim != 2:
        raise DetectorError("this model's output is not a YOLOv8-style table (the layout is not supported)")
    nc = len(labels)
    if out.shape[0] == 4 + nc:
        pass
    elif out.shape[1] == 4 + nc:
        out = out.T
    else:
        raise DetectorError(f"this model's output has {out.shape} values per box, expected {4 + nc} (4 box numbers + {nc} class scores); put a matching labels.txt beside it")
    boxes, scores = out[:4].T, out[4:].T
    best = scores.argmax(axis=1)
    conf = scores[np.arange(len(scores)), best]
    keep = np.where(conf >= min_score)[0]
    W, H = image_size
    found: list[Detection] = []
    for i in keep[np.argsort(-conf[keep])]:
        cx, cy, w, h = boxes[i]
        x1, y1, x2, y2 = ((cx - w / 2 - pad[0]) / scale, (cy - h / 2 - pad[1]) / scale, (cx + w / 2 - pad[0]) / scale, (cy + h / 2 - pad[1]) / scale)
        box = (float(max(0, min(W, x1))), float(max(0, min(H, y1))), float(max(0, min(W, x2))), float(max(0, min(H, y2))))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        label = labels[int(best[i])]
        if any(d.label == label and iou(d.box, box) > iou_limit for d in found):
            continue
        found.append(Detection(label, float(conf[i]), box))
        if len(found) >= MAX_DETECTIONS:
            break
    return found


class Detector:
    """``run(tensor) -> ndarray`` is injected (a real onnxruntime session in ``from_files``; a fake in tests)."""

    def __init__(self, run: Callable[[np.ndarray], np.ndarray], labels: Sequence[str] = COCO, size: int = 640, name: str = "YOLO (ONNX)") -> None:
        self.run, self.labels, self.size, self.name = run, tuple(labels), size, name

    @classmethod
    def from_folder(cls, data_dir: Path) -> "Detector | None":
        """The detector in ``data/models/yolo`` or None when no model is there."""
        base = Path(data_dir).joinpath(*MODEL_DIR)
        model = base / "yolo.onnx"
        if not model.is_file():
            return None
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise DetectorError("onnxruntime is not installed (pip install onnxruntime); the object detector is optional") from exc
        try:
            session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise DetectorError(f"the detector model could not be loaded ({exc.__class__.__name__})") from exc
        labels = COCO
        names = base / "labels.txt"
        if names.is_file():
            labels = tuple(line.strip() for line in names.read_text(encoding="utf-8").splitlines() if line.strip()) or COCO
        shape = session.get_inputs()[0].shape
        size = int(shape[2]) if len(shape) == 4 and isinstance(shape[2], int) else 640
        name = session.get_inputs()[0].name
        return cls(lambda t: session.run(None, {name: t})[0], labels, size)

    def detect(self, image_bytes: bytes, min_score: float = 0.35) -> list[Detection]:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(image_bytes)) as im:
                im.load()
                tensor, scale, pad = letterbox(im, self.size)
                size = im.size
        except Exception as exc:
            raise DetectorError("the image could not be read by the detector") from exc
        return decode(self.run(tensor), self.labels, scale, pad, size, min_score)


def describe(found: Sequence[Detection], image_size: tuple[int, int] | None = None, model: str = "") -> str:
    """The detector's evidence as text, counted per class, with where in the picture (left/middle/right, top/middle/bottom) for the biggest ones."""
    if not found:
        return "The object detector found none of the classes it knows" + (f" ({model})" if model else "") + " - that does not mean nothing is there, only that nothing it was trained on was recognised."
    counts: dict[str, list[float]] = {}
    for d in found:
        counts.setdefault(d.label, []).append(d.score)
    lines = [f"{len(v)} x {k} (best {max(v):.0%})" for k, v in sorted(counts.items(), key=lambda kv: -max(kv[1]))]
    text = "Objects the detector recognised" + (f" ({model})" if model else "") + ": " + "; ".join(lines) + "."
    if image_size:
        W, H = image_size
        parts = []
        for d in sorted(found, key=lambda d: -(d.box[2] - d.box[0]) * (d.box[3] - d.box[1]))[:3]:
            cx, cy = (d.box[0] + d.box[2]) / 2 / W, (d.box[1] + d.box[3]) / 2 / H
            parts.append(f"{d.label}: {'left' if cx < 1 / 3 else 'right' if cx > 2 / 3 else 'middle'}, {'top' if cy < 1 / 3 else 'bottom' if cy > 2 / 3 else 'centre'}")
        text += " Largest: " + "; ".join(parts) + "."
    return text
