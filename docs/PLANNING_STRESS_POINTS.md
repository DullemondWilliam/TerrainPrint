# Planning: `LIB_stress_points` — where a printed piece will break

A design plan for a lib that finds the **load-bearing necks** in a province: the
narrow places that hold two larger pieces of land together, scored by how much
load each one carries, and drawn as a severity gradient over the map.

> **Status: planned.** Nothing is built. Every number below was measured by
> prototype against the cleaned GeoJSON in `Data/JSonProvinces-Cleaned/` at
> 1:8,000,000, not estimated — the measurements are labelled where they appear.

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
      StressReport ---- UI_stress_points overlay (severity gradient)
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
the overlay wants. Measured: 0.02 s for Nova Scotia, 0.15 s for British
Columbia, 0.53 s for Nunavut at a 0.1 mm pixel. The rasterised area matches the
vector area to the digit (Nova Scotia, 889 mm² both ways), so the grid is not
costing accuracy at this pixel.

**Step 2 — flood the land from the thick places down.** Add land pixels to a
union-find in **descending width order**. Every time adding a pixel joins two
existing components, that is a neck, and it arrives with everything the score
needs, exactly:

* its **throat width** — the width at the pixel that closed the gap, which is by
  construction the widest cross-section that still severs the two sides;
* the **two masses** — the printed area of each side, accumulated from every
  pixel that has drained into it;
* its **location**, for the throat bar and the zoom-to.

No threshold is chosen, every neck in the province is found with its own width,
and the ordering is total: a neck is reported once, at the width where it is
genuinely the bottleneck.

**Why not cut the land at the throat width afterwards and measure the two sides?**
Prototyped; it is wrong, measured. A width threshold is global, so cutting Nova
Scotia at 2.15 mm severs every *other* narrow place too, and the pieces stop
corresponding to the neck being asked about: 528.9 / 12.8 mm² from the global cut
against 474.7 / 51.8 mm² from the flood, which is the honest pair. The flood's
accumulated catchments are the right quantity. Where an exact per-neck figure is
wanted, the cut must be **local** — remove the throat pixels at that one neck and
label — and that runs per reported neck, of which there are a handful.

### Making it fast enough: collapse the thick land

Flooding one pixel at a time in Python costs 6.9 s for British Columbia and
13.5 s for Nunavut (measured) — too slow for a button. A full-grid banded scan is
no better (6.5 s / 13.5 s), because the cost there is `ndimage.label` over the
whole grid once per band.

The fix follows from what the answer needs. Merges that happen in land wider than
a few millimetres are not stress points — there is no printing problem at a 50 mm
throat, and British Columbia's flood duly reports one at 54.6 mm. So land at or
above `ceiling_mm` can be **collapsed to one node per connected lobe**, carrying
its full pixel count, and only the narrow land is flooded pixel by pixel. The
result is identical for every neck below the ceiling, and the cost falls to the
narrow land alone:

| Province | grid | land | narrow (<3 mm) | width field | flood | necks found |
|---|---|---|---|---|---|---|
| Nova Scotia | 0.33 Mpx | 0.09 Mpx | 45 k (50%) | 0.02 s | 0.22 s | 2 |
| British Columbia | 2.97 Mpx | 1.43 Mpx | 197 k (14%) | 0.15 s | 0.98 s | 4 |
| Nunavut | 10.07 Mpx | 3.05 Mpx | 896 k (29%) | 0.53 s | 4.55 s | 13 |

All measured at `pixel_mm=0.1`, `ceiling_mm=3.0`, `min_lobe_mm2=25`. Nunavut's
223 islands are the worst case at ~5 s; dropping the ceiling to 2.0 mm takes it
to 3.9 s and loses only necks already scoring under 0.3 severity. Verified
against the one-pixel-at-a-time flood: same necks, same throats, same masses.

**Cost class: threaded, with a progress bar** — `UI_border_regions` is the
precedent. Too slow for a draw loop, so the harness and any viewer must cache the
report keyed on the geometry and the parameters, exactly as `UI_province_viewer`
already caches its `ThinReport`.

---

## 3. The stress model, calibrated off a number this project already stands behind

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

## 4. The API

Shaped like `ThinParams`/`ThinReport`, which is the pattern for a measuring lib
here: print-mm throughout, converted through `print.scale` at the moment of use,
geometry in EPSG:3347.

