"""Tests for ``lib.LIB_shared_border.simplify_shared_border``.

Two synthetic provinces are built in EPSG:3347 metres, sized so the numbers are
round: at 1:8,000,000 one print-mm is 8000 m, so an 8 km step is 1 mm on paper.
They meet along a seam whose defects are put there deliberately -- sub-nozzle
zigzag, a needle spike, a straight run -- so each conditioning pass can be aimed
at the defect it exists for.

The invariant that matters most is the one a printed pair fails without: the two
provinces must still **exactly partition** the ground they started on. Gap and
overlap are asserted on every path here, including the ones that move the seam a
long way, because a simplification that quietly opens a crack is worse than no
simplification at all.

Nothing here reads ``Data/``; the real-province numbers are in the module
docstring and in USING_SHARED_BORDER.md.
"""

import json

import numpy as np
import pytest
from rasterio.crs import CRS
from shapely.geometry import LineString, MultiLineString, Polygon, box

import geopandas as gpd

from lib.LIB_shared_border import (SeamParams, SeamResult, _chaikin,
                                   _corner_angle, _despike,
                                   _drop_short_segments, _measure,
                                   _shared_seam, _validate,
                                   simplify_shared_border)

WORK_CRS = "EPSG:3347"
SCALE = 8_000_000.0
MPM = SCALE / 1000.0          # 8000 m per print-mm
MM = MPM                      # one print-mm, in metres
ORIGIN = (7_000_000.0, 2_000_000.0)


def _params(**kw):
    """The shipped conditioning, with whatever a test wants to move.

    ``min_segment_mm`` is deliberately *not* pinned here. It defaults to
    ``nozzle_mm``, and pinning the filter while leaving the limit to the config
    is what silently decoupled the two when the project moved to a 0.4 mm
    nozzle: the filter went on collapsing runs under 0.3 mm while the report
    began counting everything under 0.4 mm as unprintable, which is the
    behaviour the lib documents and not a thing to hide from.
    """
    defaults = dict(scale=SCALE, tolerance_mm=0.2, min_angle_deg=20.0,
                    smooth_iterations=0, max_deviation_mm=1.5)
    defaults.update(kw)
    return SeamParams(**defaults)


def _pair(seam_xs_ys, width_mm=20.0, height_mm=20.0):
    """
    Two provinces meeting along ``seam_xs_ys``, given in print-mm from ORIGIN.

    The seam runs bottom-to-top and splits a rectangle, so A is everything left
    of it and B everything right — an exact partition to start from, which is
    what makes a later gap unambiguous.
    """
    ox, oy = ORIGIN
    seam = [(ox + x * MM, oy + y * MM) for x, y in seam_xs_ys]
    left = [(ox, oy)] + seam + [(ox, oy + height_mm * MM)]
    right = ([(ox + width_mm * MM, oy)] + seam
             + [(ox + width_mm * MM, oy + height_mm * MM)])
    a = Polygon(left)
    b = Polygon(right[:1] + seam + right[-1:])
    # Build B the long way round so both are simple, valid rings.
    b = Polygon([(ox + width_mm * MM, oy)] + seam
                + [(ox + width_mm * MM, oy + height_mm * MM)])
    return a, b


def _straight_seam(n=2):
    return [(10.0, y) for y in np.linspace(0.0, 20.0, n)]


def _zigzag_seam(amplitude_mm=0.05, step_mm=0.1, span_mm=20.0):
    """A seam that chatters below the nozzle: fine steps, tiny amplitude."""
    ys = np.arange(0.0, span_mm + step_mm, step_mm)
    return [(10.0 + (amplitude_mm if i % 2 else -amplitude_mm), float(y))
            for i, y in enumerate(ys)]


def _partition_error(before_a, before_b, after_a, after_b):
    """
    Gap and overlap between two geometries, measured here, not asked of the lib.

    ``SeamResult`` reports these too, but a test that reads the report is only
    testing that the lib agrees with itself: hard-coding both to zero once sneaked
    past all 46 tests. Everything about the partition is therefore asserted from
    the geometry, and the report is checked separately against this.
    """
    mm2 = MPM ** 2
    union_before = before_a.union(before_b)
    union_after = after_a.union(after_b)
    return (union_before.symmetric_difference(union_after).area / mm2,
            after_a.intersection(after_b).area / mm2)


