"""
test_island_editor_scale.py

The print scale of UI_island_editor.py: the 1:N reduction, the two map scale
bars it feeds, and the printed millimetres the editing parameters are stated in.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_keys.py.

Run with:  python -m pytest tests/test_island_editor_scale.py -v

What is worth pinning down here is the *direction* the conversion runs.  The
editor states its parameters as sizes on paper while the libs underneath take
ground kilometres, so the scale sits between the two — and the design decision
that follows is that moving the scale leaves the boxes alone and changes what
they are worth, never the reverse.  A test that only checked the arithmetic
would pass just as happily on the opposite behaviour.
"""

import pytest
from shapely.geometry import box

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

#: Two 10 km islands: 100 km² each, which is 100/64 = 1.5625 mm² at 1:8 000 000.
#: The area tests straddle that figure, so they turn on the squaring.
WEST = box(0, 0, 10_000, 10_000)
EAST = box(20_000, 0, 30_000, 10_000)
ISLAND_KM2 = 100.0

KM_BAR, MM_BAR = 0, 1


@pytest.fixture
def editor(qtbot):
    """A shown editor holding two islands, at the default print scale."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window._set_state(M._as_multi(WEST.union(EAST)), [], preserve_view=False)
    yield window
    window.undo_stack.clear()


def _settle(editor):
    """Draw, then assert the bars are a fixed point — another pass moves nothing."""
    editor.canvas.draw()
    assert editor._update_scales() is False, "the scale bars never settled"


def _bar(editor, which):
    """``(label, ground km spanned)`` for the km (0) or mm (1) scale bar."""
    span_m = editor.ax.get_window_extent().width / editor._px_per_metre()
    line, label = editor._scale_artists[which]
    xs = line.get_xdata()
    return label.get_text(), (xs[-1] - xs[0]) * span_m / 1000.0


def _view_span_km(editor):
    return editor.ax.get_window_extent().width / editor._px_per_metre() / 1000.0


class TestNiceNumbers:
    """``_nice`` picks a bar length a reader can take in at a glance."""

    @pytest.mark.parametrize("value, expected", [
        (0.9, 0.5), (1.0, 1.0), (1.7, 1.0), (2.4, 2.0), (3.0, 2.5),
        (6.0, 5.0), (11.0, 10.0), (260.0, 250.0), (999.0, 500.0),
        (1000.0, 1000.0),
    ])
    def test_it_rounds_down_to_the_sequence(self, value, expected):
        assert M._nice(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [0.003, 0.42, 1.0, 7.7, 93.0, 4_180.0])
    def test_it_never_overruns_its_budget(self, value):
        """Rounding *down* is what keeps the bar inside the span it was given."""
        assert M._nice(value) <= value

    @pytest.mark.parametrize("value", [0.003, 0.42, 1.0, 7.7, 93.0, 4_180.0])
    def test_it_never_wastes_more_than_half(self, value):
        """The sequence's widest step is 2x, so a bar is never under half-length."""
        assert M._nice(value) > value / 2

    @pytest.mark.parametrize("value", [1.3, 3.0, 7.7, 9.99])
    def test_it_reads_the_same_in_every_decade(self, value):
        assert M._nice(value * 10) == pytest.approx(M._nice(value) * 10)
        assert M._nice(value / 100) == pytest.approx(M._nice(value) / 100)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_nothing_to_round(self, value):
        assert M._nice(value) == 0.0

    def test_thousands_are_grouped_for_reading(self):
        assert M._grouped(8_000_000) == "8 000 000"
        assert M._grouped(500) == "500"


