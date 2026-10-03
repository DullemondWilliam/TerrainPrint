"""
test_province_viewer_stress.py

The **Show stress** layer of UI_province_viewer.py — the narrow land that is
holding something up, marked across a whole layer at once.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_province_viewer.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_province_viewer_stress.py -v

The measurement is covered by test_stress_points.py and nothing here re-tests
it. What this file guards is the viewer being an honest front end for it:

* **Stress is not thin.** The two layers answer different questions about the
  same coast and must be able to disagree — a spit is thin and holds nothing, a
  1 mm isthmus under half an archipelago is load-bearing and passes a 1 mm width
  test. ``TestStressIsNotThin`` is the pair of tests that would catch one layer
  quietly becoming the other.
* **The raster is thrown away.** ``stress_points`` hands back its grid so an
  edit can reuse it; keeping one per province would cost 116 MB for the eight
  cleaned provinces, and this viewer never edits.
* **Min severity filters without re-measuring.** It is a question about an
  answer already in hand, so turning it must not spend fifteen seconds.
* **The print scale does re-measure.** A throat, its load and what that load may
  weigh are all printed quantities, so every severity on screen belongs to the
  reduction that produced it.
* **A province that could not be measured is never folded into "nothing
  found"** — *unknown* and *sound* are the two answers this must never confuse.
* **Both layers can be on at once**, so the stress marks must not read as more
  of the thin layer's red.
"""

import pytest
from shapely.geometry import Polygon, box

import UI_province_viewer as V
from lib.LIB_stress_points import StressParams

pytestmark = pytest.mark.usefixtures("qapp")

MM = V.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


def square(x_mm, y_mm, size_mm=6.0):
    return box(x_mm * MM, y_mm * MM,
               (x_mm + size_mm) * MM, (y_mm + size_mm) * MM)


def necked(x_mm, neck_mm=0.2, size_mm=6.0):
    """Two squares joined by a bar of the given printed width.

    6 mm squares, so each end is 36 mm² and clears the 25 mm² floor on what
    counts as a larger area; 4 mm squares would be 16 mm² and the bar between
    them would be holding up nothing worth reporting.
    """
    half, mid = neck_mm / 2.0, size_mm / 2.0
    left, right = x_mm + size_mm, x_mm + size_mm + 2.0
    return Polygon([
        (x_mm * MM, 0), (left * MM, 0), (left * MM, (mid - half) * MM),
        (right * MM, (mid - half) * MM), (right * MM, 0),
        ((right + size_mm) * MM, 0), ((right + size_mm) * MM, size_mm * MM),
        (right * MM, size_mm * MM), (right * MM, (mid + half) * MM),
        (left * MM, (mid + half) * MM), (left * MM, size_mm * MM),
        (x_mm * MM, size_mm * MM)])


def spit(x_mm, width_mm=0.4, length_mm=3.0, size_mm=6.0):
    """A square with a thin spike: as thin as a neck, holding nothing up."""
    mid = size_mm / 2.0
    return Polygon([
        (x_mm * MM, 0), ((x_mm + size_mm) * MM, 0),
        ((x_mm + size_mm) * MM, (mid - width_mm / 2) * MM),
        ((x_mm + size_mm + length_mm) * MM, (mid - width_mm / 2) * MM),
        ((x_mm + size_mm + length_mm) * MM, (mid + width_mm / 2) * MM),
        ((x_mm + size_mm) * MM, (mid + width_mm / 2) * MM),
        ((x_mm + size_mm) * MM, size_mm * MM), (x_mm * MM, size_mm * MM)])


def _province(name, geometry):
    from pathlib import Path
    return V.Province(Path(f"{name}.geojson"), geometry)


def _viewer(qtbot, provinces, layer_name="Stage"):
    window = V.ProvinceViewer()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window.layers = [V.Layer(layer_name, None, list(provinces))]
    window.active = 0
    window._colours = window._colour_map()
    window._sync_buttons()
    window._redraw(preserve_view=False)
    return window


def _show_stress(window, on=True):
    window.btn_stress.setChecked(on)
    window._toggle_stress(on)


@pytest.fixture
def mixed(qtbot):
    """A layer of three: a plain square, a necked pair, and a spit."""
    return _viewer(qtbot, [
        _province("Fat", square(0.0, 0.0)),
        _province("Necked", necked(20.0)),
        _province("Spit", spit(50.0)),
    ])


