"""
test_thin_features.py

Unit tests for LIB_thin_features.py — the land too narrow to print.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_thin_features.py -v

Three things here are worth more than the rest.

**The square must come back clean.** A morphological opening with the default
round joins rounds off every convex corner, so a plain 4 mm square — which
prints perfectly — reports four thin patches and 0.216 mm² of them. That is not
a width problem, and a detector that cried wolf at every corner would be turned
off within a minute. ``test_a_plain_square_has_nothing_thin`` is what holds the
mitre joins in place.

**The threshold has to be the width it says.** A 0.95 mm neck is flagged at a
1 mm setting and a 1.05 mm neck is not, so the number in the box means what it
claims rather than being a knob someone turns until the picture looks right.

**"I could not tell" is not "nothing is wrong."** An invalid ring defeats the
difference outright, and a report that came back clean there would be worse than
no report at all, because the whole point of this module is to be believed.

Geometry is in metres (EPSG:3347) and the width is print-mm, so the fixtures are
written in millimetres and multiplied by ``MM``.
"""

import unittest

import shapely
from shapely.geometry import Polygon, box

from lib.LIB_thin_features import (WORST_KEPT, ThinParams, ThinReport,
                                   printable, thin_features)


class ThinTestCase(unittest.TestCase):
    """One print-mm is 8 km at the project's 1:8,000,000."""

    MM = 8000.0

    def square(self, x_mm, y_mm, size_mm=4.0):
        return box(x_mm * self.MM, y_mm * self.MM,
                   (x_mm + size_mm) * self.MM, (y_mm + size_mm) * self.MM)

    def necked(self, neck_mm, neck_length_mm=2.0):
        """Two 4 mm squares joined by a neck of the given printed width."""
        half = neck_mm / 2.0
        left, right = 4.0, 4.0 + neck_length_mm
        mm = self.MM
        return Polygon([
            (0, 0), (left * mm, 0), (left * mm, (2 - half) * mm),
            (right * mm, (2 - half) * mm), (right * mm, 0),
            ((right + 4) * mm, 0), ((right + 4) * mm, 4 * mm),
            (right * mm, 4 * mm), (right * mm, (2 + half) * mm),
            (left * mm, (2 + half) * mm), (left * mm, 4 * mm), (0, 4 * mm)])


class TestWhatCountsAsThin(ThinTestCase):

    def test_a_plain_square_has_nothing_thin(self):
        """The corner-noise regression: round joins report four patches here.

        A 4 mm square is 4 mm wide everywhere. If this fails, the buffers have
        lost their mitre joins and every convex corner on every coastline is
        about to be reported as a printing problem.
        """
        report = thin_features(self.square(0.0, 0.0))
        self.assertTrue(report.clean)
        self.assertEqual(report.patches, 0)
        self.assertEqual(report.thin_area_mm2, 0.0)

    def test_a_neck_below_the_width_is_flagged(self):
        report = thin_features(self.necked(0.4))
        self.assertFalse(report.clean)
        self.assertEqual(report.patches, 1)

    def test_a_neck_above_the_width_is_not(self):
        self.assertTrue(thin_features(self.necked(1.5)).clean)

    def test_the_threshold_is_the_width_it_says(self):
        # At a 1 mm setting: just under is caught, just over is left alone.
        self.assertFalse(thin_features(self.necked(0.95)).clean)
        self.assertTrue(thin_features(self.necked(1.05)).clean)

    def test_the_measured_area_is_the_neck(self):
        # A 0.4 mm neck 2 mm long is 0.8 mm² of land, and that is what the
        # report should say -- the number has to mean something.
        report = thin_features(self.necked(0.4, neck_length_mm=2.0))
        self.assertAlmostEqual(report.thin_area_mm2, 0.8, places=3)

    def test_a_needle_spit_is_flagged(self):
        mm = self.MM
        spit = Polygon([(0, 0), (4 * mm, 0), (4 * mm, 1.98 * mm),
                        (9 * mm, 1.99 * mm), (9 * mm, 2.01 * mm),
                        (4 * mm, 2.02 * mm), (4 * mm, 4 * mm), (0, 4 * mm)])
        self.assertTrue(spit.is_valid)
        self.assertFalse(thin_features(spit).clean)

    def test_a_wider_setting_finds_more(self):
        land = self.necked(1.2)
        self.assertTrue(thin_features(land, ThinParams(width_mm=1.0)).clean)
        self.assertFalse(thin_features(land, ThinParams(width_mm=2.0)).clean)

    def test_asking_for_no_width_flags_nothing(self):
        report = thin_features(self.necked(0.4), ThinParams(width_mm=0.0))
        self.assertTrue(report.clean)
        self.assertGreater(report.land_area_mm2, 0.0)


