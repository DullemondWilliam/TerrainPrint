# Planning: `LIB_terrain_stl` — DEM GeoTIFF to printable STL

A design plan for the second half of the raster front end: turning a clipped DEM
into a watertight, coastline-accurate province solid.

> **Status: built.** `LIB_terrain_stl` + `UI_terrain_stl` implement this plan;
> see [USING_TERRAIN_STL.md](USING_TERRAIN_STL.md) for the built behaviour and
> the verified Newfoundland run. This document is kept as the reasoning behind
> the design, and §6 records which of the open decisions were taken.

Every number in this document was measured on `Canada120m.tif` and the province
GeoJSON in `Data/`, not estimated — the measurements are labelled where they
appear.

---

## 1. Where it sits

```
Data/JSon-Provinces-Orig/*.geojson
   │  LIB_lake_removal, LIB_island_*, LIB_border_simplification
   ▼
cleaned boundary ──────────────┐
   │                           │
   │  LIB_geotiff_clip         │  (built)
   ▼                           │
clipped DEM (GeoTIFF)          │
   │                           │
   │  LIB_terrain_stl  ◀───────┘  (this plan)
   ▼
province terrain solid (STL)
   │
   │  LIB_butterfly_keys
   ▼
keyed, printable piece
```

It replaces the DEMto3D QGIS plugin for this step. Two consequences beyond
removing a manual GUI stage:

**`LIB_terrain_trim` stops being mandatory.** It exists because DEMto3D can only
print a rectangle, leaving a flat skirt that must be cut off afterwards —
`USING_DEMTO3D.md` §3 calls the coastline cut-out "the one capability the removed
`LIB_terrain_stl` had that the plugin does not". A lib that owns the meshing can
cut the coastline while building. `LIB_terrain_trim` stays useful for slabs from
any external tool; it just stops being a required pipeline step.

**`LIB_stl_reconcile` stops being needed on this path.** It recovers an unknown
metres→print-mm frame by footprint IoU, with a Y-flip search, because DEMto3D
writes STLs in a frame nobody recorded. A lib that builds the mesh *knows* that
frame — so it should return it (§4.5) rather than leave the next stage to
re-derive it. Guessing a frame we could have written down is the kind of work
worth designing out.

---

## 2. The core decision: grid mesh, not Delaunay

`REQUIREMENTS_AND_DESIGN.md` §5.6 specifies Delaunay triangulation over masked
pixel centres, and records three watertightness bugs it caused — walls built from
polygon rings the triangulation did not follow, winding-dependent edge counting,
and pinch faces where a coastline narrows to a one-triangle spit.

**Recommendation: build the top surface on the DEM's own regular grid (two
triangles per cell) and cut the coastline with a manifold boolean.** A DEM is
already a grid; Delaunay re-derives a topology that was given. With the grid kept,
the mesh topology is known analytically, so all three of those bugs are
structurally impossible — there is no unconstrained triangulation to disagree with
the rings, no winding to misread, and no scattered points to pinch.

I spiked this against the real Newfoundland clip to check the claim rather than
assert it:

| Stage | Triangles | Watertight | Time |
|---|---|---|---|
| Grid slab (453 × 328 cells, top + base + 4 walls) | 594,332 | **yes** | < 0.1 s |
| After manifold boolean against the coastline prism | 210,600 | **yes** | 0.7 s |

Result: 181 × 131 × 7.1 mm — the predicted print size at 1:8,000,000, with 2 mm
of base under 5.1 mm of relief.

| | Grid + boolean | Delaunay (§5.6) |
|---|---|---|
| Watertightness | By construction | Needs perimeter-walling, undirected edge counting, pinch removal |
| Coastline | Exact (boolean against the polygon) | Follows kept triangles; exactness depends on the mask |
| Triangles | ~2× for equal fidelity — no adaptation to flat ground | Fewer on flat terrain |
| Reuses | `LIB_terrain_trim._clip`, `_extrude_man`, `_man_to_trimesh` | Nothing existing |
| Known failure modes | Boolean cost on huge meshes | Three, all documented, all recurring |

