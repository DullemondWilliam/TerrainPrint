# Planning: `LIB_stress_points` — where a printed piece will break

A design plan for a lib that finds the **load-bearing necks** in a province: the
narrow places that hold two larger pieces of land together, scored by how much
load each one carries, and drawn as a severity gradient over the map.

It is built to be **re-asked cheaply**. The first call hands back the raster it
worked on; a second entry point takes that back with a grid of what changed and
recomputes only the parts an edit can reach. §3 is that design, and the locality
it rests on is proven by measurement, not assumed.

> **Status: built.** `lib/LIB_stress_points.py` and
> `harnesses/UI_stress_points.py` implement this plan, with
> `tests/test_stress_points.py` (44) and `tests/test_stress_points_ui.py` (43).
> This document is kept as the reasoning behind the design; §10 records what the
> build changed, and the built module's own docstring carries the final numbers.
>
> Every number below was measured by prototype against the cleaned GeoJSON in
> `Data/JSonProvinces-Cleaned/` at 1:8,000,000, not estimated — the measurements
> are labelled where they appear, and the few figures that are derived rather
> than measured say so.

---

## 1. The question, and why the existing libs cannot answer it

`LIB_thin_features` answers *where is the land narrower than W?* That is not the
same question as *where will it break*, and the gap between them is the whole
reason for a new lib:

| | `thin_features` finds it | Will it break? |
|---|---|---|
| A 0.4 mm spit off a headland | yes | no — it carries nothing but itself |
| A sliver at a bevelled corner | yes | no — not a width problem at all |
| A 0.2 mm neck holding 54 mm² of Nova Scotia | yes, as one patch among 338 | **yes, first thing** |
| A 1.0 mm neck holding 465 mm² of Nunavut | **no** — 1.0 mm passes the width test | yes, it is marginal |

Nova Scotia reports 338 thin patches at a 1 mm width. Exactly **one** of them is
a stress point. The signal is there and buried — and the last row is worse: a
neck can be wide enough to pass a width test and still be the wrong width for
what hangs off it. Width is a property of a place; stress is a property of a
place **and everything it holds up**. That is what this lib adds.

`LIB_island_bridges` already does the load half of this reasoning, for land that
is *not* joined yet — "take the tree, cut one edge, and the map falls into two:
the lighter half is the branch that edge carries" (`_branch_masses`). This lib is
that idea turned on land that is **already** joined: the necks are the edges, and
nobody chose them.

### Where it sits

```
cleaned province GeoJSON
   |
   +-- LIB_thin_features ...... is anything too narrow to print?    (built)
   +-- LIB_island_bridges ..... what should be joined, and how?     (built)
   +-- LIB_stress_points ...... what will snap when it is handled?  (this plan)
            |
            v
      StressReport + StressGrid ---- UI_stress_points overlay (severity gradient)
            |            ^                   |
            |            +-------------------+  the grid comes back for the next edit
            |
            +--> act: widen the neck (LIB_island_bridges, wider merge buffer)
                 or accept it and print the piece in two parts
```

It **only measures**. Nothing here edits geometry, for the same reason
`thin_features` does not: the repair is a judgement — widen a 0.2 mm isthmus and
you have redrawn the coast — and it belongs to the caller.

---

## 2. The core decision: a width field and a merge tree, not a threshold

The naive approach — threshold the land at W, see what the narrow patches touch —
was prototyped and **fails on the definition**, measured: with lobes defined as
land at least 3 mm wide, Nova Scotia's real 0.20 mm neck vanishes, because the
54 mm² peninsula it holds is nowhere 3 mm wide and so is not a "larger area" by
that test. It is a larger area by the only test that matters, which is **area**.
A threshold on width cannot see that; it also forces the caller to pick W, and
the answer then changes with the pick.

So the width is not an input. It is the output.