class TestTheLayer:

    def test_it_is_off_until_asked_for(self, mixed):
        assert not mixed.btn_stress.isChecked()
        assert mixed._stress_fragment() == ""

    def test_the_button_needs_a_layer(self, qtbot):
        window = _viewer(qtbot, [])
        assert not window.btn_stress.isEnabled()
        assert not window.btn_worst_neck.isEnabled()

    def test_worst_neck_waits_for_the_layer_to_be_shown(self, mixed):
        assert not mixed.btn_worst_neck.isEnabled()
        _show_stress(mixed)
        assert mixed.btn_worst_neck.isEnabled()
        _show_stress(mixed, False)
        assert not mixed.btn_worst_neck.isEnabled()

    def test_showing_it_finds_the_neck(self, mixed):
        _show_stress(mixed)
        shown = mixed.shown_necks()
        assert len(shown) == 1
        province, neck = shown[0]
        assert province.name == "Necked"
        assert neck.at_risk

    def test_the_marks_are_drawn_and_taken_away_again(self, mixed):
        before = len(mixed.ax.lines)
        _show_stress(mixed)
        assert len(mixed.ax.lines) > before
        _show_stress(mixed, False)
        assert len(mixed.ax.lines) == before


class TestStressIsNotThin:
    """The two layers answer different questions and must be able to disagree.

    This is the whole reason the layer exists: if these ever agree on everything,
    one of them has quietly become the other and the viewer is carrying two
    controls for one answer.
    """

    def test_a_spit_is_thin_and_holds_nothing_up(self, qtbot):
        window = _viewer(qtbot, [_province("Spit", spit(0.0))])
        window.btn_thin.setChecked(True)
        window._toggle_thin(True)
        _show_stress(window)
        thin = window.thin_reports()[0][1]
        assert not thin.clean, "a 0.4 mm spike should be too thin to print"
        assert window.shown_necks() == [], "and it is holding nothing up"

    def test_a_neck_can_pass_the_width_test_and_still_be_at_risk(self, qtbot):
        """A 1 mm bar under two 22 mm squares: wide enough that a 0.9 mm width
        check does not mark it, and carrying some 440 mm² of map where one
        millimetre is reckoned to hold 400. Width alone cannot see this, and
        Nunavut's real 1.0 mm neck under 457 mm² is the same case."""
        window = _viewer(qtbot, [_province("Heavy", necked(0.0, neck_mm=1.0,
                                                           size_mm=22.0))])
        window.thin_spin.setValue(0.9)
        window.btn_thin.setChecked(True)
        window._toggle_thin(True)
        _show_stress(window)
        window.stress_spin.setValue(0.0)
        necks = window.shown_necks()
        assert necks, "the bar is a neck"
        neck = necks[0][1]
        assert neck.throat_mm >= 0.9, "and wide enough to pass the width"
        assert neck.at_risk, "and still at risk, on what it is carrying"
        thin = window.thin_reports()[0][1]
        assert thin.thin is None or not thin.thin.intersects(neck.point), \
            "Show thin does not mark the bar — which is the whole point"


class TestMinSeverity:

    def test_it_filters_without_re_measuring(self, mixed):
        """A question about an answer already in hand. Re-measuring here would
        cost fifteen seconds on a real layer for no new information."""
        _show_stress(mixed)
        assert len(mixed.shown_necks()) == 1
        keys = [p._stress_key for p in mixed.provinces]
        reports = [p._stress for p in mixed.provinces]
        mixed.stress_spin.setValue(10.0)
        assert mixed.shown_necks() == []
        assert [p._stress_key for p in mixed.provinces] == keys
        assert [p._stress for p in mixed.provinces] == reports, \
            "the same report objects, so nothing was measured again"
        mixed.stress_spin.setValue(0.0)
        assert len(mixed.shown_necks()) == 1

    def test_the_default_is_the_anchor(self):
        """1.0 is marginal, and a survey of thirteen provinces is asking which
        will break rather than where every sound neck is."""
        assert V.STRESS_SEVERITY[2] == pytest.approx(1.0)

    def test_it_does_nothing_while_the_layer_is_off(self, mixed):
        mixed.stress_spin.setValue(3.0)
        assert mixed._stress_fragment() == ""


