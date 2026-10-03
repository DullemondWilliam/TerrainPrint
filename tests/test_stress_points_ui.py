"""
test_stress_points_ui.py

harnesses/UI_stress_points.py: the necks that hold a printed piece up.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp``
fixture, as in test_island_bridges_ui.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_stress_points_ui.py -v

The geometry is covered by test_stress_points.py and nothing here re-tests it.
What this file guards is the harness being an honest front end for the lib:

* **Every control reaches the lib.** A spin box wired to nothing is worse than no
  spin box, because the picture then answers a question other than the one on
  screen. ``TestControlsReachTheLib`` moves each one and insists the parameter
  block moves with it.
* **A stale report is never left on screen** beside parameters that did not
  produce it.
* **The brush edits the raster and nothing else.** Mid-stroke the necks come off
  the picture, because they belong to the land as it was before the stroke — an
  overlay drawn over land that has moved under it is the one thing this harness
  must not show.
* **The incremental path is reported, not hidden.** It is invisible when it
  works, which is exactly when a stale overlay would go unnoticed, so the status
  line says whether the grid was reused and how many tiles it cost.
* **The map stays where you put it.** Every redraw clears the axes, so the view
  has to be put back — and on an equal-aspect panel only both limits at once.
* **What is drawn is the lib's own numbers.** The bars are ``Neck.throat``, so
  they are the lines the measurement actually chose; a bar drawn from anything
  else would be a second opinion about where the neck is.
* **The severity ramp is anchored where the lib says it is.** 1.0 is marginal,
  and it has to land on the ramp's hue change or the colour means nothing.
"""

import types

import numpy as np
import pytest
import shapely
from shapely.geometry import box

from harnesses import UI_stress_points as U

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


def _dumbbell(neck_mm=0.3, size_mm=6.0, neck_len_mm=2.0):
    """Two squares joined by a bar: one neck, holding 36 mm² either side."""
    ox, oy = ORIGIN
    half, mid = neck_mm / 2.0, size_mm / 2.0
    a, b = size_mm, size_mm + neck_len_mm
    return shapely.union_all([
        box(ox, oy, ox + a * MM, oy + size_mm * MM),
        box(ox + a * MM, oy + (mid - half) * MM, ox + b * MM,
            oy + (mid + half) * MM),
        box(ox + b * MM, oy, ox + (b + size_mm) * MM, oy + size_mm * MM)])


def _loaded(geom=None):
    """A window with a province in state, as if imported (no file dialogs)."""
    window = U.StressPointsTester()
    window.geom = _dumbbell() if geom is None else geom
    window.source_crs = WORK_CRS
    window.btn_run.setEnabled(True)
    window.btn_whole.setEnabled(True)
    return window


def _measure(window):
    """Run the lib synchronously, mirroring ``StressWorker.run``.

    Through the harness's own ``params()`` and ``_on_done``, for the same reason
    the worker goes through them: those are the harness's answer, and a test that
    made up its own would not be testing it.
    """
    from lib.LIB_stress_points import stress_points
    report = stress_points(window.geom, window.params())
    window._on_done(report, 0.123)
    return report


def _remeasure(window):
    """The incremental path, synchronously: restress with the brushed raster."""
    from lib.LIB_stress_points import restress
    report = restress(window.report.grid, land=window.land,
                      params=window.params())
    window._on_done(report, 0.045)
    return report


def _paint(window, x, y, key=None):
    """One brush event at a point on the map panel.

    A stand-in for matplotlib's MouseEvent carrying the fields the handler
    reads; matplotlib delivering events is matplotlib's business.
    """
    return types.SimpleNamespace(inaxes=window.ax_map, xdata=x, ydata=y,
                                 button=1, key=key)


#: Every control on the window, with the parameter it must drive and a value
#: that differs from the default. Adding a knob to the harness without adding it
#: here leaves it untested, which is the failure this table exists to make
#: obvious.
CONTROLS = [
    ("scale_spin", "scale", 4_000_000.0),
    ("pixel_spin", "pixel_mm", 0.2),
    ("ceiling_spin", "ceiling_mm", 2.0),
    ("band_spin", "band_mm", 0.05),
    ("tile_spin", "tile_px", 64),
    ("min_lobe_spin", "min_lobe_mm2", 50.0),
    ("lever_spin", "lever_power", 0.0),
    ("above_spin", "report_above", 1.0),
]