**Step 1 — the width field.** Rasterise the land at `pixel_mm` and take a
Euclidean distance transform; the local width at a pixel is `2 × distance to the
nearest water`. One `scipy.ndimage.distance_transform_edt` call gives a
continuous width everywhere, which is both the detector's input and the gradient
the overlay wants. Measured: 0.01 s for Nova Scotia, 0.15 s for British
Columbia, 0.52 s for Nunavut at a 0.1 mm pixel. The rasterised area matches the
vector area to the digit (Nova Scotia, 889 mm² both ways), so the grid is not
costing accuracy at this pixel.

**Step 2 — flood the land from the thick places down.** Add land to a union-find
in **descending width order**. Every time an addition joins two existing
components, that is a neck, and it arrives with everything the score needs,
exactly:

* its **throat width** — the width at which the gap closed, which is by
  construction the widest cross-section that still severs the two sides;
* the **two masses** — the printed area of each side, accumulated from everything
  that has drained into it;
* its **location**, for the throat bar and the zoom-to.

No threshold is chosen, every neck in the province is found with its own width,
and the ordering is total: a neck is reported once, at the width where it is
genuinely the bottleneck.

**Why not cut the land at the throat width afterwards and measure the two sides?**
Prototyped; it is wrong, measured. A width threshold is global, so cutting Nova
Scotia at 2.15 mm severs every *other* narrow place too, and the pieces stop
corresponding to the neck being asked about: 528.9 / 12.8 mm² from the global cut
against 474.7 / 51.8 mm² from the flood, which is the honest pair. The flood's
accumulated catchments are the right quantity.

### Collapse the thick land, and do the flood in C

Merges that happen in land wider than a few millimetres are not stress points —
there is no printing problem at a 50 mm throat, and British Columbia's flood duly
reports one at 54.6 mm. So land at or above `ceiling_mm` is **collapsed to one
node per connected lobe**, carrying its full area, and only the narrow land is
flooded. The result is identical for every neck below the ceiling.

Flooding one pixel at a time in Python then costs 0.22 s for Nova Scotia, 0.98 s
for British Columbia and 4.55 s for Nunavut (measured) — the narrow land is 45 k,
197 k and 896 k pixels. Two ways round it were measured:

* **Band the descent over the whole grid** (`ndimage.label` per band): no better,
  6.5 s / 13.5 s, because every band pays for a full-grid labelling.
* **Compress to a graph and band that.** Nodes are the lobes plus the narrow
  pixels, edges are 4-adjacency weighted by `min(width)` — the width at which the
  edge activates. Sort the edges once, and each band is a prefix, so
  `csgraph.connected_components` does the descent in C. Measured: Nunavut
  3.54 s total, of which 2.62 s is the descent — against 5.2 s. This is the
  implementation to build.

`band_mm` is an accuracy knob, not just a speed one: at 0.1 mm bands British
Columbia's two 0.40 mm necks collapse into one event and the province reports 3
necks instead of 4. **Default 0.05 mm**, with a test that asserts all four.

---

## 3. Reuse and incremental update

### Where the time actually goes

Measured on Nunavut, the worst province (10.1 Mpx grid, 3.05 Mpx of land):

| stage | cost | share |
|---|---|---|
| read GeoJSON + reproject | 0.23 s | 4% |
| **rasterise** | **0.10 s** | **2%** |
| width field (EDT) | 0.52 s | 10% |
| lobe + patch labelling, bincounts | 0.07 s | 1% |
| the flood | 2.6 s (banded) – 4.6 s (per pixel) | 83% |

So the burn itself is 2% and caching it alone would buy nothing. What has to come
back from the first call is the **derived** raster state — the width field above
all — and that is what `StressGrid` carries. The rest of this section is the two
locality results that make an update cheap, and the three attempts that failed.

### The width field is exactly local — measured bit-exact

**Claim.** Clamped at `ceiling_mm`, an edit confined to a region R changes the
width field only within `dilate(R, ceiling_mm/2)`.

**Why.** A pixel whose clamped width is below the ceiling has water within
`ceiling/2` of it. If nothing flipped between land and water within `ceiling/2`
of that pixel, its nearest water is the same water as before, so its distance is
unchanged; and if its clamped width is *at* the ceiling, the only thing the
analysis asks of it is that it is thick, which is the same statement.

