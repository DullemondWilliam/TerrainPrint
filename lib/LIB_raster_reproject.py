#!/usr/bin/env python3
"""
LIB_raster_reproject.py

Warp a DEM GeoTIFF into another CRS, over a chosen region and resolution.

The elevation raster and the print pipeline do not share a projection.
``Canada120m.tif`` is EPSG:3979 (Canada Atlas Lambert); every border corridor,
dovetail and province polygon in this project is built in EPSG:3347 (Statistics
Canada Lambert).  Those are different projections, not different units, so the
same province comes out a different shape in each:

=========================  ==================  ===========
Newfoundland and Labrador  Print size @ 1:8M   Area
=========================  ==================  ===========
EPSG:3347 (print)          175.7 x 137.0 mm    394,043 km2
EPSG:3979 (DEM)            181.1 x 131.1 mm    394,042 km2
=========================  ==================  ===========

A **5.4 x 5.9 mm** disagreement, against a 1.6 mm dovetail neck and 0.2 mm of fit
clearance.  Terrain meshed straight from the DEM's own CRS cannot key into a
border built in the print CRS -- the error is over three times the feature meant
to hold them together.  This module is what removes that error.

    from lib.LIB_raster_reproject import reproject_geotiff, ReprojectParams

    res = reproject_geotiff("Data/Canada120m-Orig/Canada120m.tif", "EPSG:3347",
                            out_path="warp/nl_3347.tif",
                            params=ReprojectParams(dst_res_m=3200.0,
                                                   bounds=nl_bounds_3347))

Always warp a region, never the file
------------------------------------
Warping the whole DEM from EPSG:3979 to EPSG:3347 would be a 47831 x 42126 grid
-- 8 GB -- to extract a province that occupies a few hundred pixels of it.  So
``bounds`` (in the *destination* CRS) is the parameter that matters: give it the
province's extent and only that region is warped.  :func:`bounds_for` turns a
GeoJSON into those bounds, buffered, so callers do not have to reproject by hand.

Pair it with ``LIB_geotiff_clip`` in this order:

    reproject (to the print CRS, over the province's bounds)  ->  clip (to the
    coastline)

Warping first means the coastline cut happens **in the frame that gets printed**,
at the resolution that gets printed, so the cut is exact there rather than being
a source-CRS cut dragged through a warp afterwards.

Resampling: why the default is ``average``, and when to override it
--------------------------------------------------------------------
``LIB_geotiff_clip`` must decimate nearest-neighbour, because GDAL's RasterIO
decimation path does not skip nodata and would average -32767 sentinels into the
coastline.  The **warper is a different code path**: given ``src_nodata`` it
carries a mask and excludes those pixels, so any method here is nodata-safe.
``tests/test_raster_reproject.py`` pins that.

That frees the choice to be about *fidelity*, and the usual instinct -- bilinear,
the standard warp default -- is wrong whenever the warp also downsamples.
Bilinear reads a 2x2 source neighbourhood around each destination pixel, so
going from 120 m to 3200 m it samples 4 of every 729 source pixels and smooths
them, which drags summits down hard.  Measured warping Labrador 120 m -> 3200 m,
against Mount Caubvick's true **1652 m** (the native-resolution clip reaches
1578 m):

==========  ===========  ==============================================
Resampling  Peak         
==========  ===========  ==============================================
``max``     **1577.6 m**  matches native; biases the whole surface up
``nearest`` 1428.3 m      point sample, no smoothing
``average`` 1264.6 m      the statistically correct downsample
``cubic``   1230.9 m      overshoots into -29 m troughs (ringing)
``bilinear``1165.1 m      worst here, despite being the usual default
==========  ===========  ==============================================

So: **``average``** is the default -- it is the honest answer to "what is the
mean elevation of this cell", and it is what you want for a printed surface.
Reach for **``max``** when summits are the point and a slight upward bias is
acceptable; it recovers essentially the native-resolution peak.  Use
``bilinear`` only when the warp is not also a large downsample.

Either way, the fidelity ceiling is set by warping straight to the print
resolution.  Warping at native resolution and decimating afterwards preserves
more (1570.8 m measured) at the cost of a far larger intermediate.
"""

