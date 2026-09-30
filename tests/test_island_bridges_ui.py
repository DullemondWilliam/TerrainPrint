"""
test_island_bridges_ui.py

harnesses/UI_island_bridges.py: straight fixed-width bridges between islands.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp``
fixture, as in test_shared_border_ui.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_bridges_ui.py -v

The geometry is covered by test_island_bridges.py and nothing here re-tests it.
What this file guards is the harness being an honest front end for the lib:

* **Every control reaches the lib.**  A spin box wired to nothing is worse than
  no spin box, because the picture then answers a question other than the one
  on screen.  ``TestControlsReachTheLib`` moves each control in turn and insists
  the parameter block moves with it.
* **A stale plan is never left on screen** next to parameters that no longer
  produced it.
* **The map stays where you put it.**  Every redraw clears the axes, so the
  view has to be put back, and on an equal-aspect panel it can only be put back
  both limits at once.  ``TestTheViewSurvivesARedraw`` zooms in and then does
  each of the things that redraw.
* **The report is the plan's own numbers**, not a second count that could drift
  away from what was actually placed.
* **What is drawn is the plan's own numbers too.**  The anchor marks are read
  off ``Bridge.line``, so they are the landings the scorer actually chose; a
  mark drawn from anything else would be a second opinion about where the
  bridge went.
* **A selection asks a different question of the lib**, and the harness has to
  ask all three parts of it: bridge *these*, avoid *those*, and union the slabs
  onto the whole map.  Getting the third wrong loses every island left out of
  the job, and the picture would not show it — the panel would simply frame
  what survived.
"""

import types

import geopandas as gpd
import pytest
from shapely.geometry import box

from harnesses import UI_island_bridges as U

pytestmark = pytest.mark.usefixtures("qapp")

WORK_CRS = "EPSG:3347"
SCALE = 8_000_000.0
MM = SCALE / 1000.0                    # one print-mm, in metres
ORIGIN = (7_000_000.0, 2_000_000.0)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "warning", lambda *a, **k: None)


def _square(x_mm, y_mm, size_mm=4.0):
    ox, oy = ORIGIN
    return box(ox + x_mm * MM, oy + y_mm * MM,
               ox + (x_mm + size_mm) * MM, oy + (y_mm + size_mm) * MM)


def _loaded(islands=None):
    """A window with islands in state, as if imported (no file dialogs)."""
    window = U.IslandBridgesTester()
    if islands is None:
        islands = [_square(i * 6.0, 0.0) for i in range(3)]
    window.islands = gpd.GeoDataFrame(geometry=list(islands), crs=WORK_CRS)
    window.source_crs = WORK_CRS
    window.btn_run.setEnabled(True)
    window.btn_select_all.setEnabled(True)
    return window


def _click(window, x, y, ax=None, button=1):
    """A left click at a point on the plan panel.

    A stand-in for matplotlib's MouseEvent, carrying the four fields the
    handler reads. The handler is the harness's, and matplotlib delivering
    events is matplotlib's business.
    """
    return window._on_click(types.SimpleNamespace(
        inaxes=window.ax_before if ax is None else ax,
        xdata=x, ydata=y, button=button))


def _click_island(window, index, **kw):
    point = window.islands.geometry[index].representative_point()
    return _click(window, point.x, point.y, **kw)


def _plan(window):
    """Run the lib synchronously, mirroring ``PlanWorker.run``.

    Through ``_plan_inputs`` for the same reason the worker goes through it:
    the split into bridge / avoid / union-onto is the harness's answer to the
    selection, and a test that made up its own would not be testing it.
    """
    from lib.LIB_island_bridges import apply_bridges, plan_bridges
    chosen, everything, obstacles = window._plan_inputs()
    plan = plan_bridges(chosen, window.params(), obstacles=obstacles)
    geom = apply_bridges(everything, plan,
                         keep_lagoons=window.lagoon_check.isChecked())
    window._on_done(plan, geom)
    return plan


