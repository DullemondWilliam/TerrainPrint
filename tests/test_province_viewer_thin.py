"""
test_province_viewer_thin.py

The **Show thin** overlay of UI_province_viewer.py — the land too narrow to
print, marked across a whole layer at once.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_province_viewer.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_province_viewer_thin.py -v

The measurement itself is covered by test_thin_features.py and nothing here
re-tests it. What this file guards is the viewer reporting it honestly:

* the width is **printed** millimetres, so turning the print scale asks a
  different question of the same land and the marks have to be re-measured
  rather than left sitting under a reduction that did not produce them;
* a province that could not be measured is never folded into "nothing found",
  because *unknown* and *clean* are the two answers a printability check must
  never confuse;
* necks and whole islets are counted apart, since one wants widening and the
  other wants removing;
* and the measurement is kept between redraws, because the viewer redraws on
  every click and Nunavut takes half a second.
"""

import pytest
from shapely.geometry import Polygon, box

import UI_province_viewer as V
from lib.LIB_thin_features import ThinParams

pytestmark = pytest.mark.usefixtures("qapp")

MM = V.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


def square(x_mm, y_mm, size_mm=4.0):
    return box(x_mm * MM, y_mm * MM,
               (x_mm + size_mm) * MM, (y_mm + size_mm) * MM)


def necked(x_mm, neck_mm=0.4):
    """Two 4 mm squares joined by a neck of the given printed width."""
    half = neck_mm / 2.0
    left, right = x_mm + 4.0, x_mm + 6.0
    return Polygon([
        (x_mm * MM, 0), (left * MM, 0), (left * MM, (2 - half) * MM),
        (right * MM, (2 - half) * MM), (right * MM, 0),
        ((right + 4) * MM, 0), ((right + 4) * MM, 4 * MM),
        (right * MM, 4 * MM), (right * MM, (2 + half) * MM),
        (left * MM, (2 + half) * MM), (left * MM, 4 * MM), (x_mm * MM, 4 * MM)])


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


def _show_thin(window, on=True):
    window.btn_thin.setChecked(on)
    window._toggle_thin(on)


@pytest.fixture
def mixed(qtbot):
    """A layer of three: a fat square, a necked pair, and an islet."""
    return _viewer(qtbot, [
        _province("Fat", square(0.0, 0.0, size_mm=10.0)),
        _province("Necked", necked(20.0)),
        _province("Islet", square(40.0, 0.0, size_mm=0.5)),
    ])


class TestTheOverlay:

    def test_it_is_off_until_asked_for(self, mixed):
        assert not mixed.btn_thin.isChecked()
        assert mixed._thin_fragment() == ""

    def test_the_button_needs_a_layer(self, qtbot):
        window = _viewer(qtbot, [])
        assert not window.btn_thin.isEnabled()
        assert not window.btn_worst.isEnabled()

    def test_zooming_to_worst_needs_the_overlay_on(self, mixed):
        assert not mixed.btn_worst.isEnabled()
        _show_thin(mixed)
        assert mixed.btn_worst.isEnabled()

    def test_turning_it_on_marks_the_map(self, mixed):
        before = len(mixed.ax.collections)
        _show_thin(mixed)
        assert len(mixed.ax.collections) > before

    def test_turning_it_off_takes_the_marks_away(self, mixed):
        _show_thin(mixed)
        marked = len(mixed.ax.collections)
        _show_thin(mixed, False)
        assert len(mixed.ax.collections) < marked
        assert mixed._thin_fragment() == ""

    def test_a_clean_layer_says_so(self, qtbot):
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0, 10.0))])
        _show_thin(window)
        assert "nothing under" in window._thin_fragment()

    def test_a_thin_layer_names_how_many_provinces(self, mixed):
        _show_thin(mixed)
        fragment = mixed._thin_fragment()
        # Necked and Islet are thin; Fat is not.
        assert "in 2 province(s)" in fragment

    def test_the_report_reaches_the_status_line(self, mixed):
        _show_thin(mixed)
        assert mixed._thin_fragment() in mixed.status.text()


