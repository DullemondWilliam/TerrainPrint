# Building a printable terrain solid with `LIB_terrain_stl`

`lib/LIB_terrain_stl.py` turns a prepared DEM GeoTIFF into a watertight STL. It
replaces the DEMto3D QGIS plugin for this project, and adds the one capability
the plugin never had: it cuts the **coastline directly** instead of leaving a
rectangular skirt for `LIB_terrain_trim` to remove afterwards.

## Where it sits

```
Data/JSon-Provinces-Orig/*.geojson
   │  LIB_lake_removal, LIB_island_*, LIB_border_simplification
   ▼
cleaned boundary ───────────────┐
   │                            │
   │  LIB_raster_reproject      │   warp to EPSG:3347, coarsen to print pitch
   │  LIB_geotiff_clip          │   cut to the province
   ▼         (UI_terrain_editor)│
prepared DEM ───────────────────┤
   │                            │
   │  LIB_terrain_stl  ◀────────┘   (UI_terrain_stl)
   ▼
province terrain solid (STL + .frame.json)
```

**Hand it a raster that is already clipped, reprojected and coarsened.** The
sampling grid *is* the raster's grid, so resolution is chosen upstream — which
keeps resampling in the one module that does it nodata-aware.

## A grid mesh, not a triangulation

An earlier design specified Delaunay over masked pixel centres, and recorded
three watertightness bugs it caused: walls built from polygon rings
an unconstrained triangulation did not follow, winding-dependent edge counting,
and pinch faces where a coastline narrows to a one-triangle spit.

This module keeps the DEM's own grid instead — two triangles per cell, a mirrored
base, four walls — so the topology is known analytically and **all three of those
bugs are structurally impossible**. The coastline is then cut by a boolean
against an extruded prism (reusing `LIB_terrain_trim._clip`), which is exact.

Triangle count is therefore exactly

```
4 · (cols−1) · (rows−1)  +  4 · ((cols−1) + (rows−1))
```

which is what `max_triangles` is checked against *before* a pixel is read.

## Defaults live in `project_config.json`

Every piece of one map must be meshed the same way, so the parameters that must
agree are configuration, not call arguments:

```json
"terrain": {
  "exaggeration": 10.0,
  "base_mm": 2.0,
  "floor_m": 0.0,
  "spacing_mm": 0.4,
  "resampling": "max",
  "max_triangles": 3000000
}
```

| Key | Why it is shared |
|---|---|
| `exaggeration` | Pieces at different exaggerations do not meet at the seam |
| `base_mm` | Must exceed `print.dovetail_mm`; also sets where terrain starts |
| `floor_m` | Per-piece floors step the seam by millimetres of exaggerated relief |
| `spacing_mm` | The print pitch; `target_res_m = spacing_mm × print.scale / 1000` |
| `resampling` | How coarsening treats summits — `max` keeps them |
| `max_triangles` | The budget every piece is checked against |

`TerrainParams` reads these as its defaults, and `UI_terrain_editor` seeds its
controls from them, so changing the file changes the whole pipeline at once.
Override per call when you need to; the defaults are what keep a map consistent.