class TestPrintedSizesBecomeGroundUnits:
    """The editor states paper; the libs take ground. These are the crossings."""

    def test_the_shipped_defaults_are_the_old_kilometre_figures(self, editor):
        """0.25 mm and 0.125 mm at 1:8 000 000 are the 2 km / 1 km this tool had."""
        assert editor.scale_spin.value() == M.DEFAULT_PRINT_SCALE
        assert editor._to_km(editor.buffer_spin.value()) == pytest.approx(2.0)
        assert editor._to_km(editor.brush_spin.value()) == pytest.approx(1.0)

    def test_one_millimetre_is_the_ratio_over_a_million(self, editor):
        editor.scale_spin.setValue(8_000_000)
        assert editor._km_per_mm() == pytest.approx(8.0)
        assert editor._to_km(1.0) == pytest.approx(8.0)

    def test_area_converts_squared_not_linearly(self, editor):
        """The trap: 1 mm² at 1:8 000 000 is 64 km², not 8."""
        editor.scale_spin.setValue(8_000_000)
        assert editor._to_km2(1.0) == pytest.approx(64.0)
        assert editor._to_km2(0.02) == pytest.approx(1.28)

    @pytest.mark.parametrize("km", [0.5, 2.0, 15.0, 480.0])
    def test_a_distance_survives_the_round_trip(self, editor, km):
        assert editor._to_km(editor._to_mm(km)) == pytest.approx(km)

    def test_the_readout_states_the_same_ratio(self, editor):
        for denom, expected in ((8_000_000, "8 km/mm"), (4_000_000, "4 km/mm"),
                                (250_000, "0.25 km/mm")):
            editor.scale_spin.setValue(denom)
            assert editor.perkm_box.text() == expected

    def test_the_readout_is_output_only(self, editor):
        assert editor.perkm_box.isReadOnly()


class TestTheScaleMovesWhatBoxesAreWorth:
    """Not what they say. The constraint belongs to the paper, so it holds still."""

    def test_changing_the_scale_leaves_every_box_alone(self, editor):
        before = (editor.buffer_spin.value(), editor.brush_spin.value(),
                  editor.area_spin.value())
        editor.scale_spin.setValue(2_000_000)
        assert (editor.buffer_spin.value(), editor.brush_spin.value(),
                editor.area_spin.value()) == before

    def test_but_it_does_change_the_ground_they_stand_for(self, editor):
        editor.scale_spin.setValue(8_000_000)
        near = editor._to_km(editor.buffer_spin.value())
        editor.scale_spin.setValue(16_000_000)
        assert editor._to_km(editor.buffer_spin.value()) == pytest.approx(near * 2)

    def test_the_minimum_area_mask_follows_the_scale_alone(self, editor):
        """
        The islands do not change and neither does the threshold; only the paper
        they are being printed on does, and that is enough to condemn them.
        """
        editor.area_spin.setValue(1.5)          # 96 km² at 1:8M — islands survive
        editor.scale_spin.setValue(8_000_000)
        assert "0 below 1.5 mm²" in editor.status.text()

        editor.scale_spin.setValue(8_200_000)   # 100.86 km² — now they do not
        assert "2 below 1.5 mm²" in editor.status.text()

    def test_the_area_threshold_straddles_the_true_printed_area(self, editor):
        """100 km² prints as 1.5625 mm² at 1:8 000 000, and the mask agrees."""
        editor.scale_spin.setValue(8_000_000)
        assert editor._to_km2(ISLAND_KM2 / 64) == pytest.approx(ISLAND_KM2)
        editor.area_spin.setValue(1.56)
        assert "0 below" in editor.status.text()
        editor.area_spin.setValue(1.57)
        assert "2 below" in editor.status.text()

    def test_a_scale_change_holds_the_view(self, editor):
        """It replots for the mask, so it must preserve the camera like any edit."""
        _settle(editor)
        editor.ax.set_xlim(0, 40_000)
        editor.ax.set_ylim(0, 25_000)
        editor.canvas.draw()
        before = (editor.ax.get_xlim(), editor.ax.get_ylim())
        editor.scale_spin.setValue(4_000_000)
        editor.canvas.draw()
        assert (editor.ax.get_xlim(), editor.ax.get_ylim()) == before

    def test_the_gap_readout_is_restated_when_the_scale_moves(self, editor):
        """The ground gap is unchanged; the sentence it is rendered into is not."""
        editor.selected = [0, 1]
        editor._update_status()
        at_8m = editor.status.text()
        editor.scale_spin.setValue(4_000_000)
        editor._update_status()
        assert "gap" in at_8m and "gap" in editor.status.text()
        assert editor.status.text() != at_8m       # the cache did not go stale