The triangle-count penalty is the honest cost. It is controlled by `spacing_mm`
(§4.1), and if it ever matters, decimation after meshing is a smaller and
better-understood problem than the three bugs above.

---

## 3. Hard constraints

These are the ways the output silently fails to fit its neighbours. Each is worth
a guard in the lib, not just a line in a docstring.

### 3.1 The CRS must be the print CRS — measured

The DEM is EPSG:3979; the border corridor and dovetails are built in EPSG:3347.
The same province, at 1:8,000,000:

| CRS | Print size | Area |
|---|---|---|
| EPSG:3347 (print) | 175.7 × 137.0 mm | 394,043 km² |
| EPSG:3979 (DEM) | 181.1 × 131.1 mm | 394,042 km² |

A **5.4 × 5.9 mm** disagreement. The dovetail neck is 1.6 mm and the fit
clearance 0.2 mm, so a CRS mismatch is over three times the feature that is
supposed to hold the pieces together. Terrain meshed in the DEM's native CRS
cannot key into a border built in the print CRS.

> **Closed.** `LIB_geotiff_clip` reprojects the *polygon into the raster's CRS*,
> so on its own its output stays EPSG:3979. `LIB_raster_reproject`
> ([doc](USING_RASTER_REPROJECT.md)) now supplies the warp, and
> `UI_terrain_editor.py` chains the two. Driven end to end the
> harness reproduces the disagreement above: 176.0 × 137.2 mm warped vs
> 181.1 × 131.1 mm unwarped.

### 3.2 Base must clear the dovetail

`base_height_mm` must exceed `dovetail_mm`, or the pocket cuts through the
bottom of the piece. `LIB_terrain_trim` already states this; the defaults (2.0 mm
base, 1.0 mm pocket, both from `project_config.json`) satisfy it with 1 mm to
spare. The lib should refuse the combination rather than print a hollow base.

### 3.3 Floor and exaggeration must be shared across pieces

If each province takes its own minimum elevation as the floor, two provinces
meeting at a seam sit at different heights — Newfoundland's minimum is −17.7 m,
British Columbia's −13 m, so the seam would step by several millimetres of
exaggerated relief. The same applies to `z_exaggeration`.

Both must be decided once for a whole map and passed to every piece. That argues
for them living in `project_config.json` next to `print.scale`, not being
per-call defaults.

### 3.4 Scale must match corridor generation

Already flagged in the design doc: "Scale must match between corridor generation
and meshing, or tabs will not fit." Default from `project_config.json`
`print.scale`.

---

## 4. Variables

### 4.1 Sampling and scale

| Variable | Default | Provenance | What breaks if wrong |
|---|---|---|---|
| `scale` | `8_000_000` | `project_config.json` `print.scale` | Tabs do not fit (§3.4) |
| `spacing_mm` | `0.4` | DEMto3D "Spacing (mm)" | Triangle count; surface fidelity |
| `build_crs` | `EPSG:3347` | `project_config.json` `crs.working` | Pieces do not key (§3.1) |

`spacing_mm` is the one knob a user actually turns, and it is the bridge to the
clip lib:

```
target_res_m = spacing_mm × scale / 1000
```

At 1:8,000,000 that makes `LIB_geotiff_clip`'s existing `--res 3200` default
exactly 0.4 mm spacing. Measured cost for Newfoundland:

| `spacing_mm` | m/px | Grid | Top triangles |
|---|---|---|---|
| 1.0 | 8000 | 181 × 131 | 47 k |
| 0.5 | 4000 | 362 × 262 | 190 k |
| **0.4** | 3200 | 453 × 328 | 297 k |
| 0.2 | 1600 | 905 × 655 | 1187 k |

### 4.2 Vertical

| Variable | Default | Provenance | Notes |
|---|---|---|---|
| `z_exaggeration` | `30.0` | `USING_DEMTO3D.md` §4.5 (10–30) | At ×1 Newfoundland's 1578 m is **0.20 mm** of relief — invisible |
| `base_height_mm` | `2.0` | `project_config.json` | Must exceed `dovetail_mm` (§3.2) |
| `floor_m` | `0.0` (sea level) | new | Shared across pieces (§3.3) |
| `clamp_below_floor` | `True` | new | Below-sea-level land exists: −17.7 m (NL), −13 m (BC) |

