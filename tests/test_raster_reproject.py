"""Tests for ``lib.LIB_raster_reproject.reproject_geotiff``.

A small EPSG:3979 raster is synthesised in ``tmp_path`` and warped into
EPSG:3347 -- the same projection change the real pipeline makes. Checks fall
into four groups: the *grid* (resolution, extent, clipping the request to what
the raster holds), the *georeferencing* (a known point must land where pyproj
says it should, which is the whole point of the module), *nodata* (the warper
must not interpolate the -32767 sentinel into real terrain), and the *streaming*
path that splits the output into row bands.

No test touches ``Canada120m.tif``. A full warp of it would be 47831x42126 --
8 GB -- which is exactly why ``bounds`` exists and why it is tested here.
"""

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.enums import Resampling
from shapely.geometry import box

import geopandas as gpd

from lib.LIB_raster_reproject import (ReprojectParams, bounds_for,
                                      downsample_geotiff, reproject_geotiff)

SRC_CRS = CRS.from_epsg(3979)          # the Canada120m DEM's CRS
DST_CRS = CRS.from_epsg(3347)          # the project's print CRS
RES = 1000.0                           # metres per pixel in the fixture
ORIGIN = (1_000_000.0, 2_000_000.0)    # (west, north) in EPSG:3979
WIDTH, HEIGHT = 300, 200
NODATA = -32767.0


def _write_raster(tmp_path, name="src.tif", nodata=NODATA, hole=None,
                  crs=SRC_CRS, dtype="float32", constant=None):
    """
    A raster whose value is its column index, unless ``constant`` is given.

    A constant field is the sharpest nodata test available: any interpolation
    that touched the sentinel would leave a value that is not the constant.
    """
    if constant is None:
        data = np.tile(np.arange(WIDTH, dtype=dtype), (HEIGHT, 1))
    else:
        data = np.full((HEIGHT, WIDTH), constant, dtype=dtype)
    if hole is not None:
        data[hole] = nodata
    path = tmp_path / name
    with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                       count=1, dtype=dtype, crs=crs, nodata=nodata,
                       transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)) as ds:
        ds.write(data, 1)
    return path


def _src_bounds():
    west, north = ORIGIN
    return (west, north - HEIGHT * RES, west + WIDTH * RES, north)


def _dst_bounds():
    """The fixture's extent expressed in the destination CRS."""
    from rasterio.warp import transform_bounds
    return transform_bounds(SRC_CRS, DST_CRS, *_src_bounds())


class TestGeoreferencing:
    """The reason the module exists: pixels must land where pyproj says."""

    def test_a_known_point_lands_where_pyproj_puts_it(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES))

        # Take the source pixel at (row 50, col 100), project its centre, and
        # read the warped raster at that destination coordinate.
        west, north = ORIGIN
        sx = west + (100 + 0.5) * RES
        sy = north - (50 + 0.5) * RES
        dx, dy = Transformer.from_crs(SRC_CRS, DST_CRS,
                                      always_xy=True).transform(sx, sy)
        col = int((dx - result.transform.c) / result.transform.a)
        row = int((dy - result.transform.f) / result.transform.e)
        assert 0 <= col < result.width and 0 <= row < result.height
        # Value == source column, so the warped pixel must still read ~100.
        assert result.data[row, col] == pytest.approx(100.0, abs=1.5)

    def test_output_carries_the_destination_crs(self, tmp_path):
        src = _write_raster(tmp_path)
        out = tmp_path / "warped.tif"
        result = reproject_geotiff(src, DST_CRS, out,
                                   params=ReprojectParams(dst_res_m=RES))
        assert result.src_crs == SRC_CRS
        assert result.dst_crs == DST_CRS
        with rasterio.open(out) as ds:
            assert ds.crs == DST_CRS
            assert ds.nodata == NODATA
            assert (ds.width, ds.height) == (result.width, result.height)

    def test_projection_actually_changes_the_shape(self, tmp_path):
        """A warp that changed nothing would pass every other test here."""
        src = _write_raster(tmp_path)
        same = reproject_geotiff(src, SRC_CRS,
                                 params=ReprojectParams(dst_res_m=RES))
        other = reproject_geotiff(src, DST_CRS,
                                  params=ReprojectParams(dst_res_m=RES))
        assert (same.width, same.height) != (other.width, other.height)

    def test_source_without_crs_is_refused(self, tmp_path):
        src = _write_raster(tmp_path, name="nocrs.tif", crs=None)
        with pytest.raises(ValueError, match="no CRS"):
            reproject_geotiff(src, DST_CRS)