def _shares_border(geom_a, geom_b, expected_length_m=None, rel=1e-6):
    """
    Whether two provinces meet along a real border, not merely fail to overlap.

    ``intersection(...).area == 0`` is satisfied by a crack as readily as by a
    shared edge, so it cannot answer "does the border match on both sides". This
    asks the question directly: the boundaries must intersect in a *line*, and
    optionally one of the expected length.
    """
    if not geom_a.touches(geom_b):
        return False
    shared = geom_a.boundary.intersection(geom_b.boundary)
    if shared.is_empty or shared.length <= 0:
        return False
    if expected_length_m is None:
        return True
    return abs(shared.length - expected_length_m) <= rel * expected_length_m


class TestSeamExtraction:

    def test_finds_the_shared_line(self):
        a, b = _pair(_straight_seam())
        seam = _shared_seam(a, b)
        assert seam is not None
        assert seam.geom_type == "MultiLineString"
        assert seam.length == pytest.approx(20.0 * MM, rel=1e-6)

    def test_provinces_that_only_touch_at_a_point_are_not_neighbours(self):
        a = box(0, 0, 10, 10)
        b = box(10, 10, 20, 20)          # corner-to-corner only
        assert _shared_seam(a, b) is None

    def test_disjoint_provinces_raise(self, tmp_path):
        a = box(0, 0, 10, 10)
        b = box(100, 100, 110, 110)
        with pytest.raises(ValueError, match="do not share a border"):
            simplify_shared_border(a, b, _params())

    def test_empty_input_raises(self):
        a, b = _pair(_straight_seam())
        with pytest.raises(ValueError, match="no geometry"):
            simplify_shared_border(Polygon(), b, _params())


class TestPartition:
    """The invariant a printed pair fails without."""

    def test_pair_still_partitions_the_same_ground(self):
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        gap, overlap = _partition_error(a, b, res.a, res.b)
        assert gap == pytest.approx(0.0, abs=1e-6)
        assert overlap == pytest.approx(0.0, abs=1e-6)

    def test_the_two_still_share_their_whole_border(self):
        """The question area alone cannot answer: do they still *meet*?"""
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        assert _shares_border(res.a, res.b, res.seam.length)

    def test_reported_gap_and_overlap_match_an_independent_measurement(self):
        """The numbers the caller reads, and _validate gates on, must be real.

        Hard-coding both reports to zero passed every other test here.
        """
        a, b = _pair(_zigzag_seam(amplitude_mm=0.4))
        res = simplify_shared_border(a, b, _params())
        gap, overlap = _partition_error(a, b, res.a, res.b)
        assert res.gap_area_mm2 == pytest.approx(gap, abs=1e-9)
        assert res.overlap_area_mm2 == pytest.approx(overlap, abs=1e-9)

    def test_reported_area_moved_matches_an_independent_measurement(self):
        a, b = _pair(_zigzag_seam(amplitude_mm=0.4))
        res = simplify_shared_border(a, b, _params())
        moved = res.a.symmetric_difference(a).area / MPM ** 2
        assert res.area_moved_mm2 == pytest.approx(moved, abs=1e-9)
        assert res.area_moved_mm2 > 0.0        # and it is not trivially zero

    def test_reported_seam_length_matches_the_geometry(self):
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        assert res.seam_length_mm == pytest.approx(
            res.seam_original.length / MPM, rel=1e-9)

    def test_union_is_preserved_exactly(self):
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        before = a.union(b)
        after = res.a.union(res.b)
        assert after.area == pytest.approx(before.area, rel=1e-9)
        assert after.symmetric_difference(before).area == pytest.approx(0.0,
                                                                       abs=1e-3)

    def test_outer_boundary_is_untouched(self):
        """Only the seam may move; the coastline is someone else's business."""
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        assert res.a.union(res.b).equals(a.union(b))

    def test_land_changes_hands_but_nothing_is_lost(self):
        a, b = _pair(_zigzag_seam(amplitude_mm=0.4))
        res = simplify_shared_border(a, b, _params())
        gap, _ = _partition_error(a, b, res.a, res.b)
        assert res.area_moved_mm2 > 0.0          # the seam really did move
        assert gap == pytest.approx(0.0, abs=1e-6)
        assert res.a.area + res.b.area == pytest.approx(a.area + b.area, rel=1e-9)
        assert _shares_border(res.a, res.b)

    def test_holes_are_not_filled_in(self):
        """Regression: polygonize returns a face per lake, which was assigned.

        ``union.boundary`` carries interior rings, so every lake came back as a
        face and went to whichever province held it — filling it. Manitoba and
        Ontario gained 378 mm2 of lake before the containment filter.
        """
        ox, oy = ORIGIN
        lake = Polygon([(ox + 3 * MM, oy + 8 * MM), (ox + 6 * MM, oy + 8 * MM),
                        (ox + 6 * MM, oy + 12 * MM), (ox + 3 * MM, oy + 12 * MM)])
        a, b = _pair(_zigzag_seam())
        a_holed = a.difference(lake)
        assert len(a_holed.interiors) == 1        # the fixture really has a hole

        res = simplify_shared_border(a_holed, b, _params())
        gap, _ = _partition_error(a_holed, b, res.a, res.b)
        assert gap == pytest.approx(0.0, abs=1e-6)
        # The lake is still a hole, not land. (A's *area* may legitimately
        # change: simplifying the seam moves land across it.)
        assert len(res.a.interiors) == 1
        assert not res.a.contains(lake.representative_point())
        assert res.a.union(res.b).area == pytest.approx(
            a_holed.union(b).area, rel=1e-9)


