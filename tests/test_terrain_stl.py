"""Tests for ``lib.LIB_terrain_stl.terrain_to_stl``.

A small EPSG:3347 DEM is synthesised in ``tmp_path`` with elevation set from the
column index, so every vertex height is computable from the arithmetic in the
module docstring rather than from a second copy of it. The fixture is sized so
the numbers are round: 8000 m pixels at 1:8,000,000 are exactly 1 print-mm.

Four things carry the weight. The mesh must be **watertight with positive
volume** -- that is the whole reason the module keeps the DEM's grid instead of
triangulating, so it is asserted on every path including after the boolean cut.
The **height arithmetic** is checked against hand-computed millimetres, because
a silently wrong exaggeration prints a wrong mountain. The **frame** must round
-trip, since downstream code trusts it instead of reconciling. And the
**guards** must refuse before doing expensive work.

No test reads ``Canada120m.tif``.
"""

import json

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import Polygon, box

import geopandas as gpd

from lib.LIB_terrain_stl import (TerrainParams, _triangle_count,
                                 surface_sampler, terrain_to_stl)

PRINT_CRS = CRS.from_epsg(3347)        # the project's print CRS
OTHER_CRS = CRS.from_epsg(3979)        # the DEM's native CRS
SCALE = 8_000_000.0
MPM = SCALE / 1000.0                   # 8000 m per print-mm
RES = 8000.0                           # 1 print-mm per pixel -- round numbers
ORIGIN = (7_000_000.0, 2_000_000.0)    # (west, north) in EPSG:3347
COLS, ROWS = 21, 11                    # -> a 20 x 10 mm footprint
NODATA = -32767.0
BASE = 2.0
EXAG = 10.0

#: Elevation is 100 m per column, so column 20 is 2000 m -> 2.5 mm at EXAG.
def _elevation():
    return np.tile(np.arange(COLS, dtype="float32") * 100.0, (ROWS, 1))


def _params(**kw):
    defaults = dict(scale=SCALE, z_exaggeration=EXAG, base_height_mm=BASE,
                    floor_m=0.0, expect_crs=PRINT_CRS)
    defaults.update(kw)
    return TerrainParams(**defaults)


def _write_dem(tmp_path, name="dem.tif", data=None, nodata=NODATA,
               crs=PRINT_CRS, res=RES):
    data = _elevation() if data is None else data
    path = tmp_path / name
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[0],
                       width=data.shape[1], count=1, dtype="float32", crs=crs,
                       nodata=nodata,
                       transform=Affine.translation(*ORIGIN) * Affine.scale(res, -res)) as ds:
        ds.write(data, 1)
    return path


def _metric_box(col0, row0, col1, row1):
    """A boundary given in *pixel* coordinates of the fixture, in metres."""
    west, north = ORIGIN
    return box(west + col0 * RES, north - row1 * RES,
               west + col1 * RES, north - row0 * RES)


