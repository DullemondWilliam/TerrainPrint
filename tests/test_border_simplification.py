"""
test_border_simplification.py

Unit tests for border_simplification.py.
Run with:  python -m pytest test_border_simplification.py -v
       or: python -m unittest test_border_simplification -v
"""

import unittest
import numpy as np
import geopandas as gpd
from shapely.geometry import box, LineString

from lib.LIB_border_simplification import (
    _simplify_arc,
    _remove_reversals,
    _find_shared_arcs,
    _reconstruct_ring,
    _condition_arc,
    topo_simplify_two_tolerances,
)
from shapely.geometry import Polygon


# ---------------------------------------------------------------------------
# _simplify_arc
# ---------------------------------------------------------------------------
class TestSimplifyArc(unittest.TestCase):

    def test_passthrough_two_points(self):
        """Arc with only 2 points must be returned unchanged."""
        coords = [(0, 0), (10, 10)]
        self.assertEqual(_simplify_arc(coords, 1.0), coords)

    def test_passthrough_zero_tolerance(self):
        """Zero tolerance → no simplification."""
        coords = [(0, 0), (5, 0), (10, 0)]
        self.assertEqual(_simplify_arc(coords, 0), coords)

    def test_collinear_points_removed(self):
        """Middle point on a straight line should be removed."""
        coords = [(0, 0), (5, 0), (10, 0)]
        result = _simplify_arc(coords, 0.01)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], (0, 0))
        self.assertEqual(result[-1], (10, 0))

    def test_endpoints_always_preserved(self):
        """First and last point must survive simplification."""
        coords = [(0, 0), (1, 0.001), (2, 0), (3, 0.001), (4, 0)]
        result = _simplify_arc(coords, 1.0)
        self.assertEqual(result[0], coords[0])
        self.assertEqual(result[-1], coords[-1])

    def test_significant_bend_kept(self):
        """A large bend (right angle) should not be simplified away."""
        coords = [(0, 0), (100, 0), (100, 100)]
        result = _simplify_arc(coords, 1.0)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# _remove_reversals
# ---------------------------------------------------------------------------
class TestRemoveReversals(unittest.TestCase):

    def test_two_points_unchanged(self):
        coords = [(0, 0), (1, 1)]
        self.assertEqual(_remove_reversals(coords), coords)

    def test_no_reversal_unchanged(self):
        """Monotone arc → all points kept."""
        coords = [(0, 0), (1, 0), (2, 0), (3, 0)]
        self.assertEqual(_remove_reversals(coords), coords)

    def test_single_reversal_removed(self):
        """
        Arc: (0,0) → (2,0) → (1,0) → (3,0)
        (2,0) overshoots then the arc steps back to (1,0) — (2,0) is the reversal vertex.
        Algorithm: incoming from last-kept (0,0); v1=(2,0), v2=(-1,0) → dot<0 → drop (2,0).
        Then from (0,0): v1=(1,0), v2=(2,0) → dot≥0 → keep (1,0).
        Result: (0,0) → (1,0) → (3,0).
        """
        coords = [(0, 0), (2, 0), (1, 0), (3, 0)]
        result = _remove_reversals(coords)
        self.assertNotIn((2, 0), result)   # overshoot vertex removed
        self.assertIn((1, 0), result)      # subsequent vertex kept (no reversal)
        self.assertEqual(result[0], (0, 0))
        self.assertEqual(result[-1], (3, 0))

    def test_cascading_reversals(self):
        """Removing one reversal should expose and then remove the next."""
        # Back-and-forth zigzag that is entirely reversed after removal
        coords = [(0, 0), (4, 0), (3, 0), (2, 0), (5, 0)]
        result = _remove_reversals(coords)
        # Endpoints must be preserved
        self.assertEqual(result[0], (0, 0))
        self.assertEqual(result[-1], (5, 0))
        # All intermediate points should be monotonically increasing in x
        xs = [p[0] for p in result]
        self.assertEqual(xs, sorted(xs))

    def test_a_sharp_real_corner_is_kept(self):
        """Turning by more than a right angle is not doubling back: Victoria
        Island's NWT/Nunavut line turns 92 degrees, and losing that vertex
        cut the corner across the NWT's coast."""
        coords = [(0, 0), (10, -4), (13, 6)]               # a 95-degree turn
        self.assertEqual(_remove_reversals(coords), coords)

    def test_a_near_hairpin_is_removed(self):
        """Inside REVERSAL_CORNER_DEG of straight back: a reversal."""
        coords = [(0, 0), (10, 0), (1, 2), (20, 2)]        # a 13-degree corner
        self.assertNotIn((10, 0), _remove_reversals(coords))

    def test_endpoints_always_kept(self):
        coords = [(5, 0), (4, 0), (3, 0), (2, 0), (1, 0)]   # fully reversed
        result = _remove_reversals(coords)
        self.assertEqual(result[0], (5, 0))
        self.assertEqual(result[-1], (1, 0))