#: Every control on the window, with the parameter it must drive and a value
#: that differs from the default.  Adding a knob to the harness without adding
#: it here leaves it untested, which is the failure this table exists to make
#: obvious.
CONTROLS = [
    ("scale_spin", "scale", 4_000_000.0),
    ("width_spin", "width_mm", 2.5),
    ("span_spin", "max_span_mm", 9.0),
    ("aspect_spin", "max_aspect", 7.0),
    ("sample_spin", "sample_mm", 0.05),
    ("angle_spin", "angle_penalty_mm", 3.5),
    ("anchor_pen_spin", "anchor_penalty_mm", 0.5),
    ("anchor_spin", "min_anchor", 0.25),
    ("per_pair_spin", "max_per_pair", 3),
    ("hinge_spin", "hinge_ratio", 9.0),
]


class TestControlsReachTheLib:
    """Each knob drives the parameter it claims to."""

    @pytest.mark.parametrize("control,field,value", CONTROLS)
    def test_control_drives_its_parameter(self, control, field, value):
        window = _loaded()
        getattr(window, control).setValue(value)
        assert getattr(window.params(), field) == pytest.approx(value)

    def test_the_slender_checkbox_drives_its_flag(self):
        window = _loaded()
        assert window.params().drop_slender is False
        window.drop_slender_check.setChecked(True)
        assert window.params().drop_slender is True

    def test_defaults_match_the_libs_own(self):
        """An untouched harness must plan what the lib would plan unasked."""
        from lib.LIB_island_bridges import BridgeParams
        window = _loaded()
        shown, default = window.params(), BridgeParams()
        for field, _ in ((f, None) for f in (
                "scale", "width_mm", "max_span_mm", "sample_mm",
                "angle_penalty_mm", "anchor_penalty_mm", "min_anchor",
                "max_aspect", "max_per_pair", "hinge_ratio", "drop_slender")):
            assert getattr(shown, field) == pytest.approx(
                getattr(default, field)), field

    def test_width_actually_changes_the_bridge_that_is_built(self):
        """The wiring is only worth anything if the geometry follows it."""
        window = _loaded()
        window.width_spin.setValue(0.5)
        thin = _plan(window).bridges[0]
        window.width_spin.setValue(2.0)
        thick = _plan(window).bridges[0]
        assert thin.width_mm == pytest.approx(0.5)
        assert thick.width_mm == pytest.approx(2.0)
        assert thick.polygon.area > thin.polygon.area * 3

    def test_max_span_actually_changes_what_is_bridged(self):
        window = _loaded([_square(0.0, 0.0), _square(10.0, 0.0)])   # 6 mm apart
        window.span_spin.setValue(5.0)
        assert _plan(window).bridges == []
        window.span_spin.setValue(8.0)
        assert len(_plan(window).bridges) == 1


class TestStaleResults:
    """A plan on screen always belongs to the parameters on screen."""

    def test_a_plan_enables_saving_and_zooming(self):
        window = _loaded()
        _plan(window)
        assert window.btn_save.isEnabled()
        assert window.btn_zoom.isEnabled()

    @pytest.mark.parametrize("control,field,value", CONTROLS)
    def test_changing_a_parameter_drops_the_plan(self, control, field, value):
        window = _loaded()
        _plan(window)
        getattr(window, control).setValue(value)
        assert window.plan is None
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert not window.btn_zoom.isEnabled()

    def test_the_slender_checkbox_drops_the_plan_too(self):
        window = _loaded()
        _plan(window)
        window.drop_slender_check.setChecked(True)
        assert window.plan is None

    def test_the_stale_panel_says_so(self):
        window = _loaded()
        _plan(window)
        window.width_spin.setValue(2.0)
        assert "not yet planned" in window.ax_after.get_title()

    def test_importing_again_clears_the_previous_plan(self, monkeypatch,
                                                      tmp_path):
        window = _loaded()
        _plan(window)
        path = tmp_path / "islands.geojson"
        gpd.GeoDataFrame(geometry=[_square(0.0, 0.0), _square(6.0, 0.0)],
                         crs=WORK_CRS).to_file(path, driver="GeoJSON")
        monkeypatch.setattr(U.QFileDialog, "getOpenFileNames",
                            lambda *a, **k: ([str(path)], ""))
        window.import_geojson()
        assert window.plan is None
        assert window.result is None
        assert not window.btn_save.isEnabled()


