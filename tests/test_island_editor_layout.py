"""
test_island_editor_layout.py

Where the controls of UI_island_editor.py sit: three rows with the Scores panel
down the right of all three (FR-2.7).

These are pytest-style (not unittest) because they need pytest-qt's ``qtbot`` /
``qapp`` fixtures, as in test_island_editor_keys.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_layout.py -v

An arrangement is not decoration here. The rows answer, in order, three
questions a reader of the window asks: what document am I in, what is this edit
pointed at, and how does it reshape the land. A control that drifts into the
wrong row still works and quietly stops answering, which no other test would
notice -- so the grid positions and the group memberships are pinned here.
"""

import pytest
from shapely.geometry import box

from PySide6.QtWidgets import QGridLayout

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

#: Wide enough for a 1080p desktop, which is what three shorter rows bought
#: over two crowded ones (3198 px -> 2344 px -> 1848 px; see FR-2.7).
MAX_MINIMUM_WIDTH_PX = 1920


@pytest.fixture
def editor(qtbot):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    yield window
    window.undo_stack.clear()


def head(editor) -> QGridLayout:
    """The grid holding the three control rows and the scoreboard."""
    root = editor.centralWidget().layout()
    grids = [root.itemAt(i).layout() for i in range(root.count())
             if isinstance(root.itemAt(i).layout(), QGridLayout)]
    assert len(grids) == 1, "the controls live in exactly one grid"
    return grids[0]


def cell(editor, widget):
    """``(row, column, row span, column span)`` of whatever holds ``widget``."""
    grid = head(editor)
    for i in range(grid.count()):
        item = grid.itemAt(i)
        row = item.layout()
        if item.widget() is widget or (row is not None
                                       and row.indexOf(widget) >= 0):
            return grid.getItemPosition(i)
    raise AssertionError(f"{widget} is in no cell of the grid")


def row_layout(editor, widget):
    """The row that holds ``widget``, for reading its order left to right."""
    grid = head(editor)
    for i in range(grid.count()):
        row = grid.itemAt(i).layout()
        if row is not None and row.indexOf(widget) >= 0:
            return row
    raise AssertionError(f"{widget} is in no row of the grid")


class TestTheThreeRows:
    """Each row answers one question, so each control belongs to one."""

    @pytest.mark.parametrize("name, row", [
        # 1. the document and the view
        ("btn_import", 0), ("btn_save", 0), ("btn_undo", 0), ("btn_redo", 0),
        ("scale_spin", 0), ("btn_real", 0),
        # 2. what the edit is pointed at
        ("grp_selection", 1), ("grp_province", 1),
        # 3. how the land is reshaped
        ("grp_expansion", 2), ("grp_bridges", 2), ("grp_freehand", 2),
    ])
    def test_each_control_is_in_its_row(self, editor, name, row):
        assert cell(editor, getattr(editor, name))[:2] == (row, 0)

    def test_the_scoreboard_runs_down_the_right_of_all_three(self, editor):
        assert cell(editor, editor.grp_scores) == (0, 1, 3, 1)

    def test_the_controls_take_the_slack_and_the_scoreboard_does_not(self, editor):
        """So the panel keeps the width it needs and no more."""
        assert head(editor).columnStretch(0) > head(editor).columnStretch(1)

    @pytest.mark.parametrize("first, second", [
        ("grp_selection", "grp_province"),      # what you clicked, then everything
        ("grp_expansion", "grp_bridges"),       # the two merges, then the brush
        ("grp_bridges", "grp_freehand"),
    ])
    def test_the_groups_read_left_to_right_in_that_order(self, editor, first, second):
        row = row_layout(editor, getattr(editor, first))
        assert (row.indexOf(getattr(editor, first))
                < row.indexOf(getattr(editor, second)))


class TestTheGroups:
    """A group title names something true about every button inside it."""

    TITLES = {
        "grp_selection": "Selection",
        "grp_province": "Whole province",
        "grp_expansion": "Expansion Merge",
        "grp_bridges": "Bridge Merge",
        "grp_freehand": "Freehand",
        "grp_scores": "Scores",
    }

    @pytest.mark.parametrize("name, title", sorted(TITLES.items()))
    def test_each_group_is_titled(self, editor, name, title):
        assert getattr(editor, name).title() == title

    @pytest.mark.parametrize("group, members", [
        ("grp_selection", ["btn_select_all", "btn_clear", "btn_invert",
                           "btn_remove_sel", "lbl_count"]),
        ("grp_province", ["area_spin", "btn_remove_small", "width_spin",
                          "btn_amputate", "btn_lakes"]),
        ("grp_expansion", ["buffer_spin", "btn_gap", "btn_merge"]),
        ("grp_bridges", ["bridge_width_spin", "bridge_span_spin", "btn_bridge"]),
        ("grp_freehand", ["btn_draw", "btn_erase", "brush_spin"]),
    ])
    def test_each_group_holds_its_own(self, editor, group, members):
        box_layout = getattr(editor, group).layout()
        assert [m for m in members
                if box_layout.indexOf(getattr(editor, m)) < 0] == []

    def test_remove_sits_with_the_selection_not_with_the_merge(self, editor):
        """It is the end of the clicking gesture, and the one edit owning no
        parameter — so it owned nothing in a box of spin boxes."""
        assert editor.grp_selection.layout().indexOf(editor.btn_remove_sel) >= 0
        assert editor.grp_expansion.layout().indexOf(editor.btn_remove_sel) < 0


class TestTheWindowStillFitsAScreen:
    """The reason for three rows rather than two."""

    def test_the_minimum_width_fits_a_1080p_desktop(self, editor):
        minimum = editor.centralWidget().layout().minimumSize().width()
        assert minimum < MAX_MINIMUM_WIDTH_PX

    def test_the_map_takes_the_height_the_chrome_does_not(self, editor):
        """Rows are cheap only while the canvas keeps the stretch."""
        root = editor.centralWidget().layout()
        stretches = [root.stretch(i) for i in range(root.count())]
        assert stretches.count(0) == len(stretches) - 1
        assert root.stretch(root.indexOf(editor.canvas)) == 1
