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
* **The report is the plan's own numbers**, not a second count that could drift
  away from what was actually placed.
"""

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
    return window


def _plan(window):
    """Run the lib synchronously, mirroring ``PlanWorker.run``."""
    from lib.LIB_island_bridges import apply_bridges, plan_bridges
    islands = list(window.islands.geometry)
    plan = plan_bridges(islands, window.params())
    geom = apply_bridges(islands, plan,
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
    ("embed_spin", "embed_mm", 0.4),
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
                "scale", "width_mm", "embed_mm", "max_span_mm", "sample_mm",
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