class TestGrid:

    def test_resolution_is_honoured(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=2000.0))
        assert result.res == pytest.approx((2000.0, 2000.0))
        assert result.transform.a == pytest.approx(2000.0)
        assert result.transform.e == pytest.approx(-2000.0)

    def test_default_resolution_follows_the_source(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS)
        # Ground sampling is preserved across the projection change.
        assert result.res[0] == pytest.approx(RES, rel=0.1)

    def test_bounds_limit_the_warped_extent(self, tmp_path):
        src = _write_raster(tmp_path)
        whole = reproject_geotiff(src, DST_CRS,
                                  params=ReprojectParams(dst_res_m=RES))
        left, bottom, right, top = _dst_bounds()
        quarter = (left, bottom, left + (right - left) / 2,
                   bottom + (top - bottom) / 2)
        part = reproject_geotiff(src, DST_CRS,
                                 params=ReprojectParams(dst_res_m=RES,
                                                        bounds=quarter))
        assert part.width < whole.width and part.height < whole.height
        assert part.bounds_m[0] == pytest.approx(quarter[0], abs=RES)
        assert part.bounds_m[3] == pytest.approx(quarter[3], abs=RES)

    def test_bounds_are_clipped_to_what_the_raster_holds(self, tmp_path):
        src = _write_raster(tmp_path)
        left, bottom, right, top = _dst_bounds()
        # Ask for far more than exists, in every direction.
        huge = (left - 5e6, bottom - 5e6, right + 5e6, top + 5e6)
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES,
                                                          bounds=huge))
        assert result.bounds_m[0] == pytest.approx(left, abs=2 * RES)
        assert result.bounds_m[2] == pytest.approx(right, abs=2 * RES)

    def test_disjoint_bounds_raise(self, tmp_path):
        src = _write_raster(tmp_path)
        left, bottom, right, top = _dst_bounds()
        far = (left - 5e6, bottom - 5e6, left - 4e6, bottom - 4e6)
        with pytest.raises(ValueError, match="do not overlap"):
            reproject_geotiff(src, DST_CRS,
                              params=ReprojectParams(bounds=far))


class TestBoundsFor:

    def test_all_features_contribute_to_buffered_bounds(self):
        frame = gpd.GeoDataFrame(geometry=[box(10, 20, 30, 40), None,
                                           box(25, 35, 50, 60), box(70, 5, 80, 15),
                                           box(0, 0, 0, 0).buffer(0)], crs=SRC_CRS)
        assert bounds_for(frame, SRC_CRS, buffer_m=2) == pytest.approx((8, 3, 82, 62))

    def test_bounds_come_back_in_the_destination_crs(self, tmp_path):
        geom = box(*_src_bounds())
        frame = gpd.GeoDataFrame(geometry=[geom], crs=SRC_CRS)
        bounds = bounds_for(frame, DST_CRS)
        assert bounds == pytest.approx(_dst_bounds(), abs=5000.0)
        assert bounds != pytest.approx(_src_bounds(), abs=5000.0)

    def test_reads_a_geojson_from_disk(self, tmp_path):
        path = tmp_path / "b.geojson"
        gpd.GeoDataFrame(geometry=[box(*_src_bounds())], crs=SRC_CRS).to_file(
            path, driver="GeoJSON")
        assert bounds_for(path, DST_CRS) == pytest.approx(_dst_bounds(),
                                                          abs=5000.0)

    def test_buffer_pads_every_side(self, tmp_path):
        geom = box(*_src_bounds())
        tight = bounds_for(geom, SRC_CRS)
        padded = bounds_for(geom, SRC_CRS, buffer_m=1000.0)
        assert padded[0] == pytest.approx(tight[0] - 1000.0)
        assert padded[1] == pytest.approx(tight[1] - 1000.0)
        assert padded[2] == pytest.approx(tight[2] + 1000.0)
        assert padded[3] == pytest.approx(tight[3] + 1000.0)

    def test_shapely_geometry_accepted_directly(self):
        assert bounds_for(box(*_src_bounds()), SRC_CRS) == pytest.approx(
            _src_bounds())

    def test_empty_boundary_raises(self, tmp_path):
        path = tmp_path / "empty.geojson"
        path.write_text('{"type":"FeatureCollection","features":[]}')
        with pytest.raises(ValueError, match="no features"):
            bounds_for(path, DST_CRS)


