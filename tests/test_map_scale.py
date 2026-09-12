"""
test_map_scale.py

Reading a map in two units at once: ground kilometres, and millimetres of paper
at a print scale.

Run with:  python -m pytest tests/test_map_scale.py -v

Pure arithmetic, so these are properties rather than a table of answers wherever
a property is available -- a scale bar that "looks about right" is the failure
mode here, and only a stated invariant catches it.
"""

import pytest

from lib import LIB_map_scale as S


class TestNiceNumbers:
    """``nice_length`` picks a bar a reader can take in at a glance."""

    @pytest.mark.parametrize("value, expected", [
        (0.9, 0.5), (1.0, 1.0), (1.7, 1.0), (2.4, 2.0), (3.0, 2.5),
        (6.0, 5.0), (11.0, 10.0), (260.0, 250.0), (999.0, 500.0),
        (1000.0, 1000.0),
    ])
    def test_it_rounds_down_to_the_sequence(self, value, expected):
        assert S.nice_length(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [0.003, 0.42, 1.0, 7.7, 93.0, 4_180.0])
    def test_it_never_overruns_its_budget(self, value):
        """Rounding *down* is what keeps the bar inside the span it was given."""
        assert S.nice_length(value) <= value

    @pytest.mark.parametrize("value", [0.003, 0.42, 1.0, 7.7, 93.0, 4_180.0])
    def test_it_never_wastes_more_than_half(self, value):
        """The sequence's widest step is 2x, so a bar is never under half-length."""
        assert S.nice_length(value) > value / 2

    @pytest.mark.parametrize("value", [1.3, 3.0, 7.7, 9.99])
    def test_it_reads_the_same_in_every_decade(self, value):
        assert S.nice_length(value * 10) == pytest.approx(S.nice_length(value) * 10)
        assert S.nice_length(value / 100) == pytest.approx(
            S.nice_length(value) / 100)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_nothing_to_round(self, value):
        assert S.nice_length(value) == 0.0

    def test_thousands_are_grouped_for_reading(self):
        assert S.grouped(8_000_000) == "8 000 000"
        assert S.grouped(500) == "500"


class TestPaperAndGround:
    """The conversions joining a printed millimetre to the ground under it."""

    def test_one_millimetre_is_the_ratio_over_a_million(self):
        assert S.km_per_mm(8_000_000) == pytest.approx(8.0)
        assert S.to_km(1.0, 8_000_000) == pytest.approx(8.0)

    def test_area_converts_squared_not_linearly(self):
        """The trap: 1 mm² at 1:8 000 000 is 64 km², not 8."""
        assert S.to_km2(1.0, 8_000_000) == pytest.approx(64.0)
        assert S.to_km2(0.02, 8_000_000) == pytest.approx(1.28)

    @pytest.mark.parametrize("km", [0.5, 2.0, 15.0, 480.0])
    @pytest.mark.parametrize("denom", [250_000, 8_000_000, 20_000_000])
    def test_a_distance_survives_the_round_trip(self, km, denom):
        assert S.to_km(S.to_mm(km, denom), denom) == pytest.approx(km)

    def test_a_finer_reduction_prints_the_same_ground_bigger(self):
        assert S.to_mm(100.0, 4_000_000) == pytest.approx(
            S.to_mm(100.0, 8_000_000) * 2)

    def test_printed_size_answers_will_it_fit(self):
        """400 x 250 km at 1:8 000 000 is 50 x 31.25 mm of paper."""
        wide, tall = S.printed_size_mm(400_000.0, 250_000.0, 8_000_000)
        assert (wide, tall) == (pytest.approx(50.0), pytest.approx(31.25))


class TestTheTwoBars:
    """One view, two bars, each rounded in its own unit."""

    @pytest.mark.parametrize("denom", [1_000_000, 4_000_000, 8_000_000,
                                       20_000_000])
    @pytest.mark.parametrize("span_m", [5_000.0, 93_600.0, 2_000_000.0])
    def test_each_bar_says_what_it_spans(self, span_m, denom):
        (km_m, km_label), (mm_m, mm_label) = S.scale_bars(span_m, denom)
        assert float(km_label.split()[0]) * 1000.0 == pytest.approx(km_m)
        assert float(mm_label.split()[0]) * S.km_per_mm(denom) == pytest.approx(
            mm_m / 1000.0)

    @pytest.mark.parametrize("denom", [1_000_000, 8_000_000, 20_000_000])
    @pytest.mark.parametrize("span_m", [5_000.0, 93_600.0, 2_000_000.0])
    def test_neither_bar_overruns_the_view(self, span_m, denom):
        for ground_m, _ in S.scale_bars(span_m, denom):
            assert 0 < ground_m <= span_m * S.DEFAULT_BAR_SHARE

    def test_the_printed_bar_names_the_ratio_it_assumes(self):
        _, (_, label) = S.scale_bars(93_600.0, 3_000_000)
        assert label.endswith("mm at 1:3 000 000")

    def test_the_ground_bar_is_named_in_kilometres(self):
        (_, label), _ = S.scale_bars(93_600.0, 8_000_000)
        assert label.endswith(" km")

    @pytest.mark.parametrize("span_m", [5_000.0, 93_600.0, 2_000_000.0])
    def test_the_reduction_moves_only_the_printed_bar(self, span_m):
        """The ground bar is a fact about the view; only the other one is paper."""
        eight = S.scale_bars(span_m, 8_000_000)
        four = S.scale_bars(span_m, 4_000_000)
        assert eight[0] == four[0]

    def test_the_two_are_rounded_independently(self):
        """
        Each rounds in its **own** unit, so the ground they cover is not locked
        together: at some spans a finer reduction lands the printed bar on
        different ground, and at others -- 2 000 km here -- it happens not to.
        Both are correct, which is why nothing may assume either.
        """
        span = 2_000_000.0
        assert (S.scale_bars(span, 8_000_000)[1][0]
                == S.scale_bars(span, 4_000_000)[1][0])        # coincides
        moved = [s for s in (5_000.0, 20_000.0, 93_600.0, 400_000.0)
                 if S.scale_bars(s, 8_000_000)[1][0]
                 != S.scale_bars(s, 4_000_000)[1][0]]
        assert moved, "the printed bar never moved with the reduction"