import argparse
import logging
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio import windows
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform, transform_bounds
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

log = logging.getLogger(__name__)

#: Written when the source declares no nodata and the caller did not either.
#: Matches ``LIB_geotiff_clip.DEFAULT_NODATA`` and the sentinel Canada120m uses.
DEFAULT_NODATA = -32767.0
#: Output creation options, matching LIB_geotiff_clip's: tiled and deflated is
#: both smaller and far faster to window-read than the striped source.
_CREATION = dict(tiled=True, blockxsize=256, blockysize=256,
                 compress="deflate", predictor=3, zlevel=6, BIGTIFF="IF_SAFER")


@dataclass
class ReprojectParams:
    """How to warp the raster."""
    band: int = 1                       #: 1-based band to warp
    dst_res_m: float = 0.0              #: target metres per pixel (0 = keep the source's)
    bounds: tuple = None                #: (l, b, r, t) in the DESTINATION CRS; None = whole raster
    resampling: Resampling = Resampling.average    #: see the table in the module docstring
    src_nodata: float = None            #: override the source's declared nodata
    dst_nodata: float = None            #: nodata to write (default: the source's)
    band_bytes: int = 64 << 20          #: this module's read buffer per row band
    gdal_cache_mb: int = 0              #: cap GDAL's block cache for the warp (0 = leave alone)


@dataclass
class ReprojectResult:
    """Result of :func:`reproject_geotiff` (for inspection / tests)."""
    path: str = None                    #: written file, if ``out_path`` was given
    data: np.ndarray = None             #: the warped array, if it was kept in memory
    src_crs: object = None
    dst_crs: object = None
    transform: Affine = None            #: affine of the warped grid
    width: int = 0
    height: int = 0
    res: tuple = (0.0, 0.0)             #: metres per pixel in the destination CRS
    src_res: tuple = (0.0, 0.0)         #: metres per pixel the source carried
    nodata: float = 0.0
    bounds_m: tuple = (0.0, 0.0, 0.0, 0.0)   #: warped extent in the destination CRS
    valid_pixels: int = 0               #: pixels carrying data
    total_pixels: int = 0
    zmin: float = None                  #: elevation range over the valid pixels
    zmax: float = None

    @property
    def coverage(self):
        """Fraction of the output grid that is real terrain (0..1)."""
        return self.valid_pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def reduction(self):
        """How many source pixels went into each output pixel, per axis.

        1.0 means the resolution was preserved; 27.0 means each output pixel
        covers a 27x27 block of source, which is what warping the 120 m DEM
        straight to a 3200 m print grid actually does.
        """
        if not self.src_res[0] or not self.res[0]:
            return 1.0
        return self.res[0] / self.src_res[0]


