# 3D Terrain — Requirements and Design

Converting provincial/state boundary data and a national elevation model into
watertight, printable 3D map pieces that assemble into a physical map.

---

## 1. Purpose and scope

The system turns two inputs — administrative boundary GeoJSON and a GeoTIFF
digital elevation model (DEM) — into STL solids sized for a consumer 3D printer:

* one terrain solid per **printed region** (province/state), and
* a **border corridor** that sits between them and keys into their undersides
  with dovetail connectors.

Everything is expressed at a fixed map scale, so a millimetre on the model is a
known number of kilometres in the world, and every dimension shown in the UI is
the real printed dimension.

**Out of scope:** slicing, printer control, and any georeferencing beyond the
fixed projections listed in §4.3.

---

## 2. System overview

Two front ends share one headless core:

```
              ┌────────────────────────────────────────────┐
              │  main_app.py  (PySide6, 4-step wizard)      │   current
              └───────────────┬────────────────────────────┘
                              │
   UI_*.py (4 legacy Tkinter tools) ──┐        │              legacy
                                      ▼        ▼
                        ┌──────────────────────────────┐
                        │  core/   (Qt-free, tested)    │
                        └──────────────┬───────────────┘
                                       ▼
                        ┌──────────────────────────────┐
                        │  LIB_*.py  (geometry engines) │
                        └──────────────────────────────┘
```

`core` never imports Qt, and `app` never contains geometry logic. That split is
what makes the bulk of the system testable without a display, and it is the
single most important structural rule in the codebase.

---

## 3. Requirements

### 3.1 Functional — Step 1: Import

| ID | Requirement |
|---|---|
| FR-1.1 | Import boundary GeoJSON as **target** regions (printed) |
| FR-1.2 | Import boundary GeoJSON as **surrounding** regions (context only, never exported) |
| FR-1.3 | Import a GeoTIFF DEM |
| FR-1.4 | Reproject every geometry to the print CRS on load |
| FR-1.5 | Accept `FeatureCollection`, bare `Feature`, and bare geometry documents |
| FR-1.6 | Repair invalid geometry on load rather than failing |
| FR-1.7 | Configurable map scale; all displayed dimensions in print millimetres |
| FR-1.8 | Re-importing a region replaces it, so it cannot exist in both roles |
| FR-1.9 | Remove an imported region |

### 3.2 Functional — Step 2: Cleanup

| ID | Requirement |
|---|---|
| FR-2.1 | Topology-preserving simplification with separate coastal and internal tolerances |
| FR-2.2 | A border shared by two regions must stay matched — no gaps or overlaps |
| FR-2.3 | Filter landmasses below an area threshold |
| FR-2.4 | Never delete a region entirely, however aggressive the threshold |
| FR-2.5 | Manually remove selected landmasses |
| FR-2.6 | Manually merge selected landmasses into one connected piece |
| FR-2.7 | Close gaps between nearby landmasses by distance |
| FR-2.8 | Remove lakes (interior rings) |
| FR-2.9 | Every operation is individually undoable and redoable |
| FR-2.10 | Undo restores the exact prior state |
| FR-2.11 | Operations apply only to regions ticked in the province selector |
| FR-2.12 | Show each province's landmass count, kept current across edits and undo |
| FR-2.13 | An operation that changes nothing must not enter the undo history |

### 3.3 Functional — Step 3: Borders and connectors

| ID | Requirement |
|---|---|
| FR-3.1 | Carve a corridor of configurable width between regions |
| FR-3.2 | **Target ↔ target**: corridor split evenly between both |
| FR-3.3 | **Target ↔ surrounding**: corridor carved entirely from the surrounding side, so printed regions keep their full extent |
| FR-3.4 | **Surrounding ↔ surrounding**: no corridor |
| FR-3.5 | Inter-regional corridors must reach the frontier and meet the corridor there |
| FR-3.6 | Place dovetail connectors along the corridor |
| FR-3.7 | A connector must lie within exactly one printed region — never straddle a boundary |
| FR-3.8 | No connectors into surrounding regions (nothing there to key into) |
| FR-3.9 | Connector tab and its pocket share one profile, with assembly clearance |
| FR-3.10 | Split the corridor into pieces that fit the print bed |
| FR-3.11 | Splits must not cut through a connector |