class TestNodata:
    """Bilinear must not drag the -32767 sentinel into real terrain."""

    def test_sentinel_does_not_bleed_into_valid_pixels(self, tmp_path):
        # A constant field with a nodata hole: any valid pixel that is not
        # exactly 500 has been contaminated by interpolation over the hole.
        src = _write_raster(tmp_path, constant=500.0,
                            hole=(slice(80, 120), slice(100, 200)))
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(
                                       dst_res_m=RES,
                                       resampling=Resampling.bilinear))
        valid = result.data[result.data != result.nodata]
        assert valid.size > 0
        assert valid.min() == pytest.approx(500.0, abs=0.01)
        assert valid.max() == pytest.approx(500.0, abs=0.01)

    def test_the_hole_survives_the_warp(self, tmp_path):
        src = _write_raster(tmp_path, constant=500.0,
                            hole=(slice(80, 120), slice(100, 200)))
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES))
        assert (result.data == result.nodata).sum() > 1000

    def test_elevation_range_ignores_nodata(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES))
        assert result.zmin > NODATA
        assert result.zmax == pytest.approx(WIDTH - 1, abs=2.0)

    def test_src_nodata_override_is_honoured(self, tmp_path):
        """An undeclared sentinel must still be excluded from interpolation.

        GDAL infers src nodata from a dataset that declares one, so this is the
        case that actually exercises ``src_nodata``: a raster carrying -9999
        (the value DEMto3D writes) with no nodata tag on the band. Without the
        override the warper treats it as terrain and bilinear drags it into the
        surrounding pixels.
        """
        path = tmp_path / "undeclared.tif"
        data = np.full((HEIGHT, WIDTH), 500.0, dtype="float32")
        data[80:120, 100:200] = -9999.0
        with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                           count=1, dtype="float32", crs=SRC_CRS,
                           transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)) as ds:
            ds.write(data, 1)

        told = reproject_geotiff(path, DST_CRS, params=ReprojectParams(
            dst_res_m=RES, src_nodata=-9999.0, dst_nodata=NODATA))
        valid = told.data[told.data != NODATA]
        assert valid.min() == pytest.approx(500.0, abs=0.01)
        assert valid.max() == pytest.approx(500.0, abs=0.01)

        # Without the override the sentinel is terrain, and bilinear smears it.
        naive = reproject_geotiff(path, DST_CRS, params=ReprojectParams(
            dst_res_m=RES, dst_nodata=NODATA))
        smeared = naive.data[(naive.data != NODATA) & (naive.data < 499.0)]
        assert smeared.size > 0, "expected the undeclared sentinel to bleed"

    def test_dst_nodata_override(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES,
                                                          dst_nodata=-1.0))
        assert result.nodata == -1.0
        assert (result.data == -1.0).any()      # corners fall outside the warp

    def test_source_without_nodata_gets_the_project_sentinel(self, tmp_path):
        path = tmp_path / "nonodata.tif"
        data = np.full((HEIGHT, WIDTH), 42.0, dtype="float32")
        with rasterio.open(path, "w", driver="GTiff", height=HEIGHT,
                           width=WIDTH, count=1, dtype="float32", crs=SRC_CRS,
                           transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)) as ds:
            ds.write(data, 1)
        result = reproject_geotiff(path, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES))
        assert result.nodata == -32767.0


class TestResampling:
    """The fidelity claims in the module docstring, pinned.

    A raster of flat ground with one sharp spike, warped down by 20x: which
    method you pick decides whether the summit survives at all.
    """

    @staticmethod
    def _spiked(tmp_path):
        data = np.full((HEIGHT, WIDTH), 100.0, dtype="float32")
        data[HEIGHT // 2, WIDTH // 2] = 5000.0
        path = tmp_path / "spike.tif"
        with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                           count=1, dtype="float32", crs=SRC_CRS, nodata=NODATA,
                           transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)) as ds:
            ds.write(data, 1)
        return path

    def _peak(self, src, method):
        return reproject_geotiff(src, DST_CRS, params=ReprojectParams(
            dst_res_m=RES * 20, resampling=method)).zmax

    def test_default_is_average(self):
        assert ReprojectParams().resampling is Resampling.average

    def test_max_preserves_the_summit_bilinear_loses(self, tmp_path):
        src = self._spiked(tmp_path)
        assert self._peak(src, Resampling.max) > self._peak(src, Resampling.bilinear)

    def test_max_recovers_essentially_the_true_peak(self, tmp_path):
        src = self._spiked(tmp_path)
        assert self._peak(src, Resampling.max) == pytest.approx(5000.0, rel=0.01)

    def test_average_sits_between_the_two(self, tmp_path):
        src = self._spiked(tmp_path)
        avg = self._peak(src, Resampling.average)
        assert self._peak(src, Resampling.bilinear) <= avg <= self._peak(src, Resampling.max)


