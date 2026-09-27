"""Tests for ``lib.LIB_geotiff_clip.clip_geotiff``.

A small EPSG:3979 DEM is synthesised in ``tmp_path`` carrying a known elevation
ramp, so every pixel's expected value is computable from its column.  Boundaries
are then clipped out of it and checked on four axes: the *grid* (shape, affine,
bounds), the *cut* (which pixels survive and which became nodata), the
*reprojection* (a WGS84 boundary must land where a native-CRS one does), and the
*decimation* (target resolution and pixel budget), plus the streaming path that
splits the output into row bands.

No test touches ``Canada120m.tif`` -- the 7.3 GB reference DEM is not a unit-test
fixture.  The real-data run lives in ``docs/USING_GEOTIFF_CLIP.md``.
"""

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import Polygon, box

import geopandas as gpd

from lib.LIB_geotiff_clip import (ClipParams, clip_geotiff,
                                  clip_geotiff_from_dir)

DEM_CRS = CRS.from_epsg(3979)          # the Canada120m DEM's CRS
RES = 100.0                            # metres per pixel in the fixture
ORIGIN = (1_000_000.0, 2_000_000.0)    # (west, north) of the fixture, in metres
WIDTH, HEIGHT = 200, 150               # a 20 km x 15 km DEM
NODATA = -32767.0


def _dem_transform():
    return Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)


def _write_dem(tmp_path, name="dem.tif", nodata=NODATA, hole=None, dtype="float32"):
    """
    A DEM whose elevation is its column index, so value == column.

    That makes every assertion about *which* pixels survived a clip checkable
    from the data itself, with no second source of truth to drift.  ``hole`` is
    an optional (row slice, col slice) painted nodata, standing in for the sea.
    """
    data = np.tile(np.arange(WIDTH, dtype=dtype), (HEIGHT, 1))
    if hole is not None:
        data[hole] = nodata
    path = tmp_path / name
    with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                       count=1, dtype=dtype, crs=DEM_CRS,
                       transform=_dem_transform(), nodata=nodata) as ds:
        ds.write(data, 1)
    return path


def _metric_box(col0, row0, col1, row1):
    """A boundary box given in *pixel* coordinates, returned in metres."""
    west, north = ORIGIN
    return box(west + col0 * RES, north - row1 * RES,
               west + col1 * RES, north - row0 * RES)


def _metric_wedge(col0, row0, col1, row1):
    """A right-triangle boundary over the same pixel bbox as :func:`_metric_box`.

    It spans the full bbox (so window and shape assertions still line up) but its
    coverage of each row differs, which is what makes a row-banded mask testable.
    """
    west, north = ORIGIN
    x0, x1 = west + col0 * RES, west + col1 * RES
    y0, y1 = north - row1 * RES, north - row0 * RES
    return Polygon([(x0, y1), (x1, y1), (x1, y0)])


def _write_boundary(tmp_path, geom, name="boundary", crs=DEM_CRS):
    path = tmp_path / f"{name}.geojson"
    frame = gpd.GeoDataFrame(geometry=[geom], crs=DEM_CRS)
    if crs != DEM_CRS:
        frame = frame.to_crs(crs)
    frame.to_file(path, driver="GeoJSON")
    return path


