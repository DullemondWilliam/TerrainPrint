"""Tests for ``lib.LIB_lake_removal.remove_lakes``.

The contract is narrow but exact: strip every interior ring while leaving the
exterior shape untouched, return no-op inputs (None, empty, hole-free, and
non-polygon geometries) unchanged, and never alter the part count of a
multipolygon.  Fixtures use plain unit coordinates with known-exact areas so
assertions are precise rather than approximate.
"""

import pytest
from shapely.geometry import (GeometryCollection, LineString, MultiPolygon,
                              Point, Polygon, box)

from lib.LIB_lake_removal import remove_lakes


def _hole_count(geom) -> int:
    """Total interior rings across every polygon part."""
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.interiors)
    return sum(len(p.interiors) for p in geom.geoms)


def _holey_square(x0=0.0, side=10.0, margin=2.0) -> Polygon:
    """A ``side``x``side`` square at (x0, 0) with one centred square hole."""
    exterior = [(x0, 0), (x0 + side, 0), (x0 + side, side), (x0, side)]
    hole = [(x0 + margin, margin), (x0 + side - margin, margin),
            (x0 + side - margin, side - margin), (x0 + margin, side - margin)]
    return Polygon(exterior, [hole])


class TestNoOpInputs:
    """Inputs with nothing to strip come back untouched."""

    def test_none_passes_through(self):
        assert remove_lakes(None) is None

    def test_empty_polygon_stays_empty(self):
        result = remove_lakes(Polygon())
        assert result.is_empty

    def test_hole_free_polygon_is_same_object(self):
        # No interiors -> the code returns the original, not a rebuilt copy.
        poly = box(0, 0, 10, 10)
        assert remove_lakes(poly) is poly

    def test_hole_free_multipolygon_is_unchanged(self):
        geom = MultiPolygon([box(0, 0, 5, 5), box(10, 0, 15, 5)])
        result = remove_lakes(geom)
        assert result.equals(geom)
        assert _hole_count(result) == 0


class TestNonPolygonPassThrough:
    """Anything that is not a (multi)polygon is returned as-is."""

    def test_point(self):
        pt = Point(1, 1)
        assert remove_lakes(pt) is pt

    def test_linestring(self):
        line = LineString([(0, 0), (1, 1), (2, 0)])
        assert remove_lakes(line) is line

    def test_geometry_collection(self):
        coll = GeometryCollection([Point(0, 0), box(0, 0, 1, 1)])
        assert remove_lakes(coll) is coll


class TestPolygonHoleRemoval:

    def test_single_hole_removed(self):
        poly = _holey_square()
        assert _hole_count(poly) == 1
        assert _hole_count(remove_lakes(poly)) == 0

    def test_multiple_holes_all_removed(self):
        exterior = [(0, 0), (20, 0), (20, 20), (0, 20)]
        hole1 = [(1, 1), (4, 1), (4, 4), (1, 4)]
        hole2 = [(10, 10), (15, 10), (15, 15), (10, 15)]
        poly = Polygon(exterior, [hole1, hole2])
        assert _hole_count(poly) == 2
        assert _hole_count(remove_lakes(poly)) == 0

    def test_hole_is_filled_to_exact_exterior_area(self):
        poly = _holey_square(side=10.0, margin=2.0)   # 100 area, 36 hole
        result = remove_lakes(poly)
        assert result.area == pytest.approx(100.0)     # hole fully filled

    def test_exterior_ring_is_preserved_vertex_for_vertex(self):
        poly = _holey_square()
        result = remove_lakes(poly)
        assert list(result.exterior.coords) == list(poly.exterior.coords)

    def test_result_is_valid(self):
        result = remove_lakes(_holey_square())
        assert result.is_valid

    def test_area_never_decreases(self):
        poly = _holey_square()
        assert remove_lakes(poly).area > poly.area


class TestMultiPolygonHoleRemoval:

    def test_only_holey_part_is_rebuilt_clean_part_kept(self):
        clean = box(0, 0, 5, 5)
        geom = MultiPolygon([clean, _holey_square(x0=10)])
        assert _hole_count(geom) == 1

        result = remove_lakes(geom)
        assert _hole_count(result) == 0
        # The hole-free part survives; removal may reorder parts, so match by
        # geometry rather than position.
        assert any(part.equals(clean) for part in result.geoms)

    def test_every_holey_part_is_stripped(self):
        geom = MultiPolygon([_holey_square(x0=0), _holey_square(x0=20)])
        assert _hole_count(geom) == 2
        assert _hole_count(remove_lakes(geom)) == 0

    def test_part_count_is_preserved(self):
        geom = MultiPolygon([_holey_square(x0=0), box(20, 0, 25, 5),
                             _holey_square(x0=40)])
        result = remove_lakes(geom)
        assert result.geom_type == "MultiPolygon"
        assert len(result.geoms) == len(geom.geoms) == 3

    def test_filled_area_matches_summed_exteriors(self):
        geom = MultiPolygon([_holey_square(x0=0), _holey_square(x0=20)])
        result = remove_lakes(geom)
        assert result.area == pytest.approx(200.0)   # two 10x10 exteriors


class TestIslandInsideLake:
    """Regression for Northwest Territories: islands sit inside the lakes.

    Filling a lake by keeping only its exterior ring leaves the slab overlapping
    the island polygons that used to sit inside it — a "nested shells" error that
    made the whole multipolygon invalid until removal learned to dissolve it.
    """

    @staticmethod
    def _lake_with_island() -> MultiPolygon:
        # A 10x10 landmass with a 6x6 lake, and a 2x2 island inside that lake.
        land = _holey_square(side=10.0, margin=2.0)   # hole spans (2,2)-(8,8)
        island = box(4, 4, 6, 6)                      # sits inside the lake
        return MultiPolygon([land, island])

    def test_raw_input_is_valid(self):
        # The scenario itself is legitimate, valid geometry.
        assert self._lake_with_island().is_valid

    def test_result_is_valid(self):
        result = remove_lakes(self._lake_with_island())
        assert result.is_valid            # was False before the fix (nested shells)

    def test_no_holes_remain(self):
        assert _hole_count(remove_lakes(self._lake_with_island())) == 0

    def test_in_lake_island_is_absorbed_not_duplicated(self):
        # Filling the lake makes the island redundant; it dissolves into the slab
        # rather than remaining as an overlapping part.
        result = remove_lakes(self._lake_with_island())
        assert result.area == pytest.approx(100.0)   # the filled 10x10 exterior
