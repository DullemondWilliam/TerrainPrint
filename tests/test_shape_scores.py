"""
test_shape_scores.py

Unit tests for LIB_shape_scores.py — what an editing session cost.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_shape_scores.py -v

Two of these matter more than the rest.

**Agreement is over the land, not over the grid.** Most of a province's
bounding box is sea, so counting the water both versions agree about would put
every score above 99% and make the number decoration.
``test_two_islands_nowhere_near_each_other_agree_not_at_all`` is what holds
that: the same fixture scored over the whole grid would come out at 99.99%.

**The two versions share one grid.** Rasterising each onto a grid fitted to its
own bounding box would misalign them, and every count would carry the offset as
error — invisibly, and worst on exactly the small edits the panel exists to
report.

Geometry is in metres (EPSG:3347) and the numbers are print-mm, so the fixtures
are written in millimetres and multiplied by ``MM``. They are also aligned to
the 0.1 mm grid, so the expected cell counts are exact rather than approximate.
"""

import unittest

from shapely.geometry import Polygon, box

from lib.LIB_shape_scores import (MAX_PIXELS, ScoreParams, ShapeScores,
                                  land_mm2, outline_mm, score_shapes)


class ScoreTestCase(unittest.TestCase):
    """One print-mm is 8 km at 1:8,000,000; one pixel is 0.1 mm, so 800 m."""

    MM = 8000.0

    def square(self, x_mm, y_mm, size_mm=4.0):
        return box(x_mm * self.MM, y_mm * self.MM,
                   (x_mm + size_mm) * self.MM, (y_mm + size_mm) * self.MM)


class TestTheCheapMeasures(ScoreTestCase):
    """Outline and land: straight conversions, and the panel updates live on them."""

    def test_outline_is_the_whole_boundary_in_print_mm(self):
        # A 4 mm square has a 16 mm outline.
        self.assertAlmostEqual(outline_mm(self.square(0.0, 0.0)), 16.0, places=6)

    def test_outline_counts_lake_shores_too(self):
        lake = box(1 * self.MM, 1 * self.MM, 2 * self.MM, 2 * self.MM)
        holed = Polygon(self.square(0.0, 0.0).exterior, [lake.exterior])
        # 16 mm of coast plus 4 mm of lake shore.
        self.assertAlmostEqual(outline_mm(holed), 20.0, places=6)

    def test_land_is_area_in_print_mm2(self):
        self.assertAlmostEqual(land_mm2(self.square(0.0, 0.0)), 16.0, places=6)

    def test_nothing_measures_zero(self):
        for geom in (None, Polygon()):
            self.assertEqual(outline_mm(geom), 0.0)
            self.assertEqual(land_mm2(geom), 0.0)

    def test_both_follow_the_print_scale(self):
        half = ScoreParams(scale=4_000_000)
        self.assertAlmostEqual(outline_mm(self.square(0.0, 0.0), half), 32.0,
                               places=6)
        self.assertAlmostEqual(land_mm2(self.square(0.0, 0.0), half), 64.0,
                               places=6)

    def test_the_changes_are_signed_shares(self):
        big, small = self.square(0.0, 0.0, 4.0), self.square(0.0, 0.0, 2.0)
        scores = score_shapes(big, small)
        self.assertAlmostEqual(scores.perimeter_change, -0.5, places=6)
        self.assertAlmostEqual(scores.land_change, -0.75, places=6)

    def test_growing_reports_a_gain(self):
        scores = score_shapes(self.square(0.0, 0.0, 2.0),
                              self.square(0.0, 0.0, 4.0))
        self.assertGreater(scores.perimeter_change, 0.0)
        self.assertGreater(scores.land_change, 0.0)

    def test_an_empty_before_has_no_change_to_report(self):
        scores = score_shapes(Polygon(), self.square(0.0, 0.0))
        self.assertEqual(scores.perimeter_change, 0.0)
        self.assertEqual(scores.land_change, 0.0)


class TestAgreement(ScoreTestCase):
    """The rasterised comparison, at a quarter of the nozzle."""

    def test_the_same_shape_agrees_completely(self):
        land = self.square(0.0, 0.0)
        scores = score_shapes(land, land)
        self.assertAlmostEqual(scores.agreement, 1.0, places=9)
        self.assertEqual(scores.added_mm2, 0.0)
        self.assertEqual(scores.lost_mm2, 0.0)
        self.assertTrue(scores.identical)

    def test_two_islands_nowhere_near_each_other_agree_not_at_all(self):
        """Scored over the whole grid this fixture would read 99.99%.

        Two 1 mm squares 100 mm apart: the grid is nearly all sea, and the two
        versions agree about every cell of it. Agreement has to be measured
        over the union of the *land* or it says nothing.
        """
        scores = score_shapes(self.square(0.0, 0.0, 1.0),
                              self.square(100.0, 0.0, 1.0))
        self.assertEqual(scores.agreement, 0.0)
        self.assertFalse(scores.identical)

    def test_a_half_overlap_agrees_by_a_third(self):
        # Intersection is half of each, so union is three halves: IoU = 1/3.
        before = self.square(0.0, 0.0)
        after = self.square(2.0, 0.0)
        scores = score_shapes(before, after)
        self.assertAlmostEqual(scores.agreement, 1.0 / 3.0, places=6)

    def test_added_and_lost_are_printed_areas(self):
        # Sliding a 4 mm square 2 mm along puts 8 mm² there and takes 8 away.
        scores = score_shapes(self.square(0.0, 0.0), self.square(2.0, 0.0))
        self.assertAlmostEqual(scores.added_mm2, 8.0, places=6)
        self.assertAlmostEqual(scores.lost_mm2, 8.0, places=6)

    def test_shrinking_only_loses(self):
        scores = score_shapes(self.square(0.0, 0.0, 4.0),
                              self.square(0.0, 0.0, 2.0))
        self.assertEqual(scores.added_mm2, 0.0)
        self.assertAlmostEqual(scores.lost_mm2, 12.0, places=6)
        self.assertAlmostEqual(scores.agreement, 4.0 / 16.0, places=6)

    def test_the_two_versions_share_one_grid(self):
        """Offset shapes must not each get a grid fitted to themselves.

        If they did, a shape and the same shape moved 2 mm would rasterise
        identically and agreement would come back 1.0 — the comparison would be
        blind to exactly the edits it is for.
        """
        land = self.square(0.0, 0.0)
        moved = self.square(2.0, 0.0)
        self.assertLess(score_shapes(land, moved).agreement, 0.5)

    def test_a_lake_is_not_land(self):
        lake = box(1 * self.MM, 1 * self.MM, 3 * self.MM, 3 * self.MM)
        holed = Polygon(self.square(0.0, 0.0).exterior, [lake.exterior])
        scores = score_shapes(self.square(0.0, 0.0), holed)
        # Filling 4 mm² of lake is 4 mm² of the 16 mm² square.
        self.assertAlmostEqual(scores.lost_mm2, 4.0, places=6)
        self.assertAlmostEqual(scores.agreement, 12.0 / 16.0, places=6)

    def test_nothing_at_all_on_either_side(self):
        scores = score_shapes(Polygon(), Polygon())
        self.assertEqual(scores.agreement, 1.0)
        self.assertEqual(scores.pixels, 0)


