"""
test_stress_points.py

Unit tests for LIB_stress_points.py — the necks that hold a printed piece up.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_stress_points.py -v

Four things here are worth more than the rest.

**A spit must not be a stress point.** `LIB_thin_features` finds 338 thin patches
on Nova Scotia and exactly one of them is load-bearing; the whole reason this
module exists is to tell those apart. ``test_a_spit_holds_nothing_up`` is what
holds that line, and a plain square reporting nothing is the same test from the
other side.

**The load is what hangs off the neck, not what is wider than it.** The obvious
measure — the land standing above the throat width — leaves out the branch's own
coastal fringe, and the undercount is not small: two 36 mm² squares on a 1.6 mm
bar come out at 20 mm² each and the neck vanishes under ``min_lobe_mm2``
entirely. ``TestTheLoad`` pins the cut-based measurement that fixed it.

**Severity is anchored, not merely ordered.** 1.0 means marginal, because the
capacity comes from ``bridges.mass_per_bridge_mm2`` — this project's own claim
that 1 mm of causeway holds 400 mm² of map. ``test_the_calibration_anchor`` is
the one test that would catch the whole scale drifting.

**The incremental answer must equal the full one.** ``restress`` reuses a cached
raster and cached tiles, and the failure mode is not a crash but a confidently
stale overlay. ``TestRestressAgreesWithAFullRun`` brushes the geometry and
insists on equality; ``test_a_dirty_grid_that_lies_is_caught`` shows what
``verify`` is for.

Geometry is in metres (EPSG:3347) and everything measured is print-mm, so the
fixtures are written in millimetres and multiplied by ``MM``. A 6 mm square is
36 mm², comfortably over the 25 mm² floor on what counts as a larger area; a
4 mm square is 16 mm² and deliberately under it.
"""

import unittest

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box

from lib.LIB_stress_points import (COMPACT_SLENDERNESS, StressGrid,
                                   StressParams, StressReport, _needed_mm,
                                   branch_mask, restress, severity_field,
                                   stress_points, width_field)


class StressTestCase(unittest.TestCase):
    """One print-mm is 8 km at the project's 1:8,000,000."""

    MM = 8000.0

    def square(self, size_mm=6.0, x_mm=0.0, y_mm=0.0):
        mm = self.MM
        return box(x_mm * mm, y_mm * mm, (x_mm + size_mm) * mm,
                   (y_mm + size_mm) * mm)

    def dumbbell(self, neck_mm, size_mm=6.0, neck_len_mm=2.0):
        """Two squares joined by a bar of the given printed width."""
        mm = self.MM
        half, mid = neck_mm / 2.0, size_mm / 2.0
        a, b = size_mm, size_mm + neck_len_mm
        return shapely.union_all([
            box(0, 0, a * mm, size_mm * mm),
            box(a * mm, (mid - half) * mm, b * mm, (mid + half) * mm),
            box(b * mm, 0, (b + size_mm) * mm, size_mm * mm)])

    def spit(self, width_mm=0.4, length_mm=3.0, size_mm=6.0):
        """One square with a thin spike off it: narrow, but holding nothing."""
        mm = self.MM
        mid = size_mm / 2.0
        return shapely.union_all([
            box(0, 0, size_mm * mm, size_mm * mm),
            box(size_mm * mm, (mid - width_mm / 2) * mm,
                (size_mm + length_mm) * mm, (mid + width_mm / 2) * mm)])

    def twice_joined(self, narrow_mm, wide_mm, size_mm=6.0, gap_mm=2.0):
        """Two squares joined by two bars: a cycle, so the load is shared."""
        mm = self.MM
        a, b = size_mm, size_mm + gap_mm
        return shapely.union_all([
            box(0, 0, a * mm, size_mm * mm),
            box(a * mm, (size_mm * 0.25 - narrow_mm / 2) * mm, b * mm,
                (size_mm * 0.25 + narrow_mm / 2) * mm),
            box(a * mm, (size_mm * 0.75 - wide_mm / 2) * mm, b * mm,
                (size_mm * 0.75 + wide_mm / 2) * mm),
            box(b * mm, 0, (b + size_mm) * mm, size_mm * mm)])

    def only_neck(self, geom, params=None):
        report = stress_points(geom, params or StressParams())
        self.assertTrue(report.measured)
        self.assertEqual(len(report.necks), 1,
                         f"expected one neck, got {[str(n) for n in report.necks]}")
        return report.necks[0]