class TestNecksVersusWholeIslands(ThinTestCase):
    """The two kinds want opposite fixes, so they are counted apart."""

    def test_an_islet_below_the_width_is_a_whole_part(self):
        report = thin_features(self.square(0.0, 0.0, size_mm=0.6))
        self.assertEqual(report.parts, 1)
        self.assertEqual(report.whole_parts, 1)

    def test_a_neck_is_not_a_whole_part(self):
        report = thin_features(self.necked(0.4))
        self.assertEqual(report.whole_parts, 0)
        self.assertEqual(report.patches, 1)

    def test_they_are_counted_separately_in_one_province(self):
        # A necked landmass plus two islets: one part with a neck, two parts
        # that are nothing but thin.
        land = shapely.union_all([self.necked(0.4),
                                  self.square(20.0, 0.0, size_mm=0.5),
                                  self.square(24.0, 0.0, size_mm=0.5)])
        report = thin_features(land)
        self.assertEqual(report.parts, 3)
        self.assertEqual(report.whole_parts, 2)
        self.assertGreaterEqual(report.patches, 3)

    def test_a_big_island_is_neither(self):
        report = thin_features(self.square(0.0, 0.0, size_mm=10.0))
        self.assertEqual(report.whole_parts, 0)
        self.assertTrue(report.clean)


class TestSayingSoWhenItCannotTell(ThinTestCase):
    """A detector nobody believes is worse than no detector."""

    def broken(self):
        """A self-intersecting ring, as a real GeoJSON sometimes carries."""
        mm = self.MM
        bad = Polygon([(0, 0), (4 * mm, 0), (4 * mm, 4 * mm), (0, 4 * mm),
                       (0, 2.05 * mm), (9 * mm, 2.0 * mm), (0, 1.95 * mm)])
        assert not bad.is_valid
        return bad

    def test_invalid_geometry_is_repaired_and_measured(self):
        report = thin_features(self.broken())
        self.assertTrue(report.measured)
        self.assertTrue(report.repaired)
        self.assertFalse(report.clean)      # it is a needle, after all

    def test_valid_geometry_is_not_reported_as_repaired(self):
        self.assertFalse(thin_features(self.square(0.0, 0.0)).repaired)

    def test_an_unmeasurable_province_is_not_reported_clean(self):
        """``clean`` must mean "nothing thin", never "nothing known"."""
        report = ThinReport(measured=False, patches=0)
        self.assertFalse(report.clean)

    def test_nothing_at_all_is_clean_and_empty(self):
        for geom in (None, Polygon()):
            report = thin_features(geom)
            self.assertTrue(report.clean)
            self.assertEqual(report.parts, 0)
            self.assertEqual(report.land_area_mm2, 0.0)


class TestTheReport(ThinTestCase):

    def test_the_thin_geometry_lies_inside_the_land(self):
        land = self.necked(0.4)
        report = thin_features(land)
        self.assertTrue(land.buffer(1e-6).contains(report.thin))

    def test_the_fraction_is_thin_over_land(self):
        report = thin_features(self.necked(0.4))
        self.assertAlmostEqual(
            report.fraction, report.thin_area_mm2 / report.land_area_mm2,
            places=9)

    def test_an_empty_report_has_no_fraction(self):
        self.assertEqual(ThinReport().fraction, 0.0)

    def test_the_width_measured_against_is_recorded(self):
        report = thin_features(self.necked(0.4), ThinParams(width_mm=1.75))
        self.assertAlmostEqual(report.width_mm, 1.75)

    def test_worst_is_geometry_worst_first(self):
        land = shapely.union_all([self.necked(0.4, neck_length_mm=4.0),
                                  self.square(20.0, 0.0, size_mm=0.5)])
        report = thin_features(land)
        self.assertTrue(report.worst)
        areas = [part.area for part in report.worst]
        self.assertEqual(areas, sorted(areas, reverse=True))
        for part in report.worst:
            self.assertEqual(part.geom_type, "Polygon")

    def test_worst_is_capped(self):
        # A comb of many thin teeth: more patches than anyone wants handed back.
        mm = self.MM
        teeth = [box(i * 2 * mm, 0, (i * 2 + 0.4) * mm, 3 * mm)
                 for i in range(WORST_KEPT + 15)]
        report = thin_features(shapely.union_all(teeth))
        self.assertGreater(report.patches, WORST_KEPT)
        self.assertEqual(len(report.worst), WORST_KEPT)


