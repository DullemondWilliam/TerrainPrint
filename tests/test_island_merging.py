"""
test_island_merging.py

Unit tests for island_merging.py.
Run with:  python -m pytest test_island_merging.py -v
       or: python -m unittest test_island_merging -v
"""

import unittest
from shapely.geometry import box, MultiPolygon, Polygon

from lib.LIB_island_merging import merge_islands


def _part_count(geom) -> int:
    """Number of polygon parts in a geometry."""
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return 1
    return len(list(geom.geoms))


class TestMergeIslands(unittest.TestCase):
    """Bridge a list of islands with connecting necks of land.

    ``buffer_km`` is kilometres; geometry is in metres (EPSG:3347).  A neck
    forms between islands lying closer than ``2 * buffer_km``, with no
    intermediate floating-sliver regime.
    """

    KM = 1000.0     # metres per km, to keep the fixtures readable

    # -- bridging behaviour -------------------------------------------------

    def test_close_islands_fuse_into_one(self):
        # Gap 400 m; buffer 0.3 km → 2*300 > 400 → neck forms → one part.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)
        result = merge_islands([a, b], buffer_km=0.3)
        self.assertEqual(_part_count(result), 1)

    def test_distant_islands_stay_separate(self):
        # Gap 4 km; buffer 0.5 km → 2*500 < 4000 → no neck → two parts.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(5 * self.KM, 0, 6 * self.KM, 1 * self.KM)
        result = merge_islands([a, b], buffer_km=0.5)
        self.assertEqual(_part_count(result), 2)

    def test_threshold_is_twice_buffer_km(self):
        # Gap exactly 1 km: 2*buffer above it fuses, below it stays separate.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(2.0 * self.KM, 0, 3.0 * self.KM, 1 * self.KM)
        self.assertEqual(_part_count(merge_islands([a, b], 0.6)), 1)  # 1.2>1
        self.assertEqual(_part_count(merge_islands([a, b], 0.4)), 2)  # 0.8<1

    def test_no_floating_sliver_in_the_middle_band(self):
        # Between gap/2 and gap the old overlap method left a 3rd floating piece;
        # the neck method keeps only bridges touching both shores, so it fuses.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)   # gap 0.4 km
        self.assertEqual(_part_count(merge_islands([a, b], 0.3)), 1)

    # -- many islands -------------------------------------------------------

    def test_chain_of_three_all_fuse(self):
        # Three squares in a row, each 0.4 km from the next; buffer bridges both.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)
        c = box(2.8 * self.KM, 0, 3.8 * self.KM, 1 * self.KM)
        result = merge_islands([a, b, c], buffer_km=0.3)
        self.assertEqual(_part_count(result), 1)

    def test_far_member_forms_its_own_group(self):
        # Two close islands fuse; a third far away stays separate → two groups.
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)   # near a
        c = box(50 * self.KM, 0, 51 * self.KM, 1 * self.KM)     # far
        result = merge_islands([a, b, c], buffer_km=0.3)
        self.assertEqual(_part_count(result), 2)

    def test_every_original_survives_in_the_merge(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)
        c = box(2.8 * self.KM, 0, 3.8 * self.KM, 1 * self.KM)
        result = merge_islands([a, b, c], buffer_km=0.3)
        for island in (a, b, c):
            self.assertTrue(result.contains(island.buffer(-1)))

    # -- shape preservation (the point of this method) ----------------------

    def test_original_coastlines_are_preserved(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)
        result = merge_islands([a, b], buffer_km=0.3)
        # Both originals sit inside the result untouched; the merge only adds a
        # neck, so the area grows rather than shrinking as closing would.
        self.assertTrue(result.contains(a.buffer(-1)))
        self.assertTrue(result.contains(b.buffer(-1)))
        self.assertGreaterEqual(result.area, a.area + b.area)

    def test_island_bays_are_left_open(self):
        # A C-shaped island's own concavity must NOT be filled: only necks
        # connecting islands are added, not every bay a closing would fill.
        c = Polygon([(0, 0), (3 * self.KM, 0), (3 * self.KM, 3 * self.KM),
                     (0, 3 * self.KM), (0, 2 * self.KM), (2 * self.KM, 2 * self.KM),
                     (2 * self.KM, 1 * self.KM), (0, 1 * self.KM)])   # notch on the left
        far = box(6 * self.KM, 0, 7 * self.KM, 1 * self.KM)           # unreachable
        result = merge_islands([c, far], buffer_km=1.0)
        # The bay is >1 unit deep, so if it were filled the area would jump;
        # instead the C-shape's area is preserved (islands stay separate here).
        self.assertAlmostEqual(result.area, c.area + far.area, delta=1.0)

    def test_result_is_valid(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(1.4 * self.KM, 0, 2.4 * self.KM, 1 * self.KM)
        self.assertTrue(merge_islands([a, b], 0.3).is_valid)

    # -- interior rings -----------------------------------------------------

    def test_enclosed_strait_pocket_is_filled(self):
        # Two facing brackets whose tips nearly meet top and bottom but bow open
        # in the middle: bridging the tips traps open sea. That merge-created
        # pocket must be filled, not left as an interior border.
        km = self.KM
        a = Polygon([(x * km, y * km) for x, y in
                     [(0, 0), (60, 0), (60, 10), (10, 10),
                      (10, 90), (60, 90), (60, 100), (0, 100)]])
        b = Polygon([(x * km, y * km) for x, y in
                     [(130, 0), (70, 0), (70, 10), (120, 10),
                      (120, 90), (70, 90), (70, 100), (130, 100)]])
        result = merge_islands([a, b], buffer_km=8)
        self.assertEqual(_part_count(result), 1)
        holes = (len(result.interiors) if result.geom_type == "Polygon"
                 else sum(len(p.interiors) for p in result.geoms))
        self.assertEqual(holes, 0)

    def test_existing_lake_is_preserved(self):
        km = self.KM
        lake = box(20 * km, 20 * km, 40 * km, 40 * km)
        land = Polygon(box(0, 0, 60 * km, 60 * km).exterior.coords,
                       [lake.exterior.coords])
        other = box(62 * km, 0, 72 * km, 10 * km)
        result = merge_islands([land, other], buffer_km=3)
        self.assertEqual(len(result.interiors), 1)   # the lake survives

    # -- degenerate inputs --------------------------------------------------

    def test_zero_buffer_is_plain_union(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        b = box(5 * self.KM, 0, 6 * self.KM, 1 * self.KM)
        result = merge_islands([a, b], buffer_km=0)
        self.assertEqual(_part_count(result), 2)   # union, no bridging

    def test_none_and_empty_entries_are_ignored(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        self.assertEqual(merge_islands([a, None, Polygon()], 0.5), a)

    def test_single_island_returned_unchanged(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        self.assertEqual(merge_islands([a], 0.5), a)

    def test_empty_list_returns_none(self):
        self.assertIsNone(merge_islands([], 0.5))
        self.assertIsNone(merge_islands([None, Polygon()], 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