**Two radii, not one.** The crop cannot see water beyond its own edge, so it must
be padded further than the region spliced back: **compute on R dilated by
`ceiling_mm`, splice the inner `ceiling_mm/2`.** With a single `ceiling/2` pad the
error inside the window reaches 0.54 mm (measured, Nova Scotia) — a 5-pixel lie
exactly where the answer is being read.

**Measured.** Over 8 brush-sized edits per province, the spliced field is
**bit-exact** against a full recomputation — worst error 0.000000 mm on all three:

| Province | brush | full field | spliced | speed-up |
|---|---|---|---|---|
| Nova Scotia | 0.125 mm | 13.4 ms | 0.44 ms | 30× |
| British Columbia | 0.25 mm | 140.6 ms | 4.13 ms | 34× |
| Nunavut | 0.5 mm | 518.7 ms | 13.6 ms | 38× |

`0.125 mm` is `island_editor.brush_mm`, which is the edit this is really for.

### Three locality attempts that failed, and why

Recorded because they look right and each cost a prototype:

1. **Cache per narrow patch.** Dead on arrival: **the narrow land is one patch.**
   Every coastline pixel is narrow, so the fringe is connected the whole way
   round. Measured: Nova Scotia has 44,661 of its 44,690 narrow pixels in a
   single component; British Columbia 154,215 of 196,588; Nunavut's top three
   patches hold 640 k of 896 k. A patch is not a local unit and a brush stroke
   anywhere dirties the big one.
2. **Contract equal-width plateaus** (adjacent pixels of identical width enter
   the flood together, so collapsing them is exact). Measured gain: **1.2×**.
   Nunavut 896 k nodes → 734 k. Exact Euclidean distances are too varied to
   collapse; the idea only pays on a quantised field.
3. **Dynamic MST.** The merge tree follows from the maximum spanning tree, and an
   edit only changes edge weights inside the window — but lowering the weight of
   a *tree* edge invokes the cut rule, which needs the best edge crossing a cut
   whose other side can be the whole province. Not local.

### The flood is tileable — the reduction is measured

The flood depends on the narrow land only through **bottleneck connectivity**: for
any two lobes, the width of the widest path between them. That composes over a
partition, because any path between two tiles must cross a cell on their shared
boundary.

So tile the grid, and summarise each tile once as a tree whose **terminals** are
the narrow cells on the tile's boundary plus the lobes it touches, each carrying
the mass that joins it and the width at which that mass arrives. Reassembly is
the same band descent of §2 run over the union of the summaries. On an edit only
the tiles meeting the dirty window are re-summarised; every other summary is
reused untouched.

**Measured reduction** — boundary terminals against narrow pixels:

| Province | narrow px | 64 px tiles | 128 px tiles | 256 px tiles |
|---|---|---|---|---|
| Nova Scotia | 45 k | 2.6 k | 1.5 k | 0.4 k |
| British Columbia | 197 k | 12.0 k | 5.7 k | 2.7 k |
| Nunavut | 896 k | 56.1 k | 27.6 k | 12.7 k |

At 128 px the reassembly graph is **3.1%** of the pixel graph on all three
provinces. And the reassembly at those sizes, measured directly (60 bands):

| graph | 2.6 k | 12.7 k | 27.6 k | 56.1 k |
|---|---|---|---|---|
| band descent | 19 ms | 50 ms | 106 ms | 228 ms |

So 128 px tiles put Nunavut's whole reassembly at **0.106 s**. 256 px halves that
again but quadruples the cost of re-summarising a dirty tile; 128 px is the
recommended default, and `tile_px` is a config key because this trade is
measurable per province and nobody should have to guess.

### The budget after an edit

Nunavut, a 0.5 mm brush stroke, 128 px tiles:

| stage | full run | after an edit | |
|---|---|---|---|
| rasterise | 0.10 s | re-burn the dirty window | |
| width field | 0.52 s | **0.014 s** | measured, bit-exact |
| labelling + bincounts | 0.07 s | 0.07 s | full grid, C, left alone |
| the flood | 2.6 s | ≤4 tiles × ~16 ms + 0.106 s | *derived* |
| scoring | negligible | negligible | |
| **total** | **~3.5 s** | **~0.25 s** | |

