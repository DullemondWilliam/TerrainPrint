#!/usr/bin/env python3
"""
LIB_geotiff_clip.py

Intersect a DEM GeoTIFF with a province GeoJSON.

This is the raster half of the pipeline's front end: it takes the elevation
raster and one boundary polygon and returns *only the elevations inside that
boundary* -- everything outside the coastline becomes nodata.  The result is the
clipped DEM that a DEM->STL step (DEMto3D, or ``core.meshing`` later) consumes,
and the clip is what stops a province's mesh from carrying its neighbours'
terrain.

    from lib.LIB_geotiff_clip import clip_geotiff, ClipParams

    res = clip_geotiff("Data/Canada120m-Orig/Canada120m.tif",
                       "Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson",
                       out_path="clip/newfoundland.tif",
                       params=ClipParams(target_res_m=480.0))
    print(res.coverage, res.zmin, res.zmax)

Why this is not two lines of ``rasterio.mask``
----------------------------------------------
The reference DEM (``Canada120m.tif``) is 45921x39913 float32 -- **7.3 GB** --
*striped* (one scanline per block), uncompressed, and with no overviews.
``rasterio.mask.mask`` materialises the whole polygon-bbox window at native
resolution in RAM; for Labrador that is a 422 MB array, and for a province
spanning the mainland it is far worse.  So this module instead:

* reprojects the polygon into the raster's CRS (the GeoJSON is WGS84, the DEM is
  EPSG:3979 -- clipping without this silently produces an empty window),
* clips the read window to the raster's own extent, so a polygon hanging off the
  edge of the DEM is a normal case rather than an error,
* **decimates during the read** (``out_shape``), which is the only way to ask
  GDAL for a cheap coarse view of a raster that has no overviews, and
* streams output in row bands, so the clipped array is never held whole.

Two memory pools, not one
-------------------------
``band_bytes`` bounds only *this module's* buffer -- the array it holds and
writes.  GDAL keeps a second, usually larger pool: its block cache, which
defaults to 5% of system RAM.  Because ``Canada120m.tif`` is striped, every
windowed read pulls whole 45921-pixel scanlines into that cache, so it fills
fast and dominates the total.  Measured clipping Newfoundland and Labrador at
native resolution (12072x8737 -- 402 MB if read whole), ``band_bytes=64 MB``:

===================  =================  ======
``gdal_cache_mb``    peak working set   time
===================  =================  ======
0 (GDAL's default)   1891 MB            6.1 s
512                  256 MB             7.0 s
32                   256 MB             7.0 s
===================  =================  ======

So ``gdal_cache_mb`` is the lever that actually moves the number, at about a
second of re-reads; the clipped pixels come out identical either way.  Below
~512 MB the peak stops falling because ``band_bytes`` and the rasterised
boundary become the floor.  It is left unset by default: clamping a
process-wide GDAL setting is the caller's decision, not a library's.

Resolution is a real choice, not an optimisation
------------------------------------------------
At the project's 1:8,000,000 print scale, 1 print-mm is 8 km, so a 120 m pixel is
0.015 mm -- roughly two orders of magnitude finer than any printer resolves.
``target_res_m`` (metres per pixel) or ``max_pixels`` decimates on read; a good
rule is ``target_res_m = scale * nozzle_mm / 1000`` (1:8M with a 0.4 mm nozzle ->
3200 m).  Leave both unset to clip at native resolution.

Nodata and resampling
---------------------
Decimation defaults to nearest-neighbour on purpose.  GDAL's ``average``
resampling does *not* skip nodata on the direct RasterIO path, so averaging a
-32767 sentinel with real terrain yields a deep negative crater along every
coastline.  Pass ``resampling=Resampling.average`` only for a raster whose
window is fully valid.
"""

import argparse
import logging
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio import windows
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.features import geometry_mask
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

log = logging.getLogger(__name__)

#: Nodata to write when the source raster declares none and the caller did not
#: either.  Matches the sentinel ``Canada120m.tif`` uses.
DEFAULT_NODATA = -32767.0
#: Output creation options.  The input is striped and uncompressed; clipped
#: provinces are tiled and deflated, which is both smaller and far faster to
#: window-read downstream.
_CREATION = dict(tiled=True, blockxsize=256, blockysize=256,
                 compress="deflate", predictor=3, zlevel=6, BIGTIFF="IF_SAFER")


