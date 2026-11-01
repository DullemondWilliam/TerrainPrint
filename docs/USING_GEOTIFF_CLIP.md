# Clipping the DEM to a province with `LIB_geotiff_clip`

`lib/LIB_geotiff_clip.py` intersects the elevation raster with one boundary
polygon: everything inside the coastline keeps its elevation, everything outside
becomes nodata. It is the raster-side counterpart to `LIB_terrain_trim`, which
does the equivalent cut on a *mesh* after DEMto3D has run.

Both cuts exist because they happen at different stages:

| Step | Module | Cuts | Why |
|---|---|---|---|
| Before meshing | `LIB_geotiff_clip` | the **raster** | DEMto3D meshes a whole rectangle; clipping first means it is not meshing four neighbouring provinces |
| After meshing | `LIB_terrain_trim` | the **mesh** | DEMto3D's print area is rectangular regardless, so the flat nodata skirt still has to come off the STL |

Clipping first does not make `LIB_terrain_trim` unnecessary — see
[USING_DEMTO3D.md](USING_DEMTO3D.md) §3. It makes the DEMto3D step smaller and
faster, and gives you the elevation range you need to set DEMto3D's height
parameters.

## Command line

```bash
python -m lib.LIB_geotiff_clip <raster.tif> <boundary.geojson> <out.tif> [options]
```

| Option | Meaning |
|---|---|
| `--res M` | Decimate to ~M metres per pixel (`0` = native resolution) |
| `--max-pixels N` | Or: decimate until the output fits N pixels |
| `--buffer M` | Grow the boundary outward M metres before cutting (keeps a coastal lip) |
| `--all-touched` | Keep pixels the boundary merely touches, not only those it covers |
| `--no-crop` | Keep the raster's full extent instead of cropping to the boundary bbox |
| `--gdal-cache MB` | Cap GDAL's block cache (see [Memory](#memory)) |

## Library

```python
from lib.LIB_geotiff_clip import clip_geotiff, ClipParams

res = clip_geotiff("Data/Canada120m-Orig/Canada120m.tif",
                   "Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson",
                   out_path="Data/Clips/NL.tif",
                   params=ClipParams(target_res_m=3200.0))
print(res.width, res.height, res.coverage, res.zmin, res.zmax)
```

`clip_geotiff_from_dir(raster, geojson_dir, out_dir)` clips one raster by every
GeoJSON in a directory, skipping (with a warning) any boundary that misses the
raster entirely.

> **Reprojection lives next door.** This lib reprojects the *boundary into the
> raster's CRS*; it does not warp the raster. To get output in the print CRS
> (EPSG:3347) rather than the DEM's own (EPSG:3979), run
> [`LIB_raster_reproject`](USING_RASTER_REPROJECT.md) first — the difference is
> 5.4 x 5.9 mm of print on Newfoundland, against a 1.6 mm dovetail neck.
> `UI_terrain_editor.py` chains the two.

The boundary may be a path, a `GeoDataFrame`, or a bare shapely geometry, and is
**reprojected to the raster's CRS automatically** — the province GeoJSON is
EPSG:4326 and `Canada120m.tif` is EPSG:3979, so without that step the read
window would come out empty.

## Choosing a resolution

At the project's 1:8,000,000 scale, 1 print-mm is 8 km, so a native 120 m pixel
is 0.015 mm — far finer than any printer resolves. A reasonable rule:

```
target_res_m = scale x nozzle_mm / 1000       # 1:8M, 0.4 mm nozzle -> 3200 m
```

Decimation uses nearest-neighbour by default and that is deliberate: GDAL's
`average` resampling does **not** skip nodata on the direct RasterIO path, so
averaging a `-32767` sentinel against real terrain would carve a deep negative
trench along every coastline.

The trade-off is that nearest-neighbour sampling misses peaks — see the numbers
below.

## Verified run: Newfoundland and Labrador

Source: `Data/Canada120m-Orig/Canada120m.tif` — 45921x39913 float32, 7.3 GB,
EPSG:3979, striped (one scanline per block), uncompressed, **no overviews**.
Boundary: `Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson`.

```bash
python -m lib.LIB_geotiff_clip \
  "Data/Canada120m-Orig/Canada120m.tif" \
  "Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson" \
  "Data/Clips/NL_3200m.tif" --res 3200
```

| Run | Output grid | Coverage | Elevation range | Time | File |
|---|---|---|---|---|---|
| `--res 3200` | 453 x 328 @ 3198 m | 26.0 % | -2 .. 1363 m | 1.0 s | 146 KB |
| native | 12072 x 8737 @ 120 m | 25.9 % | -18 .. 1578 m | 7.9 s | 77 MB |

Both outputs carry identical bounds
(`1561830, 582450, 3010470, 1630890` in EPSG:3979), so decimating changes the
grid without moving the ground it covers.

**Reading the numbers as a correctness check:**

- **26 % coverage** is right, not a bug. Newfoundland and Labrador is an L
  shape — Labrador in the northwest, the island in the southeast — so most of
  its bounding box is the Gulf of St. Lawrence and the Labrador Sea.
- **1578 m** at native resolution is close to Mount Caubvick in the Torngat
  Mountains (1652 m); 120 m cells average the summit away.
- **1363 m** at 3200 m is the same mountain sampled by one nearest-neighbour
  pixel in 27. If you need peak elevations preserved, coarsen with
  [`downsample_geotiff(..., Resampling.max)`](USING_RASTER_REPROJECT.md) instead
  of this lib's `target_res_m` — it returns 1577.6 m at the same 3200 m grid,
  matching the native-resolution answer.
- Coverage barely moves between the two (26.0 % vs 25.9 %), which is what
  confirms the decimated cut still follows the same coastline.

## Memory

`band_bytes` bounds only this module's own buffer — the clipped array is written
in row bands and never held whole. GDAL keeps a **second, larger pool**: its
block cache, defaulting to 5 % of system RAM. Because the source is striped,
every windowed read pulls whole 45921-pixel scanlines into that cache, so it
fills quickly and dominates the total.

Measured on the native Labrador clip above (402 MB if read whole),
`band_bytes = 64 MB`:

| `gdal_cache_mb` | Peak working set | Time |
|---|---|---|
| `0` (GDAL default) | 1891 MB | 6.1 s |
| `512` | 256 MB | 7.0 s |
| `32` | 256 MB | 7.0 s |

These historical measurements predate the cache-unit fix: the values `512`
and `32` were passed to Rasterio as **bytes**, not megabytes. The timings are
preserved here as historical evidence; they do not establish a 512 MB memory
floor. Rerun the native DEM benchmark before comparing corrected cache limits.

`gdal_cache_mb` now sets the cache in MiB (1,048,576 bytes per unit). Zero keeps
the existing GDAL limit, and the previous limit is restored after each call,
including failures. When an array is requested, banded reads fill one output
array directly, avoiding a second full-size array during concatenation.

## Tests

```bash
python -m pytest tests/test_geotiff_clip.py
```

31 tests, ~2 s. They synthesise a small EPSG:3979 DEM in `tmp_path` whose
elevation equals its column index, so which pixels survived a clip is checkable
from the values themselves. **No test reads `Canada120m.tif`** — a 7.3 GB file is
not a unit-test fixture, which is why the real run is recorded above instead.

One note for anyone extending them: boundaries in the streaming tests are
deliberately *wedges*, not boxes. A box fills its own bounding box, so its mask
is identical on every row and a row band that forgot its offset would still pass.
A wedge's mask changes row by row, which is what makes the banding testable.