class TestNecksAndIsletsAreCountedApart:
    """One wants widening, the other wants removing."""

    def test_an_islet_is_reported_end_to_end(self, mixed):
        _show_thin(mixed)
        assert "1 island(s) too thin end to end" in mixed._thin_fragment()

    def test_a_neck_is_not(self, qtbot):
        window = _viewer(qtbot, [_province("Necked", necked(0.0))])
        _show_thin(window)
        fragment = window._thin_fragment()
        assert "thin patch(es)" in fragment
        assert "end to end" not in fragment


class TestThePrintScaleChangesTheAnswer:
    """The width is printed millimetres, so the reduction is part of the question."""

    @pytest.fixture
    def borderline(self, qtbot):
        # A 0.9 mm neck at 1:8M becomes 1.8 mm at 1:4M — thin, then not.
        return _viewer(qtbot, [_province("Borderline", necked(0.0, neck_mm=0.9))])

    def test_thin_at_one_reduction_and_not_at_another(self, borderline):
        _show_thin(borderline)
        assert "thin patch(es)" in borderline._thin_fragment()
        borderline.scale_spin.setValue(V.DEFAULT_PRINT_SCALE // 2)
        assert "nothing under" in borderline._thin_fragment()

    def test_the_marks_are_re_measured_not_left_behind(self, borderline):
        """A cache keyed on the width alone would serve the old verdict."""
        _show_thin(borderline)
        province = borderline.provinces[0]
        thin_at_8m = province.thin_report(borderline._thin_params()).patches
        borderline.scale_spin.setValue(V.DEFAULT_PRINT_SCALE // 2)
        thin_at_4m = province.thin_report(borderline._thin_params()).patches
        assert thin_at_8m > 0 and thin_at_4m == 0

    def test_a_wider_setting_finds_more(self, borderline):
        _show_thin(borderline)
        borderline.thin_spin.setValue(0.2)
        assert "nothing under" in borderline._thin_fragment()
        borderline.thin_spin.setValue(2.0)
        assert "thin patch(es)" in borderline._thin_fragment()


class TestUnknownIsNotClean:
    """The one confusion a printability check must never make."""

    def test_an_unmeasurable_province_is_reported_as_such(self, qtbot,
                                                          monkeypatch):
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0, 10.0))])

        from lib.LIB_thin_features import ThinReport
        monkeypatch.setattr(V, "thin_features",
                            lambda *a, **k: ThinReport(measured=False))
        _show_thin(window)
        fragment = window._thin_fragment()
        assert "could not be measured" in fragment
        assert "nothing under" not in fragment

    def test_it_is_not_counted_as_a_thin_province(self, qtbot, monkeypatch):
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0, 10.0))])
        from lib.LIB_thin_features import ThinReport
        monkeypatch.setattr(V, "thin_features",
                            lambda *a, **k: ThinReport(measured=False))
        _show_thin(window)
        # Not measured, so it cannot be claimed as clean *or* as flagged with a
        # patch count — only as unmeasured.
        assert "1 could not be measured" in window._thin_fragment()


class TestZoomToWorst:
    """A one-millimetre fault is a few pixels wide on a map of a country."""

    def test_it_tightens_the_view(self, mixed):
        _show_thin(mixed)
        before = mixed.ax.get_xlim()
        mixed.zoom_to_worst()
        after = mixed.ax.get_xlim()
        assert (after[1] - after[0]) < (before[1] - before[0])

    def test_it_centres_on_the_worst_patch(self, mixed):
        _show_thin(mixed)
        patch = mixed._worst_patch()
        mixed.zoom_to_worst()
        minx, miny, maxx, maxy = patch.bounds
        x0, x1 = mixed.ax.get_xlim()
        y0, y1 = mixed.ax.get_ylim()
        assert x0 <= minx and maxx <= x1
        assert y0 <= miny and maxy <= y1

    def test_it_keeps_the_view_shape(self, mixed):
        """One factor on both spans, as Real Size does it."""
        _show_thin(mixed)
        before = mixed.ax.get_xlim(), mixed.ax.get_ylim()
        ratio_before = ((before[0][1] - before[0][0])
                        / (before[1][1] - before[1][0]))
        mixed.zoom_to_worst()
        x0, x1 = mixed.ax.get_xlim()
        y0, y1 = mixed.ax.get_ylim()
        assert (x1 - x0) / (y1 - y0) == pytest.approx(ratio_before, rel=1e-9)

    def test_it_reports_the_size_of_what_it_went_to(self, mixed):
        _show_thin(mixed)
        mixed.zoom_to_worst()
        assert "At the worst patch" in mixed.status.text()
        assert "mm²" in mixed.status.text()

    def test_a_clean_layer_has_nowhere_to_go(self, qtbot):
        window = _viewer(qtbot, [_province("Fat", square(0.0, 0.0, 10.0))])
        _show_thin(window)
        before = window.ax.get_xlim()
        window.zoom_to_worst()
        assert window.ax.get_xlim() == before
        assert "Nothing too thin" in window.status.text()

    def test_it_does_nothing_with_the_overlay_off(self, mixed):
        before = mixed.ax.get_xlim()
        mixed.zoom_to_worst()
        assert mixed.ax.get_xlim() == before