class TestPrintability:
    """Each pass, aimed at the defect it exists for."""

    def test_sub_nozzle_chatter_is_removed(self):
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        assert res.short_segments_before > 50
        assert res.short_segments_after == 0
        assert res.vertices_after < res.vertices_before

    def test_a_straight_seam_collapses_to_two_points(self):
        a, b = _pair(_straight_seam(n=40))
        res = simplify_shared_border(a, b, _params())
        assert res.vertices_after == 2
        assert res.max_deviation_mm == pytest.approx(0.0, abs=1e-9)

    def test_an_already_printable_seam_is_left_alone(self):
        """Coarse, honest geometry should not be moved for its own sake."""
        # Every vertex earns its place: well over tolerance_mm from the chord
        # through its neighbours, and every segment well over a nozzle width.
        # (An earlier version of this test used collinear points and was right
        # to fail -- Douglas-Peucker removes those, correctly.)
        seam = [(10.0, 0.0), (11.0, 5.0), (9.0, 10.0), (11.0, 15.0), (10.0, 20.0)]
        a, b = _pair(seam)
        res = simplify_shared_border(a, b, _params())
        assert res.short_segments_before == 0
        assert res.vertices_after == res.vertices_before
        assert res.area_moved_mm2 == pytest.approx(0.0, abs=1e-6)

    def test_tolerance_controls_how_much_is_removed(self):
        a, b = _pair(_zigzag_seam(amplitude_mm=0.3, step_mm=0.5))
        loose = simplify_shared_border(a, b, _params(tolerance_mm=1.0,
                                                     min_segment_mm=0.0))
        tight = simplify_shared_border(a, b, _params(tolerance_mm=0.01,
                                                     min_segment_mm=0.0))
        assert loose.vertices_after < tight.vertices_after

    def test_min_segment_removes_what_tolerance_cannot(self):
        """DP measures perpendicular deviation, so tight zigzag survives it."""
        a, b = _pair(_zigzag_seam(amplitude_mm=0.02, step_mm=0.1))
        dp_only = simplify_shared_border(a, b, _params(tolerance_mm=0.0,
                                                       min_segment_mm=0.0,
                                                       min_angle_deg=0.0))
        # The filter set to the printer's own limit, which is what clears the
        # report: asking for less than the limit cannot, by construction.
        nozzle = _params().nozzle_mm
        at_the_limit = simplify_shared_border(
            a, b, _params(tolerance_mm=0.0, min_segment_mm=nozzle,
                          min_angle_deg=0.0))
        assert dp_only.short_segments_after > 50
        assert at_the_limit.short_segments_after == 0

    def test_a_filter_below_the_limit_does_not_clear_the_report(self):
        """Turning the filter down must not make a seam *look* printable.

        The separation the lib insists on: the passes are driven by the filter
        strengths, the counts are measured against the printer limits. Collapse
        runs under three quarters of the nozzle and the ones between there and
        the nozzle are still unprintable -- and must still be counted. This is
        the guard the suite was missing, and its absence is why moving the
        nozzle to 0.4 mm first showed up as a test failure rather than as the
        reported-versus-filtered distinction working exactly as written.
        """
        a, b = _pair(_zigzag_seam(amplitude_mm=0.02, step_mm=0.1))
        nozzle = _params().nozzle_mm
        detuned = simplify_shared_border(
            a, b, _params(tolerance_mm=0.0, min_segment_mm=nozzle * 0.75,
                          min_angle_deg=0.0))
        assert detuned.short_segments_after > 0

    def test_drift_is_reported_and_bounded(self):
        a, b = _pair(_zigzag_seam(amplitude_mm=0.05))
        res = simplify_shared_border(a, b, _params())
        assert 0.0 <= res.max_deviation_mm <= 0.3

    def test_excessive_drift_is_refused(self):
        a, b = _pair(_zigzag_seam(amplitude_mm=2.0, step_mm=1.0))
        with pytest.raises(ValueError, match="strays"):
            simplify_shared_border(a, b, _params(tolerance_mm=5.0,
                                                 max_deviation_mm=0.5))

    def test_vertex_reduction_is_reported(self):
        a, b = _pair(_zigzag_seam())
        res = simplify_shared_border(a, b, _params())
        assert 0.0 < res.vertex_reduction < 1.0
        assert res.vertex_reduction == pytest.approx(
            1 - res.vertices_after / res.vertices_before)