The per-tile figure is **derived, not measured**: Nunavut averages 3.2 k narrow
pixels per live tile, and the measured flood rate is 4.55 s / 896 k px =
5.1 µs/px. A 35×35 px dirty window spans at most four 128 px tiles. The build has
to confirm it; it is the one number in this document standing on arithmetic.

0.25 s is not a draw-loop figure and this plan does not pretend otherwise. It is a
figure you can hang on a brush release, which is what the island editor needs.

### The dirty grid is a contract, and it can be got wrong

`dirty` must be a **conservative superset** of the cells whose land/water state
changed. A superset costs time; a subset gives a confidently wrong answer, which
is the one failure this lib must not have — the whole value of the report is that
it is believed. Three defences:

* **The caller does not have to produce one.** Pass the new land mask and
  `restress` derives `dirty` by XOR against the cached one: correct by
  construction, and one array XOR is nothing next to the rest of this.
* **`StressParams.verify`** recomputes from scratch and asserts the necks match.
  Off by default, on in the tests, available to anyone debugging a stale overlay.
* **A full rebuild is always legal and is reported.** `report.rebuilt` is True
  when the grid could not be reused, so a caller never silently gets a slow path
  or a stale one.

A rebuild is forced by: a different `pixel_mm`, `ceiling_mm` or `tile_px` (the
summaries are keyed on all three); a different transform; a change that reaches
outside the grid's extent; or a `dirty` that cannot be reconciled with the land
handed in. The grid is therefore built with a **margin** so that growing the land
a little does not immediately throw the cache away.

### Memory

Held per province, Nunavut being the worst: the land mask as packed bits is
1.3 MB; the narrow width stored sparsely (index plus `uint16` hundredths of a
millimetre, clamped at the ceiling) is 5.4 MB over its 896 k narrow pixels; the
tile summaries are small. **~8 MB**, against 40 MB for a dense `float32` field.
Worth saying because the island editor has undo/redo: the grid must be shared
copy-on-write across undo states, not snapshotted per step.

---

## 4. The stress model, calibrated off a number this project already stands behind

A printed piece is a flat slab of one thickness `t` of one material, so a neck is
a **beam of cross-section `w × t`**, and mass is printed **area** — the same
convention `LIB_island_bridges` states, for the same reason.

Two failure modes, and they are not equally interesting:

* **Out of plane** — the sheet sags as it is lifted. Section modulus `w·t²/6`, so
  stress goes as `1/w`. A thicker base fixes it and the neck barely matters.
* **In plane** — a peninsula swings sideways and snaps at the waist. Section
  modulus `t·w²/6`, so stress goes as **`1/w²`**.

The in-plane mode governs, and it is the one worth drawing: it is quadratic in
the one quantity the map can show and the user can change.

```
moment     M  ∝  load_mm2 × lever_mm
capacity   S  ∝  t × w²
severity      =  needed_mm / throat_mm
```

**The calibration comes from `bridges.mass_per_bridge_mm2`.** That key already
says, in this project's own voice, that **1.0 mm of causeway holds 400 mm² of
map** — it is what `_bridges_for` divides by to decide a join needs a second
bridge. Reusing it means the new lib agrees with the bridge planner instead of
inventing a second opinion about the same physics:

```
capacity_mm2(w) = mass_per_bridge_mm2 × (w / bridges.width_mm)²
needed_mm       = bridges.width_mm × sqrt(load_mm2 / mass_per_bridge_mm2)
severity        = needed_mm / throat_mm
```

`severity` is dimensionless and anchored: **1.0 is marginal**, above 1 the neck
is thinner than its load wants, below 1 it is sound. A neck at severity 2 needs
twice the width it has. That anchor is what makes a colour gradient *mean*
something rather than merely rank.

The lever arm enters as a dimensionless **slenderness**, `lever_mm /
sqrt(load_mm2)`, normalised so a compact blob sitting against the neck
(`1/√π ≈ 0.564`) scores 1.0 and never reduces the load below the bridge
calibration. A long peninsula scores more.