class TestWhatCountsAsANeck(StressTestCase):

    def test_a_plain_square_has_no_necks(self):
        """Nothing holds anything up in a square, at any size.

        The counterpart of ``test_a_plain_square_has_nothing_thin`` next door: a
        detector that found a fault in a 6 mm square would be reporting its own
        corners, and would be off within a minute.
        """
        for size_mm in (4.0, 6.0, 8.0):
            report = stress_points(self.square(size_mm))
            self.assertTrue(report.measured)
            self.assertEqual(report.necks, [], f"{size_mm} mm square")
            self.assertTrue(report.sound)

    def test_a_dumbbell_has_exactly_one_neck(self):
        neck = self.only_neck(self.dumbbell(0.5))
        self.assertGreater(neck.load_mm2, 0.0)

    def test_the_throat_is_the_bar_it_measures(self):
        """Within one pixel, which is all a raster width field can promise."""
        params = StressParams()
        for bar_mm in (0.25, 0.4, 0.8, 1.0, 2.0):
            neck = self.only_neck(self.dumbbell(bar_mm), params)
            self.assertAlmostEqual(neck.throat_mm, bar_mm,
                                   delta=1.5 * params.pixel_mm,
                                   msg=f"{bar_mm} mm bar")

    def test_a_thin_bar_is_at_risk_and_a_thick_one_is_not(self):
        self.assertTrue(self.only_neck(self.dumbbell(0.25)).at_risk)
        self.assertFalse(self.only_neck(self.dumbbell(1.0)).at_risk)

    def test_severity_falls_as_the_bar_widens(self):
        """Monotone, because stress goes as one over the width squared."""
        got = [self.only_neck(self.dumbbell(bar)).severity
               for bar in (0.25, 0.5, 1.0, 2.0)]
        self.assertEqual(got, sorted(got, reverse=True), got)

    def test_a_spit_holds_nothing_up(self):
        """Narrow is not the same as load-bearing. The whole point, in one test.

        A 0.4 mm spike off a square is thinner than anything else here and
        ``LIB_thin_features`` would flag it, correctly, as too thin to print.
        Nothing hangs on it, so it is not a stress point.
        """
        report = stress_points(self.spit())
        self.assertTrue(report.measured)
        self.assertEqual(report.necks, [])

    def test_ends_below_the_floor_are_not_larger_areas(self):
        """4 mm squares are 16 mm², under the 25 mm² floor, so the bar holds up
        nothing worth reporting -- and raising the floor makes 6 mm squares stop
        counting too, which is the knob doing what it says."""
        self.assertEqual(stress_points(self.dumbbell(0.5, size_mm=4.0)).necks, [])
        strict = StressParams(min_lobe_mm2=100.0)
        self.assertEqual(stress_points(self.dumbbell(0.5), strict).necks, [])

    def test_a_join_wider_than_the_ceiling_is_not_a_neck(self):
        """There is no printing problem at a 4 mm throat, and saying so is what
        keeps the report short enough to read."""
        low = StressParams(ceiling_mm=1.0)
        self.assertEqual(stress_points(self.dumbbell(2.0), low).necks, [])