class TestDespikeInThePipeline:
    """A needle big enough that only the despike pass can remove it.

    The helper has its own tests, but those cannot tell whether the pipeline
    actually runs it -- a mutation sweep proved that by skipping the pass with
    every test still green.

    The spike is 1 mm long: past the short-segment pass, past Douglas-Peucker's
    0.2 mm corridor, and inside the 1.5 mm drift cap. Bigger is not a better
    test -- a 3 mm spike is refused outright, because removing it would move the
    border 24 km at 1:8,000,000, and the drift guard is right to say so.
    """

    @staticmethod
    def _spiked_seam():
        return [(10.0, 0.0), (10.0, 5.0), (11.0, 5.05), (10.0, 5.1),
                (10.0, 20.0)]

    def test_an_oversized_spike_is_refused_not_silently_removed(self):
        """Despiking still answers to max_deviation_mm."""
        a, b = _pair([(10.0, 0.0), (10.0, 5.0), (13.0, 5.05), (10.0, 5.1),
                      (10.0, 20.0)])
        with pytest.raises(ValueError, match="strays"):
            simplify_shared_border(a, b, _params(min_angle_deg=20.0))

    def test_spike_survives_every_other_pass(self):
        a, b = _pair(self._spiked_seam())
        res = simplify_shared_border(a, b, _params(min_angle_deg=0.0))
        assert res.spikes_before >= 1
        assert res.spikes_after >= 1          # nothing else removes it
        assert res.vertices_after == 5

    def test_despike_removes_it(self):
        a, b = _pair(self._spiked_seam())
        res = simplify_shared_border(a, b, _params(min_angle_deg=20.0))
        assert res.spikes_before >= 1
        assert res.spikes_after == 0
        assert res.vertices_after < 5

    def test_removing_it_keeps_the_pair_exact(self):
        a, b = _pair(self._spiked_seam())
        res = simplify_shared_border(a, b, _params(min_angle_deg=20.0))
        gap, overlap = _partition_error(a, b, res.a, res.b)
        assert gap == pytest.approx(0.0, abs=1e-6)
        assert overlap == pytest.approx(0.0, abs=1e-6)
        assert _shares_border(res.a, res.b, res.seam.length)

    def test_min_angle_controls_how_much_is_cut(self):
        a, b = _pair(self._spiked_seam())
        gentle = simplify_shared_border(a, b, _params(min_angle_deg=5.0))
        harsh = simplify_shared_border(a, b, _params(min_angle_deg=80.0))
        assert harsh.vertices_after <= gentle.vertices_after