def reproject_geotiff(raster, dst_crs, out_path=None, params=None,
                      keep_array=False):
    """
    Warp a raster into ``dst_crs``, over ``params.bounds`` and at ``dst_res_m``.

    Parameters
    ----------
    raster : str | pathlib.Path
        Source raster, in any CRS.
    dst_crs : str | rasterio.crs.CRS
        Destination CRS, e.g. ``"EPSG:3347"``.
    out_path : str | pathlib.Path, optional
        Write the warped GeoTIFF here (tiled + deflated).  If omitted, the
        result is returned in memory instead.
    params : ReprojectParams, optional
        ``bounds`` is the important one -- see the module docstring.
    keep_array : bool
        Also return the array when writing to disk.  Off by default, for the
        same reason as in ``LIB_geotiff_clip``: a province-sized warp can be
        hundreds of MB and the point of streaming is not to hold one.

    Returns
    -------
    ReprojectResult

    Raises
    ------
    ValueError
        If the source has no CRS, or ``bounds`` does not overlap the raster.
    """
    params = params or ReprojectParams()
    dst_crs = CRS.from_user_input(dst_crs)
    env = rasterio.Env(GDAL_CACHEMAX=params.gdal_cache_mb) \
        if params.gdal_cache_mb else _nullcontext()

    with env, rasterio.open(str(raster)) as src:
        if src.crs is None:
            raise ValueError(
                f"{raster}: no CRS, so there is nothing to warp it from. "
                "Assign one with gdal_edit or rasterio before reprojecting.")
        src_nodata = (params.src_nodata if params.src_nodata is not None
                      else src.nodata)
        nodata = _resolve_nodata(src, params, src_nodata)
        dtype = src.dtypes[params.band - 1]
        grid = _output_grid(src, dst_crs, params)

        result = ReprojectResult(
            path=str(out_path) if out_path is not None else None,
            src_crs=src.crs, dst_crs=dst_crs, transform=grid.transform,
            width=grid.width, height=grid.height,
            res=(abs(grid.transform.a), abs(grid.transform.e)),
            src_res=(abs(src.transform.a), abs(src.transform.e)),
            nodata=nodata,
            bounds_m=tuple(windows.bounds(
                windows.Window(0, 0, grid.width, grid.height), grid.transform)),
            total_pixels=grid.width * grid.height)

        profile = dict(src.profile, driver="GTiff", crs=dst_crs,
                       transform=grid.transform, width=grid.width,
                       height=grid.height, count=1, dtype=dtype,
                       nodata=nodata, **_CREATION)
        sink = rasterio.open(str(out_path), "w", **profile) if out_path else None
        keep = keep_array or out_path is None
        rows = [] if keep else None

        # A WarpedVRT presents the whole warped raster as a virtual dataset, so
        # rows can be pulled from it a band at a time.  Nothing the size of the
        # full warp is ever materialised -- which for this DEM would be 8 GB.
        vrt_args = dict(crs=dst_crs, transform=grid.transform,
                        width=grid.width, height=grid.height,
                        resampling=params.resampling, nodata=nodata)
        if src_nodata is not None:
            vrt_args["src_nodata"] = src_nodata
        try:
            with WarpedVRT(src, **vrt_args) as vrt:
                for row0, row1 in _row_bands(grid, dtype, params.band_bytes):
                    window = windows.Window(0, row0, grid.width, row1 - row0)
                    arr = vrt.read(params.band, window=window)
                    arr = arr.astype(dtype, copy=False)
                    _accumulate(result, arr, nodata)
                    if sink is not None:
                        sink.write(arr, 1, window=window)
                    if keep:
                        rows.append(arr)
        finally:
            if sink is not None:
                sink.close()
        if keep:
            result.data = np.vstack(rows) if len(rows) > 1 else rows[0]

    log.info("Warped %s %s -> %s: %dx%d @ %.0f m, %.1f%% covered.",
             Path(str(raster)).name, result.src_crs, dst_crs, result.width,
             result.height, result.res[0], result.coverage * 100.0)
    return result


