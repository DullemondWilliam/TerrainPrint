# Performance changes and findings

Review date: October 31, 2026 (America/New_York).
Baseline: `f501cb40afde40f73c61b1bbb0953fed684e9e71` from
[DullemondWilliam/TerrainPrint](https://github.com/DullemondWilliam/TerrainPrint).
These performance changes are proposed for review, with no claim of research
novelty. The pull request records their submission history; deferred fixes
remain follow-up work.

## Measured changes

Windows AMD64, Python 3.10.1, NumPy 1.24.3, Shapely 2.0.2,
Rasterio 1.3.9 and trimesh 4.0.1, using the repository's requirements.
Times are medians after checking baseline and optimized outputs. They describe
the named operations, not the entire terrain-to-STL pipeline. Normal host
activity can affect timings.

| Operation | Before | After | Measurement |
|---|---:|---:|---|
| Merge 400 synthetic islands | 0.935 s | 0.328 s | 3 runs; 2.85x faster |
| Stress overlay, 10.656M pixels and 20 overlapping regions | 1.326 s | 0.0317 s | 3 runs; 41.8x faster |
| Full stress analysis of shipped Nova Scotia geometry | 0.564 s | 0.397 s | 3 runs; 1.42x faster |
| Construct a 2,999,820-triangle terrain slab | 0.1139 s | 0.0967 s | 7 alternating runs; 15% less time |
| Clip a 4096x4096 raster, retaining the array | 0.228 s | 0.207 s | 5 alternating runs; 1 MiB bands |
| Same-CRS warp of that raster, retaining the array | 0.576 s | 0.539 s | 5 alternating runs; 1 MiB bands |
| 40 grid estimates with an explicit resolution | 2.577 s | 1.266 s | 5 alternating runs |
| `bounds_for` on preloaded, projected Nunavut geometry | 0.296 s | 0.0108 s | 5 alternating runs; includes CRS handling |

Peak traced Python/NumPy allocations fell from **189.6 to 120.9 MiB** for
the large slab, **172.8 to 40.7 MiB** for the stress overlay, and approximately
**128 to 66 MiB** for the retained raster operations. These are `tracemalloc`
peaks, excluding input setup and imports; they do not measure total process
memory or GDAL/GEOS native allocations.

## Complete implemented inventory

All nine findings below are implemented in this change and covered by output
checks or regression tests.

1. **Island intersection search.** `LIB_island_merging.merge_islands` now uses
   an STRtree for candidate neck intersections. Exact `intersects` predicates
   and original island identity are preserved, including multipart inputs.
   Synthetic 40- and 400-island outputs match the baseline with zero tolerance.
2. **Stress overlay rasterization.** `LIB_stress_points.severity_field` burns
   regions once in severity order instead of allocating full masks per neck.
   Pixels match for overlapping, reversed, clipped, empty and holed regions,
   and negative, infinite and NaN severities.
3. **Stress tile descent.** `_summarise_tile` visits only width bands where
   pixels appear. Exact lineage, mass, edge, border and geometry comparisons
   cover 100 seeded random tiles and full Nova Scotia and Nunavut reports.
   A regression checks the width and arriving mass at a join.
4. **Mesh construction allocations.** `LIB_terrain_stl` fills final vertex
   and face arrays directly. Vertices, faces, ordering, winding and metadata
   match on five random grids, including narrow grids and nodata. Tests also
   check analytic volume, watertightness and triangle counts.
5. **Raster cache units.** Clip and warp now convert `gdal_cache_mb` to bytes
   before calling Rasterio. Tests observe the active limit while opening the
   raster and its restoration after success and failure, including zero.
   [Rasterio's API uses bytes](https://rasterio.readthedocs.io/en/latest/topics/switch.html).
6. **Retained raster assembly.** Banded reads fill one result array instead
   of retaining every band and stacking a second full array. Single-band
   reads reuse their array; file-only streaming retains no output array.
   Pixel, statistics, transform and disk roundtrip checks cover both paths.
7. **Raster statistics.** Each band filters valid elevations once for both
   extrema, removing a duplicate Boolean gather. Nodata behavior is unchanged.
   Its gain is included in the raster measurements, not measured separately.
8. **Boundary extents.** `bounds_for` uses Shapely's total bounds instead of
   unioning geometries just to find their envelope. Bounds match for all 13
   shipped Canadian provinces, mixed empty/null geometries and buffered
   overlapping/disjoint inputs. The public-function timing above includes CRS
   checks; an isolated bounds kernel is much faster still, but does not measure
   the public operation.
9. **Grid estimation.** `_output_grid` calculates a default transform only
   when the caller needs a default resolution. Grid definitions are identical;
   this removes repeated unused work during triangle-budget estimation.

## Verification and reproduction

The large national DEM is not included in the repository. Raster benchmarks
use a generated float32 elevation ramp with nodata. Stress benchmarks use
shipped province boundaries in EPSG:3347. Mesh benchmarks isolate slab
construction, excluding clipping, booleans, simplification and STL writing.
No whole-pipeline speedup is claimed.

All **1,979 unique tests were verified** through a full run and targeted
reruns. The initial full run had 1,967 passes and 12 failures: two temporary
directory assertions and ten tests blocked by the fixture's Boolean backend.
After the test-harness repairs below, both affected files passed all 51 tests
in 4.54 seconds. There are no remaining failures from that run. The full suite
was not rerun after these test-only repairs; its original log and the targeted
rerun log are retained as evidence.

Focused suites also passed: 62 mesh tests, 83 geometry/stress tests and 94
initial raster tests. Dependency consistency (`pip check`), benchmark syntax
checks and `git diff --check` passed.

Two test-harness findings were repaired during validation. Settings sandbox
checks now verify that files live inside the actual `tmp_path`, rather than
requiring the directory name to contain `tmp` or `Temp`. Terrain-trim fixtures
use the project's required Manifold library directly instead of a trimesh
Boolean backend requiring an additional Blender/OpenSCAD installation. The ten
trim failures were reproduced on the untouched baseline before this repair.
Both fixes change only test setup/assertions, not production behavior.

Create an untouched baseline checkout at the revision above, then run from
the optimized checkout with the installed Python 3.10 environment:

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp=../tmp-terrainprint-tests
.venv/Scripts/python.exe benchmarks/benchmark_mesh.py --baseline-dir ../TerrainPrint-baseline
.venv/Scripts/python.exe benchmarks/benchmark_raster.py --baseline-dir ../TerrainPrint-baseline
.venv/Scripts/python.exe benchmarks/benchmark_geometry.py --baseline-dir ../TerrainPrint-baseline
```

Set `QT_QPA_PLATFORM=offscreen` and `MPLBACKEND=Agg` for headless UI tests.
Benchmark scripts accept `--repeats`; the geometry script optionally accepts
`--include-nunavut-merge`. The real Nunavut merge comparison was stopped
because the baseline was taking several minutes; no performance or equality
claim is made for that interrupted case.

The cache correction makes the existing editor request of 6144 MiB an actual
6 GiB ceiling instead of 6144 bytes. Real cache use can therefore be higher
than with the bug. Historical native-DEM memory measurements are preserved
and corrected in `USING_GEOTIFF_CLIP.md`; they need remeasurement against
the corrected cache units.

## Findings left out and why

This reconciles the raster, geometry and mesh reviews and pre-existing
repository evidence. Every known deferred finding from this optimization
pass is listed below. These findings are recorded for follow-up; their fixes
are not included. The tile-size issue was already documented upstream.
Source inspection alone is not treated as a measured speedup.

| Finding | Evidence and verification | Reason deferred / remaining work |
|---|---|---|
| **Terrain sampler height mismatch** | Verified: for `[[0,0],[0,8000]]` elevations, scale 1:8M, exaggeration 10 and base 2 mm, the NE/SW mesh diagonal midpoint is 2.0 mm high but the bilinear sampler returns 4.5 mm. | Changing interpolation affects printable border placement. Establish intended behavior and test draping against non-planar terrain before changing it. |
| **Stress results depend on tile size** | Pre-existing module documentation records BC counts 5/4/4/4, Newfoundland 2/2/3/3 and Nunavut 14/13/12/12 for different tile sizes, plus a lost wide synthetic neck. This pass preserves that evidence; it did not re-investigate those claims. | Requires a separate correctness investigation of tile summaries. The current optimization preserves baseline outputs and settings. |
| **Skip bed-fit convex hull for an already-fitting mesh** | `_finish` already has XY extents before calling `bed_turn`; source-inspected, unbenchmarked. | Measure on simplified and unsimplified meshes and preserve fit tolerance and zero-angle behavior. |
| **Cache and prune shared-border pairs** | `shared_borderlines` extracts boundaries for each pair: 13 provinces mean 78 pairs and 156 extractions. Raw WGS84 bounds leave 29 possible pairs; projection may change this count. | No complete border-pipeline timing yet. Preserve exact intersections and pair order. |
| **Reuse island editor drawing paths** | `_redraw` reconstructs GeoSeries/plot patches after selection and threshold changes; terrain editor has a related array-based path implementation. | Needs redraw profiling and visual checks for holes, selection, overlays and viewport behavior. |
| **Index island hit testing** | `_island_at` and `_islands_in_box` scan all polygons. Source-inspected only. | Establish impact relative to redraw cost; preserve first-hit order and contains/intersects boundary behavior. |
| **Index border piece/parent matches** | `piece_lobes`, `removed_flags` and `_absorb_enclaves` contain repeated searches; some already cache or prune bounds. | Measure first; preserve ownership, tie order, topology and invalidation behavior. |
| **Prepare connector corridor geometry** | `_edge_frame` repeatedly checks point containment in an immutable corridor. Source-inspected only. | Benchmark against interpolation/intersection cost and verify identical connector orientation and coordinates. |
| **Reuse stress descent graph connectivity** | `_descend` rebuilds a graph at each band even when no edge appears. Source-inspected only. | Mass can still arrive without a new edge; naive band skipping changes scoring. Separate design and equality tests are needed. |

Measured benchmark outputs are recorded in
[`benchmarks/results/2026-10-31.json`](../benchmarks/results/2026-10-31.json).
The benchmark scripts check baseline output equivalence before timing. The
sampler reproduction is supplied under `benchmarks/` as a standalone
diagnostic, separate from the passing regression suite.