class TestPrintable(ThinTestCase):
    """The other half: removing the thin land rather than reporting it."""

    def test_it_is_the_complement_of_the_thin_land(self):
        """``thin_features(g).thin == g - printable(g)``, exactly.

        The two are one operation read two ways, and a caller that highlights
        with one and cuts with the other has to be able to rely on that: what
        the map marked in red is what the button takes.
        """
        land = self.necked(0.4)
        kept = printable(land)
        thin = thin_features(land).thin
        self.assertAlmostEqual(land.difference(kept).area, thin.area, places=3)

    def test_a_clean_province_survives_whole(self):
        square = self.square(0.0, 0.0, 10.0)
        self.assertAlmostEqual(printable(square).area, square.area, places=3)
        self.assertTrue(printable(square).equals(square))

    def test_a_neck_is_cut(self):
        from lib.LIB_island_merging import _polygons
        land = self.necked(0.4)
        self.assertEqual(len(_polygons(land)), 1)
        self.assertEqual(len(_polygons(printable(land))), 2)

    def test_an_islet_below_the_width_comes_back_empty(self):
        """It must be possible to tell, not merely to get nothing useful."""
        self.assertTrue(printable(self.square(0.0, 0.0, 0.5)).is_empty)

    def test_what_is_left_has_nothing_thin_in_it(self):
        report = thin_features(printable(self.necked(0.4)))
        self.assertTrue(report.clean)

    def test_it_is_settled_after_one_pass(self):
        once = printable(self.necked(0.4))
        twice = printable(once)
        self.assertAlmostEqual(twice.area, once.area, places=3)

    def test_no_width_is_a_no_op(self):
        land = self.necked(0.4)
        self.assertIs(printable(land, ThinParams(width_mm=0.0)), land)

    def test_nothing_in_nothing_out(self):
        self.assertIsNone(printable(None))
        self.assertTrue(printable(Polygon()).is_empty)

    def test_invalid_geometry_is_repaired_rather_than_refused(self):
        mm = self.MM
        bad = Polygon([(0, 0), (4 * mm, 0), (4 * mm, 4 * mm), (0, 4 * mm),
                       (0, 2.05 * mm), (9 * mm, 2.0 * mm), (0, 1.95 * mm)])
        self.assertFalse(bad.is_valid)
        kept = printable(bad)
        self.assertTrue(kept.is_valid)
        self.assertGreater(kept.area, 0.0)

    def test_it_follows_the_print_scale(self):
        # A 0.9 mm neck is thin at 1:8M and comfortable at 1:4M.
        from lib.LIB_island_merging import _polygons
        land = self.necked(0.9)
        self.assertEqual(
            len(_polygons(printable(land, ThinParams(scale=8_000_000)))), 2)
        self.assertEqual(
            len(_polygons(printable(land, ThinParams(scale=4_000_000)))), 1)


class TestPrintScale(ThinTestCase):
    """The width is printed millimetres, so the scale decides what it is worth."""

    def test_metres_per_print_mm(self):
        self.assertAlmostEqual(ThinParams(scale=8_000_000).mpm, 8000.0)
        self.assertAlmostEqual(ThinParams(scale=8_000_000).width_m,
                               ThinParams(scale=8_000_000).width_mm * 8000.0)
        self.assertAlmostEqual(ThinParams(scale=8_000_000).radius_m,
                               ThinParams(scale=8_000_000).width_m / 2.0)

    def test_the_same_land_is_thinner_at_a_larger_reduction(self):
        # A neck 8 km across is 1 mm at 1:8M (right at the limit) and 2 mm at
        # 1:4M (comfortable). Same ground, different verdict.
        land = self.necked(0.9)         # 7.2 km, below 1 mm at 1:8M
        self.assertFalse(
            thin_features(land, ThinParams(scale=8_000_000)).clean)
        self.assertTrue(
            thin_features(land, ThinParams(scale=4_000_000)).clean)

    def test_areas_are_reported_in_printed_millimetres(self):
        land = self.square(0.0, 0.0, size_mm=4.0)
        report = thin_features(land, ThinParams(scale=8_000_000))
        self.assertAlmostEqual(report.land_area_mm2, 16.0, places=6)

    def test_the_default_comes_from_the_project_config(self):
        from lib.LIB_project_config import get
        self.assertAlmostEqual(ThinParams().width_mm, get("thin.width_mm"))
        self.assertAlmostEqual(ThinParams().scale, get("print.scale"))


if __name__ == "__main__":
    unittest.main()