class TestLagoons:
    """The lagoon switch re-applies the plan; it does not re-plan it."""

    def bars(self):
        ox, oy = ORIGIN
        return [box(ox, oy, ox + 40 * MM, oy + 4 * MM),
                box(ox, oy + 6 * MM, ox + 40 * MM, oy + 10 * MM)]

    def test_toggling_keeps_the_same_plan_object(self):
        window = _loaded(self.bars())
        plan = _plan(window)
        window.lagoon_check.setChecked(False)
        assert window.plan is plan          # re-applied, not re-planned
        assert window.result is not None

    def test_toggling_changes_the_geometry(self):
        from lib.LIB_island_merging import _polygons
        window = _loaded(self.bars())
        _plan(window)
        assert len(window.plan.bridges) == 2
        kept = sum(len(p.interiors) for p in _polygons(window.result))
        window.lagoon_check.setChecked(False)
        filled = sum(len(p.interiors) for p in _polygons(window.result))
        assert kept == 1 and filled == 0

    def test_toggling_before_a_plan_does_nothing(self):
        window = _loaded()
        window.lagoon_check.setChecked(False)
        assert window.plan is None and window.result is None


class TestTheReport:
    """The status line quotes the plan rather than counting again."""

    def test_the_bridge_count_is_the_plans(self):
        window = _loaded()
        plan = _plan(window)
        assert f"{len(plan.bridges)} bridge(s)" in window.status.text()

    def test_unbridged_islands_are_reported(self):
        window = _loaded([_square(0.0, 0.0), _square(6.0, 0.0),
                          _square(100.0, 0.0)])
        plan = _plan(window)
        assert plan.unreachable
        assert "still separate" in window.status.text()

    def test_a_slender_bridge_is_called_out_with_its_fix(self):
        window = _loaded([_square(0.0, 0.0), _square(16.0, 0.0)])
        window.span_spin.setValue(20.0)
        window.aspect_spin.setValue(10.0)
        plan = _plan(window)
        assert plan.slender
        assert "slender" in window.status.text()
        assert "widen" in window.status.text()

    def test_planning_nothing_explains_itself(self):
        window = _loaded([_square(0.0, 0.0), _square(100.0, 0.0)])
        plan = _plan(window)
        assert plan.bridges == []
        assert "No bridges" in window.status.text()
        assert not window.btn_zoom.isEnabled()

    def test_the_result_panel_counts_the_landmasses(self):
        from lib.LIB_island_merging import _polygons
        window = _loaded()
        _plan(window)
        parts = len(_polygons(window.result))
        assert f"{parts} landmass" in window.ax_after.get_title()

    def test_the_plan_panel_counts_the_bridges(self):
        window = _loaded()
        plan = _plan(window)
        assert f"{len(plan.bridges)} bridge" in window.ax_before.get_title()


