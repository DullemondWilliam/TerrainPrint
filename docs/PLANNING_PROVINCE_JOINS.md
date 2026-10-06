# Planning: `LIB_province_joins` + `UI_province_joins` — butterfly keys across the map

A plan for a library and a window that join **every** pair of neighbouring
province STLs with butterfly (bowtie) keys. The user places the keys along each
shared border, and the run writes:

* each province STL with its slots cut into the back (`<province>_keyed.stl`),
* the keys themselves, laid out on bed-sized plates for printing.

> **Status: built.** `LIB_province_joins` and `UI_province_joins` implement this
> plan. Of the decisions in §7: the key size is set in the window, map-wide,
> and saved to the `keys` config section (1, with the defaults left at today's
> lobe until a test print says otherwise); uncleaned borders are allowed and
> labelled (2); `keys.spares` adds 4 spare keys (3); province splitting is
> left out (4); and there is no chamfer (5). §9 records where the build
> differed from the plan.

---

## 1. Where it sits

### The pairwise geometry already exists

`LIB_butterfly_keys` and its harness `UI_butterfly_keys` already join **two**
province STLs with bowtie keys. Everything this plan needs per border is
there and tested:

| Already built (`LIB_butterfly_keys`) | What it does |
|---|---|
| `load_province(stl, geojson)` | reads the STL and places it in metres, from the `.frame.json` sidecar or by footprint reconciliation |
| `find_seam(a, b)` | the shared border, exact or within `seam_tol_mm` |
| `auto_place`, `snap` | keys every `spacing_mm` along a seam; snap a click to the seam |
| `key_polygon`, `check_keys` | the bowtie and its slot; refuses slots that hang off the land or overlap |
| `cut_slots(province, slots)` | cuts slots into the underside with manifold3d; only the part of a slot under that piece is removed |
| `key_solid`, `key_layout` | the printable key, and copies laid out on the bed |
| `assembly(result)` | both pieces and every key in one print-mm frame, for the 3D check |

What is missing is the **map-wide** layer, the same step `UI_border_editor` took
over `UI_shared_border`. A country has a dozen or more borders, and each
province has several neighbours. Every slot from every one of its borders has
to end up in **one** output STL, and the keys from every border have to end up
on the same print plates.

### Pipeline

```
UI_island_editor      ->  Data/JSonProvinces-Cleaned
        |
UI_border_editor      ->  Data/JSonProvinces-Bordered  (+ border_ledger.json)
        |                  seams cleaned and fit-checked (LIB_seam_fit)
        |
UI_terrain_editor     ->  Data/STL/<province>.stl  (+ .frame.json)
        |                  terrain cut by the *bordered* GeoJSON
        |
UI_province_joins (new)  ->  Data/STL-Keyed/<province>_keyed.stl  (+ .frame.json)
   LIB_province_joins      Data/STL-Keyed/butterfly_keys_01.stl, _02 …
   LIB_butterfly_keys      Data/STL-Keyed/join_plan.json
```

These butterfly keys **replace** the border-bar route, they don't add to it.
The border bar (`LIB_border_regions` corridor + `LIB_border_connectors` lobes)
joins provinces *through* a printed bar, so the provinces never touch. With
keys, the provinces meet directly along their cleaned seams. So the terrain has
to be cut by the bordered GeoJSON as saved by the Border Editor, with no
corridor carved out of it. The terrain editor already does this when the
corridor stage is skipped. The join editor checks that it was done (§3.2).

---

## 2. Window layout (`harnesses/UI_province_joins.py`)

This is a root-level app like `UI_border_editor.py`, not a harness, because it
works on the whole map. It has one main screen:

```
+-----------------------------------------------------------------------------+
| [Import Provinces …] [Import STLs …]   Save to: Data/STL-Keyed  [Change …]   |
+---------------------------+-------------------------------------------------+
| Borders                   |                                                 |
|  ● AB | SK   10 keys      |      plan view (matplotlib, EPSG:3347)          |
|  ● AB | BC    9 keys      |      all footprints; seams coloured by status;  |
|  ◐ BC | YT    4 keys, 1 ✗ |      the selected border's keys + slot outlines |
|  ○ NB | NS    —           |                                                 |
|  …                        |                                                 |
| [Auto Place] [Clear]      |                                                 |
| [Zoom to Border]          |                                                 |
+---------------------------+                                                 |
| Key (print-mm)            |                                                 |
|  Neck  Mouth  Reach       |                                                 |
|  Depth Clearance Spacing  +-------------------------------------------------+
| [Undo] [Redo]             |  3D: selected border assembly (vispy), on demand|
+---------------------------+-------------------------------------------------+
| [Cut and Save All …]  ▓▓▓▓▓▓░░░  Cutting Quebec (6 of 11) …                 |
+-----------------------------------------------------------------------------+
```