@dataclass
class ClipParams:
    """How to intersect the raster with the polygon."""
    band: int = 1                       #: 1-based band to clip
    crop: bool = True                   #: crop to the polygon bbox (else keep full extent)
    all_touched: bool = False           #: keep pixels the polygon merely touches
    buffer_m: float = 0.0               #: outward buffer on the polygon, raster-CRS metres
    target_res_m: float = 0.0           #: decimate to ~this many metres per pixel (0 = native)
    max_pixels: int = 0                 #: or: decimate until the output fits this budget
    fill_value: float = None            #: nodata to write (default: the source's)
    resampling: Resampling = Resampling.nearest   #: see module docstring before changing
    band_bytes: int = 64 << 20          #: this module's read buffer per row band
    gdal_cache_mb: int = 0              #: cap GDAL's block cache for the clip (0 = leave alone)


@dataclass
class ClipResult:
    """Result of :func:`clip_geotiff` (for inspection / tests)."""
    path: str = None                    #: written file, if ``out_path`` was given
    data: np.ndarray = None             #: the clipped array, if it was kept in memory
    crs: object = None                  #: raster CRS (unchanged by clipping)
    transform: Affine = None            #: affine of the clipped grid
    width: int = 0
    height: int = 0
    res: tuple = (0.0, 0.0)             #: metres per pixel after decimation
    decimation: tuple = (1.0, 1.0)      #: source pixels per output pixel (x, y)
    nodata: float = 0.0
    bounds_m: tuple = (0.0, 0.0, 0.0, 0.0)   #: clipped extent in the raster CRS
    valid_pixels: int = 0               #: pixels inside the polygon carrying data
    total_pixels: int = 0
    zmin: float = None                  #: elevation range over the valid pixels
    zmax: float = None

    @property
    def coverage(self):
        """Fraction of the output grid that is real terrain (0..1)."""
        return self.valid_pixels / self.total_pixels if self.total_pixels else 0.0


def clip_geotiff(raster, geojson, out_path=None, params=None, keep_array=False):
    """
    Clip a DEM to a boundary polygon, writing nodata outside it.

    Parameters
    ----------
    raster : str | pathlib.Path
        Source DEM.  Any CRS; the polygon is reprojected to match it.
    geojson : str | pathlib.Path | shapely geometry | geopandas.GeoDataFrame
        The boundary.  A path or frame is read and dissolved to a single
        geometry; a bare shapely geometry is assumed to be in the raster's CRS.
    out_path : str | pathlib.Path, optional
        Write the clipped GeoTIFF here (tiled + deflated).  If omitted, the
        result is returned in memory instead.
    params : ClipParams, optional
    keep_array : bool
        Also return the array when writing to disk.  Off by default: a province
        clip can be hundreds of MB and the whole point of streaming is not to
        hold one.

    Returns
    -------
    ClipResult

    Raises
    ------
    ValueError
        If the boundary is empty, or does not overlap the raster at all.
    """
    params = params or ClipParams()
    # A cache cap has to be in force for rasterio.open too, not just the reads:
    # GDAL sizes decisions off the cache from the moment the dataset is opened.
    env = rasterio.Env(GDAL_CACHEMAX=params.gdal_cache_mb)         if params.gdal_cache_mb else _nullcontext()
    with env, rasterio.open(str(raster)) as ds:
        geom = _load_union_crs(geojson, ds.crs, params.buffer_m)
        if geom is None or geom.is_empty:
            raise ValueError(f"{geojson}: no boundary geometry to clip against.")

        window = _read_window(ds, geom, params.crop)
        grid = _output_grid(ds, window, params)
        nodata = _resolve_nodata(ds, params)
        dtype = ds.dtypes[params.band - 1]

        result = ClipResult(
            path=str(out_path) if out_path is not None else None,
            crs=ds.crs, transform=grid.transform,
            width=grid.width, height=grid.height,
            res=(abs(grid.transform.a), abs(grid.transform.e)),
            decimation=grid.decimation, nodata=nodata,
            bounds_m=tuple(windows.bounds(window, ds.transform)),
            total_pixels=grid.width * grid.height)

        profile = dict(ds.profile, driver="GTiff", height=grid.height,
                       width=grid.width, count=1, dtype=dtype,
                       transform=grid.transform, nodata=nodata, **_CREATION)
        sink = rasterio.open(str(out_path), "w", **profile) if out_path else None
        keep = keep_array or out_path is None
        rows = [] if keep else None
        try:
            for row0, row1 in _row_bands(grid, dtype, params.band_bytes):
                arr = _read_band(ds, window, grid, row0, row1, params, dtype)
                mask = geometry_mask(
                    [geom], out_shape=arr.shape,
                    transform=grid.transform * Affine.translation(0, row0),
                    all_touched=params.all_touched)
                arr[mask] = nodata
                _accumulate(result, arr, nodata)
                if sink is not None:
                    sink.write(arr, 1, window=windows.Window(
                        0, row0, grid.width, row1 - row0))
                if keep:
                    rows.append(arr)
        finally:
            if sink is not None:
                sink.close()
        if keep:
            result.data = np.vstack(rows) if len(rows) > 1 else rows[0]

    log.info("Clipped %s: %dx%d @ %.0f m, %.1f%% covered, %s..%s m.",
             Path(str(raster)).name, result.width, result.height, result.res[0],
             result.coverage * 100.0, result.zmin, result.zmax)
    return result