class TestMeasure:
    """``_measure`` on its own, with defects the pipeline would never let out.

    Every integration test here ends with a gap of exactly zero -- that is the
    point of the module -- so a report hard-coded to zero agrees with all of
    them and sneaks past. The only way to know the numbers are computed rather
    than asserted is to hand the measurement geometry that is genuinely wrong.
    """

    @staticmethod
    def _tiles():
        """Two 10 x 10 mm tiles meeting at x = 10 mm."""
        ox, oy = ORIGIN
        return (box(ox, oy, ox + 10 * MM, oy + 10 * MM),
                box(ox + 10 * MM, oy, ox + 20 * MM, oy + 10 * MM))

    def test_a_real_gap_is_measured(self):
        a, b = self._tiles()
        ox, oy = ORIGIN
        result = SeamResult(a=a, b=box(ox + 11 * MM, oy, ox + 20 * MM,
                                       oy + 10 * MM))
        _measure(a, b, result, _params())
        # A 1 mm strip missing from a 10 mm tall seam: 10 mm2.
        assert result.gap_area_mm2 == pytest.approx(10.0, rel=1e-9)

    def test_a_real_overlap_is_measured(self):
        a, b = self._tiles()
        ox, oy = ORIGIN
        result = SeamResult(a=box(ox, oy, ox + 11 * MM, oy + 10 * MM), b=b)
        _measure(a, b, result, _params())
        # A shared 1 x 10 mm strip.
        assert result.overlap_area_mm2 == pytest.approx(10.0, rel=1e-9)

    def test_area_moved_is_measured(self):
        a, b = self._tiles()
        ox, oy = ORIGIN
        result = SeamResult(a=box(ox, oy, ox + 12 * MM, oy + 10 * MM), b=b)
        _measure(a, b, result, _params())
        # A grew by 2 x 10 mm.
        assert result.area_moved_mm2 == pytest.approx(20.0, rel=1e-9)

    def test_an_exact_partition_measures_zero(self):
        a, b = self._tiles()
        result = SeamResult(a=a, b=b)
        _measure(a, b, result, _params())
        assert result.gap_area_mm2 == pytest.approx(0.0, abs=1e-12)
        assert result.overlap_area_mm2 == pytest.approx(0.0, abs=1e-12)
        assert result.area_moved_mm2 == pytest.approx(0.0, abs=1e-12)

    def test_the_numbers_are_in_print_mm2_not_metres(self):
        """A 1 x 10 mm strip is 10 mm2 on paper and 640 million m2 on the ground."""
        a, b = self._tiles()
        ox, oy = ORIGIN
        result = SeamResult(a=a, b=box(ox + 11 * MM, oy, ox + 20 * MM,
                                       oy + 10 * MM))
        _measure(a, b, result, _params())
        assert result.gap_area_mm2 == pytest.approx(10.0, rel=1e-9)
        assert result.gap_area_mm2 * MPM ** 2 == pytest.approx(10 * MM * MM,
                                                               rel=1e-9)

    def test_measurement_follows_the_scale(self):
        """The same ground gap is a different number of printed mm2."""
        a, b = self._tiles()
        ox, oy = ORIGIN
        gapped = box(ox + 11 * MM, oy, ox + 20 * MM, oy + 10 * MM)
        fine, coarse = SeamResult(a=a, b=gapped), SeamResult(a=a, b=gapped)
        _measure(a, b, fine, _params(scale=SCALE))
        _measure(a, b, coarse, _params(scale=SCALE / 2))
        assert coarse.gap_area_mm2 == pytest.approx(4 * fine.gap_area_mm2,
                                                    rel=1e-9)