class TestControlsReachTheLib:
    """Each knob drives the parameter it claims to."""

    @pytest.mark.parametrize("control,field,value", CONTROLS)
    def test_control_drives_its_parameter(self, control, field, value):
        window = _loaded()
        getattr(window, control).setValue(value)
        assert getattr(window.params(), field) == pytest.approx(value)

    def test_every_control_says_what_it_does(self):
        """A knob with no hover is a knob you have to read the source to use.

        Enforced rather than spot-checked, so that adding one later and
        forgetting the tooltip fails here instead of being found by somebody
        turning it to see what happens. The captions carry it too: ``Band:`` on
        its own tells you nothing, and the caption is the half of the pair the
        pointer lands on first.
        """
        from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox,
                                       QComboBox, QLabel)
        window = U.StressPointsTester()
        controls = [(name, obj) for name, obj in vars(window).items()
                    if isinstance(obj, (QAbstractButton, QAbstractSpinBox,
                                        QComboBox))]
        assert len(controls) >= 20, "did the controls move off the window?"
        assert [name for name, obj in controls if not obj.toolTip()] == []
        captions = [label for label in window.findChildren(QLabel)
                    if label.text().endswith(":")]
        assert captions, "the parameter rows should be captioned"
        assert [c.text() for c in captions if not c.toolTip()] == []

    def test_a_caption_says_the_same_thing_as_its_box(self):
        """One wording per parameter: the caption takes the widget's own text
        rather than keeping a second copy that could drift from it."""
        window = U.StressPointsTester()
        caption = window._caption("Band:", window.band_spin)
        assert caption.toolTip() == window.band_spin.toolTip()
        assert caption.text() == "Band:"

    def test_the_action_buttons_explain_themselves(self):
        """The five that do something, each naming what it will cost or change
        rather than repeating its own label."""
        window = U.StressPointsTester()
        for name in ("btn_import", "btn_run", "btn_zoom", "btn_whole",
                     "btn_save"):
            tip = getattr(window, name).toolTip()
            label = getattr(window, name).text().rstrip(" …")
            assert len(tip) > 60, f"{name}: a tooltip that short is a label"
            assert tip != label

    def test_the_defaults_are_the_libs_defaults(self):
        """The window must open on the lib's own numbers, or the first run
        answers a question nobody asked."""
        from lib.LIB_stress_points import StressParams
        window = U.StressPointsTester()
        got, want = window.params(), StressParams()
        for field in ("scale", "pixel_mm", "ceiling_mm", "band_mm", "tile_px",
                      "min_lobe_mm2", "lever_power", "report_above"):
            assert getattr(got, field) == pytest.approx(getattr(want, field)), field


class TestAStaleReportIsDropped:

    def test_changing_a_parameter_drops_the_report(self):
        window = _loaded()
        _measure(window)
        assert window.report is not None
        window.min_lobe_spin.setValue(500.0)
        assert window.report is None
        assert not window.btn_save.isEnabled()
        assert not window.btn_zoom.isEnabled()
        assert window.neck_box.count() == 0
        assert "again" in window.status.text()

    def test_changing_a_drawing_option_keeps_it(self):
        """A drawing option is not a question about the province."""
        window = _loaded()
        _measure(window)
        for check in (window.width_check, window.region_check,
                      window.bars_check, window.label_check):
            check.setChecked(not check.isChecked())
            assert window.report is not None
        window.vmax_spin.setValue(3.0)
        assert window.report is not None

    def test_the_brush_is_offered_only_once_there_is_a_raster(self):
        window = _loaded()
        assert not window.brush_check.isEnabled()
        _measure(window)
        assert window.brush_check.isEnabled()
        window.pixel_spin.setValue(0.15)
        assert not window.brush_check.isEnabled()
        assert not window.brush_check.isChecked()


class TestTheReportLine:
    """Never a bare count, and never silent about which path it took."""

    def test_a_full_run_says_so(self):
        window = _loaded()
        _measure(window)
        text = window.status.text()
        assert "measured from scratch" in text
        assert "mm" in text
        assert "0.12 s" in text

    def test_an_edit_says_how_many_tiles_it_cost(self):
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        grid = window.report.grid
        row, col = grid.shape[0] // 2, grid.shape[1] // 2
        x = grid.transform.c + (col + 0.5) * grid.transform.a
        y = grid.transform.f + (row + 0.5) * grid.transform.e
        window._on_press(_paint(window, x, y))
        window._on_release(_paint(window, x, y))
        _remeasure(window)
        text = window.status.text()
        assert "re-checked" in text and "tiles" in text
        assert "measured from scratch" not in text

    def test_an_undercounted_load_is_admitted(self):
        """``severed`` False means the load is the standing mass, which leaves
        out the branch's fringe. The harness has to say so rather than print a
        number that reads as exact."""
        window = _loaded()
        _measure(window)
        assert window.report.necks
        window.report.necks[0].severed = False
        assert "undercount" in window._report_line(0.1)

    def test_no_necks_is_still_a_sentence(self):
        window = _loaded(geom=box(*_dumbbell().bounds))     # a plain rectangle
        _measure(window)
        assert window.report.necks == []
        assert "no load-bearing necks" in window.status.text()