### 3.4 Functional — Step 4: Mesh and export

| ID | Requirement |
|---|---|
| FR-4.1 | Build a terrain solid per printed region from the DEM |
| FR-4.2 | Build flat solids when no DEM is loaded |
| FR-4.3 | Mesh from **cleaned** geometry, so Step 2 edits reach the print |
| FR-4.4 | Configurable vertical exaggeration and base thickness |
| FR-4.5 | Carve connector pockets into region undersides |
| FR-4.6 | Split solids to bed size |
| FR-4.7 | **Every** exported piece must be watertight with positive volume |
| FR-4.8 | Export nothing at all if any piece fails validation |
| FR-4.9 | Report output folder and files written |

### 3.5 Functional — Visualisation and interaction

| ID | Requirement |
|---|---|
| FR-5.1 | Render regions to scale, coloured by role, labelled with printed dimensions |
| FR-5.2 | Render the DEM as a backdrop behind the regions |
| FR-5.3 | Render corridors filled and connectors as dashed outlines |
| FR-5.4 | Selecting landmasses in the list highlights them on the map |
| FR-5.5 | Clicking a landmass on the map selects it in the list |
| FR-5.6 | Ctrl+click on the map adds/toggles landmasses in the selection |
| FR-5.7 | Press-and-hold a list entry centres that landmass on the map |
| FR-5.8 | The camera is preserved across edits |
| FR-5.9 | An explicit Recenter control |
| FR-5.10 | Step navigation: Back, phase list with the current one highlighted, Confirm |

### 3.6 Non-functional

| ID | Requirement |
|---|---|
| NFR-1 | Geometry logic runs headlessly and is unit-testable without a display |
| NFR-2 | Invalid or unusual input degrades gracefully rather than crashing |
| NFR-3 | Cost of DEM preview bounded by output size, not raster size |
| NFR-4 | Printed dimensions must be truthful — projection and scale correct |
| NFR-5 | Destructive geometry operations must be reversible |

---

## 4. Architecture

### 4.1 Layers

| Layer | Modules | Rule |
|---|---|---|
| **UI** | `app/`, `main_app.py` | Qt only. No geometry. |
| **Core** | `core/` | Pure Python/Shapely. **No Qt imports.** |
| **Engines** | `LIB_*.py` | Standalone geometry/mesh algorithms |

### 4.2 Module map

