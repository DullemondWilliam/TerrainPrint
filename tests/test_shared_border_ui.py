"""
test_shared_border_ui.py

harnesses/UI_shared_border.py: simplify the border two provinces share, for printing.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp``
fixture, as in test_province_viewer.py.

Run with:  python -m pytest tests/test_shared_border_ui.py -v

The geometry is covered by test_shared_border.py; nothing here re-tests it. What
this file guards is the harness telling the truth: the "before" numbers it shows
must be the same measurement the result reports, the stale-result rules must
hold, and the seam must be drawn where it actually is. A harness that quietly
disagreed with its own lib would be worse than no harness, because it is the
thing you would trust when deciding whether a simplification is acceptable.
"""

import numpy as np
import pytest
from shapely.geometry import Polygon, box

import geopandas as gpd

from PySide6.QtWidgets import QMessageBox

from harnesses import UI_shared_border as U
from ui.seam_editor import MAX_VERTEX_MARKERS

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


WORK_CRS = "EPSG:3347"
SCALE = 8_000_000.0
MM = SCALE / 1000.0                    # one print-mm, in metres
ORIGIN = (7_000_000.0, 2_000_000.0)


def _pair(seam_xs_ys, width_mm=20.0, height_mm=20.0):
    """Two provinces meeting along a seam given in print-mm from ORIGIN."""
    ox, oy = ORIGIN
    seam = [(ox + x * MM, oy + y * MM) for x, y in seam_xs_ys]
    a = Polygon([(ox, oy)] + seam + [(ox, oy + height_mm * MM)])
    b = Polygon([(ox + width_mm * MM, oy)] + seam
                + [(ox + width_mm * MM, oy + height_mm * MM)])
    return a, b


def _zigzag(amplitude_mm=0.05, step_mm=0.1, span_mm=20.0):
    ys = np.arange(0.0, span_mm + step_mm, step_mm)
    return [(10.0 + (amplitude_mm if i % 2 else -amplitude_mm), float(y))
            for i, y in enumerate(ys)]


def _loaded(tmp_path, seam=None, write=True):
    """A window with both provinces in state (no file dialogs)."""
    window = U.SharedBorderTester()
    a, b = _pair(seam if seam is not None else _zigzag())
    for side, geom in (("a", a), ("b", b)):
        setattr(window, f"geom_{side}", geom)
        if write:
            path = tmp_path / f"{side}.geojson"
            gpd.GeoDataFrame(geometry=[geom], crs=WORK_CRS).to_file(
                path, driver="GeoJSON")
            setattr(window, f"path_{side}", path)
    window.source_crs = WORK_CRS
    window._find_seam()
    window._invalidate()
    return window


def _run(window):
    """Run the lib synchronously, mirroring ``SimplifyWorker.run``."""
    from lib.LIB_shared_border import simplify_shared_border
    result = simplify_shared_border(window.geom_a, window.geom_b,
                                    window._params())
    window._on_done(result)
    return result


class TestSeamReport:
    """The "before" numbers must be the lib's measurement, not a second one."""

    def test_before_numbers_match_what_the_result_reports(self, tmp_path):
        window = _loaded(tmp_path)
        stats = window.seam_stats()
        result = _run(window)
        assert stats["vertices"] == result.vertices_before
        assert stats["short"] == result.short_segments_before
        assert stats["spikes"] == result.spikes_before
        assert stats["length_mm"] == pytest.approx(result.seam_length_mm)

    def test_spike_count_is_real_not_a_constant_zero(self, tmp_path):
        """The zigzag fixture has no spikes, so 0 == 0 proves nothing there.

        This seam carries a genuine 1 mm needle, so a harness that never counted
        spikes would disagree with the lib instead of accidentally matching it.
        """
        spiked = [(10.0, 0.0), (10.0, 5.0), (11.0, 5.05), (10.0, 5.1),
                  (10.0, 20.0)]
        window = _loaded(tmp_path, seam=spiked)
        stats = window.seam_stats()
        assert stats["spikes"] >= 1
        result = _run(window)
        assert stats["spikes"] == result.spikes_before
        assert result.spikes_after == 0

    def test_report_appears_in_the_status_line(self, tmp_path):
        window = _loaded(tmp_path)
        stats = window.seam_stats()
        text = window.status.text()
        assert str(stats["vertices"]) in text
        assert "below the nozzle" in text

    def test_nozzle_setting_changes_the_before_count(self, tmp_path):
        """The report is measured against the printer, so the printer matters."""
        window = _loaded(tmp_path)
        window.nozzle_spin.setValue(0.05)
        lenient = window.seam_stats()["short"]
        window.nozzle_spin.setValue(1.0)
        strict = window.seam_stats()["short"]
        assert strict > lenient

    def test_no_seam_is_said_plainly(self, tmp_path):
        window = U.SharedBorderTester()
        window.geom_a = box(0, 0, 10 * MM, 10 * MM)
        window.geom_b = box(50 * MM, 50 * MM, 60 * MM, 60 * MM)
        window._find_seam()
        window._invalidate()
        assert window.seam is None
        assert "do not share a border" in window.status.text()
        assert not window.btn_run.isEnabled()

    def test_stats_are_empty_before_both_provinces_are_in(self):
        window = U.SharedBorderTester()
        assert window.seam_stats() == {}
        assert not window.btn_run.isEnabled()