class TestTheMeasurementIsKept:

    def test_a_redraw_does_not_measure_again(self, mixed):
        _show_stress(mixed)
        reports = [p._stress for p in mixed.provinces]
        mixed._redraw(preserve_view=True)
        assert [p._stress for p in mixed.provinces] == reports

    def test_the_raster_is_thrown_away(self, mixed):
        """The grid is for editing, and this viewer never edits. Keeping one per
        province costs 116 MB for the eight cleaned provinces, 51 MB of it
        Nunavut."""
        _show_stress(mixed)
        for province in mixed.provinces:
            assert province._stress.grid is None

    def test_report_above_is_not_in_the_cache_key(self, mixed):
        """Measuring with the filter applied would make Min severity a question
        about the province instead of about the answer."""
        assert mixed._stress_params().report_above == 0.0

    def test_a_new_print_scale_is_a_new_question(self, mixed):
        """Throat, load and capacity are all printed quantities, so a reduction
        that did not produce the severities on screen must not be left under
        them."""
        _show_stress(mixed)
        before = [p._stress_key for p in mixed.provinces]
        mixed.scale_spin.setValue(V.DEFAULT_PRINT_SCALE // 2)
        assert [p._stress_key for p in mixed.provinces] != before
        assert mixed.btn_stress.isChecked(), "and it is still on show"


class TestTheStatusLine:

    def test_it_names_the_worst_neck_and_where_it_is(self, mixed):
        _show_stress(mixed)
        text = mixed._stress_fragment()
        assert "Necked" in text
        assert "at risk" in text
        assert "mm" in text

    def test_nothing_found_says_so_rather_than_staying_silent(self, qtbot):
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0))])
        _show_stress(window)
        assert "no neck" in window._stress_fragment()

    def test_unmeasured_is_never_folded_into_nothing_found(self, qtbot):
        """*Unknown* and *sound* are the two answers a printability check must
        never confuse, so a province that defeated the measurement is counted
        out loud even when nothing else was found."""
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0))])
        _show_stress(window)
        window.provinces[0]._stress.measured = False
        text = window._stress_fragment()
        assert "could not be measured" in text

    def test_the_fragment_joins_the_rest_of_the_line(self, mixed):
        _show_stress(mixed)
        mixed._update_status()
        assert "at risk" in mixed.status.text()
        assert "province(s)" in mixed.status.text()


class TestWorstNeck:

    def test_it_frames_the_neck_with_its_surroundings(self, mixed):
        _show_stress(mixed)
        mixed.zoom_to_worst_neck()
        _, neck = mixed.shown_necks()[0]
        x0, x1 = mixed.ax.get_xlim()
        y0, y1 = mixed.ax.get_ylim()
        assert x0 < neck.point.x < x1
        assert y0 < neck.point.y < y1
        span_mm = (x1 - x0) / MM
        assert span_mm == pytest.approx(V.STRESS_ZOOM_MM, rel=0.5), \
            "a throat framed tightly shows nothing but the throat"

    def test_it_says_which_province(self, mixed):
        _show_stress(mixed)
        mixed.zoom_to_worst_neck()
        assert "Necked" in mixed.status.text()

    def test_nothing_to_go_to_is_said_rather_than_ignored(self, mixed):
        _show_stress(mixed)
        mixed.stress_spin.setValue(10.0)
        mixed.zoom_to_worst_neck()
        assert "No neck above" in mixed.status.text()

    def test_it_does_nothing_while_the_layer_is_off(self, mixed):
        before = (mixed.ax.get_xlim(), mixed.ax.get_ylim())
        mixed.zoom_to_worst_neck()
        assert (mixed.ax.get_xlim(), mixed.ax.get_ylim()) == before