class TestDownsample:
    """``downsample_geotiff``: reducing terrain resolution in place.

    The point of routing this through the warper rather than a plain decimation
    is nodata-aware resampling, so the tests that matter are the ones about
    *which* elevation survives -- not merely that the grid got smaller.
    """

    @staticmethod
    def _spiked(tmp_path, name="spike.tif"):
        """Flat ground at 100 m with one 5000 m spike, and a nodata hole."""
        data = np.full((HEIGHT, WIDTH), 100.0, dtype="float32")
        data[HEIGHT // 2, WIDTH // 2] = 5000.0
        data[20:40, 20:60] = NODATA
        path = tmp_path / name
        with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                           count=1, dtype="float32", crs=SRC_CRS, nodata=NODATA,
                           transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES)) as ds:
            ds.write(data, 1)
        return path

    def test_grid_shrinks_by_the_requested_factor(self, tmp_path):
        src = _write_raster(tmp_path)
        result = downsample_geotiff(src, RES * 10)
        assert result.res[0] == pytest.approx(RES * 10)
        assert result.width == pytest.approx(WIDTH / 10, abs=1)
        assert result.height == pytest.approx(HEIGHT / 10, abs=1)

    def test_crs_is_unchanged(self, tmp_path):
        src = _write_raster(tmp_path)
        result = downsample_geotiff(src, RES * 4)
        assert result.dst_crs == SRC_CRS
        assert result.src_crs == SRC_CRS

    def test_reduction_factor_is_reported(self, tmp_path):
        src = _write_raster(tmp_path)
        assert downsample_geotiff(src, RES * 8).reduction == pytest.approx(8.0)
        assert downsample_geotiff(src, RES).reduction == pytest.approx(1.0)

    def test_it_refuses_to_upsample(self, tmp_path):
        """Asking for finer than native returns native, not invented detail."""
        src = _write_raster(tmp_path)
        result = downsample_geotiff(src, RES / 10)
        assert result.res[0] == pytest.approx(RES)
        assert (result.width, result.height) == (WIDTH, HEIGHT)
        assert result.reduction == pytest.approx(1.0)

    def test_max_keeps_the_summit_that_bilinear_loses(self, tmp_path):
        src = self._spiked(tmp_path)
        keep = downsample_geotiff(src, RES * 20, params=ReprojectParams(
            resampling=Resampling.max)).zmax
        lose = downsample_geotiff(src, RES * 20, params=ReprojectParams(
            resampling=Resampling.bilinear)).zmax
        assert keep > lose
        assert keep == pytest.approx(5000.0, rel=0.01)

    def test_nodata_survives_and_does_not_bleed(self, tmp_path):
        """The reason this goes through the warper at all."""
        src = self._spiked(tmp_path)
        result = downsample_geotiff(src, RES * 5, params=ReprojectParams(
            resampling=Resampling.average))
        valid = result.data[result.data != result.nodata]
        assert (result.data == result.nodata).any()      # the hole survived
        assert valid.min() >= 100.0 - 0.01               # nothing dragged below

    def test_bounds_still_apply(self, tmp_path):
        src = _write_raster(tmp_path)
        whole = downsample_geotiff(src, RES * 4)
        west, north = ORIGIN
        half = (west, north - HEIGHT * RES, west + WIDTH * RES / 2, north)
        part = downsample_geotiff(src, RES * 4,
                                  params=ReprojectParams(bounds=half))
        assert part.width < whole.width

    def test_params_dst_res_is_overridden_not_obeyed(self, tmp_path):
        """The positional argument is the resolution; the field is ignored."""
        src = _write_raster(tmp_path)
        result = downsample_geotiff(src, RES * 10,
                                    params=ReprojectParams(dst_res_m=RES * 2))
        assert result.res[0] == pytest.approx(RES * 10)

    def test_written_file_round_trips(self, tmp_path):
        src = _write_raster(tmp_path)
        out = tmp_path / "coarse.tif"
        result = downsample_geotiff(src, RES * 4, out, keep_array=True)
        with rasterio.open(out) as ds:
            assert ds.crs == SRC_CRS
            assert (ds.width, ds.height) == (result.width, result.height)
            assert ds.res[0] == pytest.approx(RES * 4)
            assert np.array_equal(ds.read(1), result.data)

    def test_source_without_crs_is_refused(self, tmp_path):
        src = _write_raster(tmp_path, name="nocrs.tif", crs=None)
        with pytest.raises(ValueError, match="no CRS"):
            downsample_geotiff(src, RES * 4)