class TestThePixel(ScoreTestCase):
    """A quarter of the nozzle, in printed millimetres."""

    def test_it_is_a_quarter_of_the_nozzle(self):
        params = ScoreParams(nozzle_mm=0.4, pixels_per_nozzle=4)
        self.assertAlmostEqual(params.pixel_mm, 0.1, places=9)
        self.assertAlmostEqual(params.pixel_m, 0.1 * params.mpm, places=6)

    def test_a_finer_sampling_makes_a_smaller_pixel(self):
        self.assertAlmostEqual(
            ScoreParams(nozzle_mm=0.4, pixels_per_nozzle=8).pixel_mm, 0.05,
            places=9)

    def test_zero_sampling_falls_back_to_the_nozzle(self):
        # Rather than dividing by zero and taking the window with it.
        self.assertAlmostEqual(
            ScoreParams(nozzle_mm=0.4, pixels_per_nozzle=0).pixel_mm, 0.4,
            places=9)

    def test_the_pixel_used_is_reported(self):
        scores = score_shapes(self.square(0.0, 0.0), self.square(2.0, 0.0))
        self.assertAlmostEqual(scores.pixel_mm, 0.1, places=6)
        self.assertGreater(scores.pixels, 0)
        self.assertFalse(scores.coarsened)

    def test_a_finer_print_scale_would_ask_for_more_cells(self):
        coarse = score_shapes(self.square(0.0, 0.0), self.square(2.0, 0.0),
                              ScoreParams(scale=8_000_000))
        fine = score_shapes(self.square(0.0, 0.0), self.square(2.0, 0.0),
                            ScoreParams(scale=800_000))
        self.assertGreater(fine.pixels, coarse.pixels)


class TestTheCap(ScoreTestCase):
    """The pixel is a printed length, so the grid is not bounded by itself."""

    def test_an_impossible_grid_is_coarsened_not_attempted(self):
        # At 1:1,000 a 0.1 print-mm pixel is 100 mm of ground, and a province
        # is thousands of kilometres across.
        scores = score_shapes(self.square(0.0, 0.0, 400.0),
                              self.square(0.0, 0.0, 380.0),
                              ScoreParams(scale=1_000))
        self.assertTrue(scores.coarsened)
        self.assertLessEqual(scores.pixels, MAX_PIXELS * 1.01)
        self.assertGreater(scores.pixel_mm, 0.1)

    def test_a_coarsened_score_is_still_a_score(self):
        scores = score_shapes(self.square(0.0, 0.0, 400.0),
                              self.square(0.0, 0.0, 380.0),
                              ScoreParams(scale=1_000))
        self.assertTrue(scores.measured)
        self.assertAlmostEqual(scores.agreement, (380 / 400) ** 2, places=2)

    def test_an_ordinary_province_is_never_coarsened(self):
        scores = score_shapes(self.square(0.0, 0.0, 200.0),
                              self.square(0.0, 0.0, 190.0))
        self.assertFalse(scores.coarsened)


class TestSayingSoWhenItCannotTell(ScoreTestCase):
    """Unmeasured is not unchanged."""

    def test_an_unmeasured_score_is_not_identical(self):
        self.assertFalse(ShapeScores(measured=False).identical)

    def test_a_default_score_is_identical(self):
        self.assertTrue(ShapeScores().identical)

    def test_a_rasterisation_failure_is_reported(self):
        class Hostile:
            """Geometry enough to be measured, not enough to be burned."""
            is_empty = False
            bounds = (0.0, 0.0, 8000.0, 8000.0)
            length = 32000.0
            area = 64_000_000.0

            @property
            def __geo_interface__(self):
                raise ValueError("no")

        scores = score_shapes(Hostile(), Hostile())
        self.assertFalse(scores.measured)
        self.assertFalse(scores.identical)
        # The cheap measures still came through, because they never needed it.
        self.assertAlmostEqual(scores.perimeter_before_mm, 4.0, places=6)


if __name__ == "__main__":
    unittest.main()
