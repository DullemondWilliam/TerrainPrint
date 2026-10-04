# Planning: borders in `UI_terrain_editor` — import, define, print

A plan for growing the terrain editor from *one province, one DEM, one solid*
into a three-stage run that also simplifies and defines the borders between
provinces, and prints them:

1. **Import** — the DEM, the provinces to print, and the regions around them.
2. **Borders** — simplify the outlines, carve the corridor, place the connectors.
3. **STL** — the province terrain solids, pocketed, and the border bar, split
   to the bed.

> **Status: plan.** Nothing below is built. Lib behaviour quoted here is read
> from the source; numbers marked *unmeasured* still need a run on `Data/`.

---

## 1. Where it sits

Every lib this needs already exists and is tested on its own. What is missing
is a window that runs them in order and hands each one's output to the next:

```
Stage 1  Import
   DEM (GeoTIFF)                        ── existing import_geotiff
   target provinces (GeoJSON, many)     ── new; reprojected to crs.working
   surrounding regions (GeoJSON, many)  ── new; context only, never printed
        │
Stage 2  Borders
   LIB_border_simplification.topo_simplify_two_tolerances   coast / internal tol
        │
   LIB_border_regions.build_border_regions                 corridor width
        │   → trimmed targets, trimmed surrounding, border corridor
   LIB_border_connectors.generate_lobes                     dovetail lobes
        │
Stage 3  STL
   per target:  LIB_raster_reproject → LIB_geotiff_clip → LIB_terrain_stl
                (cut to the *trimmed* province)  → notch_province (pockets)
   once:        build_border_mesh (bar + lobes) → LIB_border_split.split_to_bed
```

The order is fixed by the libs themselves. `LIB_border_regions` says it
outright: *"any simplification is the caller's responsibility and should be done
before calling in"*. The terrain has to be cut by the province **after** the
corridor is carved out of it. Otherwise the terrain solid and the border bar
overlap by half the corridor width.

### The main app already tried this

`app/main_window.py` has a four-step flow (`Import / Cleanup / Borders / Mesh &
Export`) built on the same libs. It imports `from core import borders, cleanup,
measure, meshing, terrain_preview`, and **there is no `core` package in the
repo**, so that window cannot start. Decision 1 in §6 covers whether this plan
should revive that window instead of growing the editor. This plan assumes the
editor, because it runs and has 61 tests.

---

## 2. Window layout

The two toolbar rows sit inside a `QTabWidget` with three tabs: **1 · Import**,
**2 · Borders**, **3 · STL**. The panels underneath (matplotlib before|after and
the vispy 3-D view) stay shared across the tabs, and what they show depends on
the active tab:

