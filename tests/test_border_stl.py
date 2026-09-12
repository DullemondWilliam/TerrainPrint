"""Tests for ``lib.LIB_border_stl.build_border_stl``.

Extrudes border-region polygons into a flat watertight solid of a given height.
Geometry is authored directly in print-mm so the maths is exact and the height
can be checked against the mesh bounding box.
"""

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from lib.LIB_border_stl import build_border_stl


def a_strip(x0=0.0, y0=0.0, w=20.0, t=2.0):
    """A simple rectangular border strip in print-mm."""
    return box(x0, y0, x0 + w, y0 + t)


class TestBuildBorderStl:

    def test_returns_watertight_mesh(self):
        mesh = build_border_stl(a_strip(), height_mm=2.0)
        assert mesh.is_watertight
        assert mesh.is_winding_consistent

    def test_height_sets_z_extent(self):
        mesh = build_border_stl(a_strip(), height_mm=3.5)
        zmin, zmax = mesh.bounds[0][2], mesh.bounds[1][2]
        assert zmin == pytest.approx(0.0)
        assert zmax == pytest.approx(3.5)

    def test_footprint_xy_preserved(self):
        strip = a_strip(x0=5.0, y0=1.0, w=20.0, t=2.0)
        mesh = build_border_stl(strip, height_mm=2.0)
        (xmin, ymin, _), (xmax, ymax, _) = mesh.bounds
        assert (xmin, ymin, xmax, ymax) == pytest.approx((5.0, 1.0, 25.0, 3.0))

    def test_volume_matches_area_times_height(self):
        strip = a_strip(w=20.0, t=2.0)     # area 40 mm^2
        mesh = build_border_stl(strip, height_mm=2.0)
        assert mesh.volume == pytest.approx(40.0 * 2.0, rel=1e-6)

    def test_multipolygon_extrudes_all_parts(self):
        parts = MultiPolygon([a_strip(x0=0.0), a_strip(x0=40.0)])
        mesh = build_border_stl(parts, height_mm=2.0)
        assert mesh.is_watertight
        assert mesh.body_count == 2                 # two disjoint bars
        assert mesh.volume == pytest.approx(2 * 40.0 * 2.0, rel=1e-6)

    def test_scale_converts_metres_to_mm(self):
        # 160 km x 16 km strip in metres, at 1:8_000_000 -> 20 mm x 2 mm.
        strip_m = box(0.0, 0.0, 160_000.0, 16_000.0)
        mesh = build_border_stl(strip_m, height_mm=2.0, scale=8_000_000)
        (xmin, ymin, _), (xmax, ymax, _) = mesh.bounds
        assert (xmax - xmin) == pytest.approx(20.0)
        assert (ymax - ymin) == pytest.approx(2.0)

    def test_geometry_crs_matching_projection_is_noop(self):
        # Declaring the geometry already in the build CRS must not move it.
        strip = a_strip()
        plain = build_border_stl(strip, height_mm=2.0)
        tagged = build_border_stl(strip, height_mm=2.0,
                                  geometry_crs="EPSG:3347", projection="EPSG:3347")
        assert tagged.bounds == pytest.approx(plain.bounds)

    def test_reprojects_from_geometry_crs_to_projection(self):
        # A tiny WGS84 lon/lat box reprojected into EPSG:3347 becomes a large
        # metric footprint; still watertight and much bigger than its degree span.
        box_wgs = box(-75.0, 45.0, -74.9, 45.1)
        mesh = build_border_stl(box_wgs, height_mm=2.0,
                                geometry_crs="EPSG:4326", projection="EPSG:3347")
        assert mesh.is_watertight
        (xmin, ymin, _), (xmax, ymax, _) = mesh.bounds
        assert (xmax - xmin) > 1000.0               # metres, not fractions of a degree

    def test_default_projection_constant(self):
        from lib.LIB_border_stl import DEFAULT_PROJECTION
        assert DEFAULT_PROJECTION == "EPSG:3347"

    def test_exports_stl(self, tmp_path):
        out = tmp_path / "borders.stl"
        mesh = build_border_stl(a_strip(), height_mm=2.0, out_path=out)
        assert out.exists() and out.stat().st_size > 0
        assert mesh.is_watertight

    def test_zero_height_raises(self):
        with pytest.raises(ValueError):
            build_border_stl(a_strip(), height_mm=0.0)

    def test_empty_geometry_raises(self):
        with pytest.raises(ValueError):
            build_border_stl(Polygon(), height_mm=2.0)

    def test_only_sub_printable_slivers_raises(self):
        tiny = box(0.0, 0.0, 0.1, 0.1)     # 0.01 mm^2, below MIN_PART_AREA_MM2
        with pytest.raises(ValueError):
            build_border_stl(tiny, height_mm=2.0)
