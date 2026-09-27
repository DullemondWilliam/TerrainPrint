# Warping the DEM into the print CRS with `LIB_raster_reproject`

`lib/LIB_raster_reproject.py` warps a GeoTIFF into another CRS, over a chosen
region and resolution. It exists because the elevation raster and the print
pipeline do not share a projection.

## Why this is not optional

`Canada120m.tif` is **EPSG:3979** (Canada Atlas Lambert). Every border corridor,
dovetail and province polygon in this project is built in **EPSG:3347**
(Statistics Canada Lambert). Those are different projections, not different
units, so the same province is a different shape in each:

| Newfoundland and Labrador | Print size @ 1:8,000,000 | Area |
|---|---|---|
| EPSG:3347 (print) | 175.7 × 137.0 mm | 394,043 km² |
| EPSG:3979 (DEM) | 181.1 × 131.1 mm | 394,042 km² |

A **5.4 × 5.9 mm** disagreement, against a 1.6 mm dovetail neck and 0.2 mm of fit
clearance — over three times the feature meant to hold the pieces together.
Terrain built from the DEM's own CRS cannot key into a border built in the print
CRS.

Driving the two libs through `UI_terrain_editor.py` reproduces it end to end:
176.0 × 137.2 mm warped vs 181.1 × 131.1 mm unwarped, **5.1 × 6.1 mm**
of print.

## Always warp a region, never the file

A whole-DEM warp from EPSG:3979 to EPSG:3347 is a **47831 × 42126** grid — 8 GB —
to extract a province that occupies a few hundred pixels of it. So `bounds`
(in the *destination* CRS) is the parameter that matters, and `bounds_for()`
turns a GeoJSON into it:

```python
from lib.LIB_raster_reproject import reproject_geotiff, ReprojectParams, bounds_for

province = "Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson"
res = reproject_geotiff("Data/Canada120m-Orig/Canada120m.tif", "EPSG:3347",
                        out_path="Data/Clips/NL_3347.tif",
                        params=ReprojectParams(
                            dst_res_m=3200.0,
                            bounds=bounds_for(province, "EPSG:3347")))
```

Internally the warp runs through a `WarpedVRT` and is read in row bands, so
nothing the size of the full warp is materialised.

## Order of operations: warp, then clip

```
reproject (to the print CRS, over the province's bounds)  ->  clip (to the coastline)
```

Warping first means the coastline cut happens **in the frame that gets printed**,
at the resolution that gets printed, so the cut is exact there rather than being
a source-CRS cut dragged through a warp afterwards.

Because the warp is given the boundary's bounds, it *is* the bounding-box crop —
so the clip that follows only has to apply the polygon mask
(`ClipParams(crop=False, target_res_m=0)`). That is also why the final grid
equals the warped grid exactly.

## Reducing resolution without reprojecting

Coarsening terrain is the same operation minus the projection change, so it runs
through the same warper:

```python
from lib.LIB_raster_reproject import downsample_geotiff, ReprojectParams
from rasterio.enums import Resampling

res = downsample_geotiff("Data/Clips/NL_native.tif", 3200.0,
                         out_path="Data/Clips/NL_3200m.tif",
                         params=ReprojectParams(resampling=Resampling.max))
print(res.reduction)        # 26.7 -> each output pixel covers a 27x27 block
```

That is not tidiness — it is the only path that resamples **nodata-aware**, which
is what makes `average` and `max` usable. Measured on Labrador, 120 m -> 3200 m
in EPSG:3979 (no CRS change), against Mount Caubvick's 1652 m:

| Path | Peak | Time |
|---|---|---|
| `downsample_geotiff`, `max` | **1577.6 m** | 0.7 s |
| `downsample_geotiff`, `nearest` | 1513.2 m | 0.4 s |
| `downsample_geotiff`, `average` | 1241.0 m | 0.8 s |
| `LIB_geotiff_clip` `target_res_m` (nearest only) | 1363.2 m | 0.2 s |

`max` at 27x reduction returns **the same peak as the full native-resolution
raster**, from 1/700th of the pixels.

`LIB_geotiff_clip`'s own decimation stays the cheap path and is right when peaks
do not matter. `downsample_geotiff` **never upsamples**: a request finer than the
source is clamped to native rather than inventing detail.

## Command line

```bash
python -m lib.LIB_raster_reproject <raster.tif> <dst_crs> <out.tif> [options]
```

| Option | Meaning |
|---|---|
| `--res M` | Destination metres per pixel (`0` = keep the source's ground sampling) |
| `--bounds-from GEOJSON` | Warp only this boundary's extent — **strongly advised** |
| `--bounds-buffer M` | Pad that extent by M metres |
| `--resampling {average,max,nearest,bilinear,cubic}` | See below |
| `--gdal-cache MB` | Cap GDAL's block cache |

## Resampling: why the default is `average`

`LIB_geotiff_clip` must decimate nearest-neighbour, because GDAL's RasterIO
decimation path does not skip nodata. The **warper is a different code path**:
given `src_nodata` it carries a mask and excludes those pixels, so every method
here is nodata-safe (pinned by `tests/test_raster_reproject.py`).

That frees the choice to be about *fidelity* — and the usual instinct, bilinear,
is wrong whenever the warp also downsamples. Bilinear reads a 2×2 source
neighbourhood per destination pixel, so going 120 m → 3200 m it samples 4 of
every 729 source pixels and smooths them. Measured on Labrador warping
**EPSG:3979 → EPSG:3347 and coarsening 120 m → 3200 m in one pass**, against
Mount Caubvick's true **1652 m** (the native-resolution clip reaches 1578 m):

| Resampling | Peak | Notes |
|---|---|---|
| `max` | **1577.6 m** | Matches native; biases the whole surface up |
| `nearest` | 1428.3 m | Point sample, no smoothing |
| `average` | 1264.6 m | The statistically correct downsample — **default** |
| `cubic` | 1230.9 m | Overshoots into −29 m troughs (ringing) |
| `bilinear` | 1165.1 m | Worst here, despite being the usual default |

Use **`max`** when summits are the point. Use `bilinear` only when the warp is
not also a large downsample.

(The table above reprojects *and* coarsens in one pass, so it does not match the
pure-downsample table earlier — different operations, different numbers. `max`
lands on 1577.6 m either way.)

The ceiling either way is set by warping straight to print resolution: warping at
native resolution and decimating afterwards preserves more (**1570.8 m**) at the
cost of a far larger intermediate.

## Tests

```bash
python -m pytest tests/test_raster_reproject.py
```

40 tests, ~4 s, on a synthetic EPSG:3979 raster warped to EPSG:3347. The central
one projects a known pixel centre with `pyproj` and asserts the warped raster
reads the right value there — a warp that silently did nothing would otherwise
pass most of the suite, so there is an explicit test that the shape *changes*.

**No test reads `Canada120m.tif`.** A full warp of it would be 8 GB, which is
exactly why `bounds` exists and is itself tested.