class TestTheTwoScaleBars:
    """Both measure the same view, each rounded in its own unit."""

    def test_they_are_hidden_with_no_document(self, editor, qtbot):
        blank = M.IslandEditor()
        qtbot.addWidget(blank)
        blank.resize(900, 600)
        blank.show()
        qtbot.waitExposed(blank)
        blank.canvas.draw()
        assert all(not artist.get_visible()
                   for pair in blank._scale_artists for artist in pair)

    def test_they_appear_once_a_province_is_loaded(self, editor):
        _settle(editor)
        assert all(artist.get_visible()
                   for pair in editor._scale_artists for artist in pair)

    def test_the_kilometre_bar_spans_what_it_says(self, editor):
        _settle(editor)
        label, ground_km = _bar(editor, KM_BAR)
        assert label.endswith(" km")
        assert float(label.split()[0]) == pytest.approx(ground_km)

    def test_the_millimetre_bar_spans_what_it_says(self, editor):
        """Its label is paper, its length is ground; the scale is what joins them."""
        _settle(editor)
        label, ground_km = _bar(editor, MM_BAR)
        printed_mm = float(label.split()[0])
        assert printed_mm * editor._km_per_mm() == pytest.approx(ground_km)

    def test_the_millimetre_bar_names_the_ratio_it_assumes(self, editor):
        editor.scale_spin.setValue(3_000_000)
        _settle(editor)
        label, _ = _bar(editor, MM_BAR)
        assert label.endswith("mm at 1:3 000 000")

    @pytest.mark.parametrize("which", [KM_BAR, MM_BAR])
    def test_a_bar_always_fits_the_view(self, editor, which):
        _settle(editor)
        _, ground_km = _bar(editor, which)
        assert 0 < ground_km <= _view_span_km(editor) * M.SCALE_SPAN

    def test_the_bars_shrink_as_the_view_closes_in(self, editor):
        _settle(editor)
        _, wide_km = _bar(editor, KM_BAR)
        _, wide_mm = _bar(editor, MM_BAR)
        editor.ax.set_xlim(0, 2_000)
        editor.ax.set_ylim(0, 1_500)
        _settle(editor)
        assert _bar(editor, KM_BAR)[1] < wide_km
        assert _bar(editor, MM_BAR)[1] < wide_mm

    @pytest.mark.parametrize("denom", [1_000_000, 4_000_000, 8_000_000,
                                       20_000_000])
    def test_the_millimetre_bar_stays_true_at_any_reduction(self, editor, denom):
        """
        A new reduction can land the mm bar on a different piece of ground, and
        that is not a fault: the bar is rounded in *millimetres*, so halving the
        reduction doubles the millimetres on offer and the sequence need not
        double with them — 1 mm at 1:8 000 000 and 2.5 mm at 1:4 000 000 are each
        the largest round reading that fits.  What has to hold at every scale is
        that the label and the length agree through the ratio, and that the bar
        still fits the view.
        """
        editor.scale_spin.setValue(denom)
        _settle(editor)
        label, ground_km = _bar(editor, MM_BAR)
        assert float(label.split()[0]) * editor._km_per_mm() == pytest.approx(
            ground_km)
        assert 0 < ground_km <= _view_span_km(editor) * M.SCALE_SPAN

    def test_the_two_bars_need_not_cover_the_same_ground(self, editor):
        """
        Each rounds in its own unit, which is the point — one says how far, the
        other how big it comes out — so their lengths are independent. Both are
        nonetheless measuring the one view, so neither can exceed its budget.
        """
        editor.scale_spin.setValue(8_000_000)
        _settle(editor)
        budget = _view_span_km(editor) * M.SCALE_SPAN
        assert 0 < _bar(editor, KM_BAR)[1] <= budget
        assert 0 < _bar(editor, MM_BAR)[1] <= budget

    def test_the_bars_ride_the_corner_rather_than_the_map(self, editor):
        """Axes coordinates, so a pan cannot carry them off the canvas."""
        _settle(editor)
        left = editor._scale_artists[KM_BAR][0].get_xdata()[0]
        editor._shift_view(5_000, 5_000)
        editor.canvas.draw()
        assert editor._scale_artists[KM_BAR][0].get_xdata()[0] == pytest.approx(left)
