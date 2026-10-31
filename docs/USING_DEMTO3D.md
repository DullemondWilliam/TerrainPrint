# Converting a DEM to STL with the DEMto3D QGIS plugin

> **Superseded.** `LIB_terrain_stl` now does this in-repo, and cuts the coastline
> directly rather than leaving the rectangular skirt described in §3 — see
> [USING_TERRAIN_STL.md](USING_TERRAIN_STL.md). This document is kept for the
> plugin route and for the parameter cross-reference in §5.

This project no longer ships its own DEM→STL code. The clip + terrain-STL libs
(`LIB_geotiff_clip`, `LIB_terrain_stl`), their tests, and their UI harnesses were
removed in favour of the **DEMto3D** QGIS plugin, which does the same job
(DEM → printable STL with scale, vertical exaggeration, and a solid base) inside
QGIS.

- Plugin: <https://github.com/jawensi/DEMto3D-QGIS-Plugin>
- Manual / site: <http://demto3d.com>

Everything below is done in QGIS 3.x — no Python from this repo is involved.

---

## 1. Install the plugin

1. Open QGIS.
2. **Plugins ▸ Manage and Install Plugins…**
3. Search **DEMto3D**, select it, **Install Plugin**.
4. It appears under **Raster ▸ DEMto3D ▸ DEM 3D printing** (older builds: the
   **Plugins** menu).

## 2. Load and prepare the DEM

1. Drag your DEM raster (e.g. `Canada120m/Canada120m.tif`) into QGIS.
2. **Use a projected CRS in metres.** The plugin scales horizontal distance from
   the layer's units, so a geographic CRS (degrees, EPSG:4326) gives wrong
   dimensions. This project's build CRS is **EPSG:3347**; the source DEM is
   **EPSG:3979**. If the layer is in degrees, reproject first:
   **Raster ▸ Projections ▸ Warp (Reproject)** → target EPSG:3347 (or 3979).

## 3. (Optional) Clip to a province outline

DEMto3D's print area is a **rectangle**. To make the model follow a province
coastline instead of a rectangular tile, clip the DEM to the province polygon
*before* running DEMto3D:

1. **Raster ▸ Extraction ▸ Clip Raster by Mask Layer.**
2. Input = the DEM; Mask layer = the province GeoJSON (from `geojson/`).
3. Set **Assign a specified nodata value** (e.g. `-9999`) and enable
   *Match the extent of the clipped raster to the extent of the mask layer*.
4. Run — you get a province-shaped raster with nodata outside.

> **Note on shape:** DEMto3D still builds within the rectangular print extent and
> renders nodata cells at the base level, so the export is a flat rectangular
> slab with the province raised on top. That is usually what you want for a
> printable model. A true coastline *cut-out* (no surrounding slab) is not
> something DEMto3D produces — use `LIB_terrain_trim` (below) to trim the slab to
> the coastline afterward. This is the one capability the removed
> `LIB_terrain_stl` had that the plugin does not.

## 4. Export the STL

Open **Raster ▸ DEMto3D ▸ DEM 3D printing** and fill in the dialog:

1. **Layer to print** — your (clipped) DEM.
2. **Print extent** — click **Layer full extent**, draw a rectangle on the
   canvas, or type the min/max coordinates.
3. **Spacing (mm)** — model resolution: one sampled point per this many print
   millimetres. Smaller = finer surface and more triangles. This is the mm/px
   control the old clip UI exposed.
4. **Scale 1:N** — horizontal print scale. Use **8000000** to match this
   project's `DEFAULT_SCALE` (1 mm on the model = 8 000 m on the ground). Setting
   the scale fills in the model **Width/Length (mm)** automatically.
5. **Vertical exaggeration** — the height multiplier (this project used ~10–30).
   True scale (×1) is invisibly flat at 1:8 000 000.
6. **Base height (mm)** — thickness of the solid base below the lowest terrain,
   so the model has a flat bottom to print on (this project used ~2 mm).
7. The dialog reports the resulting model size and the number of faces/points —
   check it before exporting to avoid an enormous mesh (the same reason the old
   UI showed a face-count estimate).
8. **Export to STL…** → choose ASCII or binary and a path.

## 5. Parameter cross-reference

Mapping the removed libs' parameters to the DEMto3D dialog:

| Old pipeline (`LIB_terrain_stl` / clip UI) | DEMto3D dialog field |
|---|---|
| `target_res` / mm-per-px resolution | **Spacing (mm)** |
| `scale` (`DEFAULT_SCALE = 8 000 000`) | **Scale 1:N** |
| `terrain_multiplier` | **Vertical exaggeration** |
| `base_height_mm` | **Base height (mm)** |
| `clip_elevation(...)` to a province | **Clip Raster by Mask Layer** (step 3) |
| face-count estimate label | model-size / points readout in the dialog |
| projection `EPSG:3347` | the DEM layer's CRS (reproject in step 2) |

## 6. Quick recipe (Prince Edward Island)

1. Reproject `Canada120m.tif` to EPSG:3347 if needed.
2. Clip it by `geojson/…_Prince Edward Island….geojson` with nodata `-9999`.
3. DEMto3D ▸ Layer = the clip, **Layer full extent**, Scale **1:8 000 000**,
   Vertical exaggeration **30**, Base **2 mm**, Spacing to taste.
4. Export STL.

## 7. Trim the slab to the coastline (`LIB_terrain_trim`)

DEMto3D always leaves a flat rectangular skirt around the province (§3 note). To
cut the slab down to the actual coastline, run the trim lib on the exported STL
with the same province GeoJSON the DEM was clipped by:

```powershell
.\.venv\Scripts\python.exe -m lib.LIB_terrain_trim print/pei_slab.stl `
    "borders/w0.10_c0.05_i0.05/name_Prince_Edward_Island.geojson" -o print/pei_trimmed.stl
```

It reconciles the slab's local print-mm frame against the GeoJSON (matching the
raised-terrain silhouette, since the flat base is just a rectangle) and
intersects the mesh with the province polygon, so the cut follows the true
coastline — flat 0 m coastal land included. Pass `--scale` (default 8 000 000)
and `--build-crs` (default EPSG:3347; use EPSG:3979 if you kept the DEM-native
CRS in step 2) to match how the slab was built; `--buffer-mm` keeps a thin
coastal lip.

The trimmed STL now has a coastline-shaped base. Recommended order:
**DEMto3D export → `LIB_terrain_trim`**.

---

*What still lives in this repo:* the GeoJSON cleanup and border tools
(`UI_island_merge`, `UI_island_removal`, `UI_lake_removal`, `UI_border_regions`
and their `LIB_*` modules), the coastline-trim step (`LIB_terrain_trim`), and the
border/dovetail tooling. Only the DEM→STL conversion itself moved to DEMto3D.
