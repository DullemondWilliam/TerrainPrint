"""
test_island_bridging.py

Unit tests for island_bridging.py.
Run with:  python -m pytest tests/test_island_bridging.py -v
       or: python -m unittest tests.test_island_bridging -v

Coordinates are metres throughout (the project works in EPSG:3347), so a
10 000-unit island is 10 km across and a brush width of 2.0 is 2 km wide.
"""

import math
import unittest

from shapely.geometry import box, Polygon

from lib.LIB_island_bridging import brush_stroke, draw_bridge

#: Two 10 km islands with a 10 km strait between them.
WEST = box(0, 0, 10_000, 10_000)
EAST = box(20_000, 0, 30_000, 10_000)


def _part_count(geom) -> int:
    """Number of polygon parts in a geometry."""
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return 1
    return len(list(geom.geoms))


def _rings(geom) -> list:
    """Every interior ring across every part, in document order."""
    if geom is None or geom.is_empty:
        return []
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return [ring for p in parts for ring in p.interiors]


def _interiors(geom) -> int:
    """Total interior rings across every part."""
    if geom is None or geom.is_empty:
        return 0
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return sum(len(p.interiors) for p in parts)


class TestBrushStroke(unittest.TestCase):
    """The swept land of one stroke, before it meets the document."""

    def test_width_is_a_diameter_not_a_radius(self):
        """A straight stroke is as wide as asked: length x width, round caps."""
        stroke = brush_stroke([(0, 0), (10_000, 0)], 2.0)   # 10 km long, 2 km wide
        expected = 10_000 * 2_000 + math.pi * 1_000 ** 2
        self.assertAlmostEqual(stroke.area / expected, 1.0, places=2)

    def test_stroke_covers_the_path_it_was_drawn_along(self):
        """Every point of the path is inside the land it lays down."""
        stroke = brush_stroke([(0, 0), (5_000, 5_000), (10_000, 0)], 1.0)
        for point in [(0, 0), (5_000, 5_000), (10_000, 0)]:
            self.assertTrue(stroke.covers(Polygon([point, point, point]).centroid))

    def test_a_single_point_is_a_dab(self):
        """One distinct point paints a disc of the brush size."""
        stroke = brush_stroke([(0, 0)], 2.0)
        self.assertAlmostEqual(stroke.area / (math.pi * 1_000 ** 2), 1.0, places=2)

    def test_repeated_points_are_still_one_dab(self):
        """A held pointer resamples the same spot; that is not a line."""
        held = brush_stroke([(0, 0)] * 50, 2.0)
        single = brush_stroke([(0, 0)], 2.0)
        self.assertAlmostEqual(held.area, single.area, places=6)

    def test_dense_collinear_samples_match_the_bare_segment(self):
        """Simplification drops mouse-rate noise without moving the outline."""
        dense = brush_stroke([(x, 0) for x in range(0, 10_001, 10)], 2.0)
        sparse = brush_stroke([(0, 0), (10_000, 0)], 2.0)
        self.assertAlmostEqual(dense.area / sparse.area, 1.0, places=3)

    def test_nothing_to_draw(self):
        """Empty, absent, or non-positive input draws nothing rather than raising."""
        self.assertIsNone(brush_stroke([], 2.0))
        self.assertIsNone(brush_stroke(None, 2.0))
        self.assertIsNone(brush_stroke([(0, 0), (1_000, 0)], 0))
        self.assertIsNone(brush_stroke([(0, 0), (1_000, 0)], -1.0))
        self.assertIsNone(brush_stroke([(0, 0), (1_000, 0)], None))


