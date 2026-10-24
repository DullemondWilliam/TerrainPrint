# Usage Guide

How to go from province boundaries and a national elevation model to a set of
STL files that print and fit together into a physical relief map.

The map is printed as separate pieces:

- **one terrain solid per province**, with its real elevation on top, cut to
  its coastline;
- **a border bar**, a narrow strip that runs along every border between the
  provinces. Dovetail connectors on the bar's underside drop into pockets cut
  into the bottom of the province pieces, which holds the map together.

Everything is sized at one map scale, **1:8,000,000** by default, so a
millimetre of print is 8 km of ground, and every setting in the tools is
given in printed millimetres.

---

## 1. Setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Check that everything works by running the test suite. Run it with pytest;
`unittest` silently skips the Qt UI tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 2. The data

| Folder | What is in it |
|---|---|
| `Data/JSon-Provinces-Orig/` | The province and state GeoJSONs as downloaded. Never edit these. |
| `Data/JSonProvinces-Cleaned/` | Provinces after the island editor: where you save to. |
| `Data/Canada120m-Orig/Canada120m.tif` | The national DEM, 120 m pixels, in EPSG:3979. |
| `Data/Canada120m-3347/Canada120m_3347.tif` | The same DEM already in the print CRS (EPSG:3347). |
| `Data/Border/` | Saved borders: the corridor, trimmed provinces, connectors, splits. |
| `Data/STL/` | Where the STLs are written. |

The US states in `JSon-Provinces-Orig` (`US_*.geojson`) are there as
**surrounding** regions: they shape the borders along the frontier but are
never printed.

## 3. The workflow at a glance

```
ScreenRuler.py           (once per monitor, optional)   measure the screen
       │
UI_island_editor.py      (once per province)            clean up the coastline
       │                                                 → Data/JSonProvinces-Cleaned
       ▼
UI_terrain_editor.py
   1 · Import            the DEM, the provinces to print, the regions around them
   2 · Borders           carve the border corridor, place the connectors, plan splits
   3 · STL               crop, mesh and pocket each province; build the bar
                         Process All … writes every piece  → Data/STL
       │
       ▼
your slicer
```

Start both editors from the project folder:

```powershell
.\.venv\Scripts\python.exe UI_island_editor.py
.\.venv\Scripts\python.exe UI_terrain_editor.py
```

Both windows work the same way:

- Every button has a keyboard shortcut, named at the end of its tooltip. A key
  works only when its button is enabled.
- On the map, left-drag pans, the wheel zooms, and the arrow keys pan (with
  Shift for a long step, Ctrl for a small one). **Home** resets the view, and
  **Alt+Left** / **Alt+Right** step back and forward through views. Neither is
  Undo.