class TestTheLoad(StressTestCase):

    def test_the_load_is_the_whole_end_not_what_outranks_the_throat(self):
        """A 6 mm square is 36 mm², and stays 36 mm² however wide the bar.

        Measuring the branch as "the land standing above the throat width"
        leaves out its own coastal fringe, and the error grows with the throat:
        at a 1.6 mm bar each 36 mm² square reads as 20 mm², which is under
        ``min_lobe_mm2``, and the neck disappears. This is the regression for
        measuring by cutting the neck instead.
        """
        for bar_mm in (0.25, 0.5, 1.0, 1.6):
            neck = self.only_neck(self.dumbbell(bar_mm))
            self.assertTrue(neck.severed, f"{bar_mm} mm bar: cut did not part")
            self.assertAlmostEqual(neck.load_mm2, 36.0, delta=6.0,
                                   msg=f"{bar_mm} mm bar")

    def test_both_sides_are_measured_the_same_way(self):
        """A symmetric dumbbell has symmetric sides, or one of them is wrong."""
        neck = self.only_neck(self.dumbbell(0.5))
        self.assertAlmostEqual(neck.load_mm2, neck.other_mm2, delta=2.0)
        self.assertLessEqual(neck.load_mm2, neck.other_mm2 + 1e-9)

    def test_the_load_is_bigger_when_more_hangs_on_it(self):
        small = self.only_neck(self.dumbbell(0.5, size_mm=6.0))
        large = self.only_neck(self.dumbbell(0.5, size_mm=12.0))
        self.assertGreater(large.load_mm2, 3 * small.load_mm2)
        self.assertGreater(large.severity, small.severity)

    def test_the_branch_mask_is_the_land_the_load_counted(self):
        """``branch_mask`` has to be the same land ``load_mm2`` was measured on;
        an overlay that highlighted something else would be a second opinion."""
        report = stress_points(self.dumbbell(0.5))
        neck = report.necks[0]
        mask = branch_mask(report, neck)
        self.assertIsNotNone(mask)
        area_mm2 = float(mask.sum()) * report.grid.pixel_mm ** 2
        self.assertAlmostEqual(area_mm2, neck.load_mm2, places=6)

    def test_a_cycle_shares_the_load_instead_of_doubling_it(self):
        """Two bars between the same squares is one join, at the wider bar.

        The narrow bar is not a second neck: by the time it appears the two
        sides are already one piece, so nothing is being held up there. And the
        load must not be counted twice -- it is the same square either way.
        """
        once = self.only_neck(self.dumbbell(1.0))
        twice = self.only_neck(self.twice_joined(0.5, 1.0))
        self.assertAlmostEqual(twice.throat_mm, 1.0, delta=0.15)
        self.assertLess(twice.load_mm2, 2 * once.load_mm2)


class TestTheScore(StressTestCase):

    def test_the_calibration_anchor(self):
        """400 mm² wants exactly 1 mm, because that is what the bridge planner
        says one bridge holds. If this drifts, every severity drifts with it."""
        params = StressParams(lever_power=0.0)
        needed, _ = _needed_mm(params.capacity_mm2, 0.0, params)
        self.assertAlmostEqual(needed, params.capacity_at_mm, places=9)

    def test_the_load_enters_as_a_square_root(self):
        """Four times the load wants twice the width."""
        params = StressParams(lever_power=0.0)
        one, _ = _needed_mm(100.0, 0.0, params)
        four, _ = _needed_mm(400.0, 0.0, params)
        self.assertAlmostEqual(four, 2 * one, places=9)

    def test_a_compact_load_is_not_penalised_for_its_lever(self):
        """The normalisation: a blob sitting against its own neck scores 1.0, so
        the lever arm can only ever argue a load *up*, never below the bridge
        calibration."""
        params = StressParams(lever_power=1.0)
        load = 400.0
        compact_lever = COMPACT_SLENDERNESS * load ** 0.5
        plain = _needed_mm(load, 0.0, StressParams(lever_power=0.0))[0]
        self.assertAlmostEqual(_needed_mm(load, compact_lever, params)[0],
                               plain, places=9)
        self.assertGreater(_needed_mm(load, 4 * compact_lever, params)[0], plain)

    def test_severity_is_needed_over_throat(self):
        neck = self.only_neck(self.dumbbell(0.5))
        self.assertAlmostEqual(neck.severity, neck.needed_mm / neck.throat_mm,
                               places=9)
        self.assertEqual(neck.at_risk, neck.severity >= 1.0)

    def test_report_above_filters_what_is_reported(self):
        geom = self.dumbbell(1.0)
        self.assertEqual(len(stress_points(geom).necks), 1)
        quiet = StressParams(report_above=10.0)
        self.assertEqual(stress_points(geom, quiet).necks, [])

    def test_necks_come_back_worst_first(self):
        """Two necks of different widths on one piece, ordered by severity."""
        mm = self.MM
        chain = shapely.union_all([
            box(0, 0, 6 * mm, 6 * mm),
            box(6 * mm, 2.75 * mm, 8 * mm, 3.25 * mm),          # 0.5 mm
            box(8 * mm, 0, 14 * mm, 6 * mm),
            box(14 * mm, 2.9 * mm, 16 * mm, 3.1 * mm),          # 0.2 mm
            box(16 * mm, 0, 22 * mm, 6 * mm)])
        report = stress_points(chain)
        self.assertEqual(len(report.necks), 2, [str(n) for n in report.necks])
        self.assertEqual([n.severity for n in report.necks],
                         sorted([n.severity for n in report.necks], reverse=True))
        self.assertEqual(report.worst, report.necks[0].severity)