class TestCut:
    """What survives the intersection and what becomes nodata."""

    def test_inside_kept_outside_nodata(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj)

        assert res.data.shape == (70, 100)
        assert res.valid_pixels == 70 * 100      # box fills its own bbox exactly
        assert res.coverage == pytest.approx(1.0)
        # value == source column, so the clip's first column is source column 50
        assert res.data[0, 0] == pytest.approx(50.0)
        assert res.data[-1, -1] == pytest.approx(149.0)

    def test_non_rectangular_boundary_leaves_nodata_corners(self, tmp_path):
        dem = _write_dem(tmp_path)
        # A triangle inside the bbox, so the corners outside it must be cut.
        west, north = ORIGIN
        tri = Polygon([(west + 50 * RES, north - 100 * RES),
                       (west + 150 * RES, north - 100 * RES),
                       (west + 100 * RES, north - 30 * RES)])
        res = clip_geotiff(dem, _write_boundary(tmp_path, tri))

        assert res.data[0, 0] == NODATA          # top-left corner is outside
        assert res.data[-1, 50] != NODATA        # bottom-centre is inside
        assert 0.4 < res.coverage < 0.6          # a triangle is ~half its bbox

    def test_source_nodata_is_not_counted_as_terrain(self, tmp_path):
        # A 20x100 "sea" inside the clipped region.
        dem = _write_dem(tmp_path, hole=(slice(30, 50), slice(50, 150)))
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj)

        assert res.valid_pixels == 70 * 100 - 20 * 100
        assert res.coverage == pytest.approx((70 * 100 - 2000) / 7000)

    def test_elevation_range_covers_valid_pixels_only(self, tmp_path):
        dem = _write_dem(tmp_path)
        res = clip_geotiff(dem, _write_boundary(tmp_path,
                                                _metric_box(50, 30, 150, 100)))
        assert res.zmin == pytest.approx(50.0)   # not NODATA
        assert res.zmax == pytest.approx(149.0)

    def test_all_touched_keeps_at_least_as_much(self, tmp_path):
        dem = _write_dem(tmp_path)
        # Half-pixel offsets, so the boundary genuinely straddles pixel edges.
        west, north = ORIGIN
        geom = box(west + 50.5 * RES, north - 99.5 * RES,
                   west + 149.5 * RES, north - 30.5 * RES)
        gj = _write_boundary(tmp_path, geom)
        tight = clip_geotiff(dem, gj, params=ClipParams(all_touched=False))
        loose = clip_geotiff(dem, gj, params=ClipParams(all_touched=True))
        assert loose.valid_pixels > tight.valid_pixels

    def test_buffer_expands_the_kept_region(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        plain = clip_geotiff(dem, gj)
        grown = clip_geotiff(dem, gj, params=ClipParams(buffer_m=5 * RES))
        assert grown.valid_pixels > plain.valid_pixels
        assert grown.width == plain.width + 10


class TestGrid:
    """Shape, affine and georeferencing of the clipped output."""

    def test_transform_and_bounds_match_the_window(self, tmp_path):
        dem = _write_dem(tmp_path)
        res = clip_geotiff(dem, _write_boundary(tmp_path,
                                                _metric_box(50, 30, 150, 100)))
        west, north = ORIGIN
        assert res.transform.c == pytest.approx(west + 50 * RES)
        assert res.transform.f == pytest.approx(north - 30 * RES)
        assert res.res == pytest.approx((RES, RES))
        assert res.bounds_m == pytest.approx(
            (west + 50 * RES, north - 100 * RES,
             west + 150 * RES, north - 30 * RES))

    def test_crop_false_keeps_the_full_extent(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(crop=False))
        assert (res.width, res.height) == (WIDTH, HEIGHT)
        assert res.valid_pixels == 70 * 100          # same cut, bigger canvas
        assert res.data[0, 0] == NODATA

    def test_boundary_overhanging_the_raster_is_clipped_not_rejected(self, tmp_path):
        dem = _write_dem(tmp_path)
        # Extends 50 px west and 50 px north of the DEM's own extent.
        gj = _write_boundary(tmp_path, _metric_box(-50, -50, 100, 100))
        res = clip_geotiff(dem, gj)
        assert (res.width, res.height) == (100, 100)
        assert res.valid_pixels == 100 * 100

    def test_disjoint_boundary_raises(self, tmp_path):
        dem = _write_dem(tmp_path)
        west, north = ORIGIN
        far = box(west - 500_000, north - 500_000, west - 400_000, north - 400_000)
        with pytest.raises(ValueError, match="does not overlap"):
            clip_geotiff(dem, _write_boundary(tmp_path, far))

    def test_empty_boundary_raises(self, tmp_path):
        dem = _write_dem(tmp_path)
        path = tmp_path / "empty.geojson"
        path.write_text('{"type":"FeatureCollection","features":[]}')
        with pytest.raises(ValueError, match="no boundary geometry"):
            clip_geotiff(dem, path)


class TestReprojection:
    """A WGS84 boundary must clip the same pixels as a native-CRS one."""

    def test_wgs84_boundary_matches_native_crs(self, tmp_path):
        dem = _write_dem(tmp_path)
        geom = _metric_box(50, 30, 150, 100)
        native = clip_geotiff(dem, _write_boundary(tmp_path, geom, "native"))
        wgs84 = clip_geotiff(dem, _write_boundary(tmp_path, geom, "wgs84",
                                                  crs=CRS.from_epsg(4326)))
        # Reprojection bows the box edges slightly, so allow a pixel of slack.
        assert abs(wgs84.width - native.width) <= 2
        assert abs(wgs84.height - native.height) <= 2
        assert wgs84.transform.c == pytest.approx(native.transform.c, abs=2 * RES)
        assert wgs84.coverage > 0.95

    def test_geodataframe_boundary_is_reprojected(self, tmp_path):
        """Regression: a GeoDataFrame must not be mistaken for a bare geometry.

        ``hasattr(x, "geom_type")`` is true for a GeoDataFrame as well (it
        returns a Series of type names), so a duck-type check sent frames down
        the "already in the raster's CRS" branch and skipped the reprojection
        they needed -- silently, since a WGS84 frame then produced an empty or
        wrong window rather than an error.
        """
        dem = _write_dem(tmp_path)
        geom = _metric_box(50, 30, 150, 100)
        native = clip_geotiff(dem, gpd.GeoDataFrame(geometry=[geom], crs=DEM_CRS))
        wgs84 = clip_geotiff(dem, gpd.GeoDataFrame(geometry=[geom],
                                                   crs=DEM_CRS).to_crs(4326))
        assert abs(wgs84.width - native.width) <= 2
        assert wgs84.coverage > 0.95
        assert native.data[0, 0] == pytest.approx(50.0)

    def test_shapely_geometry_accepted_directly(self, tmp_path):
        dem = _write_dem(tmp_path)
        res = clip_geotiff(dem, _metric_box(50, 30, 150, 100))
        assert (res.width, res.height) == (100, 70)
        assert res.data[0, 0] == pytest.approx(50.0)


class TestDecimation:

    def test_target_res_scales_the_grid(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(target_res_m=4 * RES))
        assert (res.width, res.height) == (25, 18)   # 100/4, round(70/4)
        assert res.res[0] == pytest.approx(4 * RES)
        assert res.decimation[0] == pytest.approx(4.0)
        # The coarse grid still covers exactly the same ground.
        assert res.bounds_m == pytest.approx(
            (ORIGIN[0] + 50 * RES, ORIGIN[1] - 100 * RES,
             ORIGIN[0] + 150 * RES, ORIGIN[1] - 30 * RES))

    def test_target_res_finer_than_source_does_not_upsample(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(target_res_m=RES / 10))
        assert (res.width, res.height) == (100, 70)
        assert res.res[0] == pytest.approx(RES)

    def test_max_pixels_budget_is_respected(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(max_pixels=1000))
        assert res.total_pixels <= 1100          # rounding may overshoot slightly
        assert res.width * res.height == res.total_pixels
        assert res.zmax == pytest.approx(149.0, abs=4.0)

    def test_decimated_cut_still_follows_the_boundary(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(crop=False,
                                                      target_res_m=5 * RES))
        assert res.data[0, 0] == NODATA
        assert res.coverage == pytest.approx((70 * 100) / (WIDTH * HEIGHT),
                                             abs=0.05)


class TestStreaming:
    """Row-banded output must be identical to a single-shot read.

    Every boundary here is a *wedge*, never a box.  A box fills its own bbox, so
    its mask is the same on every row and a band that forgot its row offset would
    still produce the right answer; a wedge's mask changes row by row, so the
    offset is load-bearing and a mistake in it shows up as a mismatch.
    """

    def test_row_bands_produce_the_same_array(self, tmp_path):
        dem = _write_dem(tmp_path, hole=(slice(40, 60), slice(60, 120)))
        gj = _write_boundary(tmp_path, _metric_wedge(50, 30, 150, 100))
        whole = clip_geotiff(dem, gj)
        # 400 bytes/row forces 1 row per band: 70 bands instead of 1.
        banded = clip_geotiff(dem, gj, params=ClipParams(band_bytes=400))
        assert np.array_equal(whole.data, banded.data)
        assert banded.valid_pixels == whole.valid_pixels
        assert banded.zmin == whole.zmin and banded.zmax == whole.zmax

    def test_row_bands_with_decimation_match(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_wedge(50, 30, 150, 100))
        whole = clip_geotiff(dem, gj, params=ClipParams(target_res_m=3 * RES))
        banded = clip_geotiff(dem, gj, params=ClipParams(target_res_m=3 * RES,
                                                         band_bytes=132))
        assert np.array_equal(whole.data, banded.data)

    def test_each_row_is_read_from_its_own_source_rows(self, tmp_path):
        """A band must read the source rows under it, not re-read the first."""
        # Elevation ramps by row here instead of by column, so a band that read
        # the wrong source rows repeats values rather than advancing.
        path = tmp_path / "rowramp.tif"
        data = np.tile(np.arange(HEIGHT, dtype="float32"), (WIDTH, 1)).T.copy()
        with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                           count=1, dtype="float32", crs=DEM_CRS,
                           transform=_dem_transform(), nodata=NODATA) as ds:
            ds.write(data, 1)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(path, gj, params=ClipParams(band_bytes=400))
        assert res.data[0, 0] == pytest.approx(30.0)     # first clipped row
        assert res.data[-1, 0] == pytest.approx(99.0)    # last clipped row
        assert np.array_equal(res.data[:, 0], np.arange(30, 100, dtype="float32"))


