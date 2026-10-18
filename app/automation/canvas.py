"""Node-canvas geometry for the workflow studio (no Qt): where each step's node sits and which slot a drop lands in.

The canvas is a VIEW of the real workflow, which is a list of steps run in order (with optional conditions). It is not a
free-form graph: nodes snap to numbered slots laid out left to right, wrapping onto new rows, and the arrows between
them are always "this step, then the next". Dragging a node to another slot is the same edit as "Move up/down" repeated,
so it goes through ``studio.edit_steps`` and the same validation. Nothing here runs a step.
"""

from __future__ import annotations

from app.automation.tasks import Step

NODE_W = 190
NODE_H = 78
GAP_X = 56
GAP_Y = 48
PER_ROW = 4
PITCH_X = NODE_W + GAP_X
PITCH_Y = NODE_H + GAP_Y


def position(index: int, per_row: int = PER_ROW) -> tuple[float, float]:
    """Top-left corner of slot ``index``."""
    return (index % per_row) * PITCH_X, (index // per_row) * PITCH_Y


def slot_for(x: float, y: float, count: int, per_row: int = PER_ROW) -> int:
    """The slot whose centre is nearest to the point (a dropped node's centre), limited to existing slots."""
    best, best_distance = 0, float("inf")
    for i in range(max(count, 1)):
        px, py = position(i, per_row)
        d = (px + NODE_W / 2 - x) ** 2 + (py + NODE_H / 2 - y) ** 2
        if d < best_distance:
            best, best_distance = i, d
    return best


def moves(source: int, target: int) -> list[str]:
    """The single-step edits ("up"/"down") that carry a step from ``source`` to ``target``."""
    return ["down"] * (target - source) if target > source else ["up"] * (source - target)


def edge_label(next_step: Step) -> str:
    """What the arrow INTO ``next_step`` says: its condition, or just 'then'."""
    return next_step.when.replace("_", " ") if next_step.when else "then"


def node_title(step: Step) -> str:
    if step.type == "notify":
        return "say"
    return step.tool or "?"


def node_detail(step: Step) -> str:
    if step.type == "notify":
        return (step.message or "")[:60]
    args = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items())
    return args[:60]


def scene_size(count: int, per_row: int = PER_ROW) -> tuple[float, float]:
    rows = max(1, -(-count // per_row))
    return min(max(count, 1), per_row) * PITCH_X - GAP_X, rows * PITCH_Y - GAP_Y