### What the model says about real provinces — measured

All 21 necks found across Nova Scotia, British Columbia, Nunavut and Newfoundland
& Labrador, worst first. `sev₀` ignores the lever arm, `sev₁` includes it:

| Province | throat mm | holds mm² | lever mm | slender | needed mm | sev₀ | sev₁ |
|---|---|---|---|---|---|---|---|
| Nunavut | 0.28 | 367.9 | 20.1 | 1.86 | 0.96 | **3.39** | 4.62 |
| Nunavut | 0.20 | 110.9 | 10.1 | 1.70 | 0.53 | **2.63** | 3.43 |
| Nova Scotia | 0.20 | 53.9 | 6.6 | 1.60 | 0.37 | **1.84** | 2.32 |
| Nunavut | 1.00 | 465.2 | 18.5 | 1.52 | 1.08 | **1.08** | 1.33 |
| British Columbia | 0.40 | 31.9 | 7.1 | 2.22 | 0.28 | 0.71 | 1.05 |
| British Columbia | 0.40 | 29.2 | 5.5 | 1.79 | 0.27 | 0.67 | 0.90 |
| Nunavut | 0.60 | 61.9 | 7.9 | 1.77 | 0.39 | 0.66 | 0.87 |
| Newfoundland | 0.89 | 106.9 | 8.1 | 1.39 | 0.52 | 0.58 | 0.68 |
| … 13 more, all below 0.6 | | | | | | | |

Three things to read off it:

1. **It discriminates.** 4 necks of 21 are over the line by mass alone, 5 with
   the lever arm. A report that flagged everything would be the thin-patch
   problem over again.
2. **The ranking is robust to the lever arm.** The top four are the same either
   way; the arm promotes exactly one British Columbia neck across 1.0. So the
   lever is worth having and is not load-bearing for the conclusions — it is a
   config key, not a crisis.
3. **Nunavut's 1.00 mm neck holding 465 mm² is the result that justifies the
   lib.** It passes `thin.width_mm` cleanly. It is still marginal, and nothing in
   the repo today can say so.

---

## 5. The API

Shaped like `ThinParams`/`ThinReport`, which is the pattern for a measuring lib
here: print-mm throughout, converted through `print.scale` at the moment of use,
geometry in EPSG:3347 — with the raster state added as a returned, re-feedable
object.

```python
@dataclass
class StressParams:
    scale: float = cfg("print.scale")
    pixel_mm: float = cfg("stress.pixel_mm")          # 0.1 — the width resolution
    ceiling_mm: float = cfg("stress.ceiling_mm")      # 3.0 — above this, not a neck
    band_mm: float = cfg("stress.band_mm")            # 0.05 — throat resolution
    tile_px: int = cfg("stress.tile_px")              # 128 — the reuse unit
    min_lobe_mm2: float = cfg("stress.min_lobe_mm2")  # 25.0 — "a larger area"
    margin_mm: float = cfg("stress.margin_mm")        # 5.0 — room to grow before a rebuild
    thickness_mm: float = cfg("print.border_thickness_mm")
    capacity_mm2: float = cfg("bridges.mass_per_bridge_mm2")   # 400 — the anchor
    capacity_at_mm: float = cfg("bridges.width_mm")            # 1.0
    lever_power: float = cfg("stress.lever_power")    # 1.0 — 0 disables the arm
    report_above: float = cfg("stress.report_above")  # 0.0 — severity floor
    verify: bool = False                              # recompute fully and assert
    work_crs: str = cfg("crs.working")

@dataclass
class StressGrid:
    """The raster and everything derived from it, for the next call to reuse."""
    transform: object           # Affine; fixed for the grid's life
    shape: tuple
    land: object                # packed bits
    narrow: object              # sparse width, uint16 hundredths of a mm, clamped
    tiles: dict                 # tile key -> cached summary
    pixel_mm: float             # what was used, after any coarsening
    ceiling_mm: float
    tile_px: int
    bounds: tuple               # including the margin
    def covers(self, geom) -> bool: ...
    def dirty_from(self, land_after) -> object: ...   # XOR: a superset by construction

@dataclass
class Neck:
    point: Point          # where it closes — the zoom-to, and the bar's centre
    throat: LineString    # the bar: throat_mm long, across the neck
    region: object        # the narrow patch, for shading
    branch: object        # the land it holds, for "show me what falls off"
    throat_mm: float
    load_mm2: float       # the lighter side's printed area
    other_mm2: float
    lever_mm: float
    needed_mm: float      # the width this neck wants
    severity: float       # needed / throat; 1.0 is marginal
    @property
    def at_risk(self) -> bool: ...        # severity >= 1

@dataclass
class StressReport:
    necks: list                 # worst first
    grid: StressGrid            # hand this back to restress()
    measured: bool = True       # False when the geometry defeated it, even repaired
    repaired: bool = False      # make_valid was needed
    rebuilt: bool = True        # the grid was built from scratch, not reused
    tiles_recomputed: int = 0   # how much of the cache an edit actually cost
    land_area_mm2: float = 0.0
    @property
    def worst(self) -> float: ...
    @property
    def sound(self) -> bool: ...          # measured, and nothing at risk
```

