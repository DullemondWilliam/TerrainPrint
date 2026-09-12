"""
test_island_removal.py

Unit tests for LIB_island_removal.py.
Run with:  python -m pytest test_island_removal.py -v
       or: python -m unittest test_island_removal -v
"""

import unittest

from shapely.geometry import box, MultiPolygon, Polygon

from lib.LIB_island_removal import area_km2, is_below_area, remove_small_islands


class TestAreaKm2(unittest.TestCase):
    """Area in square kilometres, assuming a projected metre CRS."""

    KM = 1000.0     # metres per km, to keep the fixtures readable

    def test_square_area_in_km2(self):
        # A 2 km x 3 km box = 6 km², measured from metre coordinates.
        geom = box(0, 0, 2 * self.KM, 3 * self.KM)
        self.assertAlmostEqual(area_km2(geom), 6.0)

    def test_lakes_are_excluded_from_area(self):
        # A 10x10 km box (100 km²) with a 4x4 km lake (16 km²) → 84 km².
        km = self.KM
        lake = box(3 * km, 3 * km, 7 * km, 7 * km)
        land = Polygon(box(0, 0, 10 * km, 10 * km).exterior.coords,
                       [lake.exterior.coords])
        self.assertAlmostEqual(area_km2(land), 84.0)

    def test_multipolygon_area_is_the_sum_of_parts(self):
        km = self.KM
        a = box(0, 0, 2 * km, 2 * km)          # 4 km²
        b = box(10 * km, 10 * km, 11 * km, 11 * km)   # 1 km²
        self.assertAlmostEqual(area_km2(MultiPolygon([a, b])), 5.0)

    def test_none_and_empty_measure_zero(self):
        self.assertEqual(area_km2(None), 0.0)
        self.assertEqual(area_km2(Polygon()), 0.0)


class TestIsBelowArea(unittest.TestCase):
    """The threshold predicate: strictly-below the minimum area."""

    KM = 1000.0

    def test_smaller_island_is_below(self):
        geom = box(0, 0, 1 * self.KM, 1 * self.KM)   # 1 km²
        self.assertTrue(is_below_area(geom, 2.0))

    def test_larger_island_is_not_below(self):
        geom = box(0, 0, 2 * self.KM, 2 * self.KM)   # 4 km²
        self.assertFalse(is_below_area(geom, 2.0))

    def test_exact_threshold_is_kept(self):
        # Boundary: an island exactly at the threshold is NOT below it, so it
        # survives — the filter drops only strictly-smaller islands.
        geom = box(0, 0, 2 * self.KM, 1 * self.KM)   # 2 km²
        self.assertFalse(is_below_area(geom, 2.0))

    def test_none_is_below_any_positive_threshold(self):
        self.assertTrue(is_below_area(None, 0.001))


class TestRemoveSmallIslands(unittest.TestCase):
    """Keep only islands at least ``min_area_km2`` square kilometres."""

    KM = 1000.0

    def _areas(self, geoms):
        return sorted(round(area_km2(g), 6) for g in geoms)

    def test_drops_islands_below_threshold(self):
        km = self.KM
        big = box(0, 0, 3 * km, 3 * km)              # 9 km²
        small = box(10 * km, 0, 10.5 * km, 0.5 * km)  # 0.25 km²
        kept = remove_small_islands([big, small], 1.0)
        self.assertEqual(kept, [big])

    def test_keeps_island_exactly_at_threshold(self):
        km = self.KM
        exact = box(0, 0, 2 * km, 1 * km)   # 2 km²
        kept = remove_small_islands([exact], 2.0)
        self.assertEqual(kept, [exact])

    def test_order_is_preserved(self):
        km = self.KM
        a = box(0, 0, 3 * km, 3 * km)                 # 9 km²  keep
        b = box(10 * km, 0, 10.4 * km, 0.4 * km)      # 0.16   drop
        c = box(20 * km, 0, 22 * km, 2 * km)          # 4 km²  keep
        kept = remove_small_islands([a, b, c], 1.0)
        self.assertEqual(kept, [a, c])

    def test_zero_threshold_keeps_everything(self):
        km = self.KM
        a = box(0, 0, 1 * km, 1 * km)
        b = box(5 * km, 0, 5.1 * km, 0.1 * km)   # a tiny speck
        self.assertEqual(remove_small_islands([a, b], 0.0), [a, b])

    def test_negative_threshold_keeps_everything(self):
        a = box(0, 0, 1 * self.KM, 1 * self.KM)
        self.assertEqual(remove_small_islands([a], -5.0), [a])

    def test_none_and_empty_entries_are_ignored(self):
        a = box(0, 0, 3 * self.KM, 3 * self.KM)
        kept = remove_small_islands([a, None, Polygon()], 1.0)
        self.assertEqual(kept, [a])

    def test_all_below_threshold_returns_empty(self):
        km = self.KM
        a = box(0, 0, 0.5 * km, 0.5 * km)   # 0.25 km²
        b = box(5 * km, 0, 5.2 * km, 0.2 * km)
        self.assertEqual(remove_small_islands([a, b], 10.0), [])

    def test_empty_input_returns_empty(self):
        self.assertEqual(remove_small_islands([], 1.0), [])

    def test_returns_a_new_list(self):
        # The result must not be the caller's list, so mutating it is safe.
        islands = [box(0, 0, 3 * self.KM, 3 * self.KM)]
        kept = remove_small_islands(islands, 1.0)
        self.assertIsNot(kept, islands)

    def test_multipolygon_judged_by_combined_area(self):
        # A MultiPolygon element is weighed as a whole: two 0.25 km² specks sum
        # to 0.5 km², which survives a 0.4 km² threshold even though neither
        # part would on its own.
        km = self.KM
        speck_a = box(0, 0, 0.5 * km, 0.5 * km)
        speck_b = box(10 * km, 0, 10.5 * km, 0.5 * km)
        multi = MultiPolygon([speck_a, speck_b])
        self.assertEqual(remove_small_islands([multi], 0.4), [multi])
        self.assertEqual(remove_small_islands([multi], 0.6), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
