"""
test_island_metrics.py

Unit tests for LIB_island_metrics.py.
Run with:  python -m pytest test_island_metrics.py -v
       or: python -m unittest test_island_metrics -v
"""

import unittest

from shapely.geometry import box, MultiPolygon, Polygon

from lib.LIB_island_merging import _polygons, merge_islands
from lib.LIB_island_metrics import (bridging_buffer_km, closest_pair, gap_km,
                                    gap_range_km, landmass_count)

KM = 1000.0     # metres per km, to keep the fixtures readable


class TestLandmassCount(unittest.TestCase):
    """One count per body of land, however it is shaped."""

    def test_single_polygon_is_one_landmass(self):
        self.assertEqual(landmass_count(box(0, 0, KM, KM)), 1)

    def test_multipolygon_counts_its_parts(self):
        a = box(0, 0, KM, KM)
        b = box(10 * KM, 0, 11 * KM, KM)
        c = box(20 * KM, 0, 21 * KM, KM)
        self.assertEqual(landmass_count(MultiPolygon([a, b, c])), 3)

    def test_a_lake_does_not_split_a_landmass(self):
        # A 10x10 km island with a 4x4 km lake is still one landmass.
        lake = box(3 * KM, 3 * KM, 7 * KM, 7 * KM)
        land = Polygon(box(0, 0, 10 * KM, 10 * KM).exterior.coords,
                       [lake.exterior.coords])
        self.assertEqual(landmass_count(land), 1)

    def test_none_and_empty_count_zero(self):
        self.assertEqual(landmass_count(None), 0)
        self.assertEqual(landmass_count(Polygon()), 0)


class TestGapKm(unittest.TestCase):
    """Edge-to-edge distance in km, from a projected metre CRS."""

    def test_gap_is_measured_between_edges_not_centres(self):
        # Two 2 km boxes whose facing edges are 3 km apart; their centres are
        # 5 km apart, so a centre-to-centre reading would give the wrong answer.
        a = box(0, 0, 2 * KM, 2 * KM)
        b = box(5 * KM, 0, 7 * KM, 2 * KM)
        self.assertAlmostEqual(gap_km(a, b), 3.0)

    def test_diagonal_gap_is_the_corner_distance(self):
        # Nearest approach is corner to corner: 3-4-5, so 5 km.
        a = box(0, 0, KM, KM)
        b = box(4 * KM, 5 * KM, 5 * KM, 6 * KM)
        self.assertAlmostEqual(gap_km(a, b), 5.0)

    def test_touching_islands_measure_zero(self):
        a = box(0, 0, KM, KM)
        b = box(KM, 0, 2 * KM, KM)
        self.assertEqual(gap_km(a, b), 0.0)

    def test_overlapping_islands_measure_zero(self):
        a = box(0, 0, 2 * KM, 2 * KM)
        b = box(KM, KM, 3 * KM, 3 * KM)
        self.assertEqual(gap_km(a, b), 0.0)

    def test_order_does_not_matter(self):
        a = box(0, 0, KM, KM)
        b = box(9 * KM, 0, 10 * KM, KM)
        self.assertAlmostEqual(gap_km(a, b), gap_km(b, a))

    def test_none_and_empty_have_no_finite_gap(self):
        a = box(0, 0, KM, KM)
        self.assertEqual(gap_km(a, None), float("inf"))
        self.assertEqual(gap_km(a, Polygon()), float("inf"))


class TestGapRangeKm(unittest.TestCase):
    """Closest and widest gaps over every pair in a group."""

    def setUp(self):
        # Three 1 km boxes in a row, left edges at 0, 4 km and 10 km:
        # gaps are 3 km (a-b), 5 km (b-c) and 9 km (a-c).
        self.a = box(0, 0, KM, KM)
        self.b = box(4 * KM, 0, 5 * KM, KM)
        self.c = box(10 * KM, 0, 11 * KM, KM)

    def test_reports_the_closest_and_widest_pair(self):
        closest, widest = gap_range_km([self.a, self.b, self.c])
        self.assertAlmostEqual(closest, 3.0)
        self.assertAlmostEqual(widest, 9.0)

    def test_two_islands_give_the_same_figure_twice(self):
        closest, widest = gap_range_km([self.a, self.b])
        self.assertAlmostEqual(closest, 3.0)
        self.assertAlmostEqual(widest, 3.0)

    def test_input_order_does_not_matter(self):
        self.assertEqual(gap_range_km([self.c, self.a, self.b]),
                         gap_range_km([self.a, self.b, self.c]))

    def test_fewer_than_two_islands_measure_nothing(self):
        self.assertIsNone(gap_range_km([]))
        self.assertIsNone(gap_range_km([self.a]))

    def test_empty_entries_are_ignored(self):
        # The Nones and empties drop out, leaving the a-b pair to measure.
        span = gap_range_km([self.a, None, Polygon(), self.b])
        self.assertIsNotNone(span)
        self.assertAlmostEqual(span[0], 3.0)
        self.assertAlmostEqual(span[1], 3.0)

    def test_ignoring_empties_can_leave_too_few_to_measure(self):
        self.assertIsNone(gap_range_km([self.a, None, Polygon()]))

    def test_a_touching_pair_makes_the_closest_gap_zero(self):
        touching = box(KM, 0, 2 * KM, KM)       # shares an edge with a
        closest, widest = gap_range_km([self.a, touching, self.c])
        self.assertEqual(closest, 0.0)
        self.assertAlmostEqual(widest, 9.0)

    def test_multipolygon_islands_are_measured_as_wholes(self):
        # A two-part island: the gap is to its nearest part, not its first.
        split = MultiPolygon([box(20 * KM, 0, 21 * KM, KM), self.b])
        self.assertAlmostEqual(gap_range_km([self.a, split])[0], 3.0)