They are deliberately **not** copied into each piece's `.frame.json` — see
[The frame](#the-frame-so-nothing-downstream-has-to-guess). Thirteen copies of a
value that must never differ is thirteen chances for one to.

At the shipped ×10, Newfoundland gets **1.97 mm** of relief on its 2 mm base;
×30 gives **5.92 mm**. Both print; the first is a subtler map.

## The one-window route

`UI_terrain_editor.py` runs the whole thing — reproject, crop, then mesh —
without leaving the window:

```
Canada120m.tif (6.8 GB, EPSG:3979)
  -> stage 1, 3.0 s : 440x343 @ 3200 m, EPSG:3347, z 2..1578 m
  -> stage 2, 1.1 s : 210,304 triangles, 175.5 x 136.8 x 3.97 mm, watertight
```

The two stages invalidate separately on purpose: nudging the exaggeration
re-meshes in 0.9 s and leaves the cropped raster alone, rather than re-warping
7 GB to change a number that never touched the raster.

## Command line

```bash
python -m lib.LIB_terrain_stl <raster.tif> <out.stl> [-b boundary.geojson] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `-b, --boundary` | — | Cut to this coastline; omit for a rectangular slab |
| `--scale` | `8000000` | Map-scale denominator; must match the border corridor |
| `--exaggeration` | `terrain.exaggeration` (10) | Vertical exaggeration |
| `--base MM` | `terrain.base_mm` (2.0) | Solid base under the terrain |
| `--floor M` | `terrain.floor_m` (0.0) | Elevation the base measures from (sea level) |
| `--buffer MM` | `0.0` | Outward lip on the coastline cut |
| `--max-triangles` | `terrain.max_triangles` | Refuse before meshing beyond this |
| `--any-crs` | off | Skip the print-CRS guard |

## Verified run: Newfoundland and Labrador

Input: `Canada120m.tif` warped to EPSG:3347 and coarsened to 3200 m with
`Resampling.max` (440 × 343), then cut to the province. Run at **exaggeration
30** — the shipped default is now 10, which gives 1.97 mm of relief instead.

```
440x343 @ 3200 m, EPSG:3347   ->   210,784 triangles in 0.7 s
extent      175.5 x 136.8 x 7.92 mm
relief      5.92 mm on a 2.0 mm base
watertight  True    volume 22,376 mm3    fits the 256 mm bed
elevation   0 .. 1578 m
frame       origin (7611747, 1889893) m, mpm 8000
```

**Reading the numbers as a correctness check:**

- **175.5 × 136.8 mm** against 175.7 × 137.0 mm computed from the polygon alone.
  The half-millimetre is expected: vertices are *pixel centres*, so the solid is
  one pixel short of the raster's bounding box in each axis.
- **5.92 mm of relief** is 1578 m × 30 ÷ 8000 — the documented arithmetic. At
  true scale (×1) it would be 0.20 mm, which is why exaggeration is not optional.
- **1578 m** survived a 27× coarsening because the warp used `Resampling.max`;
  `bilinear` would have given 1165 m, losing 1.5 mm of mountain.
- **65 % of the grid was removed** by the coastline cut (603,676 → 210,784),
  which is the same proportion of its bounding box that the province occupies.
- The written STL reloads at 210,784 faces, watertight, same volume.

## The guards

Each refuses before doing expensive work, because every one of these failures is
invisible until the piece is printed or assembled:

| Guard | Refuses when |
|---|---|
| Print CRS | The raster is not EPSG:3347. The same province is 5.4 × 5.9 mm different in EPSG:3979 — over three times the 1.6 mm dovetail neck |
| Base vs dovetail | `base_height_mm ≤ dovetail_mm`, which would let the connector pocket cut through the bottom |
| Triangle budget | The grid would exceed `max_triangles` — checked from metadata, before reading |
| Watertight gate | The mesh is empty, open, or of non-positive volume (FR-4.7) |
| Bed fit | Reported, not refused: `bed_fits` against `print.bed_mm` less margins |

## Shared across pieces, or the map will not assemble

`z_exaggeration` and `floor_m` must be **the same for every piece of one map**.
Newfoundland's minimum elevation is −17.7 m and British Columbia's −13 m, so
per-piece floors would step the seam between them by millimetres of exaggerated
relief. `scale` must likewise match the corridor generation.

`clamp_below_floor` (on by default) rests below-sea-level land on the base
instead of letting it punch through. With it off, terrain deeper than
`base_height_mm × mpm ÷ exaggeration` crosses the base plane and the solid
self-intersects — the shell still closes, so the watertight gate passes it. The
module logs a warning when that happens, because no gate here detects it.

## The frame, so nothing downstream has to guess

`LIB_stl_reconcile` exists because DEMto3D writes STLs in a frame nobody
recorded, forcing the keying step to recover it by footprint IoU with a
Y-flip search. This module *builds* the mesh, so it knows the frame and writes a
`.frame.json` sidecar beside the STL.

**The sidecar records only what is true of that one piece:**

```json
{
  "origin_m": [7611747.02, 1889893.21],
  "settings_id": "81a34822faa3"
}
```

`origin_m` is where this province sits in metres — the one value here that
differs between pieces and cannot be read from the config. Paired with the
configured scale it *is* the metres→print-mm affine.

Everything else — exaggeration, floor, base, spacing, scale, CRS — is a property
of the **map**, not the piece. Every province must share them or the pieces do
not meet at the seam, so writing them next to each piece would make thirteen
copies of a value that must never differ, and thirteen chances for one to. They
live in `project_config.json` and are read through one function:

```python
from lib.LIB_terrain_stl import shared_settings, settings_id

shared_settings()   # {'scale': 8000000, 'crs': 'EPSG:3347',
                    #  'exaggeration': 10.0, 'base_mm': 2.0,
                    #  'floor_m': 0.0, 'spacing_mm': 0.4}
settings_id()       # '81a34822faa3'
```

### The one risk config alone does not cover

If the config changes after some pieces are built, those pieces are stale: they
describe a map that no longer exists, and nothing about the STL says so. That is
what `settings_id` is for — a fingerprint of the shared settings, recorded per
piece *instead of* the values:

```python
from lib.LIB_terrain_stl import read_frame, frame_matches_config

frame = read_frame("print/newfoundland.stl")
if not frame_matches_config(frame):
    ...   # built under different settings — rebuild, do not assemble
```

Any change to scale, CRS, exaggeration, base, floor or spacing moves the
fingerprint, so a stale piece is detectable rather than silently mixed into an
assembly it no longer fits. A sidecar written before this split has no
`settings_id` and correctly reports as not matching.

## Tests

```bash
python -m pytest tests/test_terrain_stl.py tests/test_terrain_stl_ui.py
```

39 + 31 tests, ~8 s, on a synthetic EPSG:3347 DEM sized so the numbers are round
(8000 m pixels at 1:8,000,000 are exactly 1 print-mm). Heights are asserted
against hand-computed millimetres rather than a second copy of the formula.

Two notes for anyone extending them. The watertight gate is aimed at a
deliberately broken mesh via `monkeypatch`, because every mesh the other tests
build is watertight and the gate would otherwise never be exercised. And the UI
tests stub `QMessageBox` — a modal dialog blocks forever headlessly, so a test
reaching an error path would hang the suite rather than fail.

**No test reads `Canada120m.tif`.**
