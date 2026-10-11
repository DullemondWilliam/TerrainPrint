#!/usr/bin/env python3
"""
LIB_raster_plan.py

Planning a crop before it runs: which path it takes, the grid it produces,
and the finest resolution a province can be built at.

The raster stage is either a plain clip (``LIB_geotiff_clip``) or a warp then
a clip (``LIB_raster_reproject``).  ``plan_run`` decides which from the
settings and returns the two libs' parameters; ``output_grid`` asks the same
libs what grid that run will make, so a cost preview promises the number
the run produces rather than re-deriving it; ``finest_res`` searches the
resolutions for the finest whose solid fits ``max_triangles``, counted the
way ``LIB_terrain_stl`` counts it.

A *boundary* is handed in as a function ``crs -> geometry`` (the province,
dissolved and buffered, in that CRS), because each step wants it in a
different frame: the warp bounds in the target CRS, the clip window in the
DEM's own.

Split out of ``UI_terrain_editor.py``.
"""

import numpy as np
import rasterio
from rasterio.enums import Resampling

from lib.LIB_geotiff_clip import ClipParams, read_window
from lib.LIB_geotiff_clip import output_grid as clip_grid
from lib.LIB_raster_reproject import ReprojectParams, bounds_for
from lib.LIB_raster_reproject import output_grid as warp_grid
from lib.LIB_terrain_stl import triangle_count

#: A requested resolution counts as coarser than the DEM's only past this
#: factor: float noise on an equal pitch must not route a run through the
#: warper.
COARSER = 1.001


def needs_warp(raster_crs, dst_crs) -> bool:
    """Whether the output lands in a different CRS than the DEM.

    This is the *frame* question -- what panel-linking and CRS labels key
    off -- not what decides which code path runs (see ``needs_resample``).
    """
    return (dst_crs is not None and raster_crs is not None
            and dst_crs != raster_crs)


def reduction(native_res: float, res_m: float) -> float:
    """The requested resolution as a multiple of the DEM's native pitch;
    1 for native (``res_m`` 0) and never below 1."""
    if not native_res or res_m <= 0:
        return 1.0
    return max(1.0, res_m / native_res)


def needs_resample(raster_crs, dst_crs, native_res: float,
                   res_m: float) -> bool:
    """Whether the run goes through the warper rather than plain decimation.

    A reprojection obviously needs it -- but so does a pure **downsample**.
    ``LIB_geotiff_clip`` can only decimate nearest-neighbour (GDAL's RasterIO
    path cannot skip nodata), which costs 1363 m of Mount Caubvick against
    the 1578 m ``max`` recovers.  Routing every coarsening through the warper
    is what keeps the resampling choice meaningful instead of silently inert
    whenever the target CRS happens to match the DEM's.
    """
    return (needs_warp(raster_crs, dst_crs)
            or reduction(native_res, res_m) > COARSER)


def plan_run(raster_crs, native_res: float, dst_crs, res_m: float,
             boundary, crop: bool = True, all_touched: bool = False,
             resampling: str = "average", cache_mb: int = 0):
    """
    ``(warp_params | None, clip_params)`` for a run with these settings.

    When a warp runs it is handed the boundary's bounds, so the warp *is* the
    bbox crop and the clip only applies the polygon mask -- which is also why
    the final grid equals the warped grid exactly.  ``boundary`` is
    ``crs -> geometry``.
    """
    if not needs_resample(raster_crs, dst_crs, native_res, res_m):
        return None, ClipParams(crop=crop, all_touched=all_touched,
                                target_res_m=res_m, gdal_cache_mb=cache_mb)
    # On a pure downsample this is the raster's own CRS, so the warp changes
    # only scale -- exactly what downsample_geotiff does.
    bounds = bounds_for(boundary(dst_crs), dst_crs) if crop else None
    warp = ReprojectParams(dst_res_m=res_m, bounds=bounds,
                           resampling=Resampling[resampling],
                           gdal_cache_mb=cache_mb)
    # crop=False: the warp already cut to the bbox; target_res_m=0 because
    # the warp already produced the requested resolution.
    clip = ClipParams(crop=False, all_touched=all_touched, target_res_m=0.0,
                      gdal_cache_mb=cache_mb)
    return warp, clip


def output_grid(ds, dst_crs, warp, clip, boundary_src):
    """The grid ``plan_run``'s parameters produce from the open dataset
    ``ds`` -- from the libs' own grid helpers.  ``boundary_src`` is the
    boundary in the DEM's CRS (it sets the clip window)."""
    if warp is not None:
        return warp_grid(ds, dst_crs, warp)
    return clip_grid(ds, read_window(ds, boundary_src, clip.crop), clip)


def finest_res(raster_path, raster_crs, native_res: float, dst_crs,
               boundary, crop: bool, max_triangles: int) -> float:
    """
    The finest resolution (m/px) whose solid fits ``max_triangles``: 0 for
    the DEM's own pitch when that fits and no warp is needed, else metres,
    rounded up to a whole metre and never finer than the DEM.

    Counted the way ``LIB_terrain_stl`` counts it -- over the crop's whole
    grid, whatever the coast cuts away -- from the grids the run would
    really produce, by bisection: coarser is always fewer triangles.
    ``boundary`` is ``crs -> geometry``.
    """
    native = float(native_res) or 1.0
    same_crs = not needs_warp(raster_crs, dst_crs)
    with rasterio.open(str(raster_path)) as ds:
        bounds = bounds_for(boundary(dst_crs), dst_crs) if crop else None
        window = read_window(ds, boundary(raster_crs), crop)

        def triangles(res):
            if same_crs and res <= native * COARSER:
                grid = clip_grid(ds, window, ClipParams(crop=crop,
                                                        target_res_m=0.0))
            else:
                grid = warp_grid(ds, dst_crs, ReprojectParams(
                    dst_res_m=res, bounds=bounds))
            return triangle_count(grid.width, grid.height)

        if triangles(native) <= max_triangles:
            return 0.0 if same_crs else native
        lo, hi = native, native * 2
        while triangles(hi) > max_triangles:
            lo, hi = hi, hi * 2
        for _ in range(40):                  # to well under a metre
            if hi - lo < 0.5:
                break
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if triangles(mid) <= max_triangles \
                else (mid, hi)
        res = float(np.ceil(hi))
        while triangles(res) > max_triangles:
            res += 1.0
        return res
