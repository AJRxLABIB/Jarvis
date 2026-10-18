from __future__ import annotations

import pytest

from app.automation import canvas as geo
from app.automation.tasks import Step

PySide6 = pytest.importorskip("PySide6")
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def test_slots_wrap_and_drops_pick_the_nearest_slot() -> None:
    assert geo.position(0) == (0, 0) and geo.position(1)[0] == geo.PITCH_X and geo.position(geo.PER_ROW) == (0, geo.PITCH_Y)
    for i in range(9):
        x, y = geo.position(i)
        assert geo.slot_for(x + geo.NODE_W / 2, y + geo.NODE_H / 2, 9) == i
    assert geo.slot_for(10_000, 0, 3) == 2 and geo.slot_for(-500, -500, 3) == 0          # never past the ends
    assert geo.slot_for(0, 0, 0) == 0


def test_moves_are_single_steps() -> None:
    assert geo.moves(0, 3) == ["down"] * 3 and geo.moves(4, 1) == ["up"] * 3 and geo.moves(2, 2) == []


def test_edges_and_nodes_describe_the_step() -> None:
    assert geo.edge_label(Step(type="tool", tool="a", when="weekday")) == "weekday"
    assert geo.edge_label(Step(type="tool", tool="a")) == "then"
    assert geo.node_title(Step(type="notify", message="hi")) == "say" and geo.node_detail(Step(type="notify", message="hi")) == "hi"
    assert geo.node_title(Step(type="tool", tool="t", arguments={"a": 1})) == "t" and "a=1" in geo.node_detail(
        Step(type="tool", tool="t", arguments={"a": 1}))
    w, h = geo.scene_size(5)
    assert w == geo.PER_ROW * geo.PITCH_X - geo.GAP_X and h == 2 * geo.PITCH_Y - geo.GAP_Y


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def studio_window(db):
    from app.config.files import PermissionsConfig
    from app.security.permissions import PermissionEngine
    from app.ui.workflow_studio import WorkflowStudioDialog
    from tests.test_workflow_studio import setup

    registry, ctx, store, task = setup(db)
    dialog = WorkflowStudioDialog(ctx, None, registry=registry, permissions=PermissionEngine(PermissionsConfig()))
    return dialog, store, task


from tests.test_automation import db  # noqa: E402,F401


def test_canvas_shows_every_step_with_arrows_and_follows_the_table(qapp, db) -> None:  # noqa: F811
    dialog, _, _ = studio_window(db)
    assert len(dialog.canvas.nodes) == 3 and dialog.views.tabText(1).replace("&", "") == "Canvas"
    dialog.table.selectRow(2)
    assert dialog.canvas.current == 2 and dialog.canvas.nodes[2].isSelected()
    dialog.canvas.nodes[0].setSelected(True)
    assert dialog.table.currentRow() == 0                               # click on the canvas selects the table row


def drag(dialog, source: int, target: int) -> None:
    """A real press-move-release on the canvas viewport, from the middle of one slot to the middle of another."""
    canvas = dialog.canvas
    canvas.resize(900, 420)
    canvas.show()
    QApplication.processEvents()

    def centre(i: int):
        x, y = geo.position(i)
        return canvas.mapFromScene(QPointF(x + geo.NODE_W / 2, y + geo.NODE_H / 2))

    QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre(source))
    QTest.mouseMove(canvas.viewport(), centre(target))
    QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre(target))
    QApplication.processEvents()


def test_dragging_a_node_reorders_through_the_checked_edit(qapp, db) -> None:  # noqa: F811
    dialog, _, _ = studio_window(db)
    first_tool = dialog.steps[0].tool
    drag(dialog, 0, 2)                                                  # drop the first box on slot 3
    assert dialog.dirty and dialog.steps[2].tool == first_tool          # the step moved there
    assert dialog.canvas.nodes[2].index == 2 and dialog.table.currentRow() == 2
    assert dialog.buttons["save"].isEnabled()


def test_dropping_on_its_own_slot_changes_nothing(qapp, db) -> None:  # noqa: F811
    dialog, _, _ = studio_window(db)
    drag(dialog, 1, 1)
    assert not dialog.dirty


def test_keyboard_moves_and_deletes(qapp, db) -> None:  # noqa: F811
    dialog, _, _ = studio_window(db)
    canvas = dialog.canvas
    canvas.select(0)
    QTest.keyClick(canvas, Qt.Key.Key_Right)
    assert canvas.current == 1 and dialog.table.currentRow() == 1
    names = [s.describe() for s in dialog.steps]
    QTest.keyClick(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)       # move the step down
    assert [s.describe() for s in dialog.steps] == [names[0], names[2], names[1]]
    QTest.keyClick(canvas, Qt.Key.Key_Delete)
    assert len(dialog.steps) == 2 and dialog.dirty


def test_a_dashed_border_marks_continue_on_error(qapp, db) -> None:  # noqa: F811
    dialog, _, _ = studio_window(db)
    dialog.table.selectRow(0)
    dialog.buttons["err"].click()
    assert dialog.canvas.nodes[0].pen().style() == Qt.PenStyle.DashLine and dialog.canvas.nodes[1].pen().style() == Qt.PenStyle.SolidLine



def test_undo_takes_back_unsaved_changes_one_by_one(qapp, db) -> None:  # noqa: F811
    dialog, _, task = studio_window(db)
    original = [s.describe() for s in dialog.steps]
    assert not dialog.buttons["undo"].isEnabled()
    dialog.table.selectRow(0)
    dialog.buttons["down"].click()
    dialog.buttons["remove"].click()
    assert len(dialog.steps) == 2 and dialog.buttons["undo"].isEnabled()
    dialog.buttons["undo"].click()
    assert len(dialog.steps) == 3 and dialog.dirty
    dialog.buttons["undo"].click()
    assert [s.describe() for s in dialog.steps] == original and not dialog.dirty and not dialog.buttons["save"].isEnabled()
    dialog.undo()                                                         # nothing left: harmless
    assert [s.describe() for s in dialog.steps] == original


def test_canvas_zoom_is_limited_and_ctrl0_refits(qapp, db) -> None:  # noqa: F811
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    dialog, _, _ = studio_window(db)
    canvas = dialog.canvas
    canvas.resize(700, 300)
    canvas.show()
    QApplication.processEvents()
    for _ in range(40):
        canvas.wheelEvent(QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                                      Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False))
    assert 0.1 <= canvas.transform().m11() <= 3.0 and canvas._zoomed
    QTest.keyClick(canvas, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
    assert not canvas._zoomed and canvas.transform().m11() <= 1.0