**core/**
| Module | Responsibility |
|---|---|
| `project.py` | `ProjectState`, `Region`, roles, reprojection on import |
| `geometry.py` | Loading, repair, safe union, boundary rings, polygon parts |
| `measure.py` | Scale ↔ mm/km conversions, bed-fit test |
| `cleanup.py` | Step 2 operations (all pure functions) |
| `borders.py` | Corridor construction, connector filtering, corridor bed-splitting |
| `meshing.py` | Terrain/flat solids, solid bed-splitting, validation, export |
| `terrain_preview.py` | Windowed DEM read, warp, hypsometric colouring |

**app/**
| Module | Responsibility |
|---|---|
| `main_window.py` | Wizard shell, the four step pages, all actions |
| `canvas.py` | `QGraphicsView` map: regions, terrain, corridors, highlight, picking |
| `commands.py` | `QUndoCommand` wrappers around `core.cleanup` |
| `widgets.py` | `HoldListWidget` (press-and-hold list) |

**LIB/** — `border_simplification` (TopoJSON), `border_regions` (corridor
carving), `border_connectors` (dovetails, manifold extrusion),
`island_merging`, `lake_removal`, `geotiff_clip`, `terrain_stl`.

### 4.3 Coordinate systems

| Space | CRS / units | Used for |
|---|---|---|
| Source | EPSG:4326 | Input GeoJSON |
| **Print CRS** | **EPSG:3347** (metres) | All internal geometry |
| DEM | EPSG:3979 | Elevation raster, warped on read |
| Print space | millimetres | Meshes, scene units |
| Scene | print-mm, **Y negated** | Qt canvas (Qt's Y grows downward) |

Conversion is `metres_per_mm = scale / 1000`; at the default **1:8,000,000**,
**1 mm = 8 km**.

Every printed piece is built in its **own local origin** (its minimum easting
and northing). Absolute position is therefore *not* comparable between pieces —
only shapes are. This is why connector tabs and pockets mate: both derive from
the same geometry at the same scale, so their local profiles are identical.

### 4.4 Data model

```python
Region:      name, geometry (EPSG:3347), role ∈ {target, surrounding}, source_path
ProjectState: scale, regions[], terrain_path
```

`target` regions are meshed and exported. `surrounding` regions shape the
borders and are never exported.

---

## 5. Design detail

### 5.1 Region roles → border behaviour

`LIB_border_regions` distinguishes *province* (symmetric seam) from *state*
(seam taken entirely from that side; none between two states). The new roles map
directly onto it:

| Role | Maps to | Effect |
|---|---|---|
| target | `province` | shared seam split evenly |
| surrounding | `state` | gives up the whole corridor; no seam against another surrounding |

A printed region therefore never loses land to a neighbour that isn't printed
(FR-3.3).

### 5.2 Simplification (FR-2.1, FR-2.2)

All selected regions go through **one shared TopoJSON topology**
(`topo_simplify_two_tolerances`). An arc shared by two regions is simplified
once, so the two sides cannot drift apart. Coastal and internal arcs take
separate tolerances.

### 5.3 Island merge — isthmus bridging (FR-2.6)

1. Union the selected parts (touching parts merge exactly)
2. Anything still separate is joined by a **land bridge**: the shortest line
   between two parts, buffered to `bridge_width`
3. Bridges follow a **minimum spanning tree**, so *n* parts need *n−1* bridges

> **Design history.** This replaced a convex-hull fallback. On real data the
> hull inflated British Columbia by **+363,721 km² (40%)** when merging distant
> islands and flattened both coastlines into a blob. The isthmus adds
> **+1,008 km² (0.11%)** — *99.7% less fake land* — and preserves both outlines.
> A thin bridge is also what a printed model needs to hold together.

### 5.4 Dovetail connectors (FR-3.6 … FR-3.9)

Geometry: the corridor prints **2 mm tall**; its bottom **1 mm** carries dovetail
lobes reaching into adjacent printed regions, which receive a matching 1 mm-deep
pocket in their **underside**. Each lobe is a trapezoid — narrow throat at the
corridor, widening outward — so the wide end is captured behind the throat.

Lobes are generated **once in projected metres** and left unclipped, so tab and
pocket share an identical profile; the pocket is the tab plus `clearance`.

**Placement rule.** A lobe is kept only if it reaches into *exactly one* printed
region:
* two regions ⇒ straddling a boundary; its pocket would span two separate
  pieces and could never seat
* zero ⇒ pointing at something unprinted

Lobes are tested against **trimmed** (post-carve) geometry — the actual printed
piece.

> **Known limitation.** A dovetail footprint dropped in from the back locks the
> pieces **in the map plane only**; nothing resists lifting straight off. True
> Z-locking needs an undercut, which can only be assembled by sliding, and map
> seams are too curved for that.

### 5.5 Bed splitting (FR-3.10, FR-4.6)

Two different algorithms, because the shapes differ:

| Input | Algorithm | Why |
|---|---|---|
| Border corridor | Recursive bisection along the longer axis, cut positions nudged off connectors | A thin ribbon; a grid would leave orphan fragments and slice connectors |
| Region solids | Grid of boolean intersections | An area-like solid; a grid is the natural tiling |

Bisection terminates because each cut strictly shrinks the axis it cuts.
Cut placement is a *convenience* choice — the regions hold the corridor segments
in alignment through their own connectors — but cutting a connector in half is
not acceptable, hence the nudge.

### 5.6 Terrain meshing (FR-4.1)

1. Warp the region into the DEM CRS; read only the covering window
2. Mask nodata; decimate to the print resolution (`PRINT_RES_MM`)
3. Reproject pixel centres into the print CRS, convert to millimetres, lift by
   `(elevation − floor) × terrain_factor / metres_per_mm + base`
4. Delaunay-triangulate the top surface, keeping triangles inside the region
5. Remove **pinch faces** (see below)
6. Wall the surface down to `z = 0` along its own perimeter; mirror for the base

> **Design history — three bugs, found by measurement.** The original code built
> walls from the *polygon rings*, but an unconstrained Delaunay does not follow
> those rings, so the solid never sealed; it only logged a warning, silently
> shipping non-watertight meshes. Fixing it needed:
> 1. walls derived from the **actual perimeter of the kept triangles**
> 2. **undirected** edge counting — scipy's Delaunay does not guarantee
>    consistent winding, so testing for a reversed twin misclassified interior
>    edges
> 3. **pinch removal** — where a coastline narrows to a one-triangle spit the
>    surface touches itself, giving edges with four incident faces. Diagnosed by
>    dumping an edge-multiplicity histogram: *0 unsealed edges, 7 edges shared by
>    4 faces*.

### 5.7 Watertight gate (FR-4.7, FR-4.8)

`meshing.validate()` rejects any mesh that is empty, not watertight, or of
non-positive volume. `export_meshes()` validates **every** piece before writing
**any**, so a failure cannot leave a half-exported folder.

### 5.8 Boolean operations

`manifold3d` is the boolean backend. Two lessons are encoded in the code:

* **Stay in manifold space.** Round-tripping through trimesh between operations
  loses watertightness.
* **Never rely on coplanar contact.** Stacked layers are overlapped by
  `_LAYER_OVERLAP_MM` so the union sees intersecting solids, not touching faces.

Extrusion uses manifold's `CrossSection`, not trimesh's earcut, which produced
inverted and degenerate triangulations on the thin corridor ribbon.

### 5.9 Terrain preview (FR-5.2)

The DEM is ~7 GB and in a different CRS, so it cannot be loaded whole:

1. Transform the view extent into the raster CRS
2. Read **only that window**, decimated on the way in (≤ 2× output size)
3. Warp into the print CRS
4. Colour through a hypsometric ramp; nodata → transparent

Cost is bounded by output size (`DEFAULT_MAX_PX`), not file size: ~0.2 s for a
small province, ~3–4 s for several large ones. Cached on `(raster, extent)`.
Drawn at `z = -10`; region fills drop to alpha 120 only when terrain is present.

### 5.10 Undo/redo (FR-2.9, FR-2.10, FR-2.13)

One `QUndoCommand` per action on a single `QUndoStack`. Undo is
**snapshot-based**: capture each affected region's geometry before and after,
then swap. Because `core.cleanup` always returns new geometry, holding
references is cheap and exact — undo never recomputes, so it cannot drift.

Change detection compares **by value (WKB)**, not identity: an operation may
return a rebuilt-but-identical geometry, and a no-op must not enter the history.

The `only` parameter restricts a command's snapshot to the ticked provinces, so
scoping (FR-2.11) flows through undo for free.

### 5.11 Canvas and interaction

* Scene units are **print millimetres**, so what you see is the printed size
* Highlight is **stored, not just drawn**, and re-drawn after any re-render —
  but deliberately **cleared on edits**, since it would otherwise point at
  geometry that no longer exists
* The camera is captured and restored across re-renders; fitting happens only on
  first content or via Recenter (FR-5.8, FR-5.9)
* Clicks report the projected point and the Ctrl modifier, so the window can
  resolve which landmass was hit and whether to add to the selection

---

## 6. Parameters and defaults

| Parameter | Default | Notes |
|---|---|---|
| Map scale | 1:8,000,000 | 1 mm = 8 km |
| Corridor width | 2.0 mm | |
| Coastal / internal tolerance | 2.4 / 4.0 km | |
| Print bed / margin | 256 / 10 mm | |
| Terrain exaggeration | ×10 | |
| Base thickness | 2.0 mm | must exceed pocket depth |
| Print resolution | 0.5 mm | DEM sampling target |
| Connector height / dovetail | 2.0 / 1.0 mm | |
| Connector spacing | 14 mm | |
| Throat / mouth / reach | 1.6 / 3.2 / 3.0 mm | mouth > throat locks the joint |
| Connector clearance | 0.2 mm | typical FDM fit |
| Merge bridge width | 1.5 mm | |
| Minimum printable part | 0.5 mm² | |
| Terrain preview size | 900 px | longest edge |

> **Scale must match** between corridor generation and meshing, or tabs will not
> fit pockets. Both read it from `ProjectState`.

---

## 7. Validation and testing

**255 automated tests.** Strategy by layer:

| Suite | Tests | Focus |
|---|---|---|
| `test_core_cleanup` | 25 | Exact-area fixtures; bridging vs hull; topology preservation |
| `test_terrain_and_picking` | 29 | Colour ramp, map picking, ctrl+click, camera, overlays |
| `test_core_meshing` | 23 | Watertightness, splitting, export gating, PEI pipeline |
| `test_border_simplification` | 23 | Shared-arc simplification |
| `test_core_borders` | 21 | Corridor width, bed fit, connector rules, role semantics |
| `test_hold_to_focus` | 21 | Hold gesture, focus, selection highlight |
| `test_core_geometry` | 20 | Repair, MultiPolygon, rings, loaders |
| `test_undo_redo` | 17 | Round-trip invariants, no-op rejection |
| `test_app_smoke` | 16 | Shell, navigation, import wiring |
| `test_core_project` | 14 | Roles, reprojection, PEI regression |
| `test_region_scoping` | 14 | Landmass counts, per-province scoping |
| `test_island_merging` / `test_lake_removal` | 12 / 12 | Engine behaviour |
| `test_core_measure` | 8 | Scale conversions |

Principles:

* **Exact synthetic fixtures** — a landmass of exactly 10,000 km² with islands of
  100/25/1 km², so assertions are precise rather than approximate
* **Invariants** — undo round-trips, watertightness on every export, shared
  borders staying matched, area conservation where nothing should be lost
* **One real-data regression** — Prince Edward Island: small, fast, and genuinely
  has islands
* **Thin UI tests** — scene populates, buttons fire, navigation works. Geometry
  is never tested through the GUI

---

## 8. Constraints and known limitations

| # | Limitation |
|---|---|
| L-1 | **Long operations run on the UI thread.** Meshing a large province ≈ 23 s, terrain preview ≈ 3–4 s; the window freezes meanwhile |
| L-2 | Connectors lock **in-plane only**; nothing resists lifting a piece off (§5.4) |
| L-3 | `add_base_plate()` exists in `core.meshing` but is **not wired to a UI control** |
| L-4 | **No slicer-acceptance check** has been run against exported STLs |
| L-5 | Source data contains only **3 interior rings** across 27 files, so lake removal has almost nothing to act on |
| L-6 | Scale mismatch between steps fails **silently** — nothing cross-checks it |
| L-7 | Merged-region absolute positions are not comparable between pieces (§4.3) — by design, but surprising |
| L-8 | Terrain meshing requires the DEM to cover the region; otherwise it raises |
| L-9 | Four legacy Tkinter tools remain and duplicate parts of the new app |
| L-10 | No packaging/distribution build exists |

---

## 9. Legacy tools

Still present and functional, each with `.ini` settings persistence:

| Tool | Purpose |
|---|---|
| `UI_border_regions.py` | Border corridor carving and GeoJSON export |
| `UI_terrain_to_stl.py` | Terrain → 3MF/STL, with coloured elevation bands |
| `UI_simplify_geo_borders.py` | Topology-aware simplification preview |
| `UI_island_editor.py` | Island inspection, removal, merging |

`UI_terrain_to_stl.py` additionally supports **coloured 3MF export** with
user-defined hypsometric bands — a capability the new app does not yet have.

---

## 10. Not yet implemented

1. **Threading** — move meshing, simplification, border building, and terrain
   preview onto workers (addresses L-1)
2. **Packaging** — frozen distributable; GDAL is the known risk
3. **Base-plate control** (L-3)
4. **Slicer verification** (L-4)
5. **Coloured 3MF export** in the new app
6. **Scale consistency check** between steps (L-6)
7. Retire the legacy tools once the new app covers their capabilities (L-9)
