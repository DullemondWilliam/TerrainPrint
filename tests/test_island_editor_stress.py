"""
test_island_editor_stress.py

The **Stress** layer of UI_island_editor.py — the load-bearing necks of the
document, marked on the map and kept true as it is edited.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_bridges.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_stress.py -v

The measurement itself is covered by test_stress_points.py and nothing here
re-tests it.  What this file guards is the three things that are this window's
own, and that no other test would notice going wrong:

* the layer **does not go stale**.  The editor is the one place where the
  coastline moves under a stress report, so an edit, an undo and a redo each
  clear the marks and ask again — and a mark left over a coastline that has
  moved reads as a verdict on the one now underneath it;
* the raster is **reused** across an edit and **thrown away** when it cannot be
  trusted — a different print scale, a different province, or a run that threw;
* Min severity filters an answer already in hand rather than measuring again.

The severity ramp is pinned here too, against the two other copies of it.
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


def _measure(window, timeout=180.0):
    """Wait for the measurement thread, as the window's own user would not."""
    deadline = time.monotonic() + timeout
    while window._stress_thread is not None and time.monotonic() < deadline:
        QApplication.processEvents()
    assert window._stress_thread is None, "the measurement never finished"


def _settle(window, timeout=180.0):
    """Wait for the measurement *and* for any it chains into after an edit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if window._stress_thread is None and window._stress is not None:
            return
    raise AssertionError("the measurement never settled")


def _show(window):
    """Press Show and wait for the first full run."""
    window.btn_stress.setChecked(True)
    window._toggle_stress(True)
    _measure(window)


def dumbbell(throat_mm=0.2, lobe_mm=10.0, bar_mm=3.0):
    """Two lobes on a bar too thin to hold them: one neck, at risk.

    The fixture of test_stress_points.py, in printed millimetres at the
    editor's own scale, because every number in this window is a printed one.
    These numbers come out as **0.20 mm holding 100 mm²**, wanting 0.50 mm:
    severity 2.5, which is the Nunavut end of the real range.
    """
    left = box(0, 0, lobe_mm * MM, lobe_mm * MM)
    right = box((lobe_mm + bar_mm) * MM, 0,
                (2 * lobe_mm + bar_mm) * MM, lobe_mm * MM)
    middle = lobe_mm / 2.0
    bar = box(lobe_mm * MM, (middle - throat_mm / 2.0) * MM,
              (lobe_mm + bar_mm) * MM, (middle + throat_mm / 2.0) * MM)
    return unary_union([left, right, bar])


#: A stroke in the strait beside the bar: inside the grid, so the raster is
#: reused, and real land, so the document actually changes. Brushing *inside* a
#: lobe would be a no-op -- ``draw_bridge`` answers that by identity, and no
#: edit means nothing to re-measure.
STRAIT_STROKE = [(11.5 * MM, 8.0 * MM), (11.5 * MM, 9.0 * MM)]


def _stroke(window, points, width_mm, mode=None):
    """Commit a brush stroke, as a drag on the map would."""
    window.brush_spin.setValue(width_mm)
    window._stroke = list(points)
    window._brush_mode = mode or M.DRAW
    window._finish_stroke()


def _editor(qtbot, geom):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(geom)
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    return window


@pytest.fixture
def editor(qtbot):
    return _editor(qtbot, dumbbell())


class TestShowingTheLayer:

    def test_nothing_is_measured_until_it_is_asked_for(self, editor):
        """A full run is 6.7 s on Nunavut; it is not paid for on import."""
        assert editor._stress is None
        assert editor._stress_grid is None
        assert not editor.btn_stress.isChecked()

    def test_the_toggle_needs_a_document(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        assert not window.btn_stress.isEnabled()

    def test_showing_finds_the_neck(self, editor):
        _show(editor)
        assert len(editor._stress.necks) == 1
        neck = editor._stress.necks[0]
        assert neck.at_risk
        assert neck.throat_mm == pytest.approx(0.2, abs=0.05)
        assert neck.load_mm2 == pytest.approx(100, rel=0.05)

    def test_the_marks_land_on_the_map(self, editor):
        """The throat bar, its casing and the at-risk ring, in the neck's own
        severity colour — the blob alone would be sub-pixel at this zoom."""
        before = len(editor.ax.lines)
        _show(editor)
        assert len(editor.ax.lines) > before
        neck = editor._stress.necks[0]
        want = M.SEVERITY_CMAP(min(neck.severity / M.STRESS_VMAX, 1.0))
        drawn = {tuple(round(c, 6) for c in line.get_color())
                 for line in editor.ax.lines
                 if not isinstance(line.get_color(), str)}
        assert tuple(round(c, 6) for c in want) in drawn

    def test_taking_it_off_takes_the_marks_and_the_raster(self, editor):
        """51 MB on Nunavut: worth keeping while the necks are on screen, and
        not a moment longer."""
        _show(editor)
        assert editor._stress_grid is not None
        editor.btn_stress.setChecked(False)
        editor._toggle_stress(False)
        assert editor._stress is None
        assert editor._stress_grid is None

    def test_it_can_be_switched_off_while_it_is_still_measuring(self, editor):
        """A layer is not a Run button: fifteen seconds is a long time to leave
        one stuck on. The answer in flight then comes back to nobody."""
        editor.btn_stress.setChecked(True)
        editor._toggle_stress(True)
        assert editor._stress_thread is not None
        assert editor.btn_stress.isEnabled(), "the toggle must stay live"
        editor.btn_stress.setChecked(False)
        editor._toggle_stress(False)
        _measure(editor)
        assert editor._stress is None
        assert editor._stress_grid is None      # and the raster goes with it

    def test_the_status_line_says_the_verdict_and_what_it_cost(self, editor):
        """Never a bare count — and the cost is what makes a stale incremental
        answer noticeable at all."""
        _show(editor)
        said = editor.status.text()
        assert "at risk" in said
        assert "measured from scratch" in said
        assert " s" in said


class TestItDoesNotGoStale:
    """The editor is the one window where the coast moves under the report."""

    def test_an_edit_clears_the_marks_before_anything_is_drawn(self, editor):
        _show(editor)
        assert editor._stress is not None
        _stroke(editor, STRAIT_STROKE, 0.5)
        # Cleared synchronously, by _set_state, not whenever the thread lands.
        assert editor._stress is None

    def test_and_asks_again_by_itself(self, editor):
        _show(editor)
        _stroke(editor, STRAIT_STROKE, 0.5)
        _measure(editor)
        assert editor._stress is not None

    def test_widening_the_neck_brings_the_severity_down(self, editor):
        """The loop this window exists to close: the viewer can only tell you a
        neck is at risk, where here you widen it and watch."""
        _show(editor)
        before = editor._stress.necks[0]
        assert before.at_risk
        # A 2 mm brush straight along the 0.2 mm bar: a causeway ten times the
        # width, which is what Bridge would have built.
        _stroke(editor, [(before.point.x - 2 * MM, before.point.y),
                         (before.point.x + 2 * MM, before.point.y)], 2.0)
        _measure(editor)
        assert editor._stress.worst < before.severity
        assert not editor._stress.at_risk

    def test_undo_puts_the_verdict_back(self, editor):
        _show(editor)
        worst = editor._stress.worst
        neck = editor._stress.necks[0]
        _stroke(editor, [(neck.point.x - 2 * MM, neck.point.y),
                         (neck.point.x + 2 * MM, neck.point.y)], 2.0)
        _measure(editor)
        assert editor._stress.worst < worst
        editor.undo_stack.undo()
        _measure(editor)
        assert editor._stress.worst == pytest.approx(worst)

    def test_an_edit_landing_mid_measurement_is_not_raced(self, editor):
        """The run in flight is measuring a document that no longer exists, and
        it is holding the only raster -- so its answer is dropped and the
        question asked again rather than two workers sharing one grid."""
        _show(editor)
        _stroke(editor, STRAIT_STROKE, 0.5)        # starts a measurement
        assert editor._stress_thread is not None
        _stroke(editor, [(2.0 * MM, 11.0 * MM), (3.0 * MM, 11.0 * MM)], 0.5)
        assert editor._stress_again, "the second edit must be remembered"
        _settle(editor)
        assert not editor._stress_again
        # The answer describes the document on screen, not the one it started on.
        params = editor._stress_params()
        assert editor._stress.land_area_mm2 == pytest.approx(
            M.land_mm2(editor.doc, params), rel=0.02)

    def test_an_edit_reuses_the_raster_rather_than_rebuilding(self, editor):
        """The whole reason the lib hands its grid back: an edit is a fifth to
        a seventh of a full run, and only the tiles it could reach are redone."""
        _show(editor)
        assert editor._stress.rebuilt
        _stroke(editor, STRAIT_STROKE, 0.5)
        _measure(editor)
        assert not editor._stress.rebuilt
        assert editor._stress.tiles_recomputed < editor._stress.tiles_total

    def test_the_layer_being_off_costs_an_edit_nothing(self, editor):
        """No measurement is started for marks nobody asked to see."""
        _stroke(editor, STRAIT_STROKE, 0.5)
        assert editor._stress_thread is None
        assert editor._stress is None


class TestWhatTheRasterMayNotSurvive:
    """It is a cache of printed millimetres on one province; both can change."""

    def test_a_new_print_scale_retires_it(self, editor):
        """Every number in a report is a printed one: the throat, the load, the
        width the load wants, and the pixel it was all measured at."""
        _show(editor)
        editor.scale_spin.setValue(editor.scale_spin.value() * 2)
        assert editor._stress is None
        _measure(editor)
        assert editor._stress.rebuilt

    def test_a_failed_run_drops_it_and_the_layer(self, editor, monkeypatch):
        """A run that threw may have left the raster half spliced, and a
        half-updated cache comes back as a confident wrong answer."""
        _show(editor)
        assert editor._stress_grid is not None

        def boom(*_a, **_k):
            raise RuntimeError("no")

        monkeypatch.setattr(M, "restress", boom)
        monkeypatch.setattr(M, "stress_points", boom)
        _stroke(editor, STRAIT_STROKE, 0.5)
        _measure(editor)
        assert editor._stress is None
        assert editor._stress_grid is None
        assert not editor.btn_stress.isChecked()


class TestMinSeverity:

    def test_it_filters_without_measuring_again(self, editor):
        _show(editor)
        held = editor._stress
        editor.stress_spin.setValue(10.0)
        assert editor._stress is held, "filtering must not re-measure"
        assert editor._shown_necks() == []

    def test_a_filtered_neck_is_not_drawn(self, editor):
        _show(editor)
        shown = len(editor.ax.lines)
        editor.stress_spin.setValue(10.0)
        assert len(editor.ax.lines) < shown

    def test_zero_draws_every_neck_found(self, editor):
        """A floor that hides a sound neck makes it look like the measurement
        missed it, which in this window is the wrong doubt to leave."""
        assert M.STRESS_SEVERITY[2] == 0.0
        _show(editor)
        assert len(editor._shown_necks()) == len(editor._stress.necks)

    def test_the_count_in_the_status_line_follows_it(self, editor):
        _show(editor)
        editor.stress_spin.setValue(10.0)
        assert "no neck at severity 10 or worse" in editor.status.text()


class TestWorstNeck:

    def test_it_needs_a_neck_to_go_to(self, editor):
        assert not editor.btn_worst_neck.isEnabled()
        _show(editor)
        assert editor.btn_worst_neck.isEnabled()

    def test_it_frames_the_neck(self, editor):
        """A 0.2 mm throat is a third of a pixel at province extent, so this is
        the only way to see one."""
        _show(editor)
        neck = editor._stress.necks[0]
        editor.zoom_to_worst_neck()
        (x0, x1), (y0, y1) = editor.ax.get_xlim(), editor.ax.get_ylim()
        assert x0 < neck.point.x < x1
        assert y0 < neck.point.y < y1
        assert (x1 - x0) == pytest.approx(M.STRESS_ZOOM_MM * MM, rel=0.01)

    def test_it_goes_grey_with_the_floor_that_hides_everything(self, editor):
        _show(editor)
        editor.stress_spin.setValue(10.0)
        assert not editor.btn_worst_neck.isEnabled()


class TestItDoesNotDisturbTheRest:

    def test_the_scoreboard_is_still_its_own(self, editor):
        """Two measurements in one column, neither reading the other."""
        _show(editor)
        assert editor.lbl_scores.text().startswith("Coast")

    def test_the_bar_survives_a_measurement_during_a_plan(self, editor):
        """One bar, two workers: it is only taken away when both are done."""
        _show(editor)
        editor.progress.setVisible(True)
        editor._bridge_thread = object()       # pretend a plan is in flight
        try:
            editor._set_measuring(False)
            assert editor.progress.isVisible(), "the plan still needs the bar"
        finally:
            editor._bridge_thread = None
        editor._set_measuring(False)
        assert not editor.progress.isVisible()

    def test_planning_locks_the_layer(self, editor):
        """The plan is about to replace the document the necks would be of."""
        editor._set_planning(True)
        assert not editor.grp_stress.isEnabled()
        editor._set_planning(False)
        assert editor.grp_stress.isEnabled()


class TestTheRampAgreesWithItsOtherCopies:
    """Three copies of one ramp, held in step.

    None of these windows imports another for a colour — a harness is
    throwaway, a viewer surveys and an editor edits — so this is what stops the
    three drifting into saying different things about the same number.
    """

    def test_the_stops_match_the_viewer(self):
        import UI_province_viewer as V
        assert M.SEVERITY_STOPS == V.SEVERITY_STOPS

    def test_the_stops_match_the_harness(self):
        from harnesses import UI_stress_points as H
        assert M.SEVERITY_STOPS == H.SEVERITY_STOPS

    def test_the_anchor_matches(self):
        import UI_province_viewer as V
        from harnesses import UI_stress_points as H
        assert M.STRESS_VMAX == V.STRESS_VMAX == H.DEFAULT_VMAX

    def test_one_lands_on_the_hue_change(self):
        """With the ramp running to twice the anchor, severity 1.0 sits at the
        midpoint, which has to be where the colour turns."""
        middle = sorted(at for at, _ in M.SEVERITY_STOPS if 0.0 < at < 1.0)
        assert len(middle) == 2
        assert middle[0] < 0.5 < middle[1]

    def test_the_measurement_settings_come_from_the_shared_config(self, editor):
        """Only Min severity is this window's own; what counts as a neck is not,
        so the three cannot disagree about the same coast."""
        params = editor._stress_params()
        shipped = M.StressParams(scale=params.scale)
        for field in ("pixel_mm", "ceiling_mm", "band_mm", "tile_px",
                      "min_lobe_mm2", "margin_mm", "lever_power"):
            assert getattr(params, field) == getattr(shipped, field)
        assert params.report_above == 0.0      # the filter is not the measurement


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
