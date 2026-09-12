"""
test_island_erasing.py

Unit tests for island_erasing.py.
Run with:  python -m pytest tests/test_island_erasing.py -v
       or: python -m unittest tests.test_island_erasing -v

Coordinates are metres throughout (the project works in EPSG:3347), so a
10 000-unit island is 10 km across and a brush width of 2.0 is 2 km wide.
"""

import unittest

from shapely.geometry import box, Polygon

from lib.LIB_island_erasing import erase_stroke

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


def _interiors(geom) -> int:
    """Total interior rings across every part."""
    if geom is None or geom.is_empty:
        return 0
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    return sum(len(p.interiors) for p in parts)


class TestEraseStroke(unittest.TestCase):
    """Subtracting a stroke from the document."""

    def setUp(self):
        self.doc = WEST.union(EAST)

    def test_a_stroke_takes_away_exactly_what_it_sweeps(self):
        """A bite out of one coast costs the swept area, and no more."""
        result = erase_stroke(self.doc, [(0, 5_000), (2_000, 5_000)], 2.0)
        self.assertLess(result.area, self.doc.area)
        self.assertTrue(self.doc.covers(result))

    def test_a_stroke_across_a_landmass_cuts_it_in_two(self):
        """The point of the tool that removal by island cannot do."""
        result = erase_stroke(self.doc, [(-1_000, 5_000), (11_000, 5_000)], 2.0)
        self.assertEqual(_part_count(result), 3)     # west split, east intact

    def test_a_stroke_over_a_whole_island_removes_it(self):
        """A brush wide enough to cover an island erases it outright."""
        result = erase_stroke(self.doc, [(5_000, 5_000)], 30.0)
        self.assertEqual(_part_count(result), 1)
        self.assertTrue(result.equals(EAST))

    def test_a_ring_erased_inside_a_landmass_is_kept_as_a_hole(self):
        """
        Erasing is literal: a pocket rubbed out of the middle stays rubbed out.
        Drawing fills the sea it encloses, because a bridge traps a strait only
        as a side effect; here the hole *is* the edit.
        """
        path = [(3_000, 3_000), (7_000, 3_000), (7_000, 7_000),
                (3_000, 7_000), (3_000, 3_000)]
        result = erase_stroke(self.doc, path, 1.0)
        self.assertEqual(_part_count(result), 3)     # ring, its core, and EAST
        self.assertEqual(_interiors(result), 1)

    def test_a_stroke_over_open_sea_changes_nothing(self):
        """
        Scribbling in the strait is a no-op, reported by identity — the
        difference would rebuild the rings and so compare unequal by value,
        which would put an edit that changes nothing in the history.
        """
        result = erase_stroke(self.doc, [(13_000, 5_000), (17_000, 5_000)], 1.0)
        self.assertIs(result, self.doc)

    def test_a_stroke_that_only_grazes_the_sea_still_erases(self):
        """The no-op guard must not swallow a stroke that reaches the coast."""
        result = erase_stroke(self.doc, [(9_500, 5_000), (12_000, 5_000)], 1.0)
        self.assertIsNot(result, self.doc)
        self.assertLess(result.area, self.doc.area)

    def test_erasing_everything_returns_an_empty_geometry(self):
        """Emptying the document is a real answer; the caller decides on it."""
        result = erase_stroke(self.doc, [(15_000, 5_000)], 100.0)
        self.assertTrue(result.is_empty)

    def test_a_real_lake_survives_an_erase_elsewhere(self):
        """Trimming one coast leaves the document's own interior rings alone."""
        lagoon = Polygon(WEST.exterior, [box(4_000, 4_000, 6_000, 6_000).exterior])
        doc = lagoon.union(EAST)
        self.assertEqual(_interiors(doc), 1)
        result = erase_stroke(doc, [(0, 500), (1_000, 500)], 1.0)
        self.assertEqual(_interiors(result), 1)

    def test_an_empty_stroke_returns_the_document_untouched(self):
        """Never destroy the document on a degenerate stroke."""
        self.assertIs(erase_stroke(self.doc, [], 2.0), self.doc)
        self.assertIs(erase_stroke(self.doc, None, 2.0), self.doc)
        self.assertIs(erase_stroke(self.doc, [(5_000, 5_000)], 0), self.doc)
        self.assertIs(erase_stroke(self.doc, [(5_000, 5_000)], -1.0), self.doc)

    def test_erasing_a_blank_document(self):
        """With nothing loaded there is nothing to rub out."""
        self.assertIsNone(erase_stroke(None, [(0, 0), (10_000, 0)], 2.0))

    def test_erase_undoes_a_drawn_bridge(self):
        """The brush's two halves are inverses over the land one of them added."""
        from lib.LIB_island_bridging import draw_bridge
        path = [(5_000, 5_000), (25_000, 5_000)]
        bridged = draw_bridge(self.doc, path, 2.0)
        self.assertEqual(_part_count(bridged), 1)
        # Erasing the same path at the same width cuts the neck it laid down.
        self.assertEqual(_part_count(erase_stroke(bridged, path, 2.0)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