* **Borders list**: one row per border found between the loaded provinces
  (`LIB_border_ledger.find_borders`). Each row shows the key count, the
  refused count, and a dot: ○ no keys, ◐ has refused keys, ● all keys pass. If
  the border is **not cleaned** according to the ledger, the row says so. Keys
  can still be placed on it.
* **Plan view**: clicking a border selects it. Once a border is selected,
  clicking near its seam adds a key there (`snap`), clicking a key removes it,
  and dragging a key slides it along the seam. Refused keys are drawn red, with
  the reason in a tooltip and on the status line. A key is about 6 mm on a
  province 200 mm across, so **Zoom to Border** is how placement is actually
  done.
* **Key parameters** are map-wide (decision 1). Changing one re-checks every
  border but cuts nothing.
* **Undo/Redo** covers placements, as the Border Editor does for pushes.
* **3D view**: the two pieces of the selected border, slotted, with their keys
  in place (`assembly`). It's cropped to a band around the seam, because a
  whole province pair is about a million triangles to redraw. It's built on
  request, not on every click.
* **Cut and Save All** runs on a `QThread` with per-province progress
  (`BUILDING_UI_HARNESSES` §11.C). Measured: one Alberta/Saskatchewan pair took
  15.6 s to slot.

---

## 3. Hard constraints

### 3.1 Every piece was built under the same settings

`.frame.json` carries `settings_id` (`LIB_terrain_stl.frame_matches_config`). A
piece built with a different scale, base or exaggeration does not meet its
neighbours, and its base sits at a different height from the slot depth. Such a
piece is **refused at import** and named, not keyed.

### 3.2 The STL was cut by the GeoJSON the seam comes from

The footprint check `load_province` does now (IoU ≥ `min_fit_score`, 0.90)
catches the *wrong province*. It doesn't catch a province **re-cleaned after
its STL was built**: a seam moved by up to `max_deviation_mm` (1.5 mm) still
scores far above 0.90. A slot cut there sits up to 1.5 mm off the real edge of
the piece.

So each border gets a **seam agreement** check. It measures how far the STL's
base outline runs from the seam within a band around it (Hausdorff distance, in
print-mm). Anything over `seam_tol_mm` (0.1 mm) marks the border *STL out of
date — rebuild <province>*. The terrain cut is an exact boolean against the
boundary prism, so a matching STL agrees to well under that.

This check works on the STLs already on disk. Adding a boundary fingerprint to
the sidecar would also work, but only for STLs built after that change.

### 3.3 The slot fits inside the base