class TestValidateGate:
    """The gap/overlap guard, aimed deliberately.

    Every other test here produces an exact partition, so the guard is never
    exercised by them -- removing it left the suite green until these existed.
    """

    def test_overlap_is_refused(self):
        result = SeamResult(a=box(0, 0, 1, 1), b=box(0, 0, 1, 1),
                            overlap_area_mm2=0.5)
        with pytest.raises(ValueError, match="overlap"):
            _validate(result)

    def test_gap_is_refused(self):
        result = SeamResult(a=box(0, 0, 1, 1), b=box(1, 0, 2, 1),
                            gap_area_mm2=0.5)
        with pytest.raises(ValueError, match="gap has opened"):
            _validate(result)

    def test_an_empty_province_is_refused(self):
        with pytest.raises(ValueError, match="came back empty"):
            _validate(SeamResult(a=None, b=box(0, 0, 1, 1)))

    def test_an_exact_partition_passes(self):
        _validate(SeamResult(a=box(0, 0, 1, 1), b=box(1, 0, 2, 1)))

    def test_floating_point_dust_is_tolerated(self):
        """Million-metre coordinates leave sub-picometre residue; that is fine."""
        _validate(SeamResult(a=box(0, 0, 1, 1), b=box(1, 0, 2, 1),
                             gap_area_mm2=1e-12, overlap_area_mm2=1e-12))


class TestPasses:
    """The conditioning helpers, on their own."""

    def test_drop_short_segments_keeps_both_endpoints(self):
        coords = [(0, 0), (0.1, 0), (0.2, 0), (10, 0)]
        out = _drop_short_segments(coords, 1.0)
        assert out[0] == (0, 0)
        assert out[-1] == (10, 0)
        assert len(out) == 2

    def test_drop_short_segments_is_a_no_op_below_two_points(self):
        assert _drop_short_segments([(0, 0), (1, 1)], 10.0) == [(0, 0), (1, 1)]

    def test_despike_removes_a_needle(self):
        # Out and back along almost the same line: a needle at the middle point.
        coords = [(0, 0), (10, 0), (10, 0.01), (0, 0.02), (20, 0.02)]
        out = _despike(coords, 20.0)
        assert len(out) < len(coords)
        assert out[0] == coords[0] and out[-1] == coords[-1]

    def test_despike_keeps_an_honest_corner(self):
        coords = [(0, 0), (10, 0), (10, 10)]      # a right angle
        assert _despike(coords, 20.0) == coords

    def test_despike_iterates_until_stable(self):
        """Removing one spike can expose the next."""
        coords = [(0, 0), (10, 0), (10, 0.01), (0, 0.02), (0, 0.03), (20, 0.04)]
        out = _despike(coords, 30.0)
        assert len(out) == 2 or all(
            _corner_angle(out[i - 1], out[i], out[i + 1]) >= np.radians(30.0)
            for i in range(1, len(out) - 1))

    def test_corner_angle_reads_straight_and_needle(self):
        assert _corner_angle((0, 0), (1, 0), (2, 0)) == pytest.approx(np.pi)
        assert _corner_angle((0, 0), (1, 0), (0, 0.001)) < np.radians(5)

    def test_corner_angle_survives_a_duplicate_point(self):
        assert _corner_angle((1, 1), (1, 1), (2, 2)) == pytest.approx(np.pi)

    def test_chaikin_pins_the_endpoints(self):
        coords = [(0, 0), (10, 10), (20, 0)]
        out = _chaikin(coords)
        assert out[0] == coords[0]
        assert out[-1] == coords[-1]
        assert len(out) > len(coords)

    def test_smoothing_rounds_the_seam(self):
        a, b = _pair([(10.0, 0.0), (12.0, 10.0), (10.0, 20.0)])
        sharp = simplify_shared_border(a, b, _params(smooth_iterations=0))
        round_ = simplify_shared_border(a, b, _params(smooth_iterations=2))
        assert round_.vertices_after > sharp.vertices_after
        gap, _ = _partition_error(a, b, round_.a, round_.b)
        assert gap == pytest.approx(0.0, abs=1e-6)
        assert _shares_border(round_.a, round_.b, round_.seam.length)