class TestSlab:
    """The uncut rectangular solid -- DEMto3D's output, built here instead."""

    def test_watertight_with_positive_volume(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.watertight is True
        assert res.volume_mm3 > 0.0
        assert res.mesh.is_volume        # trimesh's stricter check

    def test_triangle_count_is_exactly_the_grid_formula(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        # 4 per cell (top + base) plus two per wall quad around the perimeter.
        assert res.triangles == 4 * 20 * 10 + 2 * 2 * (20 + 10)
        assert res.triangles == _triangle_count(COLS, ROWS)
        assert res.triangles == 920

    def test_footprint_is_one_pixel_short_of_the_raster(self, tmp_path):
        """Vertices are pixel centres, so the solid is a pixel narrower."""
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.extent_mm[0] == pytest.approx((COLS - 1) * RES / MPM)
        assert res.extent_mm[1] == pytest.approx((ROWS - 1) * RES / MPM)
        assert res.extent_mm[0] == pytest.approx(20.0)
        assert res.extent_mm[1] == pytest.approx(10.0)

    def test_height_matches_the_documented_arithmetic(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        # (elev - floor) * exaggeration / mpm + base, at the 2000 m column.
        expected = 2000.0 * EXAG / MPM + BASE
        assert res.extent_mm[2] == pytest.approx(expected)
        assert res.extent_mm[2] == pytest.approx(4.5)

    def test_exaggeration_scales_only_relief_not_the_base(self, tmp_path):
        dem = _write_dem(tmp_path)
        low = terrain_to_stl(dem, params=_params(z_exaggeration=10.0))
        high = terrain_to_stl(dem, params=_params(z_exaggeration=20.0))
        assert high.extent_mm[2] - BASE == pytest.approx(
            2 * (low.extent_mm[2] - BASE))
        assert high.extent_mm[:2] == pytest.approx(low.extent_mm[:2])

    def test_floor_raises_the_datum(self, tmp_path):
        dem = _write_dem(tmp_path)
        sea = terrain_to_stl(dem, params=_params(floor_m=0.0))
        up = terrain_to_stl(dem, params=_params(floor_m=1000.0))
        # A 1000 m floor removes 1000 m of relief: 1000*10/8000 = 1.25 mm.
        assert sea.extent_mm[2] - up.extent_mm[2] == pytest.approx(1.25)

    def test_below_floor_land_is_clamped_not_punched_through(self, tmp_path):
        # Deep enough to matter: -2500 m at x10 is -3.125 mm, past the 2 mm base.
        # A shallower trench would be absorbed by the base and prove nothing.
        data = _elevation()
        data[:, 0] = -2500.0
        dem = _write_dem(tmp_path, data=data)
        clamped = terrain_to_stl(dem, params=_params(clamp_below_floor=True))
        free = terrain_to_stl(dem, params=_params(clamp_below_floor=False))

        # Clamped, the lowest terrain rests exactly on the base plane.
        assert clamped.mesh.bounds[0][2] == pytest.approx(0.0)
        assert clamped.extent_mm[2] == pytest.approx(4.5)
        # Unclamped, the surface dives through the base and out the bottom.
        assert free.mesh.bounds[0][2] == pytest.approx(-1.125)
        assert free.extent_mm[2] > clamped.extent_mm[2]

    def test_unclamped_punch_through_is_warned_about(self, tmp_path, caplog):
        """It stays legal, but silently self-intersecting solids are not on."""
        data = _elevation()
        data[:, 0] = -2500.0
        with caplog.at_level("WARNING"):
            terrain_to_stl(_write_dem(tmp_path, data=data),
                           params=_params(clamp_below_floor=False))
        assert "below the base" in caplog.text

    def test_shallow_dip_is_absorbed_by_the_base(self, tmp_path, caplog):
        data = _elevation()
        data[:, 0] = -500.0                     # -0.625 mm, inside the 2 mm base
        with caplog.at_level("WARNING"):
            res = terrain_to_stl(_write_dem(tmp_path, data=data),
                                 params=_params(clamp_below_floor=False))
        assert res.mesh.bounds[0][2] == pytest.approx(0.0)
        assert "below the base" not in caplog.text

    def test_nodata_sits_at_base_height(self, tmp_path):
        data = _elevation()
        data[:, :] = NODATA
        res = terrain_to_stl(_write_dem(tmp_path, data=data), params=_params())
        assert res.extent_mm[2] == pytest.approx(BASE)
        assert res.zmin_m is None and res.zmax_m is None
        assert res.watertight is True

    def test_elevation_range_reports_valid_pixels_only(self, tmp_path):
        data = _elevation()
        data[0, :] = NODATA
        res = terrain_to_stl(_write_dem(tmp_path, data=data), params=_params())
        assert res.zmin_m == pytest.approx(0.0)
        assert res.zmax_m == pytest.approx(2000.0)

    def test_spacing_is_reported_in_print_mm(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.spacing_mm == pytest.approx(1.0)
        assert res.grid == (COLS, ROWS)


class TestSurfaceSampler:
    """Heights a piece beside the solid stands to -- the solid's own."""

    def test_matches_the_solid_at_its_vertices(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        sample = surface_sampler(_write_dem(tmp_path), _params())
        top = res.mesh.vertices[: COLS * ROWS]
        xs = res.origin_m[0] + top[:, 0] * MPM
        ys = res.origin_m[1] + top[:, 1] * MPM
        assert sample(xs, ys) == pytest.approx(top[:, 2])

    def test_interpolates_between_centres(self, tmp_path):
        sample = surface_sampler(_write_dem(tmp_path), _params())
        west, north = ORIGIN
        # Halfway between the centres of columns 4 and 5: 450 m.
        x, y = west + 5.0 * RES, north - 5.5 * RES
        assert sample([x], [y])[0] == pytest.approx(450.0 * EXAG / MPM + BASE)

    def test_off_the_raster_and_nodata_are_the_bare_base(self, tmp_path):
        data = _elevation()
        data[:, 10] = NODATA
        sample = surface_sampler(_write_dem(tmp_path, data=data), _params())
        west, north = ORIGIN
        assert sample([west - 50 * RES, west + 10.5 * RES],
                      [north + 50 * RES, north - 5.5 * RES]) ==             pytest.approx([BASE, BASE])


class TestCut:
    """The coastline boolean -- the capability DEMto3D never had."""

    def test_cut_mesh_is_still_watertight(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), _metric_box(5, 2, 15, 8),
                             params=_params())
        assert res.watertight is True
        assert res.volume_mm3 > 0.0
        assert res.cut_to_boundary is True

    def test_cut_removes_volume(self, tmp_path):
        dem = _write_dem(tmp_path)
        whole = terrain_to_stl(dem, params=_params())
        cut = terrain_to_stl(dem, _metric_box(5, 2, 15, 8), params=_params())
        assert cut.volume_mm3 < whole.volume_mm3
        assert cut.extent_mm[0] < whole.extent_mm[0]

    def test_cut_footprint_matches_the_boundary(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), _metric_box(5, 2, 15, 8),
                             params=_params())
        # The box is 10 x 6 pixels = 10 x 6 print-mm at this fixture's scale.
        assert res.extent_mm[0] == pytest.approx(10.0, abs=1.0)
        assert res.extent_mm[1] == pytest.approx(6.0, abs=1.0)

    def test_non_rectangular_boundary_is_followed(self, tmp_path):
        west, north = ORIGIN
        triangle = Polygon([(west + 2 * RES, north - 9 * RES),
                            (west + 18 * RES, north - 9 * RES),
                            (west + 10 * RES, north - 1 * RES)])
        res = terrain_to_stl(_write_dem(tmp_path), triangle, params=_params())
        assert res.watertight is True
        whole = terrain_to_stl(_write_dem(tmp_path), params=_params())
        # A triangle is about half its bounding box.
        assert 0.3 < res.volume_mm3 / whole.volume_mm3 < 0.7

    def test_geodataframe_boundary_is_reprojected(self, tmp_path):
        """A WGS84 frame must land on the same ground as a native one."""
        geom = _metric_box(5, 2, 15, 8)
        native = terrain_to_stl(_write_dem(tmp_path), geom, params=_params())
        wgs = terrain_to_stl(
            _write_dem(tmp_path),
            gpd.GeoDataFrame(geometry=[geom], crs=PRINT_CRS).to_crs(4326),
            params=_params())
        assert wgs.volume_mm3 == pytest.approx(native.volume_mm3, rel=0.02)

    def test_buffer_keeps_a_wider_lip(self, tmp_path):
        dem = _write_dem(tmp_path)
        tight = terrain_to_stl(dem, _metric_box(5, 2, 15, 8), params=_params())
        lipped = terrain_to_stl(dem, _metric_box(5, 2, 15, 8),
                                params=_params(buffer_mm=1.0))
        assert lipped.volume_mm3 > tight.volume_mm3

    def test_boundary_that_misses_everything_raises(self, tmp_path):
        west, north = ORIGIN
        far = box(west - 5e6, north - 5e6, west - 4e6, north - 4e6)
        with pytest.raises(ValueError):
            terrain_to_stl(_write_dem(tmp_path), far, params=_params())

    def test_empty_boundary_file_raises(self, tmp_path):
        path = tmp_path / "empty.geojson"
        path.write_text('{"type":"FeatureCollection","features":[]}')
        with pytest.raises(ValueError, match="no boundary geometry"):
            terrain_to_stl(_write_dem(tmp_path), path, params=_params())


class TestGuards:
    """Each must refuse before doing expensive work."""

    def test_base_must_clear_the_dovetail(self, tmp_path):
        with pytest.raises(ValueError, match="must exceed dovetail_mm"):
            terrain_to_stl(_write_dem(tmp_path),
                           params=_params(base_height_mm=0.5, dovetail_mm=1.0))

    def test_wrong_crs_is_refused(self, tmp_path):
        """The 5.4 x 5.9 mm failure, caught instead of printed."""
        dem = _write_dem(tmp_path, name="wrong.tif", crs=OTHER_CRS)
        with pytest.raises(ValueError, match="print pipeline builds in"):
            terrain_to_stl(dem, params=_params())

    def test_crs_guard_can_be_waived(self, tmp_path):
        dem = _write_dem(tmp_path, name="wrong.tif", crs=OTHER_CRS)
        res = terrain_to_stl(dem, params=_params(expect_crs=None))
        assert res.watertight is True

    def test_triangle_budget_refuses_before_reading(self, tmp_path):
        with pytest.raises(ValueError, match="over max_triangles"):
            terrain_to_stl(_write_dem(tmp_path), params=_params(max_triangles=100))

    def test_triangle_budget_message_names_the_way_out(self, tmp_path):
        with pytest.raises(ValueError, match="downsample_geotiff"):
            terrain_to_stl(_write_dem(tmp_path), params=_params(max_triangles=100))

    def test_bed_fit_is_reported(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.bed_fits is True             # 20 x 10 mm on a 256 mm bed
        cramped = terrain_to_stl(_write_dem(tmp_path),
                                 params=_params(bed_mm=15.0, bed_margin_mm=1.0))
        assert cramped.bed_fits is False        # 20 mm will not fit 13 mm


class TestValidateGate:
    """FR-4.7. Every mesh the other tests build is watertight, so the gate has
    to be aimed at a broken one deliberately or it is never exercised."""

    @staticmethod
    def _broken():
        import trimesh
        return trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                               faces=[[0, 1, 2]], process=False)

    def test_non_watertight_mesh_is_refused(self, tmp_path, monkeypatch):
        import lib.LIB_terrain_stl as M
        monkeypatch.setattr(M, "_build_slab", lambda *a, **k: self._broken())
        with pytest.raises(ValueError, match="not watertight"):
            terrain_to_stl(_write_dem(tmp_path), params=_params())

    def test_gate_can_be_switched_off(self, tmp_path, monkeypatch):
        import lib.LIB_terrain_stl as M
        monkeypatch.setattr(M, "_build_slab", lambda *a, **k: self._broken())
        res = terrain_to_stl(_write_dem(tmp_path), params=_params(validate=False))
        assert res.watertight is False        # reported, not raised on

    def test_empty_mesh_is_refused(self, tmp_path, monkeypatch):
        import trimesh
        import lib.LIB_terrain_stl as M
        empty = trimesh.Trimesh(vertices=np.zeros((0, 3)),
                                faces=np.zeros((0, 3), dtype=int), process=False)
        monkeypatch.setattr(M, "_build_slab", lambda *a, **k: empty)
        with pytest.raises(ValueError, match="empty mesh"):
            terrain_to_stl(_write_dem(tmp_path), params=_params())

    def test_inverted_mesh_is_refused(self, tmp_path, monkeypatch):
        """A closed shell wound inside out is watertight but negative-volume."""
        import lib.LIB_terrain_stl as M
        import trimesh
        box_mesh = trimesh.creation.box(extents=(1, 1, 1))
        box_mesh.invert()
        monkeypatch.setattr(M, "_build_slab", lambda *a, **k: box_mesh)
        with pytest.raises(ValueError, match="non-positive volume"):
            terrain_to_stl(_write_dem(tmp_path), params=_params())


class TestFrame:
    """Downstream trusts this instead of reconciling, so it must round-trip."""

    def test_origin_and_mpm_reproduce_the_footprint(self, tmp_path):
        from shapely.affinity import affine_transform
        geom = _metric_box(5, 2, 15, 8)
        res = terrain_to_stl(_write_dem(tmp_path), geom, params=_params())

        x0, y0 = res.origin_m
        matrix = [1 / res.mpm, 0, 0, 1 / res.mpm, -x0 / res.mpm, -y0 / res.mpm]
        expected = affine_transform(geom, matrix).bounds
        got = res.mesh.bounds
        assert got[0][0] == pytest.approx(expected[0], abs=1.0)
        assert got[0][1] == pytest.approx(expected[1], abs=1.0)
        assert got[1][0] == pytest.approx(expected[2], abs=1.0)
        assert got[1][1] == pytest.approx(expected[3], abs=1.0)

    def test_origin_is_the_rasters_lower_left_pixel_centre(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        west, north = ORIGIN
        assert res.origin_m[0] == pytest.approx(west + 0.5 * RES)
        assert res.origin_m[1] == pytest.approx(north - (ROWS - 0.5) * RES)

    def test_piece_is_built_at_its_own_origin(self, tmp_path):
        """Design doc §4.3: every piece is built in its own local frame."""
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.mesh.bounds[0][0] == pytest.approx(0.0)
        assert res.mesh.bounds[0][1] == pytest.approx(0.0)
        assert res.mesh.bounds[0][2] == pytest.approx(0.0)

    def test_sidecar_is_written_next_to_the_stl(self, tmp_path):
        out = tmp_path / "piece.stl"
        res = terrain_to_stl(_write_dem(tmp_path), out_path=out,
                             params=_params())
        sidecar = tmp_path / "piece.frame.json"
        assert res.frame_path == str(sidecar)
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["origin_m"] == pytest.approx(list(res.origin_m))

    def test_sidecar_records_only_per_piece_facts(self, tmp_path):
        """Shared settings must not be copied next to every piece.

        A map's exaggeration, floor, CRS and scale have to be identical across
        thirteen provinces; writing them thirteen times is thirteen chances for
        one to drift, and a drifted exaggeration is invisible until the pieces
        will not meet. They live in project_config.json instead.
        """
        out = tmp_path / "piece.stl"
        terrain_to_stl(_write_dem(tmp_path), out_path=out, params=_params())
        data = json.loads((tmp_path / "piece.frame.json").read_text(encoding="utf-8"))
        for shared in ("z_exaggeration", "floor_m", "base_height_mm",
                       "spacing_mm", "crs", "mpm", "scale", "built_at_scale"):
            assert shared not in data, f"{shared} is map-wide, not per-piece"
        # Exactly two keys: the piece's own origin, and which map it belongs to.
        assert set(data) == {"origin_m", "settings_id"}

    def test_sidecar_fingerprints_the_settings_it_was_built_with(self, tmp_path):
        from lib.LIB_terrain_stl import frame_matches_config, settings_id
        out = tmp_path / "piece.stl"
        terrain_to_stl(_write_dem(tmp_path), out_path=out, params=_params())
        data = json.loads((tmp_path / "piece.frame.json").read_text(encoding="utf-8"))
        assert data["settings_id"] == settings_id()
        assert frame_matches_config(data) is True

    def test_a_piece_built_under_other_settings_is_detectable(self, tmp_path):
        """The point of the fingerprint: stale pieces do not silently assemble."""
        from lib.LIB_project_config import load
        from lib.LIB_terrain_stl import frame_matches_config, settings_id
        out = tmp_path / "piece.stl"
        terrain_to_stl(_write_dem(tmp_path), out_path=out, params=_params())
        data = json.loads((tmp_path / "piece.frame.json").read_text(encoding="utf-8"))

        changed = load()
        changed["terrain"] = dict(changed["terrain"])
        changed["terrain"]["exaggeration"] = 999.0
        assert settings_id(changed) != settings_id()
        assert frame_matches_config(data, changed) is False

    def test_read_frame_round_trips(self, tmp_path):
        from lib.LIB_terrain_stl import read_frame
        out = tmp_path / "piece.stl"
        res = terrain_to_stl(_write_dem(tmp_path), out_path=out, params=_params())
        frame = read_frame(out)
        assert frame["origin_m"] == pytest.approx(list(res.origin_m))
        assert read_frame(tmp_path / "absent.stl") is None

    def test_stl_round_trips(self, tmp_path):
        import trimesh
        out = tmp_path / "piece.stl"
        res = terrain_to_stl(_write_dem(tmp_path), out_path=out, params=_params())
        reloaded = trimesh.load(str(out))
        assert reloaded.is_watertight
        assert len(reloaded.faces) == res.triangles
        assert reloaded.volume == pytest.approx(res.volume_mm3, rel=1e-6)

    def test_no_files_written_without_out_path(self, tmp_path):
        res = terrain_to_stl(_write_dem(tmp_path), params=_params())
        assert res.path is None and res.frame_path is None
        assert not list(tmp_path.glob("*.stl"))


class TestSharedSettings:
    """One reader for the values every piece must agree on."""

    def test_without_a_config_they_are_the_shipped_settings(self):
        from lib.LIB_project_config import shipped as cfg
        from lib.LIB_terrain_stl import shared_settings
        settings = shared_settings()
        assert settings["exaggeration"] == pytest.approx(cfg("terrain.exaggeration"))
        assert settings["base_mm"] == pytest.approx(cfg("terrain.base_mm"))
        assert settings["floor_m"] == pytest.approx(cfg("terrain.floor_m"))
        assert settings["scale"] == cfg("print.scale")
        assert settings["crs"] == cfg("crs.working")

    def test_they_come_from_the_config_they_are_given(self, tmp_path):
        import json
        from lib.LIB_project_config import load_files
        from lib.LIB_terrain_stl import settings_id, shared_settings
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"terrain": {"exaggeration": 33.0}}),
                        encoding="utf-8")
        config = load_files(path)
        assert shared_settings(config)["exaggeration"] == pytest.approx(33.0)
        assert settings_id(config) != settings_id()

    def test_params_defaults_agree_with_the_shared_settings(self):
        """The dataclass defaults and the manifest must not disagree."""
        from lib.LIB_terrain_stl import shared_settings
        settings = shared_settings()
        params = TerrainParams()
        assert params.z_exaggeration == pytest.approx(settings["exaggeration"])
        assert params.base_height_mm == pytest.approx(settings["base_mm"])
        assert params.floor_m == pytest.approx(settings["floor_m"])
        assert params.scale == settings["scale"]

    def test_fingerprint_is_stable_and_short(self):
        from lib.LIB_terrain_stl import settings_id
        assert settings_id() == settings_id()
        assert len(settings_id()) == 12

    def test_fingerprint_moves_with_any_shared_value(self):
        from lib.LIB_project_config import load
        from lib.LIB_terrain_stl import settings_id
        base = settings_id()
        for section, key, value in (("terrain", "exaggeration", 99.0),
                                    ("terrain", "base_mm", 9.0),
                                    ("terrain", "floor_m", 500.0),
                                    ("terrain", "spacing_mm", 1.5),
                                    ("print", "scale", 4_000_000)):
            changed = load()
            changed[section] = dict(changed[section])
            changed[section][key] = value
            assert settings_id(changed) != base, f"{section}.{key} did not move it"

    def test_frame_matches_config_rejects_rubbish(self):
        from lib.LIB_terrain_stl import frame_matches_config
        assert frame_matches_config(None) is False
        assert frame_matches_config({}) is False
        assert frame_matches_config({"origin_m": [0, 0]}) is False


class TestCLI:

    def test_cli_builds_a_piece(self, tmp_path, capsys):
        from lib.LIB_terrain_stl import main
        dem = _write_dem(tmp_path)
        out = tmp_path / "cli.stl"
        assert main([str(dem), str(out), "--scale", str(SCALE),
                     "--exaggeration", str(EXAG), "--base", str(BASE)]) == 0
        assert out.exists() and (tmp_path / "cli.frame.json").exists()
        printed = capsys.readouterr().out
        assert "920 triangles" in printed
        assert "watertight=True" in printed

    def test_cli_cuts_to_a_boundary(self, tmp_path):
        from lib.LIB_terrain_stl import main
        dem = _write_dem(tmp_path)
        gj = tmp_path / "b.geojson"
        gpd.GeoDataFrame(geometry=[_metric_box(5, 2, 15, 8)],
                         crs=PRINT_CRS).to_file(gj, driver="GeoJSON")
        out = tmp_path / "cut.stl"
        assert main([str(dem), str(out), "-b", str(gj), "--scale", str(SCALE),
                     "--exaggeration", str(EXAG), "--base", str(BASE)]) == 0
        import trimesh
        assert trimesh.load(str(out)).is_watertight
