"""
test_raster_plan.py

``lib.LIB_raster_plan``: which path a crop takes, the grid it will make, and
the finest resolution a province can be built at -- tested without the
editor, against small DEMs written for the purpose.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_raster_plan.py -v
"""

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box

from lib import LIB_raster_plan as RP
from lib.LIB_geotiff_clip import clip_geotiff
from lib.LIB_raster_reproject import reproject_geotiff
from lib.LIB_terrain_stl import triangle_count

DEM_CRS = CRS.from_epsg(3979)
PRINT_CRS = CRS.from_epsg(3347)
RES = 100.0
ORIGIN = (1_000_000.0, 2_000_000.0)
WIDTH, HEIGHT = 200, 150


@pytest.fixture
def dem(tmp_path):
    path = tmp_path / "dem.tif"
    data = np.tile(np.arange(WIDTH, dtype="float32"), (HEIGHT, 1))
    with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                       count=1, dtype="float32", crs=DEM_CRS, nodata=-32767.0,
                       transform=Affine.translation(*ORIGIN)
                       * Affine.scale(RES, -RES)) as ds:
        ds.write(data, 1)
    return path


def _boundary(col0=50, row0=30, col1=150, row1=100):
    """``crs -> geometry`` for a box of the DEM, as the editor hands it."""
    import geopandas as gpd
    west, north = ORIGIN
    geom = box(west + col0 * RES, north - row1 * RES,
               west + col1 * RES, north - row0 * RES)

    def at(crs):
        if crs is None or CRS.from_user_input(crs) == DEM_CRS:
            return geom
        return gpd.GeoSeries([geom], crs=DEM_CRS).to_crs(crs).iloc[0]
    return at


class TestWhichPath:

    def test_needs_warp_is_the_frame_question(self):
        assert not RP.needs_warp(DEM_CRS, DEM_CRS)
        assert RP.needs_warp(DEM_CRS, PRINT_CRS)
        assert not RP.needs_warp(None, PRINT_CRS)
        assert not RP.needs_warp(DEM_CRS, None)

    def test_reduction_never_upsamples(self):
        assert RP.reduction(RES, 0.0) == 1.0
        assert RP.reduction(RES, 50.0) == 1.0
        assert RP.reduction(RES, 400.0) == 4.0

    def test_a_downsample_resamples_even_in_the_same_crs(self):
        assert not RP.needs_resample(DEM_CRS, DEM_CRS, RES, 0.0)
        assert RP.needs_resample(DEM_CRS, DEM_CRS, RES, 300.0)
        assert RP.needs_resample(DEM_CRS, PRINT_CRS, RES, 0.0)

    def test_native_in_the_dems_crs_is_a_plain_clip(self):
        warp, clip = RP.plan_run(DEM_CRS, RES, DEM_CRS, 0.0, _boundary(),
                                 crop=True, all_touched=True, cache_mb=64)
        assert warp is None
        assert clip.crop and clip.all_touched and clip.gdal_cache_mb == 64

    def test_a_warp_is_the_bbox_crop(self):
        warp, clip = RP.plan_run(DEM_CRS, RES, PRINT_CRS, 300.0, _boundary(),
                                 resampling="max")
        assert warp is not None and warp.dst_res_m == 300.0
        assert warp.bounds is not None and warp.resampling.name == "max"
        assert not clip.crop and clip.target_res_m == 0.0

    def test_no_crop_no_bounds(self):
        warp, _ = RP.plan_run(DEM_CRS, RES, PRINT_CRS, 300.0, _boundary(),
                              crop=False)
        assert warp.bounds is None


class TestOutputGrid:
    """The grid promised is the grid the run makes."""

    @pytest.mark.parametrize("dst, res", [(DEM_CRS, 0.0), (DEM_CRS, 300.0),
                                          (PRINT_CRS, 200.0)])
    def test_it_matches_the_run(self, dem, tmp_path, dst, res):
        boundary = _boundary()
        warp, clip = RP.plan_run(DEM_CRS, RES, dst, res, boundary)
        with rasterio.open(dem) as ds:
            grid = RP.output_grid(ds, dst, warp, clip, boundary(DEM_CRS))
        source = str(dem)
        if warp is not None:
            reproject_geotiff(source, dst, str(tmp_path / "w.tif"), warp)
            source = str(tmp_path / "w.tif")
        result = clip_geotiff(source, boundary(dst if warp else DEM_CRS),
                              str(tmp_path / "c.tif"), clip)
        assert (grid.width, grid.height) == (result.width, result.height)


class TestFinestRes:

    @staticmethod
    def _triangles(dem, dst, res):
        boundary = _boundary()
        warp, clip = RP.plan_run(DEM_CRS, RES, dst, res, boundary)
        with rasterio.open(dem) as ds:
            grid = RP.output_grid(ds, dst, warp, clip, boundary(DEM_CRS))
        return triangle_count(grid.width, grid.height)

    def test_native_when_it_fits(self, dem):
        assert RP.finest_res(dem, DEM_CRS, RES, DEM_CRS, _boundary(), True,
                             10_000_000) == 0.0

    def test_the_finest_that_fits(self, dem):
        res = RP.finest_res(dem, DEM_CRS, RES, DEM_CRS, _boundary(), True,
                            5_000)
        assert res > RES
        assert self._triangles(dem, DEM_CRS, res) <= 5_000
        assert self._triangles(dem, DEM_CRS, res - 1) > 5_000

    def test_never_finer_than_the_dem_when_warping(self, dem):
        res = RP.finest_res(dem, DEM_CRS, RES, PRINT_CRS, _boundary(), True,
                            10_000_000)
        assert res == RES

    def test_warped_it_still_fits(self, dem):
        res = RP.finest_res(dem, DEM_CRS, RES, PRINT_CRS, _boundary(), True,
                            5_000)
        assert self._triangles(dem, PRINT_CRS, res) <= 5_000