def clip_geotiff_from_dir(raster, geojson_dir, out_dir, pattern="*.geojson",
                          params=None):
    """
    Convenience: clip ``raster`` by every GeoJSON in a directory.

    ``CAN_Newfoundland and Labrador.geojson`` is written as
    ``<out_dir>/CAN_Newfoundland and Labrador.tif``.  A boundary that misses the
    raster entirely is logged and skipped rather than aborting the batch.

    Returns
    -------
    dict of {stem: ClipResult}
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for gj in sorted(Path(geojson_dir).glob(pattern)):
        try:
            results[gj.stem] = clip_geotiff(raster, gj, out_dir / f"{gj.stem}.tif",
                                            params=params)
        except ValueError as exc:
            log.warning("Skipped %s: %s", gj.stem, exc)
    if not results:
        raise ValueError(f"No GeoJSON clipped successfully under {geojson_dir}.")
    return results


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------

def _load_union_crs(geojson, crs, buffer_m):
    """Dissolve the boundary to one geometry in the raster's CRS, buffered."""
    # BaseGeometry, not hasattr("geom_type"): a GeoDataFrame has that attribute
    # too (it returns a Series of type names), so a duck-type check would take a
    # frame down this branch and skip the reprojection it needs.
    if isinstance(geojson, BaseGeometry):      # already a shapely geometry
        geom = geojson
    else:
        frame = geojson if isinstance(geojson, gpd.GeoDataFrame) \
            else gpd.read_file(str(geojson))
        if frame.empty:
            return None
        if frame.crs is not None and crs is not None and frame.crs != crs:
            frame = frame.to_crs(crs)
        geom = unary_union(frame.geometry.values)
    if buffer_m:
        geom = geom.buffer(buffer_m)
    return geom


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

@dataclass
class _Grid:
    """The output raster's shape and affine, and how it maps back to source."""
    width: int
    height: int
    transform: Affine
    decimation: tuple


def _read_window(ds, geom, crop):
    """The source window to read: the polygon's bbox, clipped to the raster."""
    full = windows.Window(0, 0, ds.width, ds.height)
    if not crop:
        return full
    box = windows.from_bounds(*geom.bounds, transform=ds.transform)
    box = box.round_offsets(op="floor").round_lengths(op="ceil")
    try:
        window = windows.intersection([box, full])
    except WindowError:
        raise ValueError(
            f"Boundary (bounds {tuple(round(v) for v in geom.bounds)}) does not "
            f"overlap the raster (bounds {tuple(round(v) for v in ds.bounds)}). "
            "A CRS mismatch is the usual cause.") from None
    if window.width < 1 or window.height < 1:
        raise ValueError("Boundary overlaps the raster by less than one pixel.")
    return window