class TestIO:

    def test_reads_geojson_and_reprojects(self, tmp_path):
        a, b = _pair(_zigzag_seam())
        paths = []
        for name, geom in (("a", a), ("b", b)):
            path = tmp_path / f"{name}.geojson"
            gpd.GeoDataFrame(geometry=[geom], crs=WORK_CRS).to_crs(4326).to_file(
                path, driver="GeoJSON")
            paths.append(path)
        res = simplify_shared_border(*paths, _params())
        gap, _ = _partition_error(a, b, res.a, res.b)
        assert gap == pytest.approx(0.0, abs=1e-6)
        assert res.vertices_after < res.vertices_before

    def test_geodataframe_input_is_accepted(self):
        a, b = _pair(_zigzag_seam())
        frame_a = gpd.GeoDataFrame(geometry=[a], crs=WORK_CRS)
        frame_b = gpd.GeoDataFrame(geometry=[b], crs=WORK_CRS)
        res = simplify_shared_border(frame_a, frame_b, _params())
        gap, _ = _partition_error(a, b, res.a, res.b)
        assert gap == pytest.approx(0.0, abs=1e-6)

    def test_writes_both_provinces_back_in_the_source_crs(self, tmp_path):
        a, b = _pair(_zigzag_seam())
        src = []
        for name, geom in (("a", a), ("b", b)):
            path = tmp_path / f"{name}.geojson"
            gpd.GeoDataFrame(geometry=[geom], crs=WORK_CRS).to_crs(4326).to_file(
                path, driver="GeoJSON")
            src.append(path)
        out_a, out_b = tmp_path / "a_out.geojson", tmp_path / "b_out.geojson"
        simplify_shared_border(*src, _params(), out_a, out_b)

        for path in (out_a, out_b):
            assert path.exists()
            frame = gpd.read_file(path)
            assert frame.crs.to_epsg() == 4326        # back in the input's CRS
            assert not frame.empty

    def test_written_pair_still_partitions(self, tmp_path):
        """What actually ships is the two files, so assert it on the two files.

        Checking only the overlap is not enough: a crack between them has zero
        intersection area as surely as a shared edge does. Shrinking the written
        province B by 200 m once passed this test.
        """
        a, b = _pair(_zigzag_seam())
        out_a, out_b = tmp_path / "a.geojson", tmp_path / "b.geojson"
        res = simplify_shared_border(a, b, _params(), out_a, out_b)
        ga = gpd.read_file(out_a).geometry.iloc[0]
        gb = gpd.read_file(out_b).geometry.iloc[0]

        gap, overlap = _partition_error(a, b, ga, gb)
        assert overlap == pytest.approx(0.0, abs=1e-6)
        assert gap == pytest.approx(0.0, abs=1e-6)

    def test_the_two_written_files_share_their_border(self, tmp_path):
        """The direct question: does the border match on both GeoJSONs?"""
        a, b = _pair(_zigzag_seam())
        out_a, out_b = tmp_path / "a.geojson", tmp_path / "b.geojson"
        res = simplify_shared_border(a, b, _params(), out_a, out_b)
        ga = gpd.read_file(out_a).geometry.iloc[0]
        gb = gpd.read_file(out_b).geometry.iloc[0]

        assert ga.touches(gb)
        shared = ga.boundary.intersection(gb.boundary)
        assert shared.length == pytest.approx(res.seam.length, rel=1e-6)

    def test_written_files_survive_a_reprojection_round_trip(self, tmp_path):
        """They are written in WGS84 degrees; the border must survive that."""
        a, b = _pair(_zigzag_seam())
        src = []
        for name, geom in (("a", a), ("b", b)):
            path = tmp_path / f"{name}.geojson"
            gpd.GeoDataFrame(geometry=[geom], crs=WORK_CRS).to_crs(4326).to_file(
                path, driver="GeoJSON")
            src.append(path)
        out_a, out_b = tmp_path / "oa.geojson", tmp_path / "ob.geojson"
        res = simplify_shared_border(*src, _params(), out_a, out_b)

        ga = gpd.read_file(out_a).to_crs(WORK_CRS).geometry.iloc[0]
        gb = gpd.read_file(out_b).to_crs(WORK_CRS).geometry.iloc[0]
        assert gpd.read_file(out_a).crs.to_epsg() == 4326      # written as given
        assert _shares_border(ga, gb, res.seam.length, rel=1e-4)
        gap, overlap = _partition_error(res.a, res.b, ga, gb)
        assert gap == pytest.approx(0.0, abs=1e-3)
        assert overlap == pytest.approx(0.0, abs=1e-3)