class TestSelecting:
    """Clicking an island is how the job is narrowed."""

    def test_nothing_is_selected_to_begin_with(self):
        window = _loaded()
        assert window.selected == []
        assert window.lbl_selection.text() == "none selected"

    def test_a_click_selects_the_island_under_it(self):
        window = _loaded()
        _click_island(window, 1)
        assert window.selected == [1]
        assert window.lbl_selection.text() == "1 selected"

    def test_a_second_click_deselects_it(self):
        window = _loaded()
        _click_island(window, 1)
        _click_island(window, 1)
        assert window.selected == []

    def test_a_click_on_open_water_changes_nothing(self):
        window = _loaded()
        _click_island(window, 0)
        _click(window, ORIGIN[0] + 5.0 * MM, ORIGIN[1] + 30.0 * MM)
        assert window.selected == [0]

    def test_the_result_panel_is_not_a_place_to_click(self):
        """It is the answer, not the question."""
        window = _loaded()
        point = window.islands.geometry[0].representative_point()
        _click(window, point.x, point.y, ax=window.ax_after)
        assert window.selected == []

    def test_the_nav_toolbar_keeps_its_drag(self):
        """Pan and zoom-to-rectangle start with this very event."""
        window = _loaded()
        window.mpl_toolbar.mode = "pan/zoom"
        _click_island(window, 0)
        assert window.selected == []

    def test_only_the_left_button_selects(self):
        window = _loaded()
        _click_island(window, 0, button=3)
        assert window.selected == []

    def test_clicking_while_it_plans_is_ignored(self):
        """The controls lock during a plan; the map has to lock with them."""
        window = _loaded()
        window._thread = object()
        try:
            _click_island(window, 0)
        finally:
            window._thread = None
        assert window.selected == []

    def test_the_count_label_does_not_change_width(self):
        """It says a different thing on every click, and must take the same
        room saying it: anything placed to its right would be dragged across
        the toolbar each time the selection emptied or stopped being empty."""
        window = _loaded()
        empty = window.lbl_selection.sizeHint().width()
        for selection in ([0], [0, 1], [0, 1, 2]):
            window.selected = list(selection)
            window._selection_changed()
            assert window.lbl_selection.sizeHint().width() == empty
        assert window.lbl_selection.text() == "3 selected"

    def test_select_all_and_clear_are_the_two_ends_of_it(self):
        window = _loaded()
        window.select_all()
        assert window.selected == [0, 1, 2]
        assert window.btn_clear_sel.isEnabled()
        window.clear_selection()
        assert window.selected == []
        assert not window.btn_clear_sel.isEnabled()

    def test_changing_the_selection_drops_the_plan(self):
        """It is a different question, so the answer on screen is stale."""
        window = _loaded()
        _plan(window)
        assert window.plan is not None
        _click_island(window, 0)
        assert window.plan is None
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert "not yet planned" in window.ax_after.get_title()
        assert "Plan Bridges" in window.status.text()

    def test_importing_again_clears_the_selection(self, monkeypatch, tmp_path):
        # The indices meant the old islands; keeping them would point at land
        # that is no longer there.
        window = _loaded()
        window.select_all()
        path = tmp_path / "one.geojson"
        gpd.GeoDataFrame(geometry=[_square(0.0, 0.0)],
                         crs=WORK_CRS).to_file(path, driver="GeoJSON")
        monkeypatch.setattr(U.QFileDialog, "getOpenFileNames",
                            lambda *a, **k: ([str(path)], ""))
        window.import_geojson()
        assert window.selected == []
        assert window.lbl_selection.text() == "none selected"