### 4.3 The cut

| Variable | Default | Provenance | Notes |
|---|---|---|---|
| `boundary` | required | — | Cleaned province GeoJSON, any CRS |
| `cut_to_coastline` | `True` | — | `False` gives the DEMto3D-style rectangular slab |
| `buffer_mm` | `0.0` | `LIB_terrain_trim` | Outward lip on the coastline |

### 4.4 Output and quality gates

| Variable | Default | Provenance | Notes |
|---|---|---|---|
| `out_path` | `None` | `LIB_geotiff_clip` convention | Omit to return the mesh only |
| `max_triangles` | `1_500_000` | The old clip UI's face-count readout | Fail *before* meshing, from the grid size |
| `min_part_area_mm2` | `0.5` | `LIB_border_connectors.MIN_PART_AREA_MM2` | That constant's comment already says it "matches the island filter in `LIB_terrain_stl`" |
| `validate` | `True` | FR-4.7 | Watertight, positive volume, or raise |

### 4.5 What the result must carry

This is what removes the reconciliation step (§1), so it matters as much as the
inputs:

```python
@dataclass
class TerrainResult:
    mesh            # watertight trimesh
    origin_m        # metric origin the piece was built at
    mpm             # metres per print-mm (scale / 1000)
    floor_m         # datum the base measures from
    z_exaggeration
    extent_mm       # (w, h, z) as printed
    bed_fits        # against print.bed_mm minus margin
    triangles
    volume_mm3
    watertight
```

`origin_m` + `mpm` *is* the metres→print-mm affine. Written to a sidecar JSON
next to the STL, it lets `LIB_butterfly_keys` skip the IoU search entirely and
removes the Y-flip ambiguity that search exists to resolve.

---

## 5. Preprocessing

Ordered by when it has to happen. Items 1, 3 and 4 are already built.

### 5.1 Clean the boundary first — built

`LIB_lake_removal`, `LIB_island_removal` / `LIB_island_merging`,
`LIB_border_simplification`. Islands under `min_part_area_mm2` triangulate into
non-manifold slivers and are too small to print; removing them upstream is
cheaper than repairing the mesh downstream. Filling lakes also removes interior
rings that the boolean cutter would otherwise have to carve.

### 5.2 Reproject the DEM to the print CRS — **built**

Per §3.1. Two ways:

1. **Add a `dst_crs` to `LIB_geotiff_clip`** (`rasterio.warp.reproject` into the
   output grid). The clip already computes the output grid; warping is a change
   of which grid, not new machinery. Preferred: one pass over the data, and it
   makes the clip lib the single place raster CRS is handled.
2. Warp the whole 7.3 GB DEM to EPSG:3347 once, offline, with `gdalwarp`. Simple
   and a one-time cost, but it produces a second 7 GB file and leaves the clip
   lib silently CRS-dependent.

**Built as (3):** a separate `LIB_raster_reproject`, warping through a
`WarpedVRT` over the boundary's bounds. Keeping it out of `LIB_geotiff_clip`
leaves each lib single-purpose, and the pipeline composes as
*reproject -> clip* (`UI_terrain_editor.py` drives both). The
`bounds` parameter is what makes it viable: a whole-DEM warp would be
47831 x 42126 -- 8 GB.

### 5.3 Clip and decimate — built

`LIB_geotiff_clip` with `target_res_m = spacing_mm × scale / 1000`. This is what
turns an unbounded 7.3 GB input into a known, small array; the mesher should
require an already-clipped raster rather than re-implement windowing.

### 5.4 Interior nodata — measured, no fill needed

A mesher must decide what to do with nodata *inside* the boundary. Measured
across five provinces at 3200 m, comparing valid pixels against polygon area:

| Province | Grid | Coverage |
|---|---|---|
| Newfoundland and Labrador | 453 × 328 | 100.0 % |
| Manitoba | 258 × 383 | 100.0 % |
| Ontario | 510 × 500 | 100.0 % |
| Nova Scotia | 152 × 174 | 100.1 % |
| Prince Edward Island | 56 × 27 | 100.7 % |
| British Columbia | 318 × 560 | 100.0 % |