class TestParameters:

    def test_defaults_come_from_the_config(self):
        from lib.LIB_project_config import get as cfg
        window = U.SharedBorderTester()
        assert window.tol_spin.value() == pytest.approx(cfg("seam.tolerance_mm"))
        assert window.seg_spin.value() == pytest.approx(cfg("seam.min_segment_mm"))
        assert window.angle_spin.value() == pytest.approx(cfg("seam.min_angle_deg"))
        assert window.nozzle_spin.value() == pytest.approx(cfg("seam.nozzle_mm"))
        assert window.drift_spin.value() == pytest.approx(
            cfg("seam.max_deviation_mm"))

    def test_every_control_reaches_the_lib(self, tmp_path):
        window = _loaded(tmp_path)
        window.tol_spin.setValue(0.42)
        window.seg_spin.setValue(0.55)
        window.angle_spin.setValue(33.0)
        window.smooth_spin.setValue(2)
        window.nozzle_spin.setValue(0.66)
        window.drift_spin.setValue(3.5)
        params = window._params()
        assert params.tolerance_mm == pytest.approx(0.42)
        assert params.min_segment_mm == pytest.approx(0.55)
        assert params.min_angle_deg == pytest.approx(33.0)
        assert params.smooth_iterations == 2
        assert params.nozzle_mm == pytest.approx(0.66)
        assert params.max_deviation_mm == pytest.approx(3.5)

    def test_tolerance_change_alters_the_result(self, tmp_path):
        window = _loaded(tmp_path, seam=_zigzag(amplitude_mm=0.3, step_mm=0.5))
        window.seg_spin.setValue(0.0)
        window.tol_spin.setValue(0.01)
        tight = _run(window).vertices_after
        window.tol_spin.setValue(1.0)
        loose = _run(window).vertices_after
        assert loose < tight

    def test_a_refused_drift_is_surfaced_not_swallowed(self, tmp_path):
        window = _loaded(tmp_path, seam=_zigzag(amplitude_mm=2.0, step_mm=1.0))
        window.tol_spin.setValue(5.0)
        window.drift_spin.setValue(0.5)
        from lib.LIB_shared_border import simplify_shared_border
        with pytest.raises(ValueError, match="strays"):
            simplify_shared_border(window.geom_a, window.geom_b, window._params())
        window._on_failed("the conditioned seam strays too far")
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert "strays" in window.status.text()