class TestWhatIsDrawn:

    def test_the_bars_are_the_libs_own_throats(self):
        """Read off ``Neck.throat``, so each bar is the line the measurement
        chose and is as long as the throat is wide."""
        window = _loaded()
        report = _measure(window)
        window._draw()
        drawn = {(round(line.get_xdata()[0], 3), round(line.get_ydata()[0], 3))
                 for line in window.ax_map.lines}
        for neck in report.necks:
            head = neck.throat.coords[0]
            assert (round(head[0], 3), round(head[1], 3)) in drawn
            assert neck.throat.length / MM == pytest.approx(neck.throat_mm,
                                                            abs=1e-6)

    def test_the_throats_can_be_turned_off(self):
        window = _loaded()
        _measure(window)
        window.bars_check.setChecked(False)
        window._draw()
        assert not window.ax_map.lines

    def test_the_severity_layer_can_be_turned_off(self):
        window = _loaded()
        _measure(window)
        window.region_check.setChecked(True)
        window._draw()
        with_necks = len(window.ax_map.images)
        window.region_check.setChecked(False)
        window._draw()
        assert len(window.ax_map.images) < with_necks

    def test_the_width_field_is_a_layer_of_its_own(self):
        window = _loaded()
        _measure(window)
        window.width_check.setChecked(False)
        window._draw()
        without = len(window.ax_map.images)
        window.width_check.setChecked(True)
        window._draw()
        assert len(window.ax_map.images) == without + 1

    def test_the_branch_panel_shows_what_the_neck_holds(self):
        window = _loaded()
        report = _measure(window)
        window.neck_box.setCurrentIndex(0)
        window._draw_branch()
        assert window.ax_branch.images, "the branch itself is not drawn"
        assert f"{report.necks[0].load_mm2:.0f} mm" in window.ax_branch.get_title()

    def test_the_neck_list_is_worst_first_and_marks_the_risky(self):
        window = _loaded()
        report = _measure(window)
        assert window.neck_box.count() == len(report.necks)
        first = window.neck_box.itemText(0)
        assert f"{report.necks[0].severity:.2f}" in first
        assert first.startswith("!") == report.necks[0].at_risk


class TestTheSeverityRamp:
    """1.0 is marginal, so the colour has to change there."""

    def test_one_is_the_hue_change(self):
        """With the ramp running to twice the anchor, severity 1.0 sits at the
        midpoint, which is where warning gives way to serious."""
        stops = dict(U.SEVERITY_STOPS)
        assert min(stops) == 0.0 and max(stops) == 1.0
        middle = sorted(at for at in stops if 0.0 < at < 1.0)
        assert len(middle) == 2
        assert middle[0] < 0.5 < middle[1], "1.0 must land between two stops"
        assert U.DEFAULT_VMAX == pytest.approx(2.0)

    def test_sound_reads_cool_and_at_risk_reads_hot(self):
        """Not a rainbow and not reversed: a neck over the line must be redder
        than one under it, measured on the colormap rather than eyeballed."""
        cmap = U.SEVERITY_CMAP
        sound = cmap(0.5 * 0.4 / U.DEFAULT_VMAX * 2)      # severity ~0.4
        risky = cmap(1.8 / U.DEFAULT_VMAX)                # severity 1.8
        assert risky[0] > sound[0], "the hot end must have more red"
        assert risky[1] < sound[1], "the hot end must have less green"

    def test_the_ramp_does_not_collide_with_the_harness_palette(self):
        """The marks and the land must not be mistakable for a severity."""
        for other in (U.LAND_COLOUR, U.BRANCH_COLOUR, U.THROAT_COLOUR):
            for at, hex_ in U.SEVERITY_STOPS:
                assert hex_.lower() != other.lower()