class TestStreaming:
    """Row-banded output must be identical to a single-shot warp."""

    @pytest.mark.parametrize("cache_mb", [0, 16])
    @pytest.mark.parametrize("disjoint", [False, True])
    def test_cache_uses_megabytes_and_restores_previous_limit(
            self, tmp_path, monkeypatch, cache_mb, disjoint):
        from rasterio.env import get_gdal_config

        src = _write_raster(tmp_path)
        bounds = (0, 0, 1, 1) if disjoint else None
        params = ReprojectParams(dst_res_m=RES, bounds=bounds, gdal_cache_mb=cache_mb)
        original_open = rasterio.open
        limits = []

        def observe_open(*args, **kwargs):
            limits.append(get_gdal_config("GDAL_CACHEMAX"))
            return original_open(*args, **kwargs)

        monkeypatch.setattr(rasterio, "open", observe_open)
        with rasterio.Env(GDAL_CACHEMAX=32 << 20):
            if disjoint:
                with pytest.raises(ValueError, match="do not overlap"):
                    reproject_geotiff(src, DST_CRS, params=params)
            else:
                reproject_geotiff(src, DST_CRS, params=params)
            assert get_gdal_config("GDAL_CACHEMAX") == 32 << 20
        assert limits == [(cache_mb or 32) << 20]

    def test_row_bands_produce_the_same_array(self, tmp_path):
        src = _write_raster(tmp_path, hole=(slice(50, 90), slice(60, 140)))
        params = dict(dst_res_m=RES)
        whole = reproject_geotiff(src, DST_CRS, params=ReprojectParams(**params))
        banded = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(band_bytes=512,
                                                          **params))
        assert np.array_equal(whole.data, banded.data)
        assert banded.valid_pixels == whole.valid_pixels
        assert banded.zmin == whole.zmin and banded.zmax == whole.zmax

    def test_gdal_cache_cap_does_not_change_the_result(self, tmp_path):
        src = _write_raster(tmp_path)
        plain = reproject_geotiff(src, DST_CRS,
                                  params=ReprojectParams(dst_res_m=RES))
        capped = reproject_geotiff(src, DST_CRS,
                                   params=ReprojectParams(dst_res_m=RES,
                                                          gdal_cache_mb=16))
        assert np.array_equal(plain.data, capped.data)


class TestOutputFile:

    @pytest.mark.parametrize("band_bytes", [512, 64 << 20])
    def test_written_file_round_trips(self, tmp_path, band_bytes):
        src = _write_raster(tmp_path)
        out = tmp_path / "warped.tif"
        result = reproject_geotiff(src, DST_CRS, out,
                                   params=ReprojectParams(dst_res_m=RES,
                                                          band_bytes=band_bytes),
                                   keep_array=True)
        with rasterio.open(out) as ds:
            assert ds.transform == pytest.approx(result.transform)
            assert ds.profile["compress"] == "deflate"
            assert np.array_equal(ds.read(1), result.data)

    def test_array_not_held_unless_asked(self, tmp_path):
        src = _write_raster(tmp_path)
        result = reproject_geotiff(src, DST_CRS, tmp_path / "warped.tif",
                                   params=ReprojectParams(dst_res_m=RES))
        assert result.data is None
        assert result.valid_pixels > 0          # stats still accumulated


class TestCLI:

    def test_cli_warps_a_raster(self, tmp_path, capsys):
        from lib.LIB_raster_reproject import main
        src = _write_raster(tmp_path)
        out = tmp_path / "cli.tif"
        assert main([str(src), "EPSG:3347", str(out), "--res", "2000"]) == 0
        assert out.exists()
        assert "EPSG:3979 -> EPSG:3347" in capsys.readouterr().out

    def test_cli_bounds_from_geojson(self, tmp_path, capsys):
        from lib.LIB_raster_reproject import main
        src = _write_raster(tmp_path)
        left, bottom, right, top = _src_bounds()
        gj = tmp_path / "b.geojson"
        gpd.GeoDataFrame(geometry=[box(left, bottom,
                                       left + (right - left) / 2,
                                       bottom + (top - bottom) / 2)],
                         crs=SRC_CRS).to_file(gj, driver="GeoJSON")
        out = tmp_path / "cli_bounds.tif"
        assert main([str(src), "EPSG:3347", str(out), "--res", "1000",
                     "--bounds-from", str(gj)]) == 0
        with rasterio.open(out) as ds:
            assert ds.width < WIDTH      # only the requested quarter was warped