class TestClosestPair(unittest.TestCase):
    """Picking out the two islands lying nearest each other."""

    def setUp(self):
        # Left edges at 0, 4 km and 10 km: gaps 3 (a-b), 5 (b-c), 9 (a-c).
        self.a = box(0, 0, KM, KM)
        self.b = box(4 * KM, 0, 5 * KM, KM)
        self.c = box(10 * KM, 0, 11 * KM, KM)

    def test_finds_the_nearest_two_and_their_gap(self):
        first, second, gap = closest_pair([self.a, self.c, self.b])
        self.assertAlmostEqual(gap, 3.0)
        self.assertEqual({id(first), id(second)}, {id(self.a), id(self.b)})

    def test_agrees_with_the_closest_figure_from_gap_range(self):
        islands = [self.a, self.b, self.c]
        self.assertAlmostEqual(closest_pair(islands)[2],
                               gap_range_km(islands)[0])

    def test_fewer_than_two_islands_have_no_pair(self):
        self.assertIsNone(closest_pair([]))
        self.assertIsNone(closest_pair([self.a]))

    def test_empty_entries_are_ignored(self):
        self.assertAlmostEqual(
            closest_pair([self.a, None, Polygon(), self.b])[2], 3.0)


class TestBridgingBufferKm(unittest.TestCase):
    """
    The buffer that actually merges a pair.

    Every case asserts against ``merge_islands`` itself rather than a formula:
    no fixed multiple of the gap works, and the merge is not even monotonic in
    the buffer close to its threshold.
    """

    def merged(self, a, b, buffer_km) -> bool:
        return len(_polygons(merge_islands([a, b], buffer_km))) == 1

    def test_the_returned_buffer_really_merges(self):
        a = box(0, 0, 10 * KM, 10 * KM)
        b = box(13 * KM, 0, 23 * KM, 10 * KM)       # 3 km gap
        buffer_km = bridging_buffer_km(a, b)
        self.assertIsNotNone(buffer_km)
        self.assertTrue(self.merged(a, b, buffer_km))

    def test_it_is_at_least_half_the_gap(self):
        # Below half the gap the dilated coasts cannot even reach each other,
        # so no smaller buffer could ever work.
        a = box(0, 0, 10 * KM, 10 * KM)
        b = box(13 * KM, 0, 23 * KM, 10 * KM)
        self.assertGreaterEqual(bridging_buffer_km(a, b), 3.0 / 2)

    def test_the_step_is_honoured_so_the_value_needs_no_rounding(self):
        # The caller stores this at 2 decimals; rounding a verified figure
        # afterwards could land on an untested value, so it must come back
        # already quantised — and still merge.
        a = box(0, 0, 10 * KM, 10 * KM)
        b = box(13 * KM, 0, 23 * KM, 10 * KM)
        buffer_km = bridging_buffer_km(a, b, step=0.01)
        self.assertAlmostEqual(buffer_km, round(buffer_km, 2))
        self.assertTrue(self.merged(a, b, buffer_km))

    def test_touching_islands_need_no_buffer(self):
        a = box(0, 0, KM, KM)
        b = box(KM, 0, 2 * KM, KM)
        self.assertEqual(bridging_buffer_km(a, b), 0.0)

    def test_a_ceiling_can_rule_every_candidate_out(self):
        a = box(0, 0, 10 * KM, 10 * KM)
        b = box(13 * KM, 0, 23 * KM, 10 * KM)       # needs ~1.5 km
        self.assertIsNone(bridging_buffer_km(a, b, max_km=0.5))

    def test_specks_far_apart_cannot_be_bridged(self):
        # Small islands need a buffer many times their gap; past the ladder we
        # report failure rather than returning something that will not merge.
        a = box(0, 0, KM, KM)
        b = box(100 * KM, 0, 101 * KM, KM)
        self.assertIsNone(bridging_buffer_km(a, b))

    def test_none_and_empty_cannot_be_bridged(self):
        self.assertIsNone(bridging_buffer_km(box(0, 0, KM, KM), None))
        self.assertIsNone(bridging_buffer_km(box(0, 0, KM, KM), Polygon()))


if __name__ == "__main__":
    unittest.main()