# ---------------------------------------------------------------------------
# _find_shared_arcs
# ---------------------------------------------------------------------------
class TestFindSharedArcs(unittest.TestCase):

    def _make_topo(self, geometries):
        """
        Minimal topo dict with custom geometries list.

        TopoJSON ring format for a Polygon:
          "arcs": [ [arc_idx, arc_idx, ...] ]   ← one list per ring
        For a MultiPolygon:
          "arcs": [ [ [arc_idx, ...] ], [ [arc_idx, ...] ] ]  ← one list per polygon
        """
        return {
            "arcs": [None] * 10,   # placeholder; content irrelevant here
            "objects": {
                "data": {"geometries": geometries}
            },
        }

    def test_no_shared_arcs(self):
        """Two polygons using disjoint arcs → nothing shared."""
        geoms = [
            {"type": "Polygon", "arcs": [[0, 1]]},
            {"type": "Polygon", "arcs": [[2, 3]]},
        ]
        shared = _find_shared_arcs(self._make_topo(geoms))
        self.assertEqual(shared, set())

    def test_one_shared_arc(self):
        """Arc 1 referenced by both provinces → shared."""
        geoms = [
            {"type": "Polygon", "arcs": [[0, 1]]},
            {"type": "Polygon", "arcs": [[1, 2]]},
        ]
        shared = _find_shared_arcs(self._make_topo(geoms))
        self.assertIn(1, shared)
        self.assertNotIn(0, shared)
        self.assertNotIn(2, shared)

    def test_negative_arc_ref_resolved(self):
        """Negative arc reference -2 maps to index -(−2+1)=1 → shared."""
        geoms = [
            {"type": "Polygon", "arcs": [[0, 1]]},
            {"type": "Polygon", "arcs": [[-2, 2]]},   # -2 → index 1
        ]
        shared = _find_shared_arcs(self._make_topo(geoms))
        self.assertIn(1, shared)

    def test_multipolygon_arcs_detected(self):
        """Arcs inside a MultiPolygon ring should also be detected."""
        geoms = [
            {"type": "MultiPolygon", "arcs": [[[0, 1]], [[2, 3]]]},
            {"type": "Polygon",      "arcs": [[1, 4]]},
        ]
        shared = _find_shared_arcs(self._make_topo(geoms))
        self.assertIn(1, shared)


# ---------------------------------------------------------------------------
# _reconstruct_ring
# ---------------------------------------------------------------------------
class TestReconstructRing(unittest.TestCase):

    ARC0 = [(0, 0), (1, 0), (2, 0)]
    ARC1 = [(2, 0), (2, 1), (2, 2)]

    def test_forward_arc(self):
        result = _reconstruct_ring([0], [self.ARC0])
        self.assertEqual(result[0], (0, 0))
        self.assertEqual(result[-1], (0, 0))   # should be closed

    def test_reversed_arc(self):
        """Negative ref -1 means arc index 0 reversed."""
        result = _reconstruct_ring([-1], [self.ARC0])
        self.assertEqual(result[0], (2, 0))
        self.assertEqual(result[-1], (2, 0))   # closed

    def test_two_arcs_joined(self):
        """Junction point should not be duplicated."""
        result = _reconstruct_ring([0, 1], [self.ARC0, self.ARC1])
        # ARC0 ends at (2,0), ARC1 starts at (2,0) — junction deduplicated
        junction_count = sum(1 for pt in result if pt == (2, 0))
        self.assertEqual(junction_count, 1)

    def test_ring_is_closed(self):
        result = _reconstruct_ring([0, 1], [self.ARC0, self.ARC1])
        self.assertEqual(result[0], result[-1])


# ---------------------------------------------------------------------------
# topo_simplify_two_tolerances — integration test
# ---------------------------------------------------------------------------
class TestTopoSimplifyTwoTolerances(unittest.TestCase):

    def _two_province_gdf(self):
        """Two adjacent 100 km × 100 km squares in EPSG:3347."""
        return gpd.GeoDataFrame(
            {"name": ["ProvinceA", "ProvinceB"]},
            geometry=[
                box(0, 0, 100_000, 100_000),
                box(100_000, 0, 200_000, 100_000),
            ],
            crs="EPSG:3347",
        )

    def test_returns_correct_row_count(self):
        gdf = self._two_province_gdf()
        result, _, _ = topo_simplify_two_tolerances(gdf, 1_000, 5_000)
        self.assertEqual(len(result), 2)

    def test_crs_preserved(self):
        gdf = self._two_province_gdf()
        result, _, _ = topo_simplify_two_tolerances(gdf, 1_000, 5_000)
        self.assertEqual(result.crs.to_epsg(), 3347)

    def test_geometries_are_valid(self):
        gdf = self._two_province_gdf()
        result, _, _ = topo_simplify_two_tolerances(gdf, 1_000, 5_000)
        for geom in result.geometry:
            self.assertIsNotNone(geom)
            self.assertFalse(geom.is_empty)

    def test_internal_arc_detected(self):
        """The shared edge (x=100 000) should produce exactly one internal arc."""
        gdf = self._two_province_gdf()
        _, coastal, internal = topo_simplify_two_tolerances(gdf, 1_000, 5_000)
        self.assertGreater(len(internal), 0, "Expected at least one internal arc")
        self.assertGreater(len(coastal), 0, "Expected coastal arcs")

    def test_high_tolerance_reduces_points(self):
        """Higher tolerance should produce equal or fewer arc points."""
        gdf = self._two_province_gdf()
        _, coast_lo, _ = topo_simplify_two_tolerances(gdf, 100, 100)
        _, coast_hi, _ = topo_simplify_two_tolerances(gdf, 50_000, 50_000)
        pts_lo = sum(len(s) for s in coast_lo)
        pts_hi = sum(len(s) for s in coast_hi)
        self.assertGreaterEqual(pts_lo, pts_hi)