No interior holes anywhere, including the big-lake provinces — lakes carry
elevations rather than nodata. **So no hole-filling is needed.** Keep a guard
anyway (the design doc lists "terrain meshing requires the DEM to cover the
region" as a known limitation), but as a check that raises, not an algorithm.

The slight overshoot above 100 % is pixels straddling the boundary, as expected.

### 5.5 Decide the floor before meshing anything

Per §3.3. A `floor_m` in `project_config.json`, or a first pass over all pieces
to find a shared minimum.

### 5.6 Bed-fit check before meshing, not after

Print extent comes from the boundary's bounds and the scale — arithmetic, no
raster work. Newfoundland is 175.7 × 137.0 mm against a 256 mm bed, so it fits
whole; Quebec and Ontario will not, and must go through `core.meshing`'s grid
split. Checking first turns a wasted multi-second mesh into an instant answer.

### 5.7 Resampling: nearest loses summits — measured

`LIB_geotiff_clip` decimates nearest-neighbour by design (averaging would drag
the −32767 nodata sentinel into coastal pixels). The cost, measured on
Newfoundland against Mount Caubvick's true 1652 m:

| Sampling | Peak |
|---|---|
| Native 120 m | 1578 m |
| 3200 m nearest | 1363 m |

289 m of summit lost — at ×30 exaggeration, 1.1 mm of print.

**Built.** `downsample_geotiff` in `LIB_raster_reproject` coarsens through the
warper, which *is* nodata-aware, so `Resampling.max` is available: it returns
**1577.6 m** at the same 3200 m grid — the native-resolution answer from 1/700th
of the pixels. The mesher should coarsen with that rather than with
`LIB_geotiff_clip`'s nearest-only `target_res_m`.

---

## 6. Open decisions

Recorded as taken — the defaults below are what shipped; each remains a
constructor argument.

1. **CRS:** EPSG:3347, enforced by a guard that refuses any other raster.
2. **Floor:** sea level (`floor_m=0.0`) with `clamp_below_floor=True`.
3. **Rectangular slab mode:** kept — omit the boundary.
4. **Frame:** a `.frame.json` sidecar beside the STL.

The original framing follows.

1. **Which CRS wins?** Recommend warping the DEM to EPSG:3347 (§5.2), because
   the border and dovetail geometry is already built there. The alternative —
   moving the whole pipeline to EPSG:3979 — is defensible but touches far more
   existing, tested code.
2. **Floor policy:** sea level (0 m) with below-sea-level land clamped, or the
   true minimum across all printed pieces? Sea level is simpler to reason about
   and makes pieces independent.
3. **Keep the rectangular-slab mode?** `cut_to_coastline=False` reproduces
   DEMto3D's output, which is useful for comparing old and new pieces during the
   transition, and costs almost nothing to keep.
4. **Sidecar frame file:** JSON next to the STL, or extend `project_config`? The
   sidecar keeps a piece self-describing when it moves between machines.

---

## 7. Build order

Each step ends somewhere testable, in the project's existing style — synthetic
DEMs in `tmp_path`, no test touching `Canada120m.tif`.

| # | Step | Test focus |
|---|---|---|
| 1 | ~~`dst_crs` warp~~ — **done**: `LIB_raster_reproject` (30 tests) | A known point lands where `pyproj` says; nodata does not bleed; `max` resampling recovers summits |
| 2 | ~~Grid slab~~ — **done** | Watertight, positive volume, exact triangle-count formula, heights against hand-computed mm |
| 3 | ~~Coastline boolean~~ — **done**, reusing `_clip` | Watertight after the cut; footprint follows the polygon |
| 4 | ~~Frame + sidecar~~ — **done** | `origin_m`/`mpm` reproduce the polygon's print-mm footprint; written to `.frame.json` |
| 5 | ~~Gates~~ — **done** | Print-CRS, base-vs-dovetail, `max_triangles`, watertight; each refuses before expensive work |
| 6 | ~~`harnesses/UI_terrain_stl.py`~~ — **done** | vispy for the mesh panel per §11.D |

Steps 2 and 3 are the ones already de-risked by the spike in §2.