class TestTheSelectionReachesTheLib:
    """Three lists, and the harness has to get all three right."""

    def test_nothing_selected_is_the_whole_map(self):
        window = _loaded()
        chosen, everything, obstacles = window._plan_inputs()
        assert len(chosen) == len(everything) == 3
        assert obstacles == []

    def test_a_selection_splits_them(self):
        window = _loaded()
        window.selected = [0, 2]
        chosen, everything, obstacles = window._plan_inputs()
        assert len(chosen) == 2
        assert len(everything) == 3
        assert len(obstacles) == 1
        assert obstacles[0].equals(window.islands.geometry[1])

    def test_an_unselected_island_is_an_obstacle_not_a_node(self):
        # Two islands 2 mm apart with an islet in the water between them: the
        # crossing is refused, exactly as the editor refuses it.
        islet = box(ORIGIN[0] + 4.5 * MM, ORIGIN[1],
                    ORIGIN[0] + 5.5 * MM, ORIGIN[1] + 4.0 * MM)
        window = _loaded([_square(0.0, 0.0), _square(6.0, 0.0), islet])
        window.selected = [0, 1]
        plan = _plan(window)
        assert plan.bridges == []
        assert plan.pairs_blocked == 1
        assert plan.islands == 2          # the islet was never a node

    def test_selecting_the_islet_too_lets_them_hop_through_it(self):
        islet = box(ORIGIN[0] + 4.5 * MM, ORIGIN[1],
                    ORIGIN[0] + 5.5 * MM, ORIGIN[1] + 4.0 * MM)
        window = _loaded([_square(0.0, 0.0), _square(6.0, 0.0), islet])
        window.per_pair_spin.setValue(1)        # no braces to count past
        window.select_all()
        plan = _plan(window)
        assert len(plan.bridges) == 2
        assert plan.components_after == 1

    def test_the_islands_left_out_survive_the_result(self):
        """The slabs go onto the whole map, not onto the selection -- and this
        is the one that would go unnoticed, since the panel simply frames
        whatever is left."""
        window = _loaded()
        window.selected = [0, 1]
        _plan(window)
        from lib.LIB_island_merging import _polygons
        assert len(_polygons(window.result)) == 2      # the bridged pair, and
        assert window.result.contains(                 # the one left out
            window.islands.geometry[2].representative_point())

    def test_the_lagoon_toggle_still_reapplies_over_everything(self):
        window = _loaded()
        window.selected = [0, 1]
        _plan(window)
        window.lagoon_check.setChecked(False)
        from lib.LIB_island_merging import _polygons
        assert len(_polygons(window.result)) == 2

    def test_one_island_selected_is_refused_before_launching(self):
        window = _loaded()
        window.selected = [1]
        window.run()
        assert window._thread is None
        assert window.plan is None

    def test_the_report_says_how_many_were_held_back(self):
        window = _loaded()
        window.selected = [0, 1]
        _plan(window)
        assert "2 of 3 island(s) bridged, 1 held as obstacles" in \
            window.status.text()

    def test_a_blocked_plan_names_the_way_out(self):
        islet = box(ORIGIN[0] + 4.5 * MM, ORIGIN[1],
                    ORIGIN[0] + 5.5 * MM, ORIGIN[1] + 4.0 * MM)
        window = _loaded([_square(0.0, 0.0), _square(6.0, 0.0), islet])
        window.selected = [0, 1]
        _plan(window)
        assert "another island lies across" in window.status.text().lower()
        assert "Select the islands in the way" in window.status.text()