`LIB_terrain_stl` already refuses `base_mm <= dovetail_mm`. The slot roof is
`base_mm - depth_mm` (1.0 mm at today's 2.0 / 1.0). It prints as a **bridge**
across the slot's narrow dimension, so larger keys need a test print before
they become the default (decision 1).

### 3.4 Slots stay on good land, and clear of each other

`check_keys` refuses a slot that hangs off the pair's land. Map-wide, three more
rules are needed:

1. **Across borders.** A slot may not overlap a slot on *another* border of
   the same province. This mostly matters near triple junctions such as
   AB/SK/NT.
2. **No third province.** The overhang test is measured against the union of
   *this pair* only, so a slot reaching into a third province already counts as
   overhang. Keep it that way, and test for it explicitly.
3. **Narrow land.** A slot may not sit on a `LIB_seam_fit` narrow patch or tab.
   That is land the fit check already called too thin, and a slot in it would
   leave walls under one nozzle width.

### 3.5 Every output is printable

Every keyed STL and key plate has to pass the watertight / positive-volume gate
(FR-4.7), or that file isn't written. If one province fails, the others are
still written and the failure is reported. All keys go on plates that fit
`print.bed_mm - 2 * bed_margin_mm`, using as many plates as it takes.

---

## 4. The library (`lib/LIB_province_joins.py`)

This is pure geometry with no Qt, like everything in `lib/`. It builds on
`LIB_butterfly_keys` and doesn't copy it.

```python
@dataclass
class Piece:
    name: str                      # province name, e.g. "CAN_Alberta"
    geojson: Path
    stl: Path
    province: Province             # LIB_butterfly_keys.Province, loaded once

@dataclass
class Join:
    key: str                       # border_key(a, b), as the ledger spells it
    a: str
    b: str
    seam: MultiLineString          # metres
    cleaned: bool                  # per the ledger, for the list only
    agreement_mm: float            # §3.2; inf if not measured

@dataclass
class JoinPlan:
    params: KeyParams
    keys: dict[str, list[tuple[float, float]]]   # border key -> key centres, metres
    seams: dict[str, str]                         # border key -> seam WKT when placed

    def save(self, path) / load(cls, path)        # join_plan.json

def pair_files(geojson_dir, stl_dir) -> (list[(name, geojson, stl)], list[str])
def load_pieces(pairs, params, progress=None) -> (dict[str, Piece], list[str])
def find_joins(pieces, ledger=None, params=None) -> list[Join]
def auto_plan(joins, params, plan=None, only=None) -> JoinPlan
def check_plan(plan, pieces, joins, params) -> dict[str, list[KeyCheck]]
def cut_all(plan, pieces, joins, params, progress=None, dirty=None) -> JoinResult
def export(result, out_dir) -> list[Path]
```

**Placements are stored as points, not `(part, s_m)`.** A `Placement` is only
meaningful against the exact seam it was made on. The plan stores each key's
centre in metres along with the seam WKT. On load, a border whose seam still
matches (the ledger's `MATCH_TOLERANCE_M`) snaps its points back with `snap`.
A border whose seam has moved keeps its points but is flagged *seam changed —
check keys*, and nothing is silently re-placed. This is the same reasoning the
ledger uses for "cleaned".

**`cut_all` does one boolean per province, not one per border.** It collects
every slot from every join that touches the province and passes them to a
single `cut_slots` call. A slot on the AB/SK border only removes material from
the part under each piece, so the same slot polygon goes to both pieces
unchanged. That is what keeps the two halves of a slot in step. `dirty` limits
a re-run to the provinces whose slot set changed since the last cut.

**`export`** writes these files to the output folder:

* `<province>_keyed.stl`, plus a copy of its `.frame.json`. The frame doesn't
  change, because the slots were cut in it.
* `butterfly_keys_01.stl`, `_02` …: one file per bed plate, with every key plus
  `keys.spares` (decision 3).
* `join_plan.json`: the plan, so the session can be reopened and edited.

### Changes to `LIB_butterfly_keys`

The changes are small and keep the pairwise API and its tests working:

1. **A `keys` config section.** `KeyParams` takes neck, mouth, reach, clearance
   and spacing from `ConnectorParams` today, with only `scale` and `depth_mm`
   read from config. Add `keys.neck_mm`, `keys.mouth_mm`, `keys.reach_mm`,
   `keys.clearance_mm`, `keys.spacing_mm` and `keys.spares` to `_KEYPARAMS_KEYS`
   and `project_config.json`. Their defaults are today's values, so nothing
   moves until someone changes them.
2. **`check_keys(..., avoid=None)`**: slots from other borders, plus narrow or
   tab regions, to refuse against (§3.4), each with its own `reason`.
3. **`key_plates(count, params) -> list[Trimesh]`**: `key_layout` split across
   as many beds as it takes. `key_layout` stays for the harness.

---

## 5. Pairing the files

The STLs and GeoJSONs come from two different tools and aren't named alike.
On disk today:

```
Data/JSonProvinces-Bordered/CAN_Alberta_simplified.geojson
Data/JSonProvinces-Bordered/CAN_British Columbia_simplified.geojson
Data/STL/CAN_British Columbia.stl   (+ .frame.json)
```

`pair_files` matches on the stem after stripping known suffixes (`_simplified`,
`_keyed`, `_trimmed`). Anything left unpaired is listed rather than guessed at.
The list has a *Pair …* action for the rare manual case. Once the Border Editor
writes provinces under their original names, the suffix rule is just a
fallback.

---

## 6. Config

```json
"keys": {
  "neck_mm": 1.6,
  "mouth_mm": 3.2,
  "reach_mm": 3.0,
  "clearance_mm": 0.2,
  "spacing_mm": 14.0,
  "spares": 4
},
"paths": {
  "stl": "Data/STL",
  "stl_keyed": "Data/STL-Keyed"
}
```

`depth_mm` stays `print.dovetail_mm`, because §3.3 ties it to the base. Settings
are passed to the libraries as a `KeyParams`, never read from config inside
them (`load_key_params(*settings_files())`, as every window does now).

---

## 7. Open decisions

1. **Key size.** Today's defaults are the border connector's lobe: a key 3.2 ×
   6.0 × 0.9 mm, with a 1.6 mm neck (four nozzle widths). That was sized to tie
   a province to a 2 mm bar, not to hold two 200 mm pieces together.
   *Options:* (a) keep it and add more keys; (b) bigger map-wide defaults, e.g.
   neck 3 / mouth 6 / reach 5 mm, after a test print checks the slot-roof bridge
   (§3.3); (c) per-border size overrides. **Recommend (b).** One size
   map-wide means any key fits any slot. That is worth more at assembly than
   per-border tuning, which (c) would lose.
2. **Uncleaned borders.** *Options:* allow with a warning, or refuse.
   **Recommend allow.** The seam-agreement and narrow-land checks (§3.2, §3.4)
   catch what matters, and the ledger is about simplification, not fit.
3. **Spare keys.** Keys under 1 cm get lost. **Recommend** a `keys.spares`
   count (default 4) added to the plates.
4. **Provinces larger than the bed.** Nunavut, and probably Quebec north to
   south, exceed the 236 mm usable bed at 1:8,000,000. Nothing splits province
   STLs yet. When something does, the cut between two halves is just another
   seam, and this library could key it too. *Options:* key before splitting
   (the split must avoid slots), or split first and key every seam, including
   the split seams. **Recommend** the second, and leave it out of this build.
5. **Slot-mouth chamfer.** The first layer's elephant's foot narrows the slot
   exactly where the key enters. `clearance_mm` (0.2) covers it on paper.
   **Recommend** waiting for the test print before adding a chamfer.

---

## 8. Build order

1. **`LIB_butterfly_keys` groundwork.** Add the `keys` config section,
   `check_keys(avoid=…)` and `key_plates`, with tests. The existing pair tests
   must pass unchanged.
2. **`LIB_province_joins`, without cutting.** Build `pair_files`,
   `load_pieces`, `find_joins` with seam agreement, `auto_plan`, `check_plan`
   and `JoinPlan` save/load. Test on three synthetic slab provinces meeting at
   a triple junction (cross-border collisions, third-province overhang), plus
   a re-cleaned seam (agreement failure, plan flagged as changed).
3. **`cut_all` + `export`.** Test that every slot half lands on its key in the
   assembly, that each province gets one boolean, that `dirty` re-cuts only
   what changed, that plates fit the bed, and that the outputs are watertight.
4. **`UI_province_joins`: list and plan view.** Import, pairing report, border
   list, click and drag placement, undo/redo. Tests are pytest-qt, as for
   `test_border_editor.py`.
5. **`UI_province_joins`: cut, save, 3D.** The threaded Cut and Save All, the
   cropped assembly view, and reopening `join_plan.json`.
6. **Docs.** README tree and workflow, and a `USING_PROVINCE_JOINS.md`
   covering the end-to-end order: island editor → border editor → terrain
   editor → join editor.

The window was planned as a root-level app with no separate harness; it ended
up as the library's harness instead (§9).

---

## 9. What the build changed

* **The seam-agreement check reads the base rim, not a footprint union.**
  `base_outline` takes the base-triangle edges used only once, straight from
  the faces. On the 414k-face British Columbia STL that is 0.34 s for 9,089 rim
  edges. The footprint union that reconciliation uses would cost seconds.
* **Agreement tolerance is the slot clearance** (never below `seam_tol_mm`).
  A seam off the piece's edge by less than the clearance puts the slot where
  the clearance absorbs it, so that is the line between "fine" and "rebuild".
* **The real data caught a mismatched piece on its first load.**
  `Data/STL/CAN_British Columbia.stl` is 129 × 229 mm, but the bordered British
  Columbia is 192 × 241 mm at 1:8,000,000. It was built from a DEM covering
  only part of the province, and `load_province` refuses it with an overlap of
  0.59. The window lists it as a problem instead of keying it.
* **Key plates skip themselves on re-import.** `stl_files` leaves out
  `butterfly_keys_*.stl`, and `_keyed` reduces to the province name, so the
  output folder can be loaded back as an STL folder.
* **No separate `USING_PROVINCE_JOINS.md`.** The README's Join Editor section
  and the window's own docstring cover the workflow.
* **The window became a harness.** Built at the project root as
  `UI_join_editor.py`, it was moved to `harnesses/UI_province_joins.py`
  (`ProvinceJoinsTester`) to sit with the other lib harnesses, following
  their naming. Its tests are `tests/test_province_joins_ui.py`.
