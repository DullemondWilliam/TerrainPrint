"""
test_island_editor_layout.py

Where the controls of UI_island_editor.py sit: three rows with the Scores panel
down the right of all three, and the map's own controls under the map (FR-2.7).

These are pytest-style (not unittest) because they need pytest-qt's ``qtbot`` /
``qapp`` fixtures, as in test_island_editor_keys.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_layout.py -v

An arrangement is not decoration here. The rows answer, in order, three
questions a reader of the window asks: what document am I in, what is this edit
pointed at, and how does it reshape the land. A control that drifts into the
wrong row still works and quietly stops answering, which no other test would
notice -- so the grid positions and the group memberships are pinned here.

The other thing pinned here is that the arrangement **holds still**. A control
that moves while you are working is worse than one in the wrong place: the map
is clicked over and over, and anything that shifts between clicks is something
you can click by mistake.
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


def in_grid(editor, widget) -> bool:
    """Whether the control grid holds ``widget`` at all."""
    try:
        cell(editor, widget)
    except AssertionError:
        return False
    return True


def under_the_map(editor):
    """``(position in the window, the row)`` holding the map's own controls."""
    root = editor.centralWidget().layout()
    for i in range(root.count()):
        row = root.itemAt(i).layout()
        if row is not None and row.indexOf(editor.nav) >= 0:
            return i, row
    raise AssertionError("the navigation tools are in no row of the window")


def holds(group, widget) -> bool:
    """Whether ``group`` holds ``widget``, however many rows deep.

    A group of one row holds its controls directly; one of two (Scores, Stress)
    holds a row that holds them. The question is the same either way, so the
    walk is recursive rather than the layout being flattened to suit the test.
    """
    def walk(layout):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return True
            nested = item.layout()
            if nested is not None and walk(nested):
                return True
        return False

    return walk(group.layout())


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
        # 1. the document, and nothing else
        ("btn_import", 0), ("btn_save", 0), ("btn_undo", 0), ("btn_redo", 0),
        # 2. what the edit is pointed at
        ("grp_selection", 1), ("grp_province", 1),
        # 3. how the land is reshaped
        ("grp_expansion", 2), ("grp_bridges", 2), ("grp_freehand", 2),
    ])
    def test_each_control_is_in_its_row(self, editor, name, row):
        assert cell(editor, getattr(editor, name))[:2] == (row, 0)

    def test_the_scoreboard_runs_down_the_right_of_all_three(self, editor):
        assert cell(editor, editor.grp_scores) == (0, 1, 3, 1)

    def test_the_stress_layer_sits_in_that_same_column(self, editor):
        """The two standing measurements of the document, stacked: what the
        editing has cost, and what the printer will make of the result."""
        assert cell(editor, editor.grp_stress) == (0, 1, 3, 1)

    def test_the_scoreboard_comes_first_in_it(self, editor):
        column = row_layout(editor, editor.grp_scores)
        assert column.indexOf(editor.grp_scores) < column.indexOf(editor.grp_stress)

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
        "grp_stress": "Stress",
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
        ("grp_stress", ["btn_stress", "stress_spin", "btn_worst_neck"]),
    ])
    def test_each_group_holds_its_own(self, editor, group, members):
        box = getattr(editor, group)
        assert [m for m in members if not holds(box, getattr(editor, m))] == []

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


class TestUnderTheMap:
    """The view controls belong to the map, so they sit against it.

    They are not document controls and not edits: nothing here changes a
    geometry, it changes where the map is looked at from and how its sizes are
    to be read. Row 1 is for the document; this line is for the map.
    """

    @pytest.mark.parametrize("name", ["nav", "scale_spin", "perkm_box",
                                      "btn_real"])
    def test_each_view_control_is_down_there(self, editor, name):
        _, row = under_the_map(editor)
        assert row.indexOf(getattr(editor, name)) >= 0

    @pytest.mark.parametrize("name", ["nav", "scale_spin", "perkm_box",
                                      "btn_real"])
    def test_and_not_up_in_the_control_grid(self, editor, name):
        assert not in_grid(editor, getattr(editor, name))

    def test_the_row_comes_after_the_map(self, editor):
        root = editor.centralWidget().layout()
        assert under_the_map(editor)[0] > root.indexOf(editor.canvas)

    def test_the_status_keeps_the_left_and_the_controls_the_right(self, editor):
        """Reading starts at the left, and that is where the eye goes after an
        edit; the controls take the far end."""
        _, row = under_the_map(editor)
        assert row.indexOf(editor.status) == 0
        assert row.indexOf(editor.btn_real) == row.count() - 1

    def test_the_status_may_shrink_below_its_own_text(self, editor):
        """Otherwise a long report would set the window's minimum width from
        the bottom row."""
        from PySide6.QtWidgets import QSizePolicy
        assert (editor.status.sizePolicy().horizontalPolicy()
                == QSizePolicy.Policy.Ignored)
        assert editor.status.minimumWidth() == 0

    def test_it_costs_the_map_no_height(self, editor):
        """A line of its own would come straight off the canvas."""
        root = editor.centralWidget().layout()
        rows = [root.itemAt(i).layout() for i in range(root.count())]
        assert sum(1 for r in rows if r is not None
                   and r.indexOf(editor.status) >= 0) == 1
        assert under_the_map(editor)[1].indexOf(editor.status) >= 0


class TestTheControlsHoldStillWhileSelecting:
    """Clicking an island moves nothing but the island's colour.

    The selection count is the one label whose text changes on every click, and
    the widths do not match: "none selected" ran 20 px wider than "3 selected",
    so the Selection group breathed in and out and carried *Whole province*
    sideways with it -- 20 px, on the first click and again on the last, under
    a pointer that may have been on its way to a button there.
    """

    SELECTIONS = [[], [0], [0, 1], [0, 1, 2], list(range(12)),
                  list(range(223))]           # Nunavut, the worst real case

    def settle(self, editor, selection):
        """Put a selection on the window and let the layout answer for it."""
        editor.selected = list(selection)
        editor._sync_buttons()
        editor.centralWidget().layout().activate()

    @pytest.mark.parametrize("selection", SELECTIONS)
    def test_the_count_label_keeps_its_width(self, editor, selection):
        self.settle(editor, [])
        empty = editor.lbl_count.sizeHint().width()
        self.settle(editor, selection)
        assert editor.lbl_count.sizeHint().width() == empty

    @pytest.mark.parametrize("selection", SELECTIONS)
    def test_the_selection_group_keeps_its_width(self, editor, selection):
        self.settle(editor, [])
        empty = editor.grp_selection.sizeHint().width()
        self.settle(editor, selection)
        assert editor.grp_selection.sizeHint().width() == empty

    @pytest.mark.parametrize("selection", SELECTIONS)
    def test_whole_province_does_not_slide(self, editor, selection):
        """The group next along the row, which is what the movement was seen as."""
        self.settle(editor, [])
        where = editor.grp_province.x()
        self.settle(editor, selection)
        assert editor.grp_province.x() == where

    def test_the_label_does_say_the_count(self, editor):
        """A width held by saying nothing would pass the tests above."""
        self.settle(editor, [0, 1])
        assert editor.lbl_count.text() == "2 selected"
        self.settle(editor, [])
        assert editor.lbl_count.text() == "none selected"