class TestTheMeasurementIsKept:
    """The viewer redraws on every click; Nunavut takes half a second."""

    def test_a_second_look_at_the_same_width_reuses_the_first(self, mixed):
        params = mixed._thin_params()
        province = mixed.provinces[1]
        first = province.thin_report(params)
        assert province.thin_report(params) is first

    def test_a_new_width_is_measured_afresh(self, mixed):
        province = mixed.provinces[1]
        first = province.thin_report(ThinParams(width_mm=1.0))
        second = province.thin_report(ThinParams(width_mm=2.0))
        assert second is not first

    def test_redrawing_does_not_remeasure(self, mixed):
        _show_thin(mixed)
        reports = [id(r) for _, r in mixed.thin_reports()]
        mixed._redraw(preserve_view=True)
        assert [id(r) for _, r in mixed.thin_reports()] == reports

    def test_the_cache_follows_the_scale_too(self, mixed):
        """Same width, different reduction: a different measurement."""
        province = mixed.provinces[1]
        first = province.thin_report(ThinParams(scale=8_000_000, width_mm=1.0))
        second = province.thin_report(ThinParams(scale=4_000_000, width_mm=1.0))
        assert second is not first


class TestBlinkingIsUndisturbed:
    """The overlay must not break what the viewer is for."""

    def test_the_marks_survive_a_layer_swap(self, qtbot):
        window = _viewer(qtbot, [_province("Necked", necked(0.0))])
        window.layers.append(
            V.Layer("Later", None, [_province("Necked", necked(0.0, 2.0))]))
        _show_thin(window)
        assert "thin patch(es)" in window._thin_fragment()
        window.next_layer()
        assert window.btn_thin.isChecked()
        # The second stage widened the neck, so the same overlay now passes.
        assert "nothing under" in window._thin_fragment()

    def test_the_view_does_not_move_on_a_swap(self, qtbot):
        window = _viewer(qtbot, [_province("A", necked(0.0)),
                                 _province("B", square(20.0, 0.0))])
        window.layers.append(V.Layer("Later", None, list(window.provinces)))
        _show_thin(window)
        before = window.ax.get_xlim(), window.ax.get_ylim()
        window.next_layer()
        assert (window.ax.get_xlim(), window.ax.get_ylim()) == before


class TestItOpensOnTheSavedSettings:
    """The viewer reads the settings files when it is built -- the module's
    constants are only the shipped fallbacks."""

    @pytest.mark.parametrize("key, control, value", [
        ("thin.width_mm", "thin_spin", 0.7),
        ("stress.report_above", "stress_spin", 0.5),
        ("print.scale", "scale_spin", 4000000),
    ])
    def test_a_new_viewer_starts_from_the_saved_value(self, qtbot, key,
                                                      control, value):
        from lib.LIB_project_config import update_local
        update_local(key, value)
        viewer = V.ProvinceViewer()
        qtbot.addWidget(viewer)
        assert getattr(viewer, control).value() == pytest.approx(value)

    def test_and_measures_with_it(self, qtbot):
        from lib.LIB_project_config import update_local
        update_local("thin.width_mm", 0.7)
        viewer = V.ProvinceViewer()
        qtbot.addWidget(viewer)
        assert viewer._thin_params().width_mm == pytest.approx(0.7)