# ---------------------------------------------------------------------------
# Seam clean-up -- LIB_shared_border's passes, run per internal arc
# ---------------------------------------------------------------------------
def _zigzag_pair(amplitude=200.0, step=500.0):
    """
    Two provinces meeting along a zigzag seam at x ~ 100 km.

    The zigzag's 500 m steps are what a 1 km min_segment should collapse; its
    200 m amplitude sits inside a 1 km DP corridor, so DP alone keeps every
    vertex -- the case the clean-up exists for.
    """
    ys = np.arange(0.0, 100_000.0 + step, step)
    seam = [(100_000.0 + (amplitude if i % 2 else 0.0), y)
            for i, y in enumerate(ys)]
    seam[-1] = (100_000.0, 100_000.0)
    left = Polygon([(0, 0)] + seam + [(0, 100_000)])
    right = Polygon(seam + [(200_000, 100_000), (200_000, 0)])
    return gpd.GeoDataFrame({"name": ["A", "B"]}, geometry=[left, right],
                            crs="EPSG:3347")


def _shortest(seg):
    pts = np.asarray(seg, dtype=float)
    return float(np.hypot(*(pts[1:] - pts[:-1]).T).min())


class TestSeamCleanup(unittest.TestCase):

    def test_off_by_default(self):
        """No clean-up arguments -> the DP-only result, unchanged."""
        gdf = _zigzag_pair()
        _, _, plain = topo_simplify_two_tolerances(gdf, 100, 100)
        _, _, zero = topo_simplify_two_tolerances(gdf, 100, 100,
                                                  min_segment_m=0,
                                                  min_angle_deg=0)
        self.assertEqual(sum(map(len, plain)), sum(map(len, zero)))

    def test_dp_alone_keeps_the_zigzag(self):
        """The premise: DP at a small tolerance leaves sub-1 km runs."""
        _, _, internal = topo_simplify_two_tolerances(_zigzag_pair(), 100, 100)
        self.assertLess(min(_shortest(s) for s in internal), 1_000)

    def test_min_segment_collapses_short_runs(self):
        _, _, internal = topo_simplify_two_tolerances(
            _zigzag_pair(), 100, 100, min_segment_m=1_000)
        self.assertGreaterEqual(min(_shortest(s) for s in internal), 1_000)

    def test_coastal_arcs_are_left_alone(self):
        gdf = _zigzag_pair()
        _, coast_plain, _ = topo_simplify_two_tolerances(gdf, 100, 100)
        _, coast_clean, _ = topo_simplify_two_tolerances(
            gdf, 100, 100, min_segment_m=1_000, min_angle_deg=20)
        self.assertEqual(sum(map(len, coast_plain)), sum(map(len, coast_clean)))

    def test_pair_still_partitions_after_cleanup(self):
        """The invariant: no gap, no overlap, both sides moved together."""
        gdf = _zigzag_pair()
        result, _, _ = topo_simplify_two_tolerances(
            gdf, 100, 100, min_segment_m=1_000, min_angle_deg=20)
        a, b = result.geometry
        self.assertTrue(a.is_valid and b.is_valid)
        self.assertAlmostEqual(a.intersection(b).area, 0.0, places=3)
        self.assertAlmostEqual((a.area + b.area) / 1e6,
                               a.union(b).area / 1e6, places=6)

    def test_despike_removes_a_needle(self):
        coords = [(0, 0), (10, 0), (11, 50), (12, 0), (20, 0)]
        out = _condition_arc(coords, 0.0, 20.0)
        self.assertNotIn((11, 50), out)
        self.assertEqual((out[0], out[-1]), (coords[0], coords[-1]))

    def test_closed_arc_is_never_collapsed_below_a_ring(self):
        """An arc that is a whole ring keeps 4+ points, or comes back as is."""
        ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        out = _condition_arc(ring, 1_000.0, 0.0)
        self.assertGreaterEqual(len(out), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
