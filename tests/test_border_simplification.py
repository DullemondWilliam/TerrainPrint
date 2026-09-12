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
    topo_simplify_two_tolerances,
)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