class TestTheBrushEditsTheRaster:

    def _centre_xy(self, window):
        grid = window.report.grid
        row, col = grid.shape[0] // 2, grid.shape[1] // 2
        return (grid.transform.c + (col + 0.5) * grid.transform.a,
                grid.transform.f + (row + 0.5) * grid.transform.e)

    def test_painting_changes_the_land_and_counts_what_it_changed(self):
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        before = int(window.land.sum())
        window._on_press(_paint(window, *self._centre_xy(window)))
        assert window._painted > 0
        assert int(window.land.sum()) != before

    def test_shift_erases_where_a_plain_drag_adds(self):
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        window.brush_spin.setValue(1.0)
        x, y = self._centre_xy(window)
        window.land[:] = False
        window._on_press(_paint(window, x, y))
        added = int(window.land.sum())
        assert added > 0
        window._on_press(_paint(window, x, y, key="shift"))
        assert int(window.land.sum()) < added

    def test_the_brush_does_nothing_until_it_is_switched_on(self):
        window = _loaded()
        _measure(window)
        before = window.land.copy()
        window._on_press(_paint(window, *self._centre_xy(window)))
        assert not window._painting
        assert np.array_equal(window.land, before)

    def test_the_nav_toolbar_keeps_right_of_way(self):
        """A pan or a zoom-rectangle starts with exactly this event, and must
        not also edit the province."""
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        window.mpl_toolbar.mode = "pan/zoom"
        before = window.land.copy()
        try:
            window._on_press(_paint(window, *self._centre_xy(window)))
        finally:
            window.mpl_toolbar.mode = ""
        assert not window._painting
        assert np.array_equal(window.land, before)

    def test_a_stroke_takes_the_necks_off_the_picture(self):
        """Mid-stroke the land has moved but the necks have not been re-measured,
        and an overlay drawn over land it does not belong to is the one thing
        this harness must not show."""
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        window._draw()
        assert window.ax_map.lines, "the throats should be on screen to start"
        window._on_press(_paint(window, *self._centre_xy(window)))
        assert not window.ax_map.lines
        assert "editing" in window.ax_map.get_title()

    def test_a_stroke_that_changed_nothing_asks_nothing(self):
        window = _loaded()
        _measure(window)
        window.brush_check.setChecked(True)
        window._painting = True
        window._painted = 0
        text = window.status.text()
        window._on_release(_paint(window, *self._centre_xy(window)))
        assert window.status.text() == text


class TestTheViewSurvivesARedraw:
    """Every redraw clears the axes, so the view has to be put back."""

    def _zoom(self, window):
        xlim, ylim = (ORIGIN[0], ORIGIN[0] + 2 * MM), (ORIGIN[1],
                                                       ORIGIN[1] + 2 * MM)
        window._set_view(window.ax_map, xlim, ylim)
        return xlim, ylim

    @pytest.mark.parametrize("action", [
        lambda w: w._draw(preserve=True),
        lambda w: w.width_check.setChecked(True),
        lambda w: w.region_check.setChecked(False),
        lambda w: w.bars_check.setChecked(False),
        lambda w: w.vmax_spin.setValue(4.0),
        lambda w: w.neck_box.setCurrentIndex(0),
    ])
    def test_the_map_stays_where_you_put_it(self, action):
        window = _loaded()
        _measure(window)
        xlim, ylim = self._zoom(window)
        action(window)
        assert window.ax_map.get_xlim() == pytest.approx(xlim, rel=1e-6)
        assert window.ax_map.get_ylim() == pytest.approx(ylim, rel=1e-6)

    def test_a_fresh_draw_frames_the_province(self):
        """``preserve=False`` is the one case that *should* move the view."""
        window = _loaded()
        _measure(window)
        self._zoom(window)
        window._draw()
        minx, _, maxx, _ = window.geom.bounds
        lo, hi = window.ax_map.get_xlim()
        assert lo <= minx and hi >= maxx


class TestFraming:

    def test_zoom_to_worst_frames_the_worst_neck(self):
        """A 0.2 mm throat is a third of a pixel at province extent; this is the
        only way to see one."""
        window = _loaded()
        report = _measure(window)
        window.zoom_to_worst()
        lo, hi = window.ax_map.get_xlim()
        worst = report.necks[0]
        assert lo < worst.point.x < hi
        span_mm = (hi - lo) / MM
        assert span_mm < 40.0, "a zoom that still shows the province is no zoom"
        assert f"{worst.throat_mm:.2f} mm" in window.status.text()

    def test_whole_province_goes_back_out(self):
        window = _loaded()
        _measure(window)
        window.zoom_to_worst()
        window.zoom_out()
        minx, _, maxx, _ = window.geom.bounds
        lo, hi = window.ax_map.get_xlim()
        assert lo <= minx and hi >= maxx

    def test_framing_nothing_does_not_raise(self):
        window = U.StressPointsTester()
        window.zoom_to_worst()
        window.zoom_out()


class TestSaving:

    def test_the_necks_are_written_with_their_numbers(self, tmp_path,
                                                      monkeypatch):
        window = _loaded()
        report = _measure(window)
        out = tmp_path / "necks.geojson"
        monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(out), ""))
        window.save_necks()
        assert out.exists()
        import geopandas as gpd
        frame = gpd.read_file(out)
        assert set(frame["kind"]) <= {"throat", "region"}
        assert len(frame) >= len(report.necks)
        for field in ("throat_mm", "load_mm2", "needed_mm", "severity",
                      "at_risk", "severed"):
            assert field in frame.columns
        throats = frame[frame["kind"] == "throat"]
        assert throats["severity"].iloc[0] == pytest.approx(
            report.necks[0].severity, rel=1e-6)

    def test_saving_nothing_writes_nothing(self, tmp_path, monkeypatch):
        window = _loaded()
        out = tmp_path / "nope.geojson"
        monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(out), ""))
        window.save_necks()
        assert not out.exists()