```python
@dataclass
class StressParams:
    scale: float = cfg("print.scale")
    pixel_mm: float = cfg("stress.pixel_mm")          # 0.1 — the width resolution
    ceiling_mm: float = cfg("stress.ceiling_mm")      # 3.0 — above this, not a neck
    min_lobe_mm2: float = cfg("stress.min_lobe_mm2")  # 25.0 — "a larger area"
    thickness_mm: float = cfg("print.border_thickness_mm")
    capacity_mm2: float = cfg("bridges.mass_per_bridge_mm2")   # 400 — the anchor
    capacity_at_mm: float = cfg("bridges.width_mm")            # 1.0
    lever_power: float = cfg("stress.lever_power")    # 1.0 — 0 disables the arm
    report_above: float = cfg("stress.report_above")  # 0.0 — severity floor
    work_crs: str = cfg("crs.working")

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
    width: object               # the width field, for the gradient (array + transform)
    measured: bool = True       # False when the geometry defeated it, even repaired
    repaired: bool = False      # make_valid was needed
    pixel_mm: float = 0.0       # what was actually used, after any coarsening
    land_area_mm2: float = 0.0
    @property
    def worst(self) -> float: ...
    @property
    def sound(self) -> bool: ...          # measured, and nothing at risk
```

Two functions:

```python
stress_points(geom, params=None) -> StressReport     # the whole analysis
severity_field(report) -> (array, transform)         # the gradient the overlay draws
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

## 5. The overlay and the gradient

Three layers over the existing map, each answering a different question:

1. **The width field**, continuous, faint, under everything — *where is the land
   narrow at all?* The distance transform through `imshow`, with the land as the
   alpha mask. It costs nothing extra; it is already computed.
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
  where they only re-filter, re-running only when they change the field.
* **Worst** — zoom to the highest-severity neck; the whole point of the tool.
* **Show branch** — highlight the land a selected neck holds. "This 0.2 mm neck
  holds *that*" is the sentence the overlay exists to say, and it is more
  convincing than a number.
* A one-line verdict: `3 necks at risk — worst 0.28 mm holding 368 mm² (wants
  0.96 mm)`. Never a bare count.

---

## 6. Config

A new `stress` block, beside `thin`:

```json
"stress": {
  "pixel_mm": 0.1,
  "ceiling_mm": 3.0,
  "min_lobe_mm2": 25.0,
  "lever_power": 1.0,
  "report_above": 0.0
}
```

`capacity_mm2` and `capacity_at_mm` are deliberately **not** new keys — they read
`bridges.mass_per_bridge_mm2` and `bridges.width_mm`, so retuning the bridge
planner's idea of what 1 mm holds retunes this lib with it. One opinion about the
physics, in one place.

---

## 7. Open decisions

1. **`lever_power` default: 1.0 or 0.0?** Measured in §3: the top four necks are
   the same either way, and one more crosses 1.0 with the arm in. 1.0 is the
   physics and the numbers say it is low-risk — **recommend 1.0**, reconsider if a
   province shows the arm promoting something visibly fine.
2. **Which thickness?** `print.border_thickness_mm` (2.0) and `terrain.base_mm`
   (2.0) are the same number today, so the choice is free and will not stay free.
   Default to the border thickness, let the caller pass it.
3. **Does the width field belong in `LIB_thin_features` instead?** The distance
   transform would serve both, and `thin_features`' opening is exact where a
   raster is not. Recommend keeping them apart: `thin_features` is vector and
   exact and must stay so; this lib is raster by nature. Revisit only if a third
   caller wants the field.
4. **Lakes.** A neck between two lakes is a real neck, and nothing special is
   needed to find it — a lake is a hole, and the distance transform sees its shore
   like any other. But the report should probably say when a neck's water is
   interior, because widening it means filling a lake, which is a different edit.
5. **Province viewer integration.** `UI_province_viewer` is where the printing
   verdict for the whole country lives, and this belongs there eventually,
   alongside `Show thin`. Out of scope for the first build: get the lib and its
   harness right first.

---

## 8. Build order

1. `lib/LIB_stress_points.py` — grid, width field, lobe collapse, flood, scoring.
   `tests/test_stress_points.py` alongside it, on synthetic shapes where the
   answer is known by construction: a dumbbell with a 0.5 mm bar (one neck, one
   throat, both masses exact), the same bar at 1.5 mm (sound), a plain square (no
   necks — the corner-sliver trap `thin_features` had to fix with mitre joins), a
   spit (narrow, carries nothing, must **not** be reported), two blobs joined
   twice (a cycle: the load is shared, not doubled), and an invalid
   self-intersecting ring (`measured` False, or repaired and flagged).
2. Geometry extraction for the overlay — throat bar, region, branch — tested for
   the bar's length against the throat and the branch's area against `load_mm2`.
3. Regression on real data: the four provinces measured here, asserting the known
   necks are found with their throats and masses. The table in §3 is the fixture.
4. `harnesses/UI_stress_points.py` — the standard skeleton, threaded run, the
   three layers, Worst, Show branch. `tests/test_stress_points_ui.py` following
   `test_island_bridges_ui.py`.
5. `docs/USING_STRESS_POINTS.md` once the behaviour is real, and a status note at
   the top of this file.