class TestMemoryLevers:
    """Bounding memory must not change a single clipped pixel."""

    def test_gdal_cache_cap_does_not_change_the_result(self, tmp_path):
        dem = _write_dem(tmp_path, hole=(slice(40, 60), slice(60, 120)))
        gj = _write_boundary(tmp_path, _metric_wedge(50, 30, 150, 100))
        plain = clip_geotiff(dem, gj)
        capped = clip_geotiff(dem, gj, params=ClipParams(gdal_cache_mb=16))
        assert np.array_equal(plain.data, capped.data)
        assert capped.valid_pixels == plain.valid_pixels

    def test_gdal_cache_cap_composes_with_banding_and_decimation(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_wedge(50, 30, 150, 100))
        plain = clip_geotiff(dem, gj, params=ClipParams(target_res_m=3 * RES))
        capped = clip_geotiff(dem, gj, params=ClipParams(target_res_m=3 * RES,
                                                          band_bytes=132,
                                                          gdal_cache_mb=16))
        assert np.array_equal(plain.data, capped.data)


class TestOutputFile:

    def test_written_file_round_trips(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        out = tmp_path / "clipped.tif"
        res = clip_geotiff(dem, gj, out, keep_array=True)

        assert res.path == str(out)
        with rasterio.open(out) as ds:
            assert ds.crs == DEM_CRS
            assert ds.nodata == NODATA
            assert (ds.width, ds.height) == (res.width, res.height)
            assert ds.transform == pytest.approx(res.transform)
            assert ds.profile["compress"] == "deflate"
            assert np.array_equal(ds.read(1), res.data)

    def test_array_not_held_unless_asked(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, tmp_path / "clipped.tif")
        assert res.data is None
        assert res.valid_pixels == 7000          # stats still accumulated

    def test_fill_value_overrides_source_nodata(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(crop=False,
                                                      fill_value=-1.0))
        assert res.nodata == -1.0
        assert res.data[0, 0] == -1.0

    def test_source_without_nodata_gets_the_project_sentinel(self, tmp_path):
        dem = _write_dem(tmp_path, name="nonodata.tif", nodata=None)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(crop=False))
        assert res.nodata == -32767.0
        assert res.data[0, 0] == -32767.0

    def test_integer_dem_gets_an_integer_sentinel(self, tmp_path):
        dem = _write_dem(tmp_path, name="int.tif", nodata=None, dtype="int16")
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        res = clip_geotiff(dem, gj, params=ClipParams(crop=False))
        assert res.nodata == float(np.iinfo(np.int16).min)
        assert res.data.dtype == np.int16


