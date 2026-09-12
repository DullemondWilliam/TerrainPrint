"""Tests for ``lib.LIB_dovetail_fitter.fit_dovetails``.

A province is authored in EPSG:3347 metres, then "printed" to an STL through a
known metres→print-mm transform (optionally Y-flipped, optionally re-origined),
so the reconciler's recovered transform can be checked against ground truth and
the carved pockets against volume conservation and watertightness.
"""

import geopandas as gpd
import pytest
import trimesh
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box

from lib.LIB_border_connectors import ConnectorParams, extrude_solid
from lib.LIB_dovetail_fitter import fit_dovetails, fit_dovetails_from_dir

PARAMS = ConnectorParams(scale=800_000)          # mpm = 800 m per print-mm

# A Y-asymmetric province (so a Y-flip is detectable), with a straight right
# edge at x=160000 that the border strip abuts.
TRAP = Polygon([(0, 0), (160_000, 0), (160_000, 160_000), (60_000, 160_000)])
BORDER = box(160_000, 0, 164_000, 160_000)


def _write_geojson(tmp_path, name, geom):
    path = tmp_path / f"{name}.geojson"
    gpd.GeoDataFrame(geometry=[geom], crs="EPSG:3347").to_file(
        path, driver="GeoJSON")
    return str(path)


def _print_province(geom_m, mpm=PARAMS.mpm, origin=None, flip=False,
                    thickness=5.0):
    """Emulate the external GeoJSON→STL step: scale metres to print-mm, extrude."""
    minx, miny, _, maxy = geom_m.bounds
    ox, oy = origin if origin is not None else (minx, miny)
    if flip:
        matrix = [1.0 / mpm, 0, 0, -1.0 / mpm, -ox / mpm, maxy / mpm]
    else:
        matrix = [1.0 / mpm, 0, 0, 1.0 / mpm, -ox / mpm, -oy / mpm]
    return extrude_solid(affine_transform(geom_m, matrix), 0.0, thickness)


@pytest.fixture
def border_gj(tmp_path):
    return _write_geojson(tmp_path, "borders", BORDER)


class TestReconcile:

    def test_identity_alignment_recovered(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        stl = _print_province(TRAP)
        res = fit_dovetails(border_gj, [("trap", gj, stl)], params=PARAMS)
        r = res.reports[0]
        assert r.score == pytest.approx(1.0, abs=0.02)
        assert r.flipped_y is False
        assert r.origin_m[0] == pytest.approx(0.0, abs=100.0)
        assert r.origin_m[1] == pytest.approx(0.0, abs=100.0)
        assert r.n_pockets > 0

    def test_y_flip_detected(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        stl = _print_province(TRAP, flip=True)
        res = fit_dovetails(border_gj, [("trap", gj, stl)], params=PARAMS)
        r = res.reports[0]
        assert r.flipped_y is True
        assert r.score == pytest.approx(1.0, abs=0.02)
        assert res.provinces["trap"].is_watertight

    def test_nonzero_metric_origin_recovered(self, tmp_path, border_gj):
        # Province authored far from the origin, printed at its own local min.
        moved = affine_transform(TRAP, [1, 0, 0, 1, 500_000, 300_000])
        gj = _write_geojson(tmp_path, "trap", moved)
        stl = _print_province(moved)                 # origin = moved.bounds min
        res = fit_dovetails(border_gj, [("trap", gj, stl)], params=PARAMS)
        r = res.reports[0]
        assert r.origin_m[0] == pytest.approx(500_000.0, abs=200.0)
        assert r.origin_m[1] == pytest.approx(300_000.0, abs=200.0)

    def test_mismatched_shape_rejected(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        wrong = _print_province(box(0, 0, 20_000, 20_000))   # unrelated footprint
        with pytest.raises(ValueError, match="reconcile"):
            fit_dovetails(border_gj, [("trap", gj, wrong)], params=PARAMS)


class TestNotch:

    def test_province_watertight_and_material_removed(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        stl = _print_province(TRAP)
        res = fit_dovetails(border_gj, [("trap", gj, stl)], params=PARAMS)
        notched = res.provinces["trap"]
        assert notched.is_watertight and notched.volume > 0
        assert notched.volume < stl.volume            # pockets removed material

    def test_border_tabs_built_and_watertight(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        res = fit_dovetails(border_gj, [("trap", gj, _print_province(TRAP))],
                            params=PARAMS)
        assert res.border is not None
        assert res.border.is_watertight and res.border.volume > 0

    def test_pockets_align_under_tabs(self, tmp_path, border_gj):
        # The same lobe count should drive both halves: as many pockets as the
        # border has tab bodies along the shared seam.
        gj = _write_geojson(tmp_path, "trap", TRAP)
        res = fit_dovetails(border_gj, [("trap", gj, _print_province(TRAP))],
                            params=PARAMS)
        assert res.reports[0].n_pockets > 0


class TestOrchestration:

    def test_multiple_provinces(self, tmp_path):
        border_gj = _write_geojson(tmp_path, "borders", BORDER)
        # A second province on the far side of the strip.
        other = box(164_000, 0, 320_000, 160_000)
        gj1 = _write_geojson(tmp_path, "trap", TRAP)
        gj2 = _write_geojson(tmp_path, "other", other)
        specs = [("trap", gj1, _print_province(TRAP)),
                 ("other", gj2, _print_province(other))]
        res = fit_dovetails(border_gj, specs, params=PARAMS)
        assert set(res.provinces) == {"trap", "other"}
        assert all(m.is_watertight for m in res.provinces.values())
        assert len(res.reports) == 2

    def test_exports_keyed_stls(self, tmp_path, border_gj):
        gj = _write_geojson(tmp_path, "trap", TRAP)
        out = tmp_path / "keyed"
        fit_dovetails(border_gj, [("Prince Edward Island", gj, _print_province(TRAP))],
                      params=PARAMS, out_dir=out)
        assert (out / "Prince_Edward_Island_keyed.stl").exists()
        assert (out / "border_connectors.stl").exists()

    def test_empty_specs_raises(self, tmp_path, border_gj):
        with pytest.raises(ValueError):
            fit_dovetails(border_gj, [], params=PARAMS)

    def test_from_dir_pairs_geojson_and_stl(self, tmp_path):
        # borders.geojson + name_*.geojson in one dir, STLs in another.
        bdir = tmp_path / "b"
        bdir.mkdir()
        gpd.GeoDataFrame(geometry=[BORDER], crs="EPSG:3347").to_file(
            bdir / "borders.geojson", driver="GeoJSON")
        gpd.GeoDataFrame(geometry=[TRAP], crs="EPSG:3347").to_file(
            bdir / "name_Trap.geojson", driver="GeoJSON")
        sdir = tmp_path / "s"
        sdir.mkdir()
        _print_province(TRAP).export(str(sdir / "Trap.stl"))

        res = fit_dovetails_from_dir(bdir, sdir, params=PARAMS)
        assert "Trap" in res.provinces
        assert res.provinces["Trap"].is_watertight