class TestTheMarks(StressTestCase):

    def test_the_bar_is_as_long_as_the_throat_is_wide(self):
        """The bar is the line the piece would break along, so its length is the
        measurement -- a mark of any other length would be decoration."""
        for bar_mm in (0.25, 0.5, 1.0):
            neck = self.only_neck(self.dumbbell(bar_mm))
            self.assertIsInstance(neck.throat, LineString)
            self.assertAlmostEqual(neck.throat.length / self.MM, neck.throat_mm,
                                   places=6)

    def test_the_bar_crosses_the_neck_rather_than_running_along_it(self):
        """A horizontal bar between two squares breaks on a vertical line."""
        neck = self.only_neck(self.dumbbell(0.5))
        (x0, y0), (x1, y1) = list(neck.throat.coords)
        self.assertGreater(abs(y1 - y0), abs(x1 - x0))

    def test_the_point_and_the_region_sit_on_the_neck(self):
        neck = self.only_neck(self.dumbbell(0.5))
        geom = self.dumbbell(0.5)
        self.assertTrue(geom.buffer(self.MM * 0.05).contains(neck.point))
        self.assertIsNotNone(neck.region)
        self.assertGreater(neck.region.area, 0.0)
        # The blob is the waist, not the neighbourhood: it is also the cut the
        # load is weighed by, so if it sprawled the load would be wrong.
        self.assertLess(neck.region.area / self.MM ** 2, 4.0)