Three functions:

```python
stress_points(geom, params=None) -> StressReport
    # the whole analysis; report.grid is the raster state to keep

restress(grid, land=None, geom=None, dirty=None, params=None) -> StressReport
    # the new land state as a mask or as geometry, plus optionally which cells
    # changed.  dirty=None derives it by XOR.  Falls back to a full rebuild when
    # the grid cannot be reused, and says so in report.rebuilt.

severity_field(report) -> (array, transform)
    # the gradient the overlay draws, straight off the cached grid
```

**`measured` is not optional.** Copied deliberately from `thin_features`: real
GeoJSON self-intersects, so `make_valid` and retry, and if it still fails the
report comes back `measured=False` rather than clean. "I could not tell" and
"nothing will break" are different answers, and this report is only worth having
if it is believed.

**Coarsening, honestly reported.** A `MAX_PIXELS` cap as in
`LIB_shape_scores._grid`: grow the pixel rather than refuse the grid, and record
in `pixel_mm` what was used. A coarser pixel raises the smallest throat the field
can resolve, so the report has to say which pixel produced it.

---

## 6. The overlay and the gradient

Three layers over the existing map, each answering a different question:

1. **The width field**, continuous, faint, under everything — *where is the land
   narrow at all?* The distance transform through `imshow`, with the land as the
   alpha mask. It costs nothing extra: it is in the grid already, which is also
   what makes a redraw after an edit cheap.
2. **The necks**, each narrow patch filled by its **severity** on a sequential
   ramp with **1.0 anchored at the ramp's midpoint** — so the colour reads as
   sound / marginal / at-risk rather than merely dark / light. This is the layer
   the request is about. Clipped at a `vmax` (2.0 or so) so one Nunavut outlier
   does not flatten everything else to the same colour.
3. **The throat bar**, a short line across the neck at the breaking point, plus a
   severity label on the top N. Essential, not decoration: a 0.2 mm neck is
   sub-pixel at the zoom a whole province is read at. `UI_province_viewer` already
   hit this and solved it with a stroke width and a zoom-to-worst button; the
   same answer applies here.

Colour choice follows the `dataviz` skill when the overlay is written, with one
constraint from this repo: the harness palette in
`docs/BUILDING_UI_HARNESSES.md` §3 already spends red on *marked for removal*,
and `UI_province_viewer` spends a saturated red on *too thin*. A severity ramp
must not read as a third meaning for the same red — so the ramp wants to end
somewhere clearly "hot" without colliding with `MARK_COLOUR`, decided with the
two windows side by side.

Interaction, mirroring the viewer's `Show thin` group:

* **Find Stress Points** — threaded, progress bar, status naming the next step.
* **Min area (mm²)** and **Ceiling (mm)** spinboxes — live off the cached report
  where they only re-filter; `min_lobe_mm2` and `report_above` re-filter, while
  `ceiling_mm` invalidates the tile summaries and re-runs.
