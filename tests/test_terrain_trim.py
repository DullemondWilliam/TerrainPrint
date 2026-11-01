"""Tests for ``lib.LIB_terrain_trim.trim_terrain``.

A province is authored in EPSG:3347 metres, then "printed" as a DEMto3D-style
slab: a rectangular base over the province bbox with the province raised on top
and a flat nodata skirt around it (optionally Y-flipped / re-origined).  The
reconciler's recovered frame is checked against ground truth and the coastline
cut against volume conservation, footprint area, and watertightness.
"""

import geopandas as gpd
import manifold3d as m3d
import numpy as np
import pytest
import trimesh
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box

from lib.LIB_border_connectors import ConnectorParams, extrude_solid
from lib.LIB_stl_reconcile import base_footprint
from lib.LIB_terrain_trim import trim_terrain, trim_terrain_from_dir

PARAMS = ConnectorParams(scale=800_000)          # mpm = 800 m per print-mm

# A Y-asymmetric province (so a Y-flip is detectable) that does not fill its
# bounding box, so the DEMto3D skirt is a real, removable region.
TRAP = Polygon([(0, 0), (160_000, 0), (160_000, 160_000), (60_000, 160_000)])


def _write_geojson(tmp_path, name, geom):
    path = tmp_path / f"{name}.geojson"
    gpd.GeoDataFrame(geometry=[geom], crs="EPSG:3347").to_file(
        path, driver="GeoJSON")
    return str(path)


def _synthesize_demto3d(geom_m, mpm=PARAMS.mpm, base_mm=2.0, terrain_mm=6.0,
                        origin=None, flip=False):
    """Emulate a DEMto3D export: rectangular base + raised province + flat skirt."""
    minx, miny, maxx, maxy = geom_m.bounds
    ox, oy = origin if origin is not None else (minx, miny)
    if flip:
        matrix = [1.0 / mpm, 0, 0, -1.0 / mpm, -ox / mpm, maxy / mpm]
    else:
        matrix = [1.0 / mpm, 0, 0, 1.0 / mpm, -ox / mpm, -oy / mpm]
    base = extrude_solid(affine_transform(box(minx, miny, maxx, maxy), matrix),
                         0.0, base_mm)
    terrain = extrude_solid(affine_transform(geom_m, matrix),
                            base_mm - 0.01, base_mm + terrain_mm)
    # The pinned trimesh version needs an external Blender/OpenSCAD install
    # for booleans; manifold3d is already a required project dependency.
    def solid(mesh):
        return m3d.Manifold(m3d.Mesh(
            vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
            tri_verts=np.asarray(mesh.faces, dtype=np.uint32)))

    union = (solid(base) + solid(terrain)).to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(union.vert_properties)[:, :3],
                           faces=np.asarray(union.tri_verts), process=False)


class TestReconcile:

    def test_identity_alignment_recovered(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        res = trim_terrain(_synthesize_demto3d(TRAP), gj, params=PARAMS)
        assert res.score == pytest.approx(1.0, abs=0.05)
        assert res.flipped_y is False
        assert res.origin_m[0] == pytest.approx(0.0, abs=200.0)
        assert res.origin_m[1] == pytest.approx(0.0, abs=200.0)

    def test_y_flip_detected(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        res = trim_terrain(_synthesize_demto3d(TRAP, flip=True), gj, params=PARAMS)
        assert res.flipped_y is True
        assert res.score == pytest.approx(1.0, abs=0.05)
        assert res.mesh.is_watertight

    def test_nonzero_metric_origin_recovered(self, tmp_path):
        moved = affine_transform(TRAP, [1, 0, 0, 1, 500_000, 300_000])
        gj = _write_geojson(tmp_path, "trap", moved)
        res = trim_terrain(_synthesize_demto3d(moved), gj, params=PARAMS)
        assert res.origin_m[0] == pytest.approx(500_000.0, abs=400.0)
        assert res.origin_m[1] == pytest.approx(300_000.0, abs=400.0)

    def test_mismatched_shape_rejected(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        wrong = _synthesize_demto3d(box(0, 0, 40_000, 160_000))   # different shape
        with pytest.raises(ValueError, match="reconcile"):
            trim_terrain(wrong, gj, params=PARAMS)


class TestTrim:

    def test_watertight_and_skirt_removed(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        slab = _synthesize_demto3d(TRAP)
        res = trim_terrain(slab, gj, params=PARAMS)
        assert res.mesh.is_watertight and res.mesh.volume > 0
        assert res.mesh.volume < slab.volume            # skirt removed
        assert 0.0 < res.kept_fraction < 1.0

    def test_trimmed_footprint_matches_province(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        slab = _synthesize_demto3d(TRAP)
        res = trim_terrain(slab, gj, params=PARAMS)
        prov_area_mm2 = TRAP.area / (PARAMS.mpm ** 2)
        # The trimmed base is coastline-shaped, not the rectangular slab base.
        assert base_footprint(res.mesh).area == pytest.approx(
            prov_area_mm2, rel=0.05)
        assert base_footprint(res.mesh).area < base_footprint(slab).area * 0.99

    def test_buffer_grows_kept_footprint(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        slab = _synthesize_demto3d(TRAP)
        plain = trim_terrain(slab, gj, params=PARAMS)
        lipped = trim_terrain(slab, gj, params=PARAMS, buffer_mm=2.0)
        assert base_footprint(lipped.mesh).area > base_footprint(plain.mesh).area


class TestOrchestration:

    def test_exports_trimmed_stl(self, tmp_path):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        out = tmp_path / "pei_trimmed.stl"
        trim_terrain(_synthesize_demto3d(TRAP), gj, params=PARAMS, out_path=out)
        assert out.exists() and out.stat().st_size > 0

    def test_from_dir_pairs_geojson_and_stl(self, tmp_path):
        gdir = tmp_path / "g"
        gdir.mkdir()
        gpd.GeoDataFrame(geometry=[TRAP], crs="EPSG:3347").to_file(
            gdir / "name_Trap.geojson", driver="GeoJSON")
        sdir = tmp_path / "s"
        sdir.mkdir()
        _synthesize_demto3d(TRAP).export(str(sdir / "Trap.stl"))
        odir = tmp_path / "out"

        results = trim_terrain_from_dir(gdir, sdir, params=PARAMS, out_dir=odir)
        assert "Trap" in results
        assert results["Trap"].mesh.is_watertight
        assert (odir / "Trap_trimmed.stl").exists()

    def test_from_dir_skips_missing_stl(self, tmp_path):
        gdir = tmp_path / "g"
        gdir.mkdir()
        gpd.GeoDataFrame(geometry=[TRAP], crs="EPSG:3347").to_file(
            gdir / "name_Trap.geojson", driver="GeoJSON")
        gpd.GeoDataFrame(geometry=[TRAP], crs="EPSG:3347").to_file(
            gdir / "name_Missing.geojson", driver="GeoJSON")
        sdir = tmp_path / "s"
        sdir.mkdir()
        _synthesize_demto3d(TRAP).export(str(sdir / "Trap.stl"))

        results = trim_terrain_from_dir(gdir, sdir, params=PARAMS)
        assert set(results) == {"Trap"}          # Missing has no STL