class TestTheAnchorPoints:
    """Both ends of every bridge are marked: where it was tied to each coast.

    The span is legible from the picture already; *where along the coastline*
    the crossing was put is not, and it is what the anchorage floor and the
    angle penalty are choosing between. So the marks have to come off the same
    line the scorer measured -- ``Bridge.line`` is the crossing coast to coast,
    and its two ends are the landings.
    """

    def marks(self, window):
        """The plotted anchor points, as a set of rounded (x, y)."""
        assert len(window.ax_before.lines) <= 1, "one artist for all of them"
        if not window.ax_before.lines:
            return set()
        return {(round(x, 3), round(y, 3))
                for x, y in window.ax_before.lines[0].get_xydata()}

    def landings(self, plan):
        return {(round(x, 3), round(y, 3))
                for bridge in plan.bridges for x, y in bridge.line.coords}

    def test_they_are_shown_by_default(self):
        window = _loaded()
        assert window.anchors_check.isChecked()

    def test_every_landing_is_marked(self):
        window = _loaded()
        plan = _plan(window)
        assert len(plan.bridges) == 2
        assert self.marks(window) == self.landings(plan)
        assert len(self.marks(window)) == 2 * len(plan.bridges)

    def test_a_brace_is_marked_like_any_other_bridge(self):
        # Two long facing coasts: the pair is braced, and the second bridge has
        # landings of its own worth seeing.
        window = _loaded([_square(0.0, 0.0, 12.0), _square(14.0, 0.0, 12.0)])
        plan = _plan(window)
        assert plan.braces, "the fixture is meant to provoke a brace"
        assert self.marks(window) == self.landings(plan)

    def test_turning_them_off_takes_them_away(self):
        window = _loaded()
        _plan(window)
        window.anchors_check.setChecked(False)
        assert self.marks(window) == set()

    def test_and_back_on_brings_them_back(self):
        window = _loaded()
        plan = _plan(window)
        window.anchors_check.setChecked(False)
        window.anchors_check.setChecked(True)
        assert self.marks(window) == self.landings(plan)

    def test_toggling_them_does_not_touch_the_plan(self):
        """A drawing option, not a parameter: it must not invalidate anything."""
        window = _loaded()
        plan = _plan(window)
        result = window.result
        window.anchors_check.setChecked(False)
        assert window.plan is plan
        assert window.result is result
        assert window.btn_save.isEnabled()

    def test_nothing_is_marked_before_there_is_a_plan(self):
        window = _loaded()
        window._draw_plan()
        assert self.marks(window) == set()

    def test_they_survive_a_redraw_that_keeps_the_view(self):
        """Every pan and zoom goes through a redraw; the marks are not a
        one-off decoration of the draw that placed them."""
        window = _loaded()
        plan = _plan(window)
        window._draw_plan(preserve_view=True)
        assert self.marks(window) == self.landings(plan)


class TestFraming:
    """A 1 mm bridge is invisible at province extent."""

    def test_zoom_to_bridges_frames_them(self):
        window = _loaded()
        plan = _plan(window)
        window.zoom_to_bridges()
        x0, x1 = window.ax_before.get_xlim()
        y0, y1 = window.ax_before.get_ylim()
        for bridge in plan.bridges:
            bx0, by0, bx1, by1 = bridge.polygon.bounds
            assert x0 <= bx0 and bx1 <= x1
            assert y0 <= by0 and by1 <= y1

    def test_the_view_is_tighter_than_the_islands(self):
        window = _loaded()
        _plan(window)
        window.ax_before.autoscale()
        wide = window.ax_before.get_xlim()
        window.zoom_to_bridges()
        near = window.ax_before.get_xlim()
        assert (near[1] - near[0]) < (wide[1] - wide[0])

    def test_zooming_without_a_plan_is_harmless(self):
        window = _loaded()
        window.zoom_to_bridges()        # must not raise


