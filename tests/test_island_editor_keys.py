"""
test_island_editor_keys.py

Keyboard behaviour of UI_island_editor.py — arrow-key panning, and the widgets
whose own key handling must survive it.

These are pytest-style (not unittest) because they need pytest-qt's ``qtbot``
fixture, which cannot be injected into a TestCase.

Run with:  python -m pytest tests/test_island_editor_keys.py -v

The point of most of these is contention, not arithmetic: a window-level
shortcut for the arrows would fire over the spin boxes too and swallow the
Up/Down that steps their value, so the bindings are scoped to the canvas.  The
tests pin that scoping down, along with the focus policy it depends on.
"""

import pytest
from shapely.geometry import box

from PySide6.QtCore import Qt

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

WEST = box(0, 0, 10_000, 10_000)
EAST = box(20_000, 0, 30_000, 10_000)


@pytest.fixture
def editor(qtbot):
    """A shown editor holding two islands, with the window active."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window._set_state(M._as_multi(WEST.union(EAST)), [], preserve_view=False)
    yield window
    window.undo_stack.clear()


def _span(editor):
    (x0, x1), (y0, y1) = editor.ax.get_xlim(), editor.ax.get_ylim()
    return x1 - x0, y1 - y0


def _centre(editor):
    (x0, x1), (y0, y1) = editor.ax.get_xlim(), editor.ax.get_ylim()
    return (x0 + x1) / 2, (y0 + y1) / 2


class TestArrowPanning:
    """Arrows slide the view while the canvas holds the keyboard."""

    @pytest.mark.parametrize("key, dx, dy", [
        (Qt.Key.Key_Right, 1, 0),
        (Qt.Key.Key_Left, -1, 0),
        (Qt.Key.Key_Up, 0, 1),
        (Qt.Key.Key_Down, 0, -1),
    ])
    def test_each_arrow_moves_its_own_way(self, editor, qtbot, key, dx, dy):
        editor.canvas.setFocus()
        wide, tall = _span(editor)
        cx, cy = _centre(editor)
        qtbot.keyClick(editor.canvas, key)
        moved_x, moved_y = (c - o for c, o in zip(_centre(editor), (cx, cy)))
        assert moved_x == pytest.approx(wide * M.PAN_STEP * dx)
        assert moved_y == pytest.approx(tall * M.PAN_STEP * dy)

    def test_panning_does_not_zoom(self, editor, qtbot):
        """The view slides; it must not stretch."""
        editor.canvas.setFocus()
        before = _span(editor)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Up)
        assert _span(editor) == pytest.approx(before)

    def test_the_step_is_a_fraction_of_the_view_not_a_distance(self, editor, qtbot):
        """Zoomed in twice as far, one press covers half the ground."""
        editor.canvas.setFocus()
        editor.ax.set_xlim(0, 30_000)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        far = _centre(editor)[0]
        editor.ax.set_xlim(0, 15_000)
        near_before = _centre(editor)[0]
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        near = _centre(editor)[0] - near_before
        assert (far - 15_000) == pytest.approx(near * 2)

    @pytest.mark.parametrize("modifier, step", [
        (Qt.KeyboardModifier.NoModifier, M.PAN_STEP),
        (Qt.KeyboardModifier.ShiftModifier, M.PAN_STEP_COARSE),
        (Qt.KeyboardModifier.ControlModifier, M.PAN_STEP_FINE),
    ])
    def test_modifiers_scale_the_stride(self, editor, qtbot, modifier, step):
        editor.canvas.setFocus()
        wide, _ = _span(editor)
        cx, _ = _centre(editor)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right, modifier)
        assert _centre(editor)[0] - cx == pytest.approx(wide * step)

    def test_a_held_arrow_keeps_panning(self, editor, qtbot):
        """Auto-repeat accumulates rather than latching after the first press."""
        editor.canvas.setFocus()
        wide, _ = _span(editor)
        cx, _ = _centre(editor)
        for _ in range(5):
            qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        assert _centre(editor)[0] - cx == pytest.approx(wide * M.PAN_STEP * 5)

    def test_key_pan_and_drag_pan_agree(self, editor, qtbot):
        """Both go through _shift_view, so the same delta lands the same place."""
        editor.canvas.setFocus()
        wide, _ = _span(editor)
        cx, _ = _centre(editor)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        by_key = _centre(editor)[0]
        editor._shift_view(-wide * M.PAN_STEP, 0)          # back where we started
        assert _centre(editor)[0] == pytest.approx(cx)
        editor._shift_view(wide * M.PAN_STEP, 0)
        assert _centre(editor)[0] == pytest.approx(by_key)


class TestArrowsYieldToTheWidgetsThatNeedThem:
    """The bindings are canvas-scoped, so nothing else loses its arrows."""

    def test_spin_box_up_down_still_steps_the_value(self, editor, qtbot):
        editor.area_spin.setFocus()
        before_value = editor.area_spin.value()
        before_view = _centre(editor)
        qtbot.keyClick(editor.area_spin, Qt.Key.Key_Up)
        assert editor.area_spin.value() > before_value
        assert _centre(editor) == pytest.approx(before_view)   # and did not pan

    def test_spin_box_left_right_does_not_pan(self, editor, qtbot):
        editor.buffer_spin.setFocus()
        before = _centre(editor)
        qtbot.keyClick(editor.buffer_spin, Qt.Key.Key_Left)
        qtbot.keyClick(editor.buffer_spin, Qt.Key.Key_Right)
        assert _centre(editor) == pytest.approx(before)

    def test_the_shortcuts_are_scoped_to_the_canvas(self, editor):
        """Context, not a focus guard, is what keeps them out of the spin boxes."""
        from PySide6.QtGui import QShortcut
        arrows = {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down}
        pan_shortcuts = [s for s in editor.canvas.findChildren(QShortcut)
                         if s.key()[0].key() in arrows]
        assert len(pan_shortcuts) == 12          # 4 arrows x 3 strides
        assert all(s.context() == Qt.ShortcutContext.WidgetShortcut
                   for s in pan_shortcuts)


class TestFocusReturnsToTheMap:
    """Arrows are only useful if the canvas is where the keyboard rests."""

    def test_the_canvas_can_take_focus_at_all(self, editor):
        """Matplotlib leaves an embedded canvas at NoFocus; the editor must not."""
        assert editor.canvas.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_clicking_a_button_hands_the_keyboard_back(self, editor, qtbot):
        editor.area_spin.setFocus()
        qtbot.mouseClick(editor.btn_select_all, Qt.MouseButton.LeftButton)
        assert editor.canvas.hasFocus()

    def test_arrows_pan_right_after_a_button_click(self, editor, qtbot):
        """The whole point of handing focus back."""
        qtbot.mouseClick(editor.btn_select_all, Qt.MouseButton.LeftButton)
        cx, _ = _centre(editor)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Right)
        assert _centre(editor)[0] > cx


class TestUndoShortcutsSurvive:
    """Ctrl+Z must keep working from the spin boxes (see ``eventFilter``)."""

    @pytest.mark.parametrize("focus_on", ["canvas", "area_spin", "buffer_spin"])
    def test_undo_fires_wherever_the_focus_is(self, editor, qtbot, focus_on):
        doc, other = editor.doc, M._as_multi(WEST)
        editor.undo_stack.push(M.EditCommand(editor, "probe", doc, other, [], []))
        getattr(editor, focus_on if focus_on != "canvas" else "canvas").setFocus()
        qtbot.keyClick(editor.canvas if focus_on == "canvas"
                       else getattr(editor, focus_on),
                       Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert editor.undo_stack.index() == 0
        assert editor.doc.equals(doc)