class TestBatch:

    def test_clips_every_boundary_in_a_directory(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj_dir = tmp_path / "boundaries"
        gj_dir.mkdir()
        for name, geom in (("west", _metric_box(0, 0, 80, 150)),
                           ("east", _metric_box(120, 0, 200, 150))):
            _write_boundary(gj_dir, geom, name)
        out = tmp_path / "clips"
        results = clip_geotiff_from_dir(dem, gj_dir, out)

        assert set(results) == {"west", "east"}
        assert (out / "west.tif").exists() and (out / "east.tif").exists()
        assert results["west"].zmax == pytest.approx(79.0)
        assert results["east"].zmin == pytest.approx(120.0)

    def test_disjoint_boundary_is_skipped_not_fatal(self, tmp_path):
        dem = _write_dem(tmp_path)
        gj_dir = tmp_path / "boundaries"
        gj_dir.mkdir()
        _write_boundary(gj_dir, _metric_box(0, 0, 80, 150), "west")
        west, north = ORIGIN
        _write_boundary(gj_dir, box(west - 500_000, north - 500_000,
                                    west - 400_000, north - 400_000), "far")
        results = clip_geotiff_from_dir(dem, gj_dir, tmp_path / "clips")
        assert set(results) == {"west"}


class TestCLI:

    def test_cli_writes_a_clip(self, tmp_path, capsys):
        from lib.LIB_geotiff_clip import main
        dem = _write_dem(tmp_path)
        gj = _write_boundary(tmp_path, _metric_box(50, 30, 150, 100))
        out = tmp_path / "cli.tif"
        assert main([str(dem), str(gj), str(out), "--res", "400"]) == 0
        assert out.exists()
        assert "25x18 @ 400 m" in capsys.readouterr().out