* **Worst** — zoom to the highest-severity neck; the whole point of the tool.
* **Show branch** — highlight the land a selected neck holds. "This 0.2 mm neck
  holds *that*" is the sentence the overlay exists to say, and it is more
  convincing than a number.
* A one-line verdict: `3 necks at risk — worst 0.28 mm holding 368 mm² (wants
  0.96 mm)`. Never a bare count.
* After a re-run, the status says what it cost: `re-checked 3 tiles in 0.24 s`.
  The incremental path is invisible when it works and that is exactly when a
  stale overlay would go unnoticed, so it reports itself.

The harness should also carry a **brush**, or the incremental path has nothing to
be driven by. The simplest honest version: a drag that flips cells, feeding
`restress` on release — which is the island editor's gesture in miniature, and
the thing worth proving before the editor depends on it.

---

## 7. Config

A new `stress` block, beside `thin`:

```json
"stress": {
  "pixel_mm": 0.1,
  "ceiling_mm": 3.0,
  "band_mm": 0.05,
  "tile_px": 128,
  "min_lobe_mm2": 25.0,
  "margin_mm": 5.0,
  "lever_power": 1.0,
  "report_above": 0.0
}
```

`capacity_mm2` and `capacity_at_mm` are deliberately **not** new keys — they read
`bridges.mass_per_bridge_mm2` and `bridges.width_mm`, so retuning the bridge
planner's idea of what 1 mm holds retunes this lib with it. One opinion about the
physics, in one place.

---

## 8. Open decisions

1. **`lever_power` default: 1.0 or 0.0?** Measured in §4: the top four necks are
   the same either way, and one more crosses 1.0 with the arm in. 1.0 is the
   physics and the numbers say it is low-risk — **recommend 1.0**, reconsider if a
   province shows the arm promoting something visibly fine.
2. **Which thickness?** `print.border_thickness_mm` (2.0) and `terrain.base_mm`
   (2.0) are the same number today, so the choice is free and will not stay free.
   Default to the border thickness, let the caller pass it.
3. **Is the labelling worth making local too?** It is 0.07 s of a 0.25 s edit —
   the largest remaining fixed cost once the flood is tiled. Connected components
   can split under an erase, which is the hard direction, so leave it global until
   0.07 s is the thing in the way.
4. **Does the width field belong in `LIB_thin_features` instead?** The distance
   transform would serve both, and `thin_features`' opening is exact where a
   raster is not. Recommend keeping them apart: `thin_features` is vector and
   exact and must stay so; this lib is raster by nature. Revisit only if a third
   caller wants the field.
5. **Lakes.** A neck between two lakes is a real neck, and nothing special is
   needed to find it — a lake is a hole, and the distance transform sees its shore
   like any other. But the report should probably say when a neck's water is
   interior, because widening it means filling a lake, which is a different edit.
6. **Province viewer and island editor integration.** `UI_province_viewer` is
   where the printing verdict for the whole country lives, and `UI_island_editor`
   is what the incremental path is built for. Both are out of scope for the first
   build: get the lib, the grid contract and the harness right first.

---

## 9. Build order

The width-field splice and the tiling ship **together**. The splice alone takes
Nunavut from 3.5 s to 3.0 s, which nobody would notice; the tiling is where the
order of magnitude is, and the splice is what makes the tiling's input cheap.

1. `lib/LIB_stress_points.py` — grid, width field, lobe collapse, the compressed
   graph, the banded descent, scoring. `tests/test_stress_points.py` on synthetic
   shapes where the answer is known by construction: a dumbbell with a 0.5 mm bar
   (one neck, one throat, both masses exact), the same bar at 1.5 mm (sound), a
   plain square (no necks — the corner-sliver trap `thin_features` had to fix with
   mitre joins), a spit (narrow, carries nothing, must **not** be reported), two
   blobs joined twice (a cycle: the load is shared, not doubled), and an invalid
   self-intersecting ring (`measured` False, or repaired and flagged).
2. Geometry extraction for the overlay — throat bar, region, branch — tested for
   the bar's length against the throat and the branch's area against `load_mm2`.
