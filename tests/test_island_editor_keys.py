"""
test_island_editor_keys.py

Keyboard behaviour of UI_island_editor.py — arrow-key panning, the shortcut on
every button, and the widgets whose own key handling must survive both.

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
from PySide6.QtWidgets import QPushButton

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

WEST = box(0, 0, 10_000, 10_000)
EAST = box(20_000, 0, 30_000, 10_000)

#: Long enough for a key's ``animateClick`` to have landed if it was going to.
KEY_SETTLE_MS = 300


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


class TestButtonShortcuts:
    """Every button answers to a key, and the key is the button.

    The keys hang on the buttons rather than on free-standing ``QShortcut``s,
    which is what makes ``setEnabled`` the single gate for both the mouse and
    the keyboard; these pin that down, and that nothing is bound twice.
    """

    EXPECTED = {
        "btn_import": "Ctrl+O",
        "btn_save": "Ctrl+S",
        "btn_real": "Ctrl+R",
        "btn_recalc": "F5",
        "btn_select_all": "Ctrl+A",
        "btn_clear": "Ctrl+Shift+A",
        "btn_invert": "Ctrl+I",
        "btn_gap": "G",
        "btn_merge": "M",
        "btn_bridge": "B",
        "btn_remove_sel": "Del",
        "btn_remove_small": "S",
        "btn_amputate": "A",
        "btn_lakes": "L",
        "btn_draw": "D",
        "btn_erase": "E",
        "btn_thin": "F6",
        "btn_worst_patch": "W",
        "btn_stress": "F7",
        "btn_worst_neck": "Shift+W",
    }

    @pytest.mark.parametrize("name, keys", sorted(EXPECTED.items()))
    def test_each_button_carries_its_key(self, editor, name, keys):
        assert getattr(editor, name).shortcut().toString() == keys

    @pytest.mark.parametrize("name, keys", sorted(EXPECTED.items()))
    def test_the_tooltip_says_so(self, editor, name, keys):
        """A key nobody can find is not a shortcut."""
        assert f"Shortcut: {keys}" in getattr(editor, name).toolTip()

    def test_undo_and_redo_are_annotated_but_not_rebound(self, editor):
        """Binding a button to Ctrl+Z as well would make both ambiguous, and Qt
        answers an ambiguous shortcut by firing neither."""
        assert editor.btn_undo.shortcut().isEmpty()
        assert editor.btn_redo.shortcut().isEmpty()
        assert "Shortcut: Ctrl+Z" in editor.btn_undo.toolTip()
        assert "Shortcut: Ctrl+Shift+Z" in editor.btn_redo.toolTip()

    def test_the_view_history_keys_are_nowhere_near_undo(self, editor):
        """Back/Forward step the view, not the edits — so they take the browser
        sequences, not anything that reads as Undo."""
        bound = {a.text(): a.shortcut().toString() for a in editor.nav.actions()
                 if not a.shortcut().isEmpty()}
        assert bound == {"Home": "Home", "Back": "Alt+Left",
                         "Forward": "Alt+Right", "Pan": "P", "Zoom": "Z"}

    def test_no_key_is_claimed_twice(self, editor):
        """Including by the arrows, which are the canvas's own."""
        from PySide6.QtGui import QShortcut
        claimed = [w.shortcut().toString()
                   for w in editor.findChildren(QPushButton) + editor.nav.actions()
                   if not w.shortcut().isEmpty()]
        claimed += [s.key().toString() for s in editor.findChildren(QShortcut)]
        assert len(claimed) == len(set(claimed))


class TestKeysActAsTheirButtonsDo:
    """Pressed, a key does what clicking would — and only when clicking would.

    A key on a button fires it through ``animateClick``, so the press lands a
    tenth of a second later with the button visibly down; that flash is the
    point of hanging the keys on the buttons, and it is why these wait rather
    than assert on the next line.
    """

    def test_a_key_fires_its_action(self, editor, qtbot):
        editor.canvas.setFocus()
        qtbot.keyClick(editor.canvas, Qt.Key.Key_A,
                       Qt.KeyboardModifier.ControlModifier)
        qtbot.waitUntil(lambda: editor.selected == [0, 1])

    def test_a_toggle_key_toggles(self, editor, qtbot):
        editor.canvas.setFocus()
        qtbot.keyClick(editor.canvas, Qt.Key.Key_D)
        qtbot.waitUntil(lambda: editor._brush_mode == M.DRAW)
        qtbot.keyClick(editor.canvas, Qt.Key.Key_E)
        qtbot.waitUntil(lambda: editor._brush_mode == M.ERASE)   # a radio pair

    def test_a_key_edits_the_document(self, editor, qtbot):
        editor.selected = [0]
        editor._sync_buttons()
        editor.canvas.setFocus()
        qtbot.keyClick(editor.canvas, Qt.Key.Key_Delete)
        qtbot.waitUntil(lambda: editor.undo_stack.count() == 1)
        assert len(editor.parts) == 1

    def test_a_nav_key_works_the_toolbar(self, editor, qtbot):
        """P reaches the pan tool — the one way left to drag with a brush on."""
        pan = next(a for a in editor.nav.actions() if a.text() == "Pan")
        editor.canvas.setFocus()
        qtbot.keyClick(editor.canvas, Qt.Key.Key_P)
        qtbot.waitUntil(lambda: pan.isChecked())

    def test_a_disabled_button_is_a_dead_key(self, editor, qtbot):
        """Nothing selected, so Merge is greyed — and M is greyed with it."""
        editor.clear_selection()
        assert not editor.btn_merge.isEnabled()
        editor.canvas.setFocus()
        qtbot.keyClick(editor.canvas, Qt.Key.Key_M)
        qtbot.wait(KEY_SETTLE_MS)
        assert editor.undo_stack.count() == 0

    @pytest.mark.parametrize("spin", ["buffer_spin", "brush_spin",
                                      "bridge_width_spin"])
    def test_a_focused_spin_box_keeps_its_letters(self, editor, qtbot, spin):
        """The entry takes the keys it would accept as input before shortcut
        dispatch sees them, so typing a value cannot set off an edit."""
        editor.select_all()
        getattr(editor, spin).setFocus()
        for key in (Qt.Key.Key_M, Qt.Key.Key_S, Qt.Key.Key_A, Qt.Key.Key_E):
            qtbot.keyClick(getattr(editor, spin), key)
        qtbot.wait(KEY_SETTLE_MS)
        assert editor.undo_stack.count() == 0
        assert editor._brush_mode is None

    @pytest.mark.parametrize("spin", ["brush_spin", "bridge_width_spin",
                                      "bridge_span_spin"])
    def test_undo_still_fires_from_every_spin_box(self, editor, qtbot, spin):
        """Every entry filters the undo override, not just the first three."""
        doc = editor.doc
        editor.undo_stack.push(
            M.EditCommand(editor, "probe", doc, M._as_multi(WEST), [], []))
        getattr(editor, spin).setFocus()
        qtbot.keyClick(getattr(editor, spin), Qt.Key.Key_Z,
                       Qt.KeyboardModifier.ControlModifier)
        assert editor.undo_stack.index() == 0
        assert editor.doc.equals(doc)
