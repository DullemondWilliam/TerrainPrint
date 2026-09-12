"""
test_island_editor_selection.py

The selection controls of UI_island_editor.py — **All**, **None**, and the
**Invert** that turns "the few worth keeping" into "the many to drop".

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_keys.py.

Run with:  python -m pytest tests/test_island_editor_selection.py -v

Invert earns its own tests mostly for its *edges*: it is the one selection
control that stays live at both extremes, because inverting nothing selects
everything and inverting everything clears it — cases where All and None have
nothing left to do and grey themselves out.
"""

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

#: Four separate islands, so a partial selection is a real partial selection.
ISLANDS = [box(x, 0, x + 5_000, 5_000) for x in (0, 20_000, 40_000, 60_000)]


@pytest.fixture
def editor(qtbot):
    """A shown editor holding four islands, nothing selected."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window._set_state(M._as_multi(unary_union(ISLANDS)), [], preserve_view=False)
    yield window
    window.undo_stack.clear()


@pytest.fixture
def blank(qtbot):
    """A shown editor with no province loaded."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    yield window


def _picked(editor):
    return sorted(editor.selected)


class TestInvert:
    """Selected becomes unselected, and unselected selected."""

    def test_it_swaps_a_partial_selection(self, editor):
        editor.selected = [0, 2]
        editor.invert_selection()
        assert _picked(editor) == [1, 3]

    def test_it_is_its_own_inverse(self, editor):
        editor.selected = [1]
        editor.invert_selection()
        editor.invert_selection()
        assert _picked(editor) == [1]

    def test_inverting_nothing_selects_everything(self, editor):
        editor.clear_selection()
        editor.invert_selection()
        assert _picked(editor) == [0, 1, 2, 3]

    def test_inverting_everything_selects_nothing(self, editor):
        editor.select_all()
        editor.invert_selection()
        assert _picked(editor) == []

    def test_it_leaves_no_island_in_both_states(self, editor):
        """Every index lands on exactly one side of the swap."""
        editor.selected = [0, 3]
        before = set(editor.selected)
        editor.invert_selection()
        after = set(editor.selected)
        assert before & after == set()
        assert before | after == set(range(len(editor.parts)))

    def test_it_does_not_touch_the_document(self, editor):
        """Selection is not an edit: nothing lands on the undo stack."""
        doc, depth = editor.doc, editor.undo_stack.count()
        editor.selected = [2]
        editor.invert_selection()
        assert editor.doc is doc
        assert editor.undo_stack.count() == depth

    def test_the_count_follows(self, editor):
        editor.selected = [0]
        editor.invert_selection()
        assert "3 selected" in editor.lbl_count.text()
        assert "3 selected" in editor.status.text()

    def test_the_button_does_what_the_method_does(self, editor, qtbot):
        from PySide6.QtCore import Qt
        editor.selected = [0, 2]
        qtbot.mouseClick(editor.btn_invert, Qt.MouseButton.LeftButton)
        assert _picked(editor) == [1, 3]


class TestWhenTheControlsAreOffered:
    """Invert is the one that stays live at both ends of the range."""

    def test_nothing_is_offered_without_a_province(self, blank):
        assert not blank.btn_invert.isEnabled()
        assert not blank.btn_select_all.isEnabled()
        assert not blank.btn_clear.isEnabled()

    def test_invert_is_live_with_an_empty_selection(self, editor):
        """Where None has nothing to clear, Invert still has work to do."""
        editor.clear_selection()
        assert editor.btn_invert.isEnabled()
        assert not editor.btn_clear.isEnabled()

    def test_invert_is_live_with_everything_selected(self, editor):
        editor.select_all()
        assert editor.btn_invert.isEnabled()

    def test_clear_wakes_up_only_once_something_is_picked(self, editor):
        editor.clear_selection()
        assert not editor.btn_clear.isEnabled()
        editor.invert_selection()
        assert editor.btn_clear.isEnabled()


class TestSelectionDrivesTheOperations:
    """The selection-sensitive buttons track what Invert leaves behind."""

    def test_merge_needs_two_and_invert_can_supply_them(self, editor):
        editor.selected = [0, 1]
        editor.invert_selection()           # -> [2, 3]
        editor._sync_buttons()
        assert editor.btn_merge.isEnabled()

    def test_inverting_a_full_selection_stands_the_operations_down(self, editor):
        editor.select_all()
        editor.invert_selection()
        assert not editor.btn_merge.isEnabled()
        assert not editor.btn_remove_sel.isEnabled()