3. Regression on real data: the four provinces measured here, asserting the known
   necks are found with their throats and masses. The table in §4 is the fixture,
   and the four British Columbia necks are the `band_mm` regression.
4. `StressGrid` and `restress`. **The test that matters is equality, not speed:**
   for a few hundred random brush edits per province, the incremental necks must
   equal a from-scratch run exactly. `verify=True` is that test's mechanism, and
   the same switch ships for anyone debugging a stale overlay. Then assert the
   rebuild triggers fire — pixel change, transform change, an edit off the edge.
5. `harnesses/UI_stress_points.py` — the standard skeleton, threaded run, the
   three layers, Worst, Show branch, and a brush to exercise `restress`.
   `tests/test_stress_points_ui.py` following `test_island_bridges_ui.py`.
6. `docs/USING_STRESS_POINTS.md` once the behaviour is real, with the measured
   edit budget, and a status note at the top of this file.


---

## 10. What the build changed

The plan survived contact, with four corrections worth recording. Each came from
measurement, and each is pinned by a test.

**The band default is 0.025 mm, not 0.05 mm.** §2 called `band_mm` an accuracy
knob and guessed the accuracy was free. It is not: two joins landing in the same
band are simultaneous, so one neck's load can be split across both. At 0.05 mm
British Columbia reports three necks where the pixel-by-pixel flood finds four,
the fourth losing its load to a sibling and falling under `min_lobe_mm2`. 0.025 mm
finds all four with their right masses and costs about half as much again; 0.01 mm
buys nothing and costs double.

**The load is measured by cutting the neck, not by thresholding the province.**
The plan weighed the branch as the component standing above the throat width, on
the grounds that it is what the descent reports and is exactly reproducible.
Both true, and it undercounts, because the branch's own coastal fringe is
narrower than the throat and so is left out. Two 36 mm² squares on a 1.6 mm bar
came out at 20 mm² each — under the floor, so the neck vanished on mass it
demonstrably has. The load is now the land severed by removing the neck's own
blob. That does not always part the two sides (five of Nunavut's fourteen necks
part, nine do not), so `Neck.severed` says which measurement is being read and
the harness prints the caveat.

**The region doubles as the cut, which fixed its size.** §6 wanted the shaded
blob big enough to see. It is also the cut the load is weighed by, and the tight
job wins: at a 1.5× reach with a width tolerance the cut ate the ends it was
supposed to be weighing. Reach 2.5× and 4× were measured and are worse, not
better. The blob is now the waist and the *bar* carries the visibility, in screen
units.

**The costs.** The plan's ~0.25 s for a Nunavut edit was optimistic: it counted
the tile work and the reassembly and nothing else. The built figures, at the
0.025 mm band the answer actually needs:

| Province | grid | full | an edit | tiles redone | held |
|---|---|---|---|---|---|
| Nova Scotia | 0.5 Mpx | 0.36 s | 87 ms | 1.2 / 35 | 2 MB |
| Newfoundland | 2.7 Mpx | 1.49 s | 229 ms | 2.2 / 180 | 12 MB |
| British Columbia | 3.3 Mpx | 1.74 s | 243 ms | 2.0 / 209 | 15 MB |
| Nunavut | 10.7 Mpx | 6.72 s | 979 ms | 1.5 / 675 | 51 MB |

An edit is a fifth to a seventh of a full run rather than the twentieth the plan
hoped for, and the memory is the dense `uint16` level field rather than the
sparse 8 MB §3 sketched — simpler, and worth the difference. Two measured
decisions got it there: the branch search grows a window from the seed instead of
labelling the province (the held side is nearly always small), and joins are
dropped during the descent on an **upper** bound of their mass, so the pre-filter
cannot lose a neck the cut would have rescued.

**What held exactly.** The two locality results the plan rests on both survived:
the spliced width field is bit-exact against a full pass, and the incremental
necks equal a from-scratch run edit after edit — which is what
`TestRestressAgreesWithAFullRun` asserts, and what `verify=True` checks from
inside.