def downsample_geotiff(raster, dst_res_m, out_path=None, params=None,
                       keep_array=False):
    """
    Coarsen a raster in its own CRS: a warp that changes only scale.

    Reducing terrain resolution is the same operation as reprojecting, minus the
    projection change, so it runs through the same warper rather than a second
    implementation.  That is not a tidiness argument -- it is the only path that
    resamples **nodata-aware**, which is what makes ``average`` and ``max``
    usable at all (see the resampling table in the module docstring).  Measured
    on Labrador, 120 m -> 3200 m in EPSG:3979, against Mount Caubvick's 1652 m:

    =========================================  ========
    Path                                       Peak
    =========================================  ========
    ``downsample_geotiff(..., max)``           1577.6 m
    ``downsample_geotiff(..., nearest)``       1513.2 m
    ``downsample_geotiff(..., average)``       1241.0 m
    ``LIB_geotiff_clip`` ``target_res_m``      1363.2 m
    =========================================  ========

    ``LIB_geotiff_clip``'s own decimation stays the cheap path (~0.2 s against
    ~0.7 s here) and is right when peaks do not matter; it is nearest-only
    because GDAL's RasterIO decimation cannot skip nodata.

    Parameters
    ----------
    raster : str | pathlib.Path
    dst_res_m : float
        Target metres per pixel.  **Clamped to the source's own resolution**:
        this function coarsens, and asking it to go finer returns native rather
        than inventing detail.  Use :func:`reproject_geotiff` directly if you
        genuinely want to upsample.
    out_path, params, keep_array
        As :func:`reproject_geotiff`.  ``params.dst_res_m`` is overridden by
        ``dst_res_m``; everything else (``bounds``, ``resampling``, the memory
        levers) applies unchanged.

    Returns
    -------
    ReprojectResult
        ``result.reduction`` reports the factor actually achieved.
    """
    params = params or ReprojectParams()
    with rasterio.open(str(raster)) as src:
        crs = src.crs
        native = abs(src.transform.a)
    if crs is None:
        raise ValueError(
            f"{raster}: no CRS, so there is no ground resolution to coarsen.")
    res = max(float(dst_res_m), native)
    if res != dst_res_m:
        log.info("Clamped requested %.1f m to the source's native %.1f m; "
                 "downsampling does not upsample.", dst_res_m, native)
    return reproject_geotiff(raster, crs, out_path,
                             replace(params, dst_res_m=res), keep_array)


def bounds_for(geojson, dst_crs, buffer_m=0.0):
    """
    The extent of a boundary in ``dst_crs``, for :attr:`ReprojectParams.bounds`.

    Saves callers reprojecting by hand, and makes the common case -- warp only
    the region a province occupies -- a one-liner:

        params = ReprojectParams(bounds=bounds_for(province, "EPSG:3347"))

    ``buffer_m`` pads the extent so a later clip has whole pixels to work with
    at the coastline rather than landing exactly on the edge of the warp.
    """
    dst_crs = CRS.from_user_input(dst_crs)
    # BaseGeometry, not hasattr("geom_type"): a GeoDataFrame has that attribute
    # too (it returns a Series of type names), so a duck-type check would take a
    # frame down this branch and silently skip its reprojection.
    if isinstance(geojson, BaseGeometry):
        geom = geojson
    else:
        frame = geojson if isinstance(geojson, gpd.GeoDataFrame) \
            else gpd.read_file(str(geojson))
        if frame.empty:
            raise ValueError(f"{geojson}: no features to take bounds from.")
        if frame.crs is not None and frame.crs != dst_crs:
            frame = frame.to_crs(dst_crs)
        geom = unary_union(frame.geometry.values)
    left, bottom, right, top = geom.bounds
    if buffer_m:
        left, bottom = left - buffer_m, bottom - buffer_m
        right, top = right + buffer_m, top + buffer_m
    return (left, bottom, right, top)


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

@dataclass
class _Grid:
    width: int
    height: int
    transform: Affine


def _output_grid(src, dst_crs, params):
    """
    The warped grid: resolution from the caller or the source, extent from
    ``bounds`` or the whole raster.
    """
    # calculate_default_transform answers "what resolution does this source have,
    # expressed in the destination CRS?" -- the right default when the caller has
    # no opinion, since it preserves ground sampling across the projection change.
    default_transform, _, _ = calculate_default_transform(
        src.crs, dst_crs, src.width, src.height, *src.bounds)
    res = params.dst_res_m if params.dst_res_m > 0 else abs(default_transform.a)

    if params.bounds is not None:
        left, bottom, right, top = params.bounds
        full = transform_bounds(src.crs, dst_crs, *src.bounds)
        # Clip the request to what the raster can actually supply, so a boundary
        # hanging off the edge of the DEM is a normal case, not an error.
        left, bottom = max(left, full[0]), max(bottom, full[1])
        right, top = min(right, full[2]), min(top, full[3])
        if right <= left or top <= bottom:
            raise ValueError(
                f"Requested bounds {tuple(round(v) for v in params.bounds)} do "
                f"not overlap the raster, whose extent in {dst_crs} is "
                f"{tuple(round(v) for v in full)}.")
    else:
        left, bottom, right, top = transform_bounds(src.crs, dst_crs, *src.bounds)

    width = max(1, int(np.ceil((right - left) / res)))
    height = max(1, int(np.ceil((top - bottom) / res)))
    # Anchor at the top-left and grow right/down, so the requested origin is the
    # one the output actually has (ceil above may extend past `right`/`bottom`).
    transform = Affine.translation(left, top) * Affine.scale(res, -res)
    return _Grid(width, height, transform)