| Tab | Left panel | Right panel | 3-D |
|---|---|---|---|
| 1 · Import | DEM overview + every outline (targets red, surrounding grey) | — | — |
| 2 · Borders | outlines as imported | simplified outlines, corridor filled, lobes drawn; coastal and internal arcs in two colours (the lib already returns them separately) | — |
| 3 · STL | DEM overview + selected province's trimmed outline (today's before panel) | cropped raster (today's after panel) | terrain solid, border bar, or both |

A tab stays disabled until its inputs exist: tab 2 needs at least one target,
and tab 3's terrain half needs a DEM. The border-bar half of tab 3 does not need
a DEM.

**Back-compat rule.** Today's single-province run has to keep working unchanged
when no borders are defined. Stage 3 is the current editor. `self.boundary`
stays, and becomes "the province selected for stage 3". `_boundary_geom()` keeps
its signature and returns the trimmed, simplified geometry when stage 2 has run,
and the imported one when it has not. **Import Boundary …** stays as a shortcut
that adds one target and selects it. This keeps the existing tests in
`tests/test_terrain_editor.py` meaningful rather than rewritten.

---

## 3. Stage 1 — Import

| Control | Does |
|---|---|
| **Import GeoTIFF …** | unchanged |
| **Add Provinces …** | `getOpenFileNames`, many at once; each file becomes a *target* |
| **Add Surrounding …** | same, as *surrounding* (US states, other provinces left unprinted) |
| region list | name, role, vertex count; **Remove**; re-adding a name replaces it, so a region cannot hold both roles (FR-1.8) |

On load, each file is dissolved to one geometry, `make_valid`-ed (FR-1.6), and
reprojected to `crs.working` (EPSG:3347) (FR-1.4). The source CRS is kept for
saving back. The file dialogs open on `paths.provinces_out`
(`Data/JSonProvinces-Cleaned`), because that is where the island editor writes
and cleaned outlines are the right input (FR-4.3).

Status line: how many targets, how many neighbour pairs share a border, and any
target that touches nothing. A target that touches nothing is legitimate (an
island), but it is worth saying, because it gets no seam.

State: `self.targets: dict[str, geom]` and `self.surrounding: dict[str, geom]`,
both in EPSG:3347, plus `self.source_crs`.

---

## 4. Stage 2 — Borders

Stage 2 runs three passes, each with its own button, so the cheap preview of one
does not re-run the others:

### 4.1 Simplify

`topo_simplify_two_tolerances(gdf, coast_tol_m, internal_tol_m)` runs over
targets **and** surrounding together, in one topology. A target/surrounding
frontier is then a shared arc, and both sides of it move together.

Controls are in **print-mm** and converted with `print.scale` (×8000 at
1:8,000,000). The question being asked is whether the outline is printable, and
`seam.*` and `stress.*` already use mm. The main app used 2.4 km coastal and
4.0 km internal, which is 0.30 mm and 0.50 mm. Those become new config keys
`borders.coast_tol_mm` and `borders.internal_tol_mm`.

Report: vertices before → after per region, and coastal/internal arc counts.
There is also an **overlap check**: pairwise intersection area between
simplified targets must be zero. The lib guarantees this by construction, and
the editor checks it, the same way `LIB_shared_border` rejects its own output on
a non-zero gap.

### 4.2 Corridor

`build_border_regions(core=targets, surrounding=surrounding,
border_width_km=width_mm * scale / 1e6)` runs on the simplified geometry. The
control is **Width (mm)**, default 2.0 (16 km), as new key
`borders.width_mm`. The lib already implements the role rules: target↔target is
split evenly, target↔surrounding comes entirely from the surrounding side, and
surrounding↔surrounding gets no corridor (FR-3.2 to FR-3.4).

### 4.3 Connectors

`generate_lobes(border, unary_union(trimmed targets), ConnectorParams())` is
given only the targets' union, so no lobe reaches into a surrounding region
(FR-3.8). Only **Spacing** is exposed. Neck, mouth and reach stay at the
`ConnectorParams` defaults until there is a printed reason to change them.

### 4.4 Running and saving

All three passes run in a `BorderWorker`, off the UI thread like `RunWorker`.
`UI_shared_border` already measured Northwest Territories/Nunavut at 7.7 s for a
*single pair*. A whole map with US states is *unmeasured* and will be slower.

**Save Borders …** writes a folder holding `borders.geojson` plus one
`name_<province>.geojson` per trimmed target, in the source CRS. That is the
layout `build_border_from_dir` and `fit_dovetails_from_dir` already read, so the
CLI tools and this window can hand off to each other.

---

## 5. Stage 3 — STL

### 5.1 Province terrain

This is today's raster row and mesh row, with three changes:

* A **Province** combo picks which trimmed target to build. Terrain is built one
  province at a time, because a DEM run is the expensive step and one at a time
  is how it is inspected.
* **Buffer is disabled once borders are defined.** It grows the cut outward, and
  outward is the corridor. If it were left live, the solid would overlap the
  border bar by exactly the buffer.
* A **Pockets** checkbox runs `notch_province(mesh, lobes, *result.origin_m,
  params)` after `terrain_to_stl`, inside `MeshWorker`, using only the lobes
  that intersect this province. `TerrainResult.origin_m` is the metres→print-mm
  origin, so no reconciliation is needed. **Verify before relying on it:** the
  pocket cutters assume the solid's base sits at `z = 0`, and that `origin_m`
  means the same min-easting/northing that `notch_province` takes. The
  watertight gate runs again after the boolean (FR-4.7).

### 5.2 Border bar

* **Build Border STL** runs `build_border_mesh(border, lobes, ConnectorParams())`.
  The result is the stepped bar: a 2 mm corridor on a 1 mm lobe layer. With
  connectors turned off it runs `build_border_stl` for a plain bar instead.
* **Split to Bed** runs `split_to_bed(mesh, print.bed_mm,
  print.bed_margin_mm)`. **Caveat:** the splitter is connector-blind, and its
  docstring reserves a `keepout` hook for exactly this. It can cut through a
  lobe, which breaks FR-3.11. See decision 3.
* **Save Border Pieces …** writes `border_001.stl …` to a folder.

### 5.3 Assembly view

The 3-D panel gets a **Show assembly** toggle. The terrain solid and the border
bar are each built in their own print-mm frame, so each is translated by its
metres origin divided by `mpm` into one shared frame and drawn together. This
is the view where a pocket that missed its lobe becomes visible, rather than
being found after a 6-hour print.

### 5.4 Invalidation

The editor already has a two-level rule (raster stales mesh). It becomes:

| Change in | Stales |
|---|---|
| Stage 1 (any region added or removed) | stage 2 results, then all of stage 3 |
| Simplify tolerances | simplify, corridor, lobes, then all of stage 3 |
| Corridor width | corridor, lobes, then all of stage 3 |
| Lobe spacing | lobes, border bar, pocketed solid |
| Raster / mesh rows | as today |

Stage 2 changes stale the raster crop as well, not only the solid, because the
crop's polygon mask *is* the trimmed province.

---

## 6. Open decisions

1. **Editor or app?** `app/main_window.py` already describes this flow, but it
   cannot start without `core/`. Growing the editor (this plan) and reviving the
   app would duplicate each other. *Recommendation:* grow the editor now, since
   it runs and is tested. Retire or rebuild the app once this works.
2. **Printability of the seams.** Topology simplification is Douglas-Peucker
   only. It leaves the sub-nozzle zigzag and spikes that `LIB_shared_border`
   exists to remove. That lib was measured on Ontario/Québec: 54.5% of segments
   are under 0.4 mm. The options:
   * (a) Do nothing. The seams print ragged.
   * (b) Run `simplify_shared_border` on each neighbour pair after the topology
     pass. Applying it pair after pair across a whole map is unverified.
   * (c) Add an optional arc conditioner to `topo_simplify_two_tolerances` that
     applies the short-segment and despike passes to each internal arc.

   *Recommendation: (c).* An arc is conditioned once and shared by both sides
   by construction, and its endpoints are the triple junctions, so they stay
   pinned for free. It is a lib change with its own tests, so it comes last in
   the build order.
3. **Splitting through connectors.** *Recommendation:* implement the reserved
   `keepout` in `LIB_border_split` (cut planes nudged off any lobe) before the
   border pieces are saved. Until that is built, report how many lobes a cut
   crosses rather than saving silently.
4. **Tolerance units.** This plan says print-mm (§4.1). If km is preferred, to
   match the old app, that is only a conversion change.

---

## 7. Build order

Each step is one commit, and each leaves the window working. Every step is
tested with the venv `pytest`, which runs the Qt UI tests (`unittest` skips
them).

1. **Config + tabs.** Add the `borders.*` keys. Move the existing two rows into
   tab 3. No behaviour change, and all of `test_terrain_editor.py` passes as is.
2. **Stage 1.** Multi-file targets and surrounding, the region list, reprojection
   on load, and the left-panel outlines.
3. **Stage 2 simplify.** `BorderWorker`, the preview, the report, and the
   overlap check.
4. **Stage 2 corridor + lobes.** The right-panel corridor and lobes, and
   **Save Borders …** in the `build_border_from_dir` layout.
5. **Stage 3 province selector.** `_boundary_geom` returns the trimmed target,
   the buffer rule, and the invalidation table.
6. **Stage 3 border bar.** Build, split, and save the pieces.
7. **Stage 3 pockets.** Verify the `z = 0` and `origin_m` assumptions with a
   test first, then add the assembly view.
8. **Decisions 2 and 3.** The arc conditioner and the split keep-out, as lib
   changes with lib tests.
