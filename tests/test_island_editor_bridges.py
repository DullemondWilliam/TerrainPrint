"""
test_island_editor_bridges.py

The **Bridge** action of UI_island_editor.py — straight bridges of a fixed
printed width across the selected islands.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_selection.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_bridges.py -v

The geometry and the spanning tree are covered by test_island_bridges.py and
nothing here re-tests them. What this file guards is the editor asking the lib
the right question about *this* document:

* the islands that were **not** selected are handed over as obstacles, so a
  bridge is never run across land that is still on the map — the one thing the
  lib cannot know from the selection alone;
* the slabs are unioned onto the whole document, not onto the selection, so
  unselected islands survive the edit;
* the result is one undoable step, like every other edit here;
* and when nothing can be placed, the reason names the control that would
  change it — the three causes want opposite fixes.
"""

import time

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from PySide6.QtWidgets import QApplication

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

MM = M.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(M.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(M.QMessageBox, "warning", lambda *a, **k: None)


def square(x_mm, y_mm, size_mm=4.0):
    return box(x_mm * MM, y_mm * MM,
               (x_mm + size_mm) * MM, (y_mm + size_mm) * MM)


def _editor(qtbot, islands, selected=()):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window._set_state(M._as_multi(unary_union(list(islands))),
                      list(selected), preserve_view=False)
    return window


def _ordered(window):
    """Part indices left to right, so a test can name the islands it built."""
    return sorted(range(len(window.parts)),
                  key=lambda i: window.parts[i].bounds[0])


def _bridge(window, timeout=180.0):
    """Press Bridge and wait for the planning thread to finish.

    Planning runs off the UI thread — 8 s on a whole-province selection in
    British Columbia, 102 s in Nunavut — so the tests drive the real path
    rather than calling the lib themselves: the wiring is most of what is worth
    testing here.
    """
    window.bridge_selected()
    deadline = time.monotonic() + timeout
    while window._bridge_thread is not None and time.monotonic() < deadline:
        QApplication.processEvents()
    assert window._bridge_thread is None, "planning never finished"


def _select(window, *positions):
    order = _ordered(window)
    window.selected = [order[i] for i in positions]
    window._sync_buttons()


@pytest.fixture
def chain(qtbot):
    """Three islands in a row 2 mm apart, and a fourth far out of reach."""
    return _editor(qtbot, [square(0.0, 0.0), square(6.0, 0.0),
                           square(12.0, 0.0), square(60.0, 0.0)])


class TestBridgingTheSelection:

    def test_it_needs_two_islands(self, chain):
        assert not chain.btn_bridge.isEnabled()
        _select(chain, 0)
        assert not chain.btn_bridge.isEnabled()
        _select(chain, 0, 1)
        assert chain.btn_bridge.isEnabled()

    def test_bridging_nothing_does_nothing(self, chain):
        before = chain.doc
        _bridge(chain)
        assert chain.doc is before

    def test_the_selected_islands_become_one_landmass(self, chain):
        _select(chain, 0, 1, 2)
        _bridge(chain)
        # Three bridged into one, and the far island still on its own.
        assert len(chain.parts) == 2

    def test_one_bridge_per_join_not_one_per_pair(self, chain):
        """The spanning tree, seen through the status line.

        Three islands in a row have three pairs within reach; joining them
        costs two bridges, and a harness that bridged every pair would say
        three while looking much the same on the map.
        """
        _select(chain, 0, 1, 2)
        _bridge(chain)
        assert "Placed 2 bridge(s)" in chain.status.text()

    def test_unselected_islands_are_left_alone(self, chain):
        far = next(p for p in chain.parts if p.bounds[0] > 50 * MM)
        _select(chain, 0, 1)
        _bridge(chain)
        survivor = [p for p in chain.parts if p.bounds[0] > 50 * MM]
        assert len(survivor) == 1
        assert survivor[0].equals(far)

    def test_it_is_one_undoable_step(self, chain):
        _select(chain, 0, 1, 2)
        before = len(chain.parts)
        _bridge(chain)
        assert chain.undo_stack.undoText().startswith("Bridge 3 islands")
        chain.undo_stack.undo()
        assert len(chain.parts) == before

    def test_the_bridged_landmass_stays_selected(self, chain):
        _select(chain, 0, 1, 2)
        _bridge(chain)
        # The three became one, so the selection lands on that one part.
        assert len(chain.selected) == 1
        joined = chain.parts[chain.selected[0]]
        assert joined.bounds[0] == pytest.approx(0.0, abs=1.0)
        assert joined.bounds[2] == pytest.approx(16.0 * MM, abs=1.0)


class TestUnselectedIslandsAreObstacles:
    """The reason the editor cannot simply hand the lib its selection."""

    @pytest.fixture
    def strait(self, qtbot):
        """Two islands 2 mm apart with an islet in the water between them."""
        return _editor(qtbot, [square(0.0, 0.0), square(6.0, 0.0),
                               box(4.5 * MM, 0, 5.5 * MM, 4 * MM)])

    def test_a_bridge_is_not_run_across_an_unselected_island(self, strait):
        _select(strait, 0, 2)          # the two outer islands, not the islet
        before = len(strait.parts)
        _bridge(strait)
        assert len(strait.parts) == before       # refused, nothing changed

    def test_and_it_says_what_is_in_the_way(self, strait):
        _select(strait, 0, 2)
        _bridge(strait)
        assert "Another island lies across" in strait.status.text()

    def test_selecting_the_islet_too_bridges_the_lot(self, strait):
        _select(strait, 0, 1, 2)
        _bridge(strait)
        assert len(strait.parts) == 1

    def test_the_obstacle_is_not_counted_as_something_to_join(self, strait):
        _select(strait, 0, 1, 2)
        _bridge(strait)
        assert "3 → 1 landmass(es)" in strait.status.text()


class TestABridgeStopsAtTheCoast:
    """Bridge onto a thin islet and no land appears on its far side.

    The shape itself is the lib's business and is pinned there
    (test_island_bridges.py, ``TestTheEmbeddedEnds``).  This asks it the way
    the editor does -- a whole document in, a whole document out -- because
    that is where it shows: you bridge to an islet, and a tab of land is left
    standing in the water behind it, past a coast the crossing had already
    reached.

    0.6 mm is 4.8 km at the project's 1:8,000,000, so the islet here is an
    entirely ordinary one, and the default 1 mm embedment is 8 km.
    """

    @pytest.fixture
    def islet(self, qtbot):
        """A 4 mm island and, 2 mm east, an islet 0.6 mm thick."""
        return _editor(qtbot, [square(0.0, 0.0),
                               box(6.0 * MM, 0, 6.6 * MM, 4.0 * MM)])

    def test_nothing_is_laid_down_past_the_far_coast(self, islet):
        _select(islet, 0, 1)
        _bridge(islet)
        assert islet.doc.bounds[2] <= 6.6 * MM + 1e-6

    def test_the_islet_is_joined_all_the_same(self, islet):
        _select(islet, 0, 1)
        _bridge(islet)
        assert len(islet.parts) == 1
        assert "2 → 1 landmass(es)" in islet.status.text()

    def test_the_document_gains_the_crossing_and_nothing_else(self, islet):
        before = islet.doc.area
        _select(islet, 0, 1)
        _bridge(islet)
        crossing = islet.bridge_width_spin.value() * 2.0        # width x span
        assert (islet.doc.area - before) / MM / MM == pytest.approx(crossing,
                                                                    abs=1e-3)


class TestWhenNothingCanBePlaced:
    """Three causes, three different fixes — so three different sentences."""

    def test_out_of_reach_points_at_max_span(self, chain):
        _select(chain, 0, 3)
        _bridge(chain)
        assert "within Max span" in chain.status.text()

    def test_raising_max_span_then_bridges_them(self, chain):
        # The last two: 44 mm of open water, and nothing in between to cross.
        _select(chain, 2, 3)
        _bridge(chain)
        assert "within Max span" in chain.status.text()
        chain.bridge_span_spin.setValue(80.0)
        _bridge(chain)
        assert "Placed 1 bridge(s)" in chain.status.text()

    def test_reaching_past_an_unselected_island_is_still_refused(self, chain):
        """Max span is not the only thing between two distant islands.

        Selecting the two ends of the chain and giving them all the span they
        need still leaves islands 1 and 2 sitting in the way — and a bridge
        over them would weld four landmasses while claiming to join two.
        """
        _select(chain, 0, 3)
        chain.bridge_span_spin.setValue(80.0)
        before = chain.doc
        _bridge(chain)
        assert "Another island lies across" in chain.status.text()
        assert chain.doc is before

    def test_an_island_too_small_to_hold_one_points_at_the_width(self, qtbot):
        window = _editor(qtbot, [square(0.0, 0.0),
                                 box(6 * MM, 1.9 * MM, 6.2 * MM, 2.1 * MM)])
        _select(window, 0, 1)
        _bridge(window)
        assert "try a narrower one" in window.status.text()

    def test_a_refusal_leaves_the_document_alone(self, chain):
        _select(chain, 0, 3)
        before = chain.doc
        _bridge(chain)
        assert chain.doc is before
        assert not chain.undo_stack.canUndo()


class TestTheControlsReachTheLib:

    def test_width_is_the_width_the_bridge_comes_out(self, qtbot):
        added = {}
        for width_mm in (0.5, 2.0):
            window = _editor(qtbot, [square(0.0, 0.0), square(6.0, 0.0)])
            before = window.doc.area
            _select(window, 0, 1)
            window.bridge_width_spin.setValue(width_mm)
            _bridge(window)
            added[width_mm] = window.doc.area - before
        # 2 mm of bridge over the same 2 mm of water is four times the land.
        assert added[2.0] == pytest.approx(added[0.5] * 4.0, rel=0.01)

    def test_the_width_is_printed_millimetres(self, qtbot):
        """Halving the scale halves what one millimetre is worth on the ground.

        This is the editor's whole convention — the boxes state what comes off
        the printer — and a bridge built in raw metres would ignore it.
        """
        added = {}
        for scale in (M.DEFAULT_PRINT_SCALE, M.DEFAULT_PRINT_SCALE // 2):
            window = _editor(qtbot, [square(0.0, 0.0), square(6.0, 0.0)])
            window.scale_spin.setValue(scale)
            before = window.doc.area
            _select(window, 0, 1)
            _bridge(window)
            added[scale] = window.doc.area - before
        assert added[M.DEFAULT_PRINT_SCALE] == pytest.approx(
            added[M.DEFAULT_PRINT_SCALE // 2] * 2.0, rel=0.01)

    def test_max_span_decides_what_is_left_separate(self, chain):
        _select(chain, 0, 1, 2, 3)
        chain.bridge_span_spin.setValue(5.0)
        _bridge(chain)
        assert "1 still out of reach" in chain.status.text()

    def test_the_parameters_default_to_the_libs_own(self, chain):
        from lib.LIB_island_bridges import BridgeParams
        shown, default = chain._bridge_params(), BridgeParams()
        assert shown.width_mm == pytest.approx(default.width_mm)
        assert shown.max_span_mm == pytest.approx(default.max_span_mm)
        assert shown.scale == chain.scale_spin.value()
        # Everything not on the toolbar is left at the project default.
        assert shown.min_anchor == pytest.approx(default.min_anchor)
        assert shown.hinge_ratio == pytest.approx(default.hinge_ratio)


class TestPlanningIsNotOnTheUiThread:
    """A 102-second freeze reads as a hang, so the plan runs off the thread."""

    def test_the_editing_controls_lock_while_it_plans(self, chain):
        chain._set_planning(True)
        assert chain.progress.isVisible()
        for group in (chain.grp_selection, chain.grp_expansion,
                      chain.grp_bridges, chain.grp_province,
                      chain.grp_freehand):
            assert not group.isEnabled()
        # Anything that could change the document under the worker, too.
        for button in (chain.btn_import, chain.btn_save, chain.btn_undo,
                       chain.btn_redo):
            assert not button.isEnabled()

    def test_the_view_controls_do_not(self, chain):
        """Panning and zooming while it thinks is the point of threading it."""
        chain._set_planning(True)
        assert chain.nav.isEnabled()
        assert chain.scale_spin.isEnabled()

    def test_the_controls_come_back_afterwards(self, chain):
        _select(chain, 0, 1)
        chain._set_planning(True)
        chain._set_planning(False)
        assert not chain.progress.isVisible()
        assert chain.btn_import.isEnabled()
        assert chain.btn_bridge.isEnabled()          # two are still selected

    def test_a_second_press_is_ignored_while_one_is_running(self, chain):
        _select(chain, 0, 1, 2)
        chain._set_planning(True)
        chain._bridge_thread = object()              # pretend one is in flight
        before = chain.doc
        chain.bridge_selected()
        assert chain.doc is before
        chain._bridge_thread = None
        chain._set_planning(False)

    def test_a_brush_mode_is_left_before_planning(self, chain):
        """A stroke aimed at the map as it was would land on the map as it is."""
        chain._set_brush_mode(M.DRAW)
        assert chain._brush_mode == M.DRAW
        chain._set_planning(True)
        assert chain._brush_mode is None
        chain._set_planning(False)

    def test_a_failure_unlocks_and_reports(self, chain):
        chain._set_planning(True)
        chain._on_bridge_failed("boom")
        assert chain._bridge_thread is None
        assert not chain.progress.isVisible()
        assert chain.btn_import.isEnabled()
        assert "failed" in chain.status.text()