class TestTheViewSurvivesARedraw:
    """Zoom in, and the map stays where you put it.

    Clearing the axes is how every redraw here starts, so each one has to put
    the view back afterwards -- and on axes with an equal aspect and
    ``adjustable="datalim"`` that can only be done both limits at once, through
    ``_set_view``. Set x on its own and the limit callback fires at once,
    settles the aspect against a y still holding the extent the redraw
    autoscaled to, and the zoom is gone before y is reached.

    Selecting an island was simply the redraw that happens often enough for
    anyone to notice; the same fault was under every parameter change.
    """

    @staticmethod
    def span(ax):
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        return (x1 - x0, y1 - y0)

    def zoomed(self, window):
        """Zoom the plan panel right in, and return the view that landed.

        The view that landed, not the one asked for: the aspect widens
        whichever direction is short of the panel's own shape, so the panel
        itself has the last word on the frame.
        """
        minx, miny, maxx, maxy = window.islands.total_bounds
        window._set_view(window.ax_before,
                         (minx, minx + (maxx - minx) / 4.0),
                         (miny, miny + (maxy - miny) / 4.0))
        view = self.span(window.ax_before)
        assert view[0] < (maxx - minx), "the fixture did not actually zoom in"
        return view

    def test_selecting_an_island_holds_the_view(self):
        window = _loaded()
        before = self.zoomed(window)
        window.selected = [1]
        window._selection_changed()
        assert self.span(window.ax_before) == pytest.approx(before)

    def test_clearing_the_selection_holds_it_too(self):
        window = _loaded()
        window.select_all()
        before = self.zoomed(window)
        window.clear_selection()
        assert self.span(window.ax_before) == pytest.approx(before)

    def test_changing_a_parameter_holds_the_view(self):
        """The same redraw, reached the other way — this was broken too."""
        window = _loaded()
        _plan(window)
        before = self.zoomed(window)
        window.width_spin.setValue(2.5)
        assert window.plan is None                  # it did invalidate, and
        assert self.span(window.ax_before) == pytest.approx(before)   # held

    def test_planning_holds_the_view(self):
        """You zoom to the strait you care about, then plan; staying there is
        the whole point of preserving it."""
        window = _loaded()
        before = self.zoomed(window)
        _plan(window)
        assert window.plan.bridges
        assert self.span(window.ax_before) == pytest.approx(before)

    def test_toggling_the_anchors_holds_the_view(self):
        window = _loaded()
        _plan(window)
        before = self.zoomed(window)
        window.anchors_check.setChecked(False)
        assert self.span(window.ax_before) == pytest.approx(before)

    def test_toggling_the_lagoons_holds_the_view(self):
        """It redraws only the result panel -- which is the panel whose redraw
        was carrying the other one off."""
        window = _loaded()
        _plan(window)
        before = self.zoomed(window)
        window.lagoon_check.setChecked(False)
        assert self.span(window.ax_before) == pytest.approx(before)

    def test_the_two_panels_stay_together_through_it(self):
        """A preserved view is only preserved if the result panel came along."""
        window = _loaded()
        _plan(window)
        self.zoomed(window)
        window.selected = [0]
        window._selection_changed()
        assert (window.ax_after.get_xlim()
                == pytest.approx(window.ax_before.get_xlim()))
        assert (window.ax_after.get_ylim()
                == pytest.approx(window.ax_before.get_ylim()))

    def test_a_draw_that_is_not_preserving_still_frames_everything(self):
        """The other branch: an import frames the islands rather than holding
        whatever view the last file left behind."""
        window = _loaded()
        self.zoomed(window)
        window._draw_plan()
        minx, miny, maxx, maxy = window.islands.total_bounds
        (x0, x1), (y0, y1) = (window.ax_before.get_xlim(),
                              window.ax_before.get_ylim())
        assert x0 <= minx and x1 >= maxx
        assert y0 <= miny and y1 >= maxy
        assert (window.ax_after.get_xlim()
                == pytest.approx(window.ax_before.get_xlim()))


class TestGuards:
    """The harness refuses what the lib cannot answer."""

    def test_running_with_no_islands_does_nothing(self):
        window = U.IslandBridgesTester()
        window.run()
        assert window.plan is None
        assert window._thread is None

    def test_a_zero_width_bridge_is_refused_before_launching(self):
        window = _loaded()
        window.width_spin.setValue(0.0)
        window.run()
        assert window._thread is None       # never started
        assert window.plan is None

    def test_a_failure_is_surfaced_and_the_controls_come_back(self):
        window = _loaded()
        window._set_busy(True)
        window._on_failed("boom")
        assert window.btn_import.isEnabled()
        assert not window.progress.isVisible()
        assert "failed" in window.status.text()

    def test_saving_without_a_result_does_nothing(self, monkeypatch):
        window = _loaded()
        called = []
        monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                            lambda *a, **k: called.append(1) or ("", ""))
        window.save_geojson()
        assert not called

    def test_saving_writes_the_source_crs(self, monkeypatch, tmp_path):
        window = _loaded()
        window.source_crs = "EPSG:4326"
        _plan(window)
        path = tmp_path / "out.geojson"
        monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(path), ""))
        window.save_geojson()
        written = gpd.read_file(path)
        assert written.crs.to_epsg() == 4326
        assert "Saved" in window.status.text()