def output_grid(src, dst_crs, params):
    """The grid a warp of ``src`` to ``dst_crs`` with ``params`` produces:
    ``.width``, ``.height`` and ``.transform``.  Public so a caller can know
    the size of a warp before running it."""
    return _output_grid(src, dst_crs, params)


def _row_bands(grid, dtype, band_bytes):
    """Split the output into row bands that each fit the memory budget."""
    per_row = max(1, grid.width * np.dtype(dtype).itemsize)
    rows = max(1, min(grid.height, band_bytes // per_row))
    return [(r, min(r + rows, grid.height)) for r in range(0, grid.height, rows)]


def _resolve_nodata(src, params, src_nodata):
    """The sentinel to write where the warp has no source data."""
    if params.dst_nodata is not None:
        return float(params.dst_nodata)
    if src_nodata is not None:
        return float(src_nodata)
    dtype = np.dtype(src.dtypes[params.band - 1])
    if np.issubdtype(dtype, np.floating):
        return DEFAULT_NODATA
    return float(np.iinfo(dtype).min)


def _accumulate(result, arr, nodata):
    """Fold one band's valid pixels into the running coverage / range stats."""
    valid = ~np.isnan(arr) if np.isnan(nodata) else (arr != nodata)
    count = int(valid.sum())
    if not count:
        return
    result.valid_pixels += count
    lo = float(arr[valid].min())
    hi = float(arr[valid].max())
    result.zmin = lo if result.zmin is None else min(result.zmin, lo)
    result.zmax = hi if result.zmax is None else max(result.zmax, hi)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Warp a DEM GeoTIFF into another CRS.")
    ap.add_argument("raster", help="source GeoTIFF")
    ap.add_argument("dst_crs", help="destination CRS, e.g. EPSG:3347")
    ap.add_argument("out", help="output GeoTIFF")
    ap.add_argument("--res", type=float, default=0.0, metavar="M",
                    help="destination metres per pixel (0 = keep the source's)")
    ap.add_argument("--bounds-from", metavar="GEOJSON",
                    help="warp only this boundary's extent (strongly advised: "
                         "warping a national DEM whole can be many GB)")
    ap.add_argument("--bounds-buffer", type=float, default=0.0, metavar="M",
                    help="pad the boundary extent by this many metres")
    ap.add_argument("--resampling", default="average",
                    choices=["average", "max", "nearest", "bilinear", "cubic"],
                    help="resampling method; 'max' preserves summits, 'average' "
                         "is the neutral downsample (default)")
    ap.add_argument("--gdal-cache", type=int, default=0, metavar="MB",
                    help="cap GDAL's block cache (0 = its default, 5%% of RAM)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    bounds = (bounds_for(args.bounds_from, args.dst_crs, args.bounds_buffer)
              if args.bounds_from else None)
    res = reproject_geotiff(
        args.raster, args.dst_crs, args.out,
        ReprojectParams(dst_res_m=args.res, bounds=bounds,
                        resampling=Resampling[args.resampling],
                        gdal_cache_mb=args.gdal_cache))
    print(f"{res.width}x{res.height} @ {res.res[0]:.0f} m  "
          f"{res.src_crs} -> {res.dst_crs}  "
          f"coverage {res.coverage * 100:.1f}%  "
          f"elevation {res.zmin:.0f}..{res.zmax:.0f} m  -> {res.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