class TestDrawBridge(unittest.TestCase):
    """Committing a stroke onto the document."""

    def setUp(self):
        self.doc = WEST.union(EAST)

    def test_a_stroke_across_the_strait_joins_the_islands(self):
        """The point of the tool: two landmasses become one."""
        self.assertEqual(_part_count(self.doc), 2)
        result = draw_bridge(self.doc, [(5_000, 5_000), (25_000, 5_000)], 2.0)
        self.assertEqual(_part_count(result), 1)

    def test_both_coastlines_survive_the_bridge(self):
        """Drawing adds land; it never trims what was already there."""
        result = draw_bridge(self.doc, [(5_000, 5_000), (25_000, 5_000)], 2.0)
        self.assertTrue(result.covers(WEST))
        self.assertTrue(result.covers(EAST))

    def test_a_stroke_reaching_only_one_island_leaves_them_apart(self):
        """A bridge that stops short of the far shore is not a bridge."""
        result = draw_bridge(self.doc, [(5_000, 5_000), (14_000, 5_000)], 2.0)
        self.assertEqual(_part_count(result), 2)

    def test_a_narrow_dab_in_the_strait_makes_a_new_island(self):
        """Land drawn touching nothing is new land, and says so by the count."""
        result = draw_bridge(self.doc, [(15_000, 5_000)], 2.0)
        self.assertEqual(_part_count(result), 3)

    def test_a_wide_enough_dab_spans_both_shores(self):
        """Brush width alone can close the strait — the disc reaches both."""
        result = draw_bridge(self.doc, [(15_000, 5_000)], 22.0)
        self.assertEqual(_part_count(result), 1)

    def test_a_stroke_inside_existing_land_changes_nothing(self):
        """
        Redrawing over land the document already has is a no-op, reported by
        identity — the union would rebuild the rings and so compare unequal by
        value, which would put an edit that changes nothing in the history.
        """
        result = draw_bridge(self.doc, [(2_000, 5_000), (8_000, 5_000)], 1.0)
        self.assertIs(result, self.doc)

    def test_a_stroke_touching_the_coast_from_inside_still_adds_land(self):
        """The no-op guard must not swallow a stroke that spills into the sea."""
        result = draw_bridge(self.doc, [(9_500, 5_000), (12_000, 5_000)], 1.0)
        self.assertIsNot(result, self.doc)
        self.assertGreater(result.area, self.doc.area)

    def test_a_stroke_that_encircles_the_strait_keeps_the_water(self):
        """
        Crossing at two places seals sea between the crossings.  The brush adds
        only the land it was dragged over, so that water stays water -- a lake
        in the finished landmass, exactly as the eraser keeps a hole it opens.
        """
        path = [(5_000, 1_000), (25_000, 1_000), (25_000, 9_000), (5_000, 9_000)]
        result = draw_bridge(self.doc, path, 1.0)
        self.assertEqual(_part_count(result), 1)
        self.assertEqual(_interiors(result), 1)
        # The lagoon is the strait between the two crossings, less their width:
        # 10 km of water across, 8 km between the stroke's inner edges.
        lagoon = Polygon(_rings(result)[0])
        self.assertAlmostEqual(lagoon.area / (10_000 * 7_000), 1.0, places=2)

    def test_a_stroke_ringed_around_open_sea_makes_a_lake(self):
        """The same rule with no coastline involved: a drawn ring holds water."""
        path = [(40_000, 0), (60_000, 0), (60_000, 20_000), (40_000, 20_000),
                (40_000, 0)]
        result = draw_bridge(self.doc, path, 2.0)
        self.assertEqual(_interiors(result), 1)
        lagoon = Polygon(_rings(result)[0])
        self.assertAlmostEqual(lagoon.area / (18_000 * 18_000), 1.0, places=2)

    def test_a_hairline_ring_the_union_leaves_is_dropped(self):
        """
        Sealing a hairline slit leaves a ring too thin to hold water -- the same
        crumb the union leaves where a stroke grazes a real coastline (30 m2
        against an 18 km brush).  It would draw as a scratch inside solid land,
        so it goes, where a slit wide enough to be water is kept.
        """
        # A 5 cm slit 5 km into the coast; a stroke along the shore seals the
        # top 2 km of it, which rings the 150 m2 that is left.
        slit = box(9_999.975, 5_000, 10_000.025, 10_000)
        shore = [(2_000, 10_000), (18_000, 10_000)]
        hairline = draw_bridge(box(0, 0, 20_000, 10_000).difference(slit),
                               shore, 4.0)
        self.assertEqual(_interiors(hairline), 0)

        # The same inlet 100 m wide is water, and a 4 km brush keeps it.
        inlet = box(9_950, 5_000, 10_050, 10_000)
        lagoon = draw_bridge(box(0, 0, 20_000, 10_000).difference(inlet),
                             shore, 4.0)
        self.assertEqual(_interiors(lagoon), 1)

    def test_a_real_lake_survives_a_bridge(self):
        """Filling merge-made pockets must not swallow the document's own lakes."""
        lagoon = Polygon(WEST.exterior, [box(4_000, 4_000, 6_000, 6_000).exterior])
        doc = lagoon.union(EAST)
        self.assertEqual(_interiors(doc), 1)
        result = draw_bridge(doc, [(2_000, 1_000), (25_000, 1_000)], 2.0)
        self.assertEqual(_part_count(result), 1)
        self.assertEqual(_interiors(result), 1)

    def test_drawing_on_a_blank_document(self):
        """With nothing loaded the stroke is the whole document."""
        result = draw_bridge(None, [(0, 0), (10_000, 0)], 2.0)
        self.assertEqual(_part_count(result), 1)

    def test_an_empty_stroke_returns_the_document_untouched(self):
        """Never destroy the document on a degenerate stroke."""
        self.assertIs(draw_bridge(self.doc, [], 2.0), self.doc)
        self.assertIs(draw_bridge(self.doc, [(5_000, 5_000)], 0), self.doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