def _output_grid(ds, window, params):
    """Pick the output shape from the decimation request, and its affine."""
    src_w, src_h = int(window.width), int(window.height)
    factor = 1.0
    if params.target_res_m > 0:
        factor = max(1.0, params.target_res_m / abs(ds.transform.a))
    elif params.max_pixels > 0 and src_w * src_h > params.max_pixels:
        factor = np.sqrt((src_w * src_h) / params.max_pixels)
    width = max(1, int(round(src_w / factor)))
    height = max(1, int(round(src_h / factor)))
    # Derive the affine from the achieved shape, not the requested factor, so the
    # output covers exactly the source window however the rounding landed.
    transform = windows.transform(window, ds.transform) * Affine.scale(
        src_w / width, src_h / height)
    return _Grid(width, height, transform, (src_w / width, src_h / height))



def read_window(ds, geom, crop):
    """The source window a clip of ``geom`` reads (public: callers that must
    agree with the clip on its grid -- a cost preview -- ask this rather
    than redo the arithmetic)."""
    return _read_window(ds, geom, crop)


def output_grid(ds, window, params):
    """The grid a clip of ``window`` with ``params`` produces: ``.width``,
    ``.height`` and ``.transform``.  Public for the same reason."""
    return _output_grid(ds, window, params)


def _row_bands(grid, dtype, band_bytes):
    """Split the output into row bands that each fit the memory budget."""
    per_row = max(1, grid.width * np.dtype(dtype).itemsize)
    rows = max(1, min(grid.height, band_bytes // per_row))
    return [(r, min(r + rows, grid.height)) for r in range(0, grid.height, rows)]


def _read_band(ds, window, grid, row0, row1, params, dtype):
    """Read output rows ``[row0, row1)``, decimating in GDAL rather than after."""
    src_r0 = window.row_off + int(round(row0 * window.height / grid.height))
    src_r1 = window.row_off + int(round(row1 * window.height / grid.height))
    src = windows.Window(window.col_off, src_r0, window.width,
                         max(1, src_r1 - src_r0))
    arr = ds.read(params.band, window=src, out_shape=(row1 - row0, grid.width),
                  resampling=params.resampling)
    return arr.astype(dtype, copy=False)


def _resolve_nodata(ds, params):
    """The sentinel to write outside the polygon."""
    if params.fill_value is not None:
        return float(params.fill_value)
    if ds.nodata is not None:
        return float(ds.nodata)
    dtype = np.dtype(ds.dtypes[params.band - 1])
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
        description="Clip a DEM GeoTIFF to a boundary GeoJSON.")
    ap.add_argument("raster", help="source DEM GeoTIFF")
    ap.add_argument("geojson", help="boundary GeoJSON (any CRS)")
    ap.add_argument("out", help="output GeoTIFF")
    ap.add_argument("--res", type=float, default=0.0, metavar="M",
                    help="decimate to ~M metres per pixel (0 = native)")
    ap.add_argument("--max-pixels", type=int, default=0,
                    help="or: decimate until the output fits this pixel budget")
    ap.add_argument("--buffer", type=float, default=0.0, metavar="M",
                    help="outward buffer on the boundary, in metres")
    ap.add_argument("--all-touched", action="store_true",
                    help="keep pixels the boundary merely touches")
    ap.add_argument("--no-crop", action="store_true",
                    help="keep the raster's full extent instead of cropping")
    ap.add_argument("--gdal-cache", type=int, default=0, metavar="MB",
                    help="cap GDAL's block cache (0 = its default, 5%% of RAM)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = clip_geotiff(args.raster, args.geojson, args.out,
                       ClipParams(target_res_m=args.res,
                                  max_pixels=args.max_pixels,
                                  buffer_m=args.buffer,
                                  all_touched=args.all_touched,
                                  crop=not args.no_crop,
                                  gdal_cache_mb=args.gdal_cache))
    print(f"{res.width}x{res.height} @ {res.res[0]:.0f} m  "
          f"coverage {res.coverage * 100:.1f}%  "
          f"elevation {res.zmin:.0f}..{res.zmax:.0f} m  -> {res.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