class TestTheOverlayLayers(StressTestCase):

    def test_the_severity_field_is_zero_except_at_the_necks(self):
        report = stress_points(self.dumbbell(0.25))
        field, transform = severity_field(report)
        self.assertEqual(field.shape, tuple(report.grid.shape))
        self.assertEqual(transform, report.grid.transform)
        self.assertAlmostEqual(float(field.max()), report.worst, places=5)
        painted = int((field > 0).sum())
        self.assertGreater(painted, 0)
        self.assertLess(painted, int(report.grid.land.sum()) // 4)

    def test_the_width_field_is_the_width_in_millimetres(self):
        report = stress_points(self.dumbbell(0.5))
        field, _ = severity_field(report)
        widths, _ = width_field(report)
        grid = report.grid
        self.assertEqual(widths.shape, tuple(grid.shape))
        self.assertLessEqual(float(widths.max()), grid.ceiling_mm + 1e-6)
        self.assertEqual(float(widths[~grid.land].max(initial=0.0)), 0.0)
        # The bar is the narrowest land in the picture.
        self.assertGreater(float(widths[grid.land].min()), 0.0)

    def test_an_empty_report_draws_nothing_rather_than_crashing(self):
        field, transform = severity_field(StressReport())
        self.assertIsNone(field)
        self.assertIsNone(transform)


class TestHonesty(StressTestCase):

    def test_an_invalid_ring_is_repaired_or_refused_never_silently_clean(self):
        """A bow-tie defeats most geometry outright. Either the repair worked and
        we say so, or we could not tell -- but "nothing will break" is not an
        answer available here."""
        bow_tie = Polygon([(0, 0), (6 * self.MM, 6 * self.MM),
                           (6 * self.MM, 0), (0, 6 * self.MM)])
        self.assertFalse(bow_tie.is_valid)
        report = stress_points(bow_tie)
        self.assertTrue(report.repaired or not report.measured)

    def test_nothing_to_measure_is_not_a_clean_bill(self):
        for geom in (None, Polygon()):
            report = stress_points(geom)
            self.assertEqual(report.necks, [])
            self.assertIsNone(report.grid)

    def test_the_verdict_carries_the_numbers(self):
        report = stress_points(self.dumbbell(0.25))
        verdict = report.verdict()
        self.assertIn("at risk", verdict)
        self.assertIn("mm", verdict)
        self.assertEqual(stress_points(self.square()).verdict(),
                         "no load-bearing necks found")
        self.assertEqual(StressReport(measured=False).verdict(),
                         "could not measure this geometry")

    def test_the_land_area_is_the_geometry_it_was_given(self):
        geom = self.dumbbell(1.0)
        report = stress_points(geom)
        self.assertAlmostEqual(report.land_area_mm2,
                               geom.area / self.MM ** 2, delta=0.5)


class TestTheGridComesBack(StressTestCase):

    def test_the_report_carries_the_raster_it_worked_on(self):
        report = stress_points(self.dumbbell(0.5))
        grid = report.grid
        self.assertIsInstance(grid, StressGrid)
        self.assertEqual(grid.land.shape, tuple(grid.shape))
        self.assertEqual(grid.level.shape, tuple(grid.shape))
        self.assertTrue(grid.tiles)
        self.assertEqual(report.tiles_recomputed, report.tiles_total)
        self.assertGreater(grid.nbytes, 0)

    def test_the_grid_has_room_to_grow(self):
        """A margin, so that nudging the coast does not throw the cache away."""
        geom = self.dumbbell(0.5)
        grid = stress_points(geom).grid
        self.assertTrue(grid.covers(geom))
        minx, miny, maxx, maxy = geom.bounds
        gminx, gminy, gmaxx, gmaxy = grid.bounds
        margin_m = StressParams().margin_mm * self.MM
        self.assertLessEqual(gminx, minx - margin_m * 0.9)
        self.assertGreaterEqual(gmaxy, maxy + margin_m * 0.9)
        self.assertFalse(grid.covers(box(gmaxx + 1, gmaxy + 1,
                                         gmaxx + 2, gmaxy + 2)))

    def test_the_level_field_never_overflows_its_type(self):
        """0.01 mm bands make 300 steps of a 3 mm ceiling, and a uint8 would
        wrap the thick land round to 44 and call it narrow."""
        fine = StressParams(band_mm=0.01)
        grid = stress_points(self.dumbbell(0.5), fine).grid
        self.assertEqual(int(grid.level.max()), fine.nbands)

    def test_dirty_from_is_the_exclusive_or(self):
        grid = stress_points(self.dumbbell(0.5)).grid
        after = grid.land.copy()
        after[10, 10] = not after[10, 10]
        dirty = grid.dirty_from(after)
        self.assertEqual(int(dirty.sum()), 1)
        self.assertTrue(dirty[10, 10])


class TestRestressAgreesWithAFullRun(StressTestCase):
    """The test that matters: reuse must not change the answer.

    ``restress`` splices the width field and keeps every tile the edit could not
    reach, and the failure mode is not a crash but an overlay that is quietly
    out of date. So the assertion is equality against a from-scratch run, edit
    after edit, rather than anything about speed.
    """

    def setUp(self):
        self.params = StressParams()
        self.geom = self.dumbbell(0.5)
        self.base = stress_points(self.geom, self.params)
        self.grid = self.base.grid

    def _brush(self, land, row, col, radius=1):
        out = land.copy()
        out[max(0, row - radius):row + radius + 1,
            max(0, col - radius):col + radius + 1] ^= True
        return out

    def _same(self, got, want, where=""):
        key = lambda ns: [(round(n.severity, 9), round(n.throat_mm, 9),
                           round(n.load_mm2, 6)) for n in ns]
        self.assertEqual(key(got), key(want), where)

    def test_a_brush_stroke_gives_the_same_answer_either_way(self):
        rng = np.random.default_rng(17)
        rows, cols = np.nonzero(self.grid.land)
        grid, land = self.grid, self.grid.land
        for k in range(6):
            i = int(rng.integers(rows.size))
            land = self._brush(land, int(rows[i]), int(cols[i]))
            got = restress(grid, land=land, params=self.params)
            want = stress_points_on(land, grid, self.params)
            self.assertFalse(got.rebuilt)
            self._same(got.necks, want, f"edit {k}")
            grid = got.grid

    def test_verify_does_the_same_check_from_inside(self):
        checked = StressParams(verify=True)
        rows, cols = np.nonzero(self.grid.land)
        land = self._brush(self.grid.land, int(rows[0]) + 3, int(cols[0]) + 3)
        got = restress(self.grid, land=land, params=checked)   # must not raise
        self.assertFalse(got.rebuilt)

    def test_a_dirty_grid_that_lies_is_caught(self):
        """``dirty`` has to be a superset of what changed. Too small is the one
        failure that would otherwise be silent, so ``verify`` exists to make it
        loud -- and this is the test that proves it does."""
        rows, cols = np.nonzero(self.grid.land)
        mid = int(rows.size // 2)
        land = self._brush(self.grid.land, int(rows[mid]), int(cols[mid]), 2)
        nothing = np.zeros(self.grid.shape, bool)
        with self.assertRaises(AssertionError):
            restress(self.grid, land=land, dirty=nothing,
                     params=StressParams(verify=True))

    def test_an_edit_touches_only_a_few_tiles(self):
        rows, cols = np.nonzero(self.grid.land)
        land = self._brush(self.grid.land, int(rows[0]) + 5, int(cols[0]) + 5)
        got = restress(self.grid, land=land, params=self.params)
        self.assertGreater(got.tiles_recomputed, 0)
        self.assertLessEqual(got.tiles_recomputed, max(4, got.tiles_total // 2))

    def test_an_unchanged_edit_changes_nothing(self):
        got = restress(self.grid, land=self.grid.land.copy(),
                       params=StressParams(verify=True))
        self._same(got.necks, self.base.necks)
        self.assertEqual(got.tiles_recomputed, 0)

    def test_geometry_can_be_handed_in_instead_of_a_raster(self):
        got = restress(self.grid, geom=self.dumbbell(0.8), params=self.params)
        self.assertFalse(got.rebuilt)
        self.assertEqual(len(got.necks), 1)
        self.assertAlmostEqual(got.necks[0].throat_mm, 0.8, delta=0.15)

    def test_a_dirty_grid_of_the_wrong_shape_is_refused(self):
        with self.assertRaises(ValueError):
            restress(self.grid, land=self.grid.land, dirty=np.zeros((3, 3), bool),
                     params=self.params)
        with self.assertRaises(ValueError):
            restress(self.grid, params=self.params)


class TestWhenTheGridCannotBeReused(StressTestCase):
    """A rebuild is always legal, and is always reported.

    The caller is never left to guess whether it got the fast path, because a
    silent fallback and a silent staleness look the same from outside.
    """

    def setUp(self):
        self.geom = self.dumbbell(0.5)
        self.grid = stress_points(self.geom, StressParams()).grid

    def test_a_different_pixel_rebuilds(self):
        got = restress(self.grid, geom=self.geom,
                       params=StressParams(pixel_mm=0.2))
        self.assertTrue(got.rebuilt)
        self.assertAlmostEqual(got.pixel_mm, 0.2)

    def test_a_different_ceiling_or_band_or_tile_rebuilds(self):
        for params in (StressParams(ceiling_mm=2.0), StressParams(band_mm=0.05),
                       StressParams(tile_px=64)):
            got = restress(self.grid, geom=self.geom, params=params)
            self.assertTrue(got.rebuilt)

    def test_land_that_grows_past_the_grid_rebuilds(self):
        mm = self.MM
        bigger = shapely.union_all([self.geom,
                                    box(40 * mm, 40 * mm, 46 * mm, 46 * mm)])
        got = restress(self.grid, geom=bigger, params=StressParams())
        self.assertTrue(got.rebuilt)
        self.assertTrue(got.grid.covers(bigger))

    def test_a_raster_of_the_wrong_shape_rebuilds_from_the_geometry(self):
        got = restress(self.grid, land=np.zeros((5, 5), bool), geom=self.geom,
                       params=StressParams())
        self.assertTrue(got.rebuilt)


def stress_points_on(land, grid, params):
    """A from-scratch run on a given raster: the ground truth for reuse tests.

    Goes through the module's own internals rather than through
    :func:`stress_points` so that the comparison is against the same grid and
    the same transform, and only the reuse is on trial.
    """
    import lib.LIB_stress_points as S
    fresh = S.StressGrid(
        transform=grid.transform, shape=grid.shape,
        land=np.asarray(land, bool).copy(),
        level=S._levels(land, grid.pixel_mm, grid.band_mm, params.nbands),
        pixel_mm=grid.pixel_mm, ceiling_mm=grid.ceiling_mm,
        band_mm=grid.band_mm, tile_px=grid.tile_px)
    necks, _, _ = S._run(fresh, params)
    return necks


if __name__ == "__main__":
    unittest.main()