class TestTheMarks:

    def test_a_neck_is_outlined_rather_than_filled(self, mixed):
        """Show thin paints whole swathes of coast in its own red — 327 patches
        on Nova Scotia alone — and a filled neck would land in the middle of
        that as one more red shape."""
        _show_stress(mixed)
        filled = [p for p in mixed.ax.collections
                  if any(c[-1] for c in p.get_facecolor())]
        outlines = [p for p in mixed.ax.collections
                    if len(p.get_facecolor()) == 0
                    or not any(c[-1] for c in p.get_facecolor())]
        assert outlines, "the neck's blob should be an outline"
        # The provinces themselves are still filled; this only asserts the neck
        # did not add another fill on top of them.
        assert len(filled) == len([p for p in mixed.provinces if p.parts])

    def test_an_at_risk_neck_gets_a_ring(self, mixed):
        _show_stress(mixed)
        rings = [line for line in mixed.ax.lines
                 if line.get_marker() == "o"]
        assert len(rings) == 2, "one ring, over one casing"

    def test_the_marks_carry_the_severity_colour(self, mixed):
        """Drawn from the lib's own number, so what is on screen is the
        measurement rather than a second opinion about it."""
        _show_stress(mixed)
        _, neck = mixed.shown_necks()[0]
        want = V.SEVERITY_CMAP(min(neck.severity / V.STRESS_VMAX, 1.0))
        drawn = {tuple(round(c, 6) for c in line.get_color())
                 for line in mixed.ax.lines
                 if not isinstance(line.get_color(), str)}
        assert tuple(round(c, 6) for c in want) in drawn


class TestTheRampAgreesWithTheHarness:
    """Two copies of one ramp, held in step.

    The harness and the viewer both spell the severity ramp out, because neither
    should import the other — a harness is throwaway and a viewer is kept. This
    is what stops the two drifting into saying different things about the same
    number.
    """

    def test_the_stops_match(self):
        from harnesses import UI_stress_points as H
        assert V.SEVERITY_STOPS == H.SEVERITY_STOPS

    def test_the_anchor_matches(self):
        from harnesses import UI_stress_points as H
        assert V.STRESS_VMAX == H.DEFAULT_VMAX

    def test_one_lands_on_the_hue_change(self):
        """With the ramp running to twice the anchor, severity 1.0 sits at the
        midpoint, which has to be where the colour turns."""
        stops = dict(V.SEVERITY_STOPS)
        middle = sorted(at for at in stops if 0.0 < at < 1.0)
        assert len(middle) == 2
        assert middle[0] < 0.5 < middle[1]


class TestItDoesNotDisturbTheRest:

    def test_both_layers_can_be_on_at_once(self, mixed):
        mixed.btn_thin.setChecked(True)
        mixed._toggle_thin(True)
        _show_stress(mixed)
        assert mixed.btn_thin.isChecked() and mixed.btn_stress.isChecked()
        assert "thin patch" in mixed._thin_fragment()
        assert "at risk" in mixed._stress_fragment()

    def test_clearing_the_layers_turns_it_off(self, mixed):
        _show_stress(mixed)
        mixed.clear_all()
        assert not mixed.btn_stress.isChecked()
        assert not mixed.btn_stress.isEnabled()

    def test_a_second_press_while_measuring_is_ignored(self, mixed,
                                                      monkeypatch):
        """Letting the event loop through so the window keeps painting also lets
        the button be pressed again; this is the guard that catches it."""
        calls = []
        real = V.stress_points

        def counting(geom, params):
            calls.append(geom)
            mixed._toggle_stress(True)      # the second press, mid-run
            return real(geom, params)

        monkeypatch.setattr(V, "stress_points", counting)
        _show_stress(mixed)
        assert len(calls) == len(mixed.provinces), \
            "each province measured once, not once per re-entry"

    def test_the_selection_still_works_under_the_layer(self, mixed):
        _show_stress(mixed)
        mixed.select_all()
        assert len(mixed.picked) == len(mixed.provinces)
        assert mixed.btn_stress.isChecked()

    def test_a_layer_swap_measures_the_new_layer_with_its_progress(self, qtbot):
        """Two seconds a province is long enough that doing it inside a redraw,
        province by province, would read as the swap having hung."""
        window = _viewer(qtbot, [_province("Necked", necked(20.0))], "First")
        window.layers.append(V.Layer("Second", None,
                                     [_province("Also", necked(40.0))]))
        _show_stress(window)
        assert window.layers[1].provinces[0]._stress is None
        window.show_layer(1)
        assert window.layers[1].provinces[0]._stress is not None
        assert window.shown_necks(), "and the new layer's necks are on show"