- Every setting you change is saved as you change it, so the next launch starts
  where you left off. See [Settings](#7-settings).

---

## 4. Measure your screen (optional)

The island editor's **Real Size** button shows the province at the exact size
it will print, held up to the glass. To do that it needs to know your monitor's
real pixel density, which monitors usually report wrongly.

```powershell
.\.venv\Scripts\python.exe ScreenRuler.py
```

Hold a credit card or a ruler against the on-screen bar and nudge it until they
match (**Left/Right** or **+/-**), then press **Enter**. **r** resets to the
operating system's guess, **c** recalibrates from the ruler view, and **q** or
**Esc** quits.

Calibrations are kept per monitor. Changing a monitor's resolution needs a new
one.

---

## 5. Clean up each province: the island editor

Raw province files are full of things a printer cannot make: islets smaller
than the nozzle, spits narrower than a line of plastic, necks of land that will
snap when the piece is handled. The island editor is where you fix those, one
province at a time.

### Open and save

- **Import GeoJSON …** (Ctrl+O) opens one province from
  `Data/JSon-Provinces-Orig`. Each separate piece of land becomes an *island*
  you can select.
- **Save GeoJSON …** (Ctrl+S) writes it to `Data/JSonProvinces-Cleaned`, in the
  same coordinate system and with the same attributes the file came in with.

### Select

- **Click** an island to select it, or click it again to deselect it.
- **Shift+drag** a box to add every island it touches; **Ctrl+drag** removes
  them from the selection.
- **All** (Ctrl+A), **None** (Ctrl+Shift+A) and **Invert** (Ctrl+I) work on the
  whole selection.

### Edit

Every edit is one step that **Undo** (Ctrl+Z) and **Redo** (Ctrl+Shift+Z) can
take back.

| Edit | Key | What it does |
|---|---|---|
| **Merge** | M | Joins the selected islands with necks of land, up to the *Buffer* distance. |
| **From Gap** | G | Sets *Buffer* to the smallest value that joins the two closest selected islands. |
| **Bridge** | B | Joins the selected islands with straight bridges of a fixed printed width: the cheapest set that connects them all. Use this when the width of the join matters. |
| **Draw Bridge** | D | Paint land by hand with the brush, for gaps nothing else closes. Esc cancels. |
| **Erase** | E | The same brush in reverse: rub land out. |
| **Remove** | Delete | Drops the selected islands. |
| **Remove Small** | S | Drops every island below *Min area*. |
| **Amputate** | A | Drops every piece of land narrower than *Min width*: islets, spits and thin necks alike. Usually the best single fix for a ragged coast. |
| **Remove Lakes** | L | Fills every lake in the province. |

*Buffer*, *Brush*, *Min area* and *Min width* are all **printed millimetres**.
Change **Print scale** (under the map) and the numbers stay the same, but what
they mean on the ground changes with it.

### Read the side panel

Down the right of the map are three checks of the province as it stands now:

- **Scores**: what your edits have cost compared with the file as imported.
  This covers how much coastline and land are left, and, after **Recalculate**
  (F5), how much of the print still matches the original.
  **Original** (O) lays the imported shape over the edited one in purple, so
  you can see what moved.
- **Printability**: marks in red every piece of land narrower than *Min width*,
  which is exactly what **Amputate** would remove. **Show** is F6, and **Worst
  patch** (W) zooms to the biggest one.
- **Stress**: marks every neck that is *holding land up*, coloured by how close
  it is to breaking. **1.0** means the neck is exactly as wide as its load needs.
  Below that it is sound; above it, it is at risk. **Show** is F7, and **Worst
  neck** (Shift+W) zooms to the most likely break. It updates by itself after
  every edit, so you can widen a neck with **Bridge** or the brush and watch
  the colour come down.

**Real Size** zooms the map to its printed size on your screen (see section 4).

### A good order

1. **Remove Small** to clear the islets.
2. **Amputate** at the printer's minimum width.
3. Join the islands that should print as one piece: **From Gap** then
   **Merge**, or **Bridge** where the width of the join matters.
4. Check **Stress** and widen any neck that is at risk.
5. Save to `Data/JSonProvinces-Cleaned`.

---

## 6. Build the pieces: the terrain editor

The terrain editor has three tabs, worked left to right. The map always shows
the latest result of the tab you're on.

### Tab 1 · Import

- **Import GeoTIFF …** (Ctrl+O) opens the DEM. Only a small overview is read, so
  even the 7 GB national file opens in under a second. Use
  `Data/Canada120m-3347/Canada120m_3347.tif` if you have it: it is already in
  the print CRS, so no warp is needed.
- **Add Provinces …** (Ctrl+P) adds the provinces to **print**, normally the
  cleaned files from section 5.
- **Add Surrounding …** (Ctrl+Shift+P) adds regions that shape the borders but
  are never printed, such as the US states along the frontier.
- **Remove** (Shift+Del) takes a region out of the list.

### Tab 2 · Borders

This tab carves the **corridor** between the provinces (the space the border bar
fills) and places the **connectors** that lock the bar to them.

1. **Set the borders up.** These are the tolerances for coastlines and for
   shared borders, the seam clean-up, the corridor **Width**, **Enclaves** (bits
   of province cut off by the corridor that are smaller than this go into the
   bar instead of printing loose), and **Bridge** (how wide a gap between a
   province and a surrounding region the corridor will close).
2. **Define** (D) simplifies every outline together, carves the corridor and
   places the connectors. The report underneath says what it changed and what
   it dropped.
3. **Shape the connectors.** **Place** turns connectors on or off. **Spacing**,
   **Height**, **Depth**, **Width** and **Ratio** set their size, and
   **Clearance** and **Height clear.** set how loosely they fit their pockets.
   Changing these on defined borders reshapes the connectors where they stand,
   so hand edits are kept.
4. **Edit by hand.** With **Edit Connectors** (E) on:
   - click a connector to select it, drag it to move it along the corridor, and
     click beside the corridor to add one;
   - **Delete** (Del) removes the selected one, and **Reset** (R) squares it to
     the corridor at the size the Shape group sets;
   - **]** / **[** turn it by 5°, **}** / **{** by 1°, and **+** / **-** change
     its depth.

   A connector that breaks a rule (it reaches two provinces, runs out of its
   province, or crowds another) turns **red**. It stays until you fix it, and
   nothing is built while one is red.
5. **Plan the splits.** The bar is usually longer than the print bed. With
   **Edit Splits** (X) on, click across the corridor to cut it there; select a
   split and press **Remove** (Backspace) to take it away. A piece that will not
   fit the bed at any angle is drawn red. **Pieces** lets you click a piece of
   bar to leave it out of the build.
6. **Save …** (Ctrl+B) writes the borders, connectors and splits to a folder such
   as `Data/Border`. **Load …** (Ctrl+L) reads them back so you can carry on
   later. Import the same provinces first.

Every edit on this tab can be undone (**Undo** Ctrl+Z, **Redo** Ctrl+Shift+Z).

### Tab 3 · STL

Pick a province under **Province**, then:

1. **Warp and crop.** Keep **CRS** at EPSG:3347, the print CRS: terrain cropped
   in any other CRS will not line up with the borders, and pocketing is refused.
   With **Auto** ticked, **Res** is set to the finest resolution whose STL stays
   under the triangle limit. **Resample** `max` keeps mountain tops when the DEM
   is coarsened. The status line shows the size of the crop before you run it.
2. **Reproject + Crop** (C) produces the province's DEM. **Save GeoTIFF …**
   (Ctrl+Shift+S) keeps it if you want it.
3. **Set the solid up.** **Exag** is the vertical exaggeration (×10 by default),
   **Base** is the flat plate under the terrain, and **Floor** is the elevation
   that sits on the base. **Cut coastline** trims the solid to the province's
   outline, and **Pockets** cuts the connector pockets into its underside.
4. **Build STL** (S), then **Save STL …** (Ctrl+S).
5. **Build Border STL** (B) builds the bar in its planned pieces, draped over
   the DEM so it is as tall as the terrain either side. **Save Border Pieces …**
   (Ctrl+Shift+B) writes them.
6. The 3D view's **Show** menu switches between **Terrain**, **Border bar** and
   **Assembly** (the pieces together, to check they meet).

### Process All

**Process All …** (Ctrl+E), in the **Everything** group, does all of tab 3 for
every province and the border bar in one run, into one folder. It asks before
writing over files, and a province that fails is reported while the rest carry
on.

It writes:

- `<province>.stl` with `<province>.frame.json` beside it, for each province;
- `border_<province>_01.stl`, `_02` … for the bar's pieces, each named after the
  province it runs along most.

The `.frame.json` sidecar records the settings the piece was built with. A piece
built under different map-wide settings (scale, exaggeration, base, floor,
spacing) will not meet its neighbours, so rebuild every piece after changing
one of those.

---

## 7. Settings

Settings live in two files, and the second wins:

- `project_config.json`: the project's settings, tracked in git.
- `project_config.local.json`: what you have changed on this computer. It is not
  tracked, and the windows write to it as you turn their controls.

Most settings are on screen. A few are only in the files: edit
`project_config.local.json` to change them, then restart the editor.

| Setting | Default | What it is |
|---|---|---|
| `print.scale` | 8000000 | The map scale, 1 : this. |
| `print.bed_mm` | 256 | Your printer's bed size. |
| `print.bed_margin_mm` | 10 | Kept clear on each side of the bed. |
| `print.border_thickness_mm` | 2.0 | The border bar's minimum height. |
| `print.dovetail_mm` | 1.0 | The connector layer's height, and the pocket depth. |
| `print.border_res_m` | 1000 | The DEM resolution the bar's top is draped at. |
| `seam.nozzle_mm` | 0.4 | Your nozzle width: the shortest run worth printing. |
| `terrain.max_triangles` | 3000000 | The most triangles a province STL may have. |

For example, a 220 mm bed:

```json
{ "print": { "bed_mm": 220.0 } }
```

---

## 8. Troubleshooting

**"Pockets need the terrain built in EPSG:3347"**: set **CRS** on the STL tab
back to EPSG:3347 and run **Reproject + Crop** again.

**"The pockets … cut through the Base"**: the pocket depth (connector
**Height** plus **Height clear.**) must be less than **Base**. Raise **Base** or
make the connectors shallower.

**A connector is red**: select it; the status line says which rule it breaks.
Move, turn or shrink it until it turns back, or delete it.

**A piece of bar is red**: it will not fit the bed at any angle. Add a split
across it on the Borders tab.

**Saved borders will not load**: import the same provinces on the Import tab
first; the borders are matched to them by name.

**A province opens as "Nothing to show"**: the file holds no polygons, or its
coordinates are not longitude/latitude. A GeoJSON with no CRS is always read
as WGS 84.

**`ModuleNotFoundError`**: run `pip install -r requirements.txt` in the venv.

---

## 9. Other tools

- **`harnesses/`**: one small window per library, for checking that library
  on real geometry by itself. They are developer tools, described in
  [BUILDING_UI_HARNESSES.md](BUILDING_UI_HARNESSES.md).
- **Command lines**: some libraries run on their own, for example
  `python -m lib.LIB_geotiff_clip` ([USING_GEOTIFF_CLIP.md](USING_GEOTIFF_CLIP.md)),
  `python -m lib.LIB_raster_reproject` ([USING_RASTER_REPROJECT.md](USING_RASTER_REPROJECT.md))
  and `python -m lib.LIB_terrain_stl` ([USING_TERRAIN_STL.md](USING_TERRAIN_STL.md)).
- **QGIS route**: [USING_DEMTO3D.md](USING_DEMTO3D.md) describes building
  terrain with the DEMto3D plugin instead, and trimming it with
  `LIB_terrain_trim`.