class TestInvalidation:

    def test_run_disabled_until_a_seam_exists(self, tmp_path):
        window = U.SharedBorderTester()
        assert not window.btn_run.isEnabled()
        window.geom_a, _ = _pair(_zigzag())
        window._find_seam()
        window._invalidate()
        assert not window.btn_run.isEnabled()      # one province is not a seam
        _, window.geom_b = _pair(_zigzag())
        window._find_seam()
        window._invalidate()
        assert window.btn_run.isEnabled()

    def test_parameter_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        assert window.btn_save.isEnabled() and window.result is not None
        window.tol_spin.setValue(0.5)
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert window.ax_after.get_title() == "Not yet simplified"

    def test_smoothing_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        window.smooth_spin.setValue(2)
        assert window.result is None
        assert not window.btn_save.isEnabled()

    def test_busy_locks_and_restores_preconditions(self, tmp_path):
        window = _loaded(tmp_path)
        window._set_busy(True)
        assert window.progress.minimum() == 0 and window.progress.maximum() == 0
        assert not window.btn_run.isEnabled()
        assert not window.tol_spin.isEnabled()

        window._set_busy(False)
        assert window.progress.maximum() == 1
        assert window.btn_run.isEnabled()          # a seam is loaded
        # Save must stay disabled: the blanket enable would otherwise offer a
        # save with no result behind it.
        assert not window.btn_save.isEnabled()

    def test_busy_restore_enables_save_once_there_is_a_result(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        window._set_busy(True)
        window._set_busy(False)
        assert window.btn_save.isEnabled()


class TestPanels:

    def test_before_panel_draws_both_provinces_and_the_seam(self, tmp_path):
        window = _loaded(tmp_path)
        window._draw_before()
        # Provinces are filled, so they are patches; the seam is a line, with a
        # second line carrying its vertex markers.
        assert len(window.ax_before.patches) == 2
        assert len(window.ax_before.lines) == 2

    def test_vertex_markers_are_dropped_when_there_are_too_many(self, tmp_path):
        """Past a few thousand they are a smear, not information."""
        window = _loaded(tmp_path,
                         seam=_zigzag(amplitude_mm=0.02, step_mm=0.002))
        assert window.seam_stats()["vertices"] > MAX_VERTEX_MARKERS
        window._draw_before()
        assert len(window.ax_before.lines) == 1        # the seam, no markers

    def test_after_panel_ghosts_the_original_seam(self, tmp_path):
        """Drift you can see, not merely read off the status line."""
        window = _loaded(tmp_path)
        _run(window)
        alphas = [line.get_alpha() for line in window.ax_after.lines]
        assert any(a is not None and a < 1.0 for a in alphas)

    def test_after_panel_reports_the_reduction_in_its_title(self, tmp_path):
        window = _loaded(tmp_path)
        result = _run(window)
        title = window.ax_after.get_title()
        assert str(result.vertices_after) in title
        assert "fewer" in title

    def test_panels_move_together(self, tmp_path):
        """Both panels are the same provinces in the same CRS, so they should.

        They are linked by hand rather than by ``sharex``: matplotlib forbids a
        fixed axes box on shared axes, and the fixed box is what keeps the
        panels a constant size.
        """
        window = _loaded(tmp_path)
        _run(window)
        window._fit_view((ORIGIN[0], ORIGIN[1],
                          ORIGIN[0] + 5 * MM, ORIGIN[1] + 5 * MM))
        assert window.ax_after.get_xlim() == pytest.approx(
            window.ax_before.get_xlim())
        assert window.ax_after.get_ylim() == pytest.approx(
            window.ax_before.get_ylim())

    def test_panel_size_does_not_follow_the_seam(self, tmp_path):
        """The complaint this fixed: a north-south border gave two slivers.

        Matplotlib's default resizes the axes box to the data's shape, so every
        import rearranged the window. These three views are tall, wide and
        square; the panels must not care.
        """
        window = _loaded(tmp_path)
        window._draw_before()
        ox, oy = ORIGIN
        sizes = set()
        for bounds in ((ox, oy, ox + 1 * MM, oy + 20 * MM),      # tall
                       (ox, oy, ox + 20 * MM, oy + 1 * MM),      # wide
                       (ox, oy, ox + 10 * MM, oy + 10 * MM)):    # square
            window._fit_view(bounds)
            window.figure.canvas.draw()
            pos = window.ax_before.get_position()
            sizes.add((round(pos.width, 6), round(pos.height, 6)))
        assert len(sizes) == 1, f"panel resized: {sizes}"

    def test_panel_keeps_its_size_after_an_off_aspect_zoom(self, tmp_path):
        """The case ``_fit_view`` alone does not cover.

        Fitting the view makes the data the panel's shape, so the box would sit
        still even with matplotlib's default ``adjustable="box"``. But the
        toolbar's rubber-band zoom sets limits of whatever shape the user drew,
        and only a fixed box survives that -- which is why the axes are
        ``adjustable="datalim"`` and not merely fitted.
        """
        window = _loaded(tmp_path)
        window._draw_before()
        window.zoom_to_seam()
        window.figure.canvas.draw()
        before = window.ax_before.get_position()

        ox, oy = ORIGIN
        window.ax_before.set_xlim(ox, ox + 1 * MM)       # a tall, thin drag
        window.ax_before.set_ylim(oy, oy + 20 * MM)
        window.figure.canvas.draw()
        after = window.ax_before.get_position()

        assert after.width == pytest.approx(before.width)
        assert after.height == pytest.approx(before.height)

    def test_both_panels_are_the_same_size(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        window.zoom_to_seam()
        window.figure.canvas.draw()
        before = window.ax_before.get_position()
        after = window.ax_after.get_position()
        assert before.width == pytest.approx(after.width)
        assert before.height == pytest.approx(after.height)

    def test_fitting_a_view_never_crops_it(self, tmp_path):
        """``adjustable='datalim'`` crops to satisfy the aspect if it is let to.

        Asking for a 20-unit-tall view once returned 1.2 units, with the seam
        off the screen. The view is fitted to the panel's shape first so there
        is nothing left to crop; this checks that on every shape.
        """
        window = _loaded(tmp_path)
        window._draw_before()
        ox, oy = ORIGIN
        for bounds in ((ox, oy, ox + 1 * MM, oy + 20 * MM),
                       (ox, oy, ox + 20 * MM, oy + 1 * MM),
                       (ox, oy, ox + 10 * MM, oy + 10 * MM)):
            window._fit_view(bounds)
            window.figure.canvas.draw()
            (x0, x1), (y0, y1) = (window.ax_before.get_xlim(),
                                  window.ax_before.get_ylim())
            assert x0 <= bounds[0] and x1 >= bounds[2], f"cropped x on {bounds}"
            assert y0 <= bounds[1] and y1 >= bounds[3], f"cropped y on {bounds}"

    def test_the_fitted_view_already_satisfies_the_aspect(self, tmp_path):
        """If it does, matplotlib changes nothing at draw time."""
        window = _loaded(tmp_path)
        window._draw_before()
        window.zoom_to_seam()
        before = (window.ax_before.get_xlim(), window.ax_before.get_ylim())
        window.figure.canvas.draw()
        after = (window.ax_before.get_xlim(), window.ax_before.get_ylim())
        assert after[0] == pytest.approx(before[0], rel=1e-9)
        assert after[1] == pytest.approx(before[1], rel=1e-9)

    def test_scroll_zoom_keeps_the_panels_together(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        window.zoom_to_seam()

        class _Event:
            pass
        event = _Event()
        event.inaxes = window.ax_before
        event.xdata, event.ydata = ORIGIN[0] + 10 * MM, ORIGIN[1] + 10 * MM
        event.button = "up"
        window._on_scroll(event)
        assert window.ax_after.get_xlim() == pytest.approx(
            window.ax_before.get_xlim())

    def test_scrolling_the_after_panel_drives_the_before_panel(self, tmp_path):
        window = _loaded(tmp_path)
        _run(window)
        window.zoom_to_seam()
        start = window.ax_before.get_xlim()

        class _Event:
            pass
        event = _Event()
        event.inaxes = window.ax_after
        event.xdata, event.ydata = ORIGIN[0] + 10 * MM, ORIGIN[1] + 10 * MM
        event.button = "up"
        window._on_scroll(event)
        assert window.ax_before.get_xlim() != pytest.approx(start)
        assert window.ax_before.get_xlim() == pytest.approx(
            window.ax_after.get_xlim())

    def test_zoom_to_seam_frames_the_seam(self, tmp_path):
        window = _loaded(tmp_path)
        window._draw_before()
        window.ax_before.set_xlim(0, 1e9)          # somewhere useless
        window.zoom_to_seam()
        minx, _, maxx, _ = window.seam.bounds
        lo, hi = window.ax_before.get_xlim()
        assert lo <= minx and hi >= maxx
        assert (hi - lo) < 1e9                     # and it actually zoomed in

    def test_zoom_is_a_no_op_without_a_seam(self):
        window = U.SharedBorderTester()
        window.zoom_to_seam()                      # must not raise

    def test_lakes_stay_visible_as_holes(self, tmp_path):
        ox, oy = ORIGIN
        lake = Polygon([(ox + 3 * MM, oy + 8 * MM), (ox + 6 * MM, oy + 8 * MM),
                        (ox + 6 * MM, oy + 12 * MM), (ox + 3 * MM, oy + 12 * MM)])
        window = _loaded(tmp_path)
        window.geom_a = window.geom_a.difference(lake)
        window._find_seam()
        window._draw_before()
        # The hole is drawn as its own white patch on top of the province.
        assert len(window.ax_before.patches) >= 3


class TestSave:

    def test_writes_both_provinces_named_after_their_sources(self, tmp_path):
        window = _loaded(tmp_path)
        result = _run(window)
        out = tmp_path / "out"
        out.mkdir()
        window._write(result.a, window.path_a, out)
        window._write(result.b, window.path_b, out)
        assert (out / "a_simplified.geojson").exists()
        assert (out / "b_simplified.geojson").exists()

    def test_written_pair_still_shares_its_border(self, tmp_path):
        """The harness must not undo what the lib guaranteed."""
        window = _loaded(tmp_path)
        result = _run(window)
        out = tmp_path / "out"
        out.mkdir()
        pa = window._write(result.a, window.path_a, out)
        pb = window._write(result.b, window.path_b, out)

        ga = gpd.read_file(pa).to_crs(WORK_CRS).geometry.iloc[0]
        gb = gpd.read_file(pb).to_crs(WORK_CRS).geometry.iloc[0]
        assert ga.touches(gb)
        assert ga.intersection(gb).area == pytest.approx(0.0, abs=1.0)
        shared = ga.boundary.intersection(gb.boundary)
        assert shared.length == pytest.approx(result.seam.length, rel=1e-6)

    def test_written_in_the_crs_the_inputs_arrived_in(self, tmp_path):
        window = _loaded(tmp_path)
        window.source_crs = "EPSG:4326"
        result = _run(window)
        out = tmp_path / "out"
        out.mkdir()
        path = window._write(result.a, window.path_a, out)
        assert gpd.read_file(path).crs.to_epsg() == 4326
