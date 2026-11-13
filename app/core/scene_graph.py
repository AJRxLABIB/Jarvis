"""Scene graph: detections turned into a small structured picture - objects, where they are, how they relate - with confidence and a timestamp.

Input is what the optional detector already produces (``core/object_detect.Detection``: label, score, box in image pixels); nothing new is captured. Relations are
computed from the BOXES only (geometry), never invented:

    inside / contains   one box lies (almost) wholly within another
    on                  the lower edge of A touches the upper part of B and A is horizontally over B (a 2-D picture cannot prove physical support: it says "on top of in the image")
    left_of / right_of / above / below   clear separation along one axis
    overlaps            boxes intersect without containment
    near                close compared with their size, no other relation

Every node carries ``source="detector"``, the score and the observation time; a relation is ``inferred`` from geometry and says so. ``diff`` compares two scenes
(appeared / gone / moved) for visual change detection without comparing raw pixels. Depth, 3-D layout and object identity across frames are NOT claimed: matching
between frames is by label and nearest box and can be wrong when several objects share a label.
Pure logic; tested without a model, a camera or an image.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

MAX_NODES = 40
CONTAIN = 0.85          # share of the smaller box that must lie inside the other
NEAR_GAP = 0.5          # gap smaller than this fraction of the larger box size = near
MOVE_FRACTION = 0.25    # centre moved by more than this fraction of the box size = moved
SEP = 0.0


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    score: float
    box: tuple[float, float, float, float]
    at: float
    source: str = "detector"

    @property
    def centre(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    @property
    def size(self) -> tuple[float, float]:
        return max(1.0, self.box[2] - self.box[0]), max(1.0, self.box[3] - self.box[1])


@dataclass(frozen=True)
class Relation:
    a: str
    kind: str
    b: str
    basis: str = "inferred from box geometry"


@dataclass
class Scene:
    nodes: list[Node]
    relations: list[Relation]
    image_size: tuple[int, int] | None = None
    at: float = 0.0
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        if not self.nodes:
            return "Scene: nothing detected (the detector only knows its trained classes)."
        names = {n.id: f"{n.label} ({n.score:.0%})" for n in self.nodes}
        lines = [f"Scene graph: {len(self.nodes)} object(s)" + (f", image {self.image_size[0]}x{self.image_size[1]}" if self.image_size else "") + "."]
        lines += [f"  {names[r.a]} {r.kind.replace('_', ' ')} {names[r.b]}" for r in self.relations[:20]]
        if len(self.relations) > 20:
            lines.append(f"  ... and {len(self.relations) - 20} more relation(s)")
        lines += [f"  note: {n}" for n in self.notes]
        lines.append("Relations come from 2-D boxes only; depth and real support are not known.")
        return "\n".join(lines)


def _area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a: Sequence[float], b: Sequence[float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def relations_between(a: Node, b: Node) -> list[Relation]:
    """Relations from ``a`` to ``b`` (and the reverse where it is not symmetric). Only the strongest applicable one per pair is returned."""
    inter = _inter(a.box, b.box)
    small, big = (a, b) if _area(a.box) <= _area(b.box) else (b, a)
    if inter and inter / max(1.0, _area(small.box)) >= CONTAIN and _area(a.box) != _area(b.box):
        return [Relation(small.id, "inside", big.id), Relation(big.id, "contains", small.id)]
    h_overlap = min(a.box[2], b.box[2]) - max(a.box[0], b.box[0])
    if inter == 0 and h_overlap > 0:
        gap_ab = b.box[1] - a.box[3]          # a above b when positive
        gap_ba = a.box[1] - b.box[3]
        if 0 <= gap_ab <= NEAR_GAP * a.size[1] * 0.2 and a.size[0] <= b.size[0] * 1.5:
            return [Relation(a.id, "on", b.id)]
        if 0 <= gap_ba <= NEAR_GAP * b.size[1] * 0.2 and b.size[0] <= a.size[0] * 1.5:
            return [Relation(b.id, "on", a.id)]
    if inter:
        # an overlap whose lower part rests on the other box reads as "on"
        if a.box[3] > b.box[1] and a.box[1] < b.box[1] and a.box[3] - b.box[1] <= 0.2 * a.size[1] and h_overlap > 0.5 * min(a.size[0], b.size[0]):
            return [Relation(a.id, "on", b.id)]
        if b.box[3] > a.box[1] and b.box[1] < a.box[1] and b.box[3] - a.box[1] <= 0.2 * b.size[1] and h_overlap > 0.5 * min(a.size[0], b.size[0]):
            return [Relation(b.id, "on", a.id)]
        return [Relation(a.id, "overlaps", b.id)]
    dx = max(a.box[0] - b.box[2], b.box[0] - a.box[2], 0.0)
    dy = max(a.box[1] - b.box[3], b.box[1] - a.box[3], 0.0)
    if dx > dy:
        return [Relation(a.id, "left_of", b.id)] if a.box[2] <= b.box[0] else [Relation(b.id, "left_of", a.id)]
    if dy > 0:
        return [Relation(a.id, "above", b.id)] if a.box[3] <= b.box[1] else [Relation(b.id, "above", a.id)]
    return []


def build(detections: Sequence, image_size: tuple[int, int] | None = None, now: float | None = None) -> Scene:
    """``detections``: objects with ``label``, ``score`` and ``box`` (x1, y1, x2, y2). The strongest ``MAX_NODES`` are kept."""
    now = time.time() if now is None else now
    ordered = sorted(detections, key=lambda d: -float(d.score))[:MAX_NODES]
    counts: dict[str, int] = {}
    nodes = []
    for d in ordered:
        counts[d.label] = counts.get(d.label, 0) + 1
        nodes.append(Node(f"{d.label.replace(' ', '_')}#{counts[d.label]}", d.label, float(d.score), tuple(float(v) for v in d.box), now))
    rels: list[Relation] = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            rels += relations_between(a, b)
    notes = [f"only the {MAX_NODES} most confident of {len(detections)} detections are kept"] if len(detections) > MAX_NODES else []
    return Scene(nodes, rels, image_size, now, notes)


@dataclass
class Change:
    appeared: list[Node]
    gone: list[Node]
    moved: list[tuple[Node, Node]]

    @property
    def any(self) -> bool:
        return bool(self.appeared or self.gone or self.moved)

    def text(self) -> str:
        if not self.any:
            return "No visible change between the two scenes."
        lines = [f"appeared: {n.label} ({n.score:.0%})" for n in self.appeared] + [f"gone: {n.label}" for n in self.gone]
        lines += [f"moved: {a.label} by about {abs(b.centre[0] - a.centre[0]):.0f} px across, {abs(b.centre[1] - a.centre[1]):.0f} px down/up" for a, b in self.moved]
        return "\n".join(lines) + "\nMatching between frames is by label and nearest box, so look-alike objects can be confused."


def diff(old: Scene, new: Scene) -> Change:
    left = list(new.nodes)
    gone: list[Node] = []
    moved: list[tuple[Node, Node]] = []
    for o in old.nodes:
        same = [n for n in left if n.label == o.label]
        if not same:
            gone.append(o)
            continue
        best = min(same, key=lambda n: (n.centre[0] - o.centre[0]) ** 2 + (n.centre[1] - o.centre[1]) ** 2)
        left.remove(best)
        dist = ((best.centre[0] - o.centre[0]) ** 2 + (best.centre[1] - o.centre[1]) ** 2) ** 0.5
        if dist > MOVE_FRACTION * max(o.size):
            moved.append((o, best))
    return Change(left, gone, moved)