class TestConfig:

    def test_defaults_come_from_the_config(self):
        from lib.LIB_project_config import get as cfg
        params = SeamParams()
        assert params.nozzle_mm == pytest.approx(cfg("seam.nozzle_mm"))
        assert params.spike_angle_deg == pytest.approx(cfg("seam.spike_angle_deg"))
        assert params.tolerance_mm == pytest.approx(cfg("seam.tolerance_mm"))
        assert params.min_segment_mm == pytest.approx(cfg("seam.min_segment_mm"))
        assert params.min_angle_deg == pytest.approx(cfg("seam.min_angle_deg"))
        assert params.max_deviation_mm == pytest.approx(
            cfg("seam.max_deviation_mm"))
        assert params.scale == cfg("print.scale")

    def test_report_is_measured_against_the_printer_not_the_filter(self):
        """Turning a pass off must not make the seam look printable."""
        a, b = _pair(_zigzag_seam(amplitude_mm=0.02, step_mm=0.1))
        off = simplify_shared_border(a, b, _params(tolerance_mm=0.0,
                                                   min_segment_mm=0.0,
                                                   min_angle_deg=0.0))
        assert off.short_segments_after > 50     # still unprintable, and said so
        assert off.short_segments_after == off.short_segments_before

    def test_filter_strengths_default_to_the_printer_limits(self):
        params = SeamParams()
        assert params.min_segment_mm == pytest.approx(params.nozzle_mm)
        assert params.min_angle_deg == pytest.approx(params.spike_angle_deg)

    def test_mpm_follows_the_scale(self):
        assert SeamParams(scale=8_000_000).mpm == pytest.approx(8000.0)
        assert SeamParams(scale=4_000_000).mpm == pytest.approx(4000.0)

    def test_scale_changes_what_counts_as_sub_nozzle(self):
        """The same ground detail is printable at one scale and not another."""
        a, b = _pair(_zigzag_seam(amplitude_mm=0.05, step_mm=0.1))
        fine = simplify_shared_border(a, b, _params(scale=SCALE))
        coarse = simplify_shared_border(a, b, _params(scale=SCALE / 8))
        assert coarse.vertices_after >= fine.vertices_after


class TestCLI:

    def test_cli_reports_the_improvement(self, tmp_path, capsys):
        from lib.LIB_shared_border import main
        a, b = _pair(_zigzag_seam())
        paths = []
        for name, geom in (("a", a), ("b", b)):
            path = tmp_path / f"{name}.geojson"
            gpd.GeoDataFrame(geometry=[geom], crs=WORK_CRS).to_file(
                path, driver="GeoJSON")
            paths.append(str(path))
        out_a, out_b = tmp_path / "oa.geojson", tmp_path / "ob.geojson"
        assert main([*paths, "-A", str(out_a), "-B", str(out_b),
                     "--scale", str(SCALE)]) == 0
        printed = capsys.readouterr().out
        assert "vertices" in printed and "sub-nozzle" in printed
        assert out_a.exists() and out_b.exists()
