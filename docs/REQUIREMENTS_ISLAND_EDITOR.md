# Requirements — `UI_island_editor.py` (combined Island Editor)

A single standalone harness that performs **island merging**, **island
removal**, **lake removal**, and **freehand drawing and erasing** on one
province GeoJSON at a time, with **undo/redo**, reusing the existing `lib.LIB_*`
modules.

## 1. Purpose & scope

Fold the three single-purpose harnesses ([UI_island_merge.py](../harnesses/UI_island_merge.py),
[UI_island_removal.py](../harnesses/UI_island_removal.py),
[UI_lake_removal.py](../harnesses/UI_lake_removal.py))
into one interactive editor, reusing:

- `lib.LIB_island_merging.merge_islands(islands, buffer_km)`
- `lib.LIB_island_bridging.draw_bridge(doc, points, width_km)`
- `lib.LIB_island_erasing.erase_stroke(doc, points, width_km)`
- `lib.LIB_island_removal.remove_small_islands` / `is_below_area` / `area_km2`
- `lib.LIB_lake_removal.remove_lakes(geom)`
- `lib.LIB_screen_calibration.monitor_at` / `load_saved_ppi` / `px_per_mm`

It stays independent of the `app`/`core` main window, like the other `UI_*`
tools. (The `app` window already offers a combined, undoable cleanup editor via
`core.cleanup`; this tool is the lib-based harness equivalent, and fulfils the
`UI_island_editor.py` entry the README already names.)

## 2. Data model & I/O

- **FR-1.1** Load **one GeoJSON file at a time** (one province). Reproject to the
  working CRS **EPSG:3347** (metres) so the ground distances the mm parameters
  convert to (FR-3.5) are real; remember the source CRS for save.
- **FR-1.2** The **document** is a single working geometry (a MultiPolygon) held
  in EPSG:3347; its polygon *parts* are the islands. Every operation is a pure
  `geometry -> geometry` transform, which makes undo a one-object snapshot and
  lets lake removal use the whole-geometry dissolve the lib performs.
- **FR-1.3** Save writes the current document back **in the source CRS**, driver
  `GeoJSON`. Dialog directories follow the pipeline — load from
  `Data/JSon-Provinces-Orig`, save to `Data/JSonProvinces-Cleaned`.
- **FR-1.4** A file with no CRS: warn that km / km² act in raw data units (as the
  existing harnesses do), and proceed.
- **FR-1.5** Loading a new file resets the document, selection, and the undo
  stack.

## 3. Display & interaction

- **FR-2.1** A **single live canvas** (matplotlib) shows the current document —
  no before/after split — because edits are applied in place and stepped through
  with undo/redo.
- **FR-2.2** Pan/zoom via the nav toolbar and cursor-anchored scroll zoom;
  **preserve the view** across redraws (reuse the existing `_on_scroll` /
  `preserve_view` pattern).
- **FR-2.3** **Click to select/deselect** an island part; any number may be
  selected. Selected parts are highlighted; parts that the current **Min area**
  threshold would drop are highlighted in a distinct colour and update live as
  the threshold changes (reuse `_island_at`, and `is_below_area` for the mask).
- **FR-2.4** **Select All**, **Clear Selection**, and **Invert** — swap selected
  for unselected. Picking out the few islands worth keeping is often far quicker
  than clicking the many to drop, and this turns the one into the other. Unlike
  All/None it stays enabled at both extremes, since inverting nothing selects
  everything and inverting everything clears it.
- **FR-2.5** Two freehand brush modes, **Draw Bridge** and **Erase** (checkable
  toolbar toggles). While either is on, left-drag paints a stroke instead of
  panning; panning stays available through the nav toolbar's pan tool, and
  scroll zoom is unaffected. The stroke is previewed live as it is drawn, at the
  true brush width, in the colour of what it will do — land going down, or (in
  the same red the area threshold marks doomed islands with) land coming away.
  **Esc** backs out one step: it cancels a stroke in progress, and otherwise
  leaves the mode. Both toggles are disabled with no document loaded.
- **FR-2.5.1** The two modes are **mutually exclusive** — a stroke is either
  laying land down or lifting it off — so entering one leaves the other, and
  they share a single **Brush (km)** width: it is one brush, used both ways.
- **FR-2.6** **Arrow keys pan** the map by a fraction of the *visible span*
  (10%; Shift 50%, Ctrl 2%), so one press covers the same amount of screen at
  every zoom. The bindings are scoped to the canvas (Qt `WidgetShortcut`), live
  only while it holds the keyboard focus — a window-scoped shortcut would fire
  over the spin boxes too and swallow the Up/Down that steps their value.
  Clicking any toolbar button therefore hands focus back to the canvas, so the
  map is where the keyboard rests unless a number is being edited. This depends
  on the canvas being given a focus policy at all: matplotlib only sets one in
  its own managed (pyplot) window, and leaves an embedded canvas at `NoFocus`.
- **FR-2.6.1** **Every button carries a keyboard shortcut**, hung on the button
  itself (`QPushButton.setShortcut`) rather than on a free-standing `QShortcut`.
  That makes `setEnabled` the single gate for mouse and keyboard alike — the
  keyboard can no more merge with nothing selected than the mouse can — and the
  button flashes as the key fires, so a press made with the pointer out over the
  map is visibly *this* control acting. The tooltip names the key, since the
  label cannot: an ampersand mnemonic would claim Alt, which is a different
  mechanism, and "Merge (M)" on the face of a button spends width the row has
  not got. The map:

  | | |
  | --- | --- |
  | Import / Save | Ctrl+O / Ctrl+S |
  | Undo / Redo | Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y |
  | Home / Back / Forward (the *view* history) | Home / Alt+Left / Alt+Right |
  | Pan / Zoom (the nav modes) | P / Z |
  | Real Size / Recalculate | Ctrl+R / F5 |
  | All / None / Invert | Ctrl+A / Ctrl+Shift+A / Ctrl+I |
  | From Gap / Merge / Bridge / Remove | G / M / B / Delete |
  | Remove Small / Amputate / Remove Lakes | S / A / L |
  | Draw Bridge / Erase | D / E |

  The modified sequences are the ones that must work from anywhere; the bare
  letters are for the map in hand, and they give way to a focused spin box,
  which takes the keys it would accept as input before shortcut dispatch sees
  them — so typing a value can never set off an edit. Undo and redo keep their
  free-standing bindings, because they answer to more than one sequence and a
  button holds only one; binding a second would make both ambiguous, and Qt
  answers an ambiguous shortcut by firing neither.
- **FR-2.7** Controls sit in **three rows**, with the **Scores** panel down the
  right spanning all three:

  1. *the document* — Import, Save, Undo, Redo, and nothing else: what is open
     and where in the editing of it you are;
  2. *what the edit is pointed at* — **Selection** (All, None, Invert, Remove,
     and the live count) beside **Whole province** (Remove Small, Amputate,
     Remove Lakes with their thresholds);
  3. *how the land is reshaped* — **Expansion Merge**, **Bridge Merge**,
     **Freehand**.

  The controls that move or measure the *map* — the navigation tools, the print
  scale, the km/mm box and **Real Size** — are not document controls and do not
  ride in row 1. They sit **under the map, bottom right**, on the same line as
  the status: against the thing they act on, and costing no height, because a
  line of their own would come straight off the map. The status keeps the left
  of that line and is allowed to shrink below its own text, so a long report
  cannot set the window's minimum width from the bottom row.

  The recurring question in this tool is whether an action hits the islands you
  clicked or the whole province, and row 2 *is* that question: the two scopes
  side by side. Row 3 then answers a second one — expansion and bridging both
  close a gap and are told different things about it (a closing distance, and a
  width to build to), so the titles name the *method* and put them up as the
  alternatives they are, with the brush as the third for when neither rule can
  be made to produce the shape. **Remove** sits with the selection rather than
  with the merge it used to share a box with: it is the end of the clicking
  gesture, and the one edit that needs no parameter, so it owned nothing in a
  box of spin boxes. Group titles let members drop the qualifier from their own
  labels (“Merge”, not “Merge Selected”; “Remove”, not “Remove Selected”), and
  the live selection count sits with the selection controls rather than only in
  the status line. Each group box is the enable handle for its contents — with
  no document loaded the group greys out whole, while selection-sensitivity
  stays on the individual members. Three shorter rows also cost less width than
  two crowded ones: the window's minimum went from 3198 px when the editing
  groups shared a row, to 2344 px once *Whole province* moved down, to 1848 px
  at this arrangement.
  The navigation toolbar is trimmed to **Home / Back / Forward / Pan / Zoom**:
  `Subplots` and `Customize` can undo the deliberate edge-to-edge axes, and its
  `Save` writes a PNG of the figure one button away from **Save GeoJSON …**.
  Back/Forward tooltips state that they step the *view* history, not the edit
  history, which two arrows beside Undo/Redo otherwise invite.
- **FR-2.8** The map carries **two scale bars**, bottom left: one in **km of
  ground**, one in **mm of paper** at the current print scale. Both measure the
  same view, so the second answers what the first cannot — how big what is on
  screen comes out once the province is printed. Each is rounded **in its own
  unit** (a 1 / 2 / 2.5 / 5 × 10ⁿ length at or below ~22% of the view), so both
  read as whole numbers and their lengths therefore differ; the mm bar is
  labelled with the ratio it assumes, so the map stays self-describing away from
  the toolbar. The bars are hidden with no document loaded, where a ground scale
  over matplotlib's own unit square would be a fiction.
- **FR-2.9** The **Print scale** is user-editable as `1:N` (default
  **1:8 000 000**, the project's working reduction), under the map with the
  navigation tools: it is a statement about how the map is read, not an edit, so
  it belongs with the camera controls rather than in any of the editing groups,
  and it changes no geometry.
- **FR-2.10** A **read-only km/mm box** sits beside it, showing the ground
  kilometres one printed millimetre covers (`N / 1 000 000` — at 1:8 000 000,
  `8 km/mm`). It is the same statement as the ratio, in the units that relate the
  map's two scale bars and the mm parameters of FR-3.5 to ground distance. It
  tracks the spin box and is never typed into — the scale is the one input.
- **FR-2.11 Real Size** — zoom so the map is its **printed** size on the glass:
  one millimetre of screen carries exactly the ground one millimetre of paper
  will at the current reduction, so a province held against the monitor is the
  province that comes off the printer. It is a camera action driven by the print
  scale, so it sits in the *document and view* row beside it and changes no
  geometry. The zoom scales the *current* view by one factor rather than
  computing limits from the axes box: matplotlib has already put those limits
  through `apply_aspect` (FR-2.2's `adjustable="datalim"` widens them to fill the
  box), and multiplying both spans by one factor preserves that agreement where
  fresh limits could fight it. Zooming is about the centre, so nothing is lost
  off-screen.
- **FR-2.12** Real Size needs the monitor **measured**, which `ScreenRuler.py`
  does and `lib.LIB_screen_calibration` stores per monitor. The physical size a
  monitor reports through EDID is missing or wrong on a great many panels, so
  there is no honest figure to fall back on: without a measurement the button
  declines, names the tool that takes one, and leaves the view alone rather than
  guessing at 96 dpi. The calibration is read live, so moving the window to
  another monitor and pressing it again uses that monitor's figure.

## 4. Operations (each is one undoable command)

- **FR-3.1 Merge Selected** — bridge ≥2 selected parts via
  `merge_islands(selected, buffer_km)`; replace them with the merged result and
  keep the rest. Enabled only when ≥2 parts are selected. Report whether all
  bridged into one landmass or several groups remain (too far to bridge → raise
  the buffer).
- **FR-3.1a Bridge Selected** — span ≥2 selected parts with straight bridges
  of a fixed printed width via `plan_bridges(selected, params,
  obstacles=unselected)` then `apply_bridges(all_parts, plan.bridges)`. Where
  **Merge** is told a closing distance and hands back whatever necks that
  produces, a bridge is told the width it must come out at, so the two answer
  different questions about the same selection. The spanning tree spends one
  bridge per join, not one per pair; a pair that would hinge gets a second.
  The **unselected** parts are passed as obstacles — they are still on the map,
  so a bridge may not cross one, but they are not what was asked to be joined,
  and without them the plan would be answering a question about a different
  province. When nothing can be placed the report names the control that would
  change it: out of **Max span**, blocked by another island, or no landing able
  to hold a bridge that wide.
- **FR-3.2 Remove Selected** — drop the hand-picked selected parts.
- **FR-3.3 Remove Small** — drop every part below **Min area (km²)** using
  `is_below_area` / `remove_small_islands`.
- **FR-3.3a Amputate** — drop every piece of land narrower than **Min width**
  via `printable(document, params)`: the morphological opening the thin-feature
  detector already computes, returned as geometry instead of as a report, so
  what the province viewer marks in red is exactly what this takes. Reaches the
  necks *inside* a landmass, which no island-at-a-time removal can. Refuse by
  name when the whole province is below the width, and — because an opening
  reorders the vertices of a shape it did not change — treat a geometrically
  equal result as no edit rather than letting the WKB comparison in `_apply`
  put a no-op on the undo stack (FR-2.13).
- **FR-3.4 Remove Lakes** — strip interior rings via `remove_lakes` applied to
  the **whole document**, preserving genuine in-lake islands through the lib's
  dissolve.
- **FR-3.5** Parameters are **printed sizes**, converted to ground units through
  the print scale at the moment of use: **Buffer (mm)** for merge (default 0.25),
  **Width (mm)** and **Max span (mm)** for bridging (defaults 1.0 and 5.0; the
  rest of `BridgeParams` keeps its `bridges.*` defaults and is turned in
  `harnesses/UI_island_bridges.py`), **Min width (mm)** for amputation (default
  `thin.width_mm`, 1.0 — its own knob rather than the bridge width, because one
  is a limit you measure against and the other a size you build to),
  **Brush (mm)** for both freehand strokes (default 0.125, a **width** across the
  stroke, not a radius) and **Min area (mm²)** for removal (default 0.02). The
  defaults are what the tool previously held in km, read at 1:8 000 000 — 2 km,
  1 km, and ≈1.28 km².
- **FR-3.5.1** Changing the print scale **never rewrites these boxes**; it
  changes what they are worth. The constraint they express belongs to the paper
  (the finest neck a cutter will hold, the smallest island worth printing) and
  does not move when the reduction does — the ground distance standing behind it
  is what moves. A scale change therefore re-marks the **Min area** mask and
  redraws, exactly as editing the threshold itself does, and holds the view.
- **FR-3.5.2** **From Gap** (FR-3.1) converts the *ceiling and the quantum*
  rather than the answer: one increment of the mm box becomes
  `bridging_buffer_km`'s `step`, so every value the probe tries already lies on
  the box's grid and the one it settles on converts back to a millimetre the box
  holds exactly. This matters because the merge is **not monotonic** near its
  threshold — the lib only trusts a value it has tried at full precision, so a
  figure rounded after the fact can fail to bridge.
- **FR-3.5.3** Gaps are reported in **mm** for the same reason: the buffer beside
  them is in mm and From Gap answers one with the other, so a status line quoting
  km would leave the two incomparable. The gap cache is keyed on the print scale
  as well as the document and selection — the ground measurement is unchanged by
  a scale change, but the sentence it is rendered into is not.
- **FR-3.5.4** A file with **no CRS** (FR-1.4) has no ground distance to convert,
  so the parameters act in raw data units and the print scale does not apply;
  the warning says so.
- **FR-3.6** Every operation keeps the document **valid** and never silently
  empties it: the libs already fall back safely, and the UI validates the result
  and warns (leaving the document unchanged) rather than committing an empty or
  invalid geometry.
- **FR-3.7 Draw Bridge** — paint land along a freehand stroke via
  `draw_bridge(document, points, brush_km)`, unioning it onto the document.
  Where **Merge Selected** is conservative by construction — it keeps only the
  necks spanning ≥2 islands, so it can never add land that was not asked for —
  the brush honours the stroke **literally**. It is the manual recourse for what
  the neck heuristic cannot serve: a gap too wide for any workable buffer, or a
  buffer that bridges the wrong pair before it reaches the intended one. Report
  the outcome by landmass count: islands joined, one landmass extended, or a new
  island where the stroke touched no land. Straits the stroke encloses are filled
  (as in a merge); the document's genuine lakes survive.
- **FR-3.8** A stroke that adds no land (drawn over land the document already
  has) is **not an edit**: it does not enter the undo history. The union alone
  cannot establish this — it rebuilds the rings, so the result compares unequal
  to FR-4.2's by-value check while being the same shape — so `draw_bridge`
  reports the case by returning the document object itself.
- **FR-3.9 Erase** — rub land out along a freehand stroke via
  `erase_stroke(document, points, brush_km)`, subtracting the swept region from
  the document. It is the inverse of **Draw Bridge**, and the freehand recourse
  for what FR-3.2/FR-3.3 cannot do: those drop islands **whole**, by click or by
  area, so neither can trim a spit, rub out a causeway, or cut one landmass into
  two. Report the outcome by landmass count: land cut apart, islands erased
  entirely, or a coastline trimmed.
- **FR-3.10** Erasing is **literal — holes it opens are kept**. A stroke closing
  a ring inside a landmass is a pocket the user deliberately rubbed out, so
  filling it back in (as a *bridge* fills the strait it encloses, per FR-3.7)
  would undo the edit; **Remove Lakes** covers a pocket that turns out not to be
  wanted. A stroke over open sea takes nothing away and is reported by identity
  like FR-3.8. A stroke covering the whole document returns an **empty**
  geometry, which FR-3.6 refuses with a warning, leaving the document unchanged.

### Scores (bottom right, not an operation — it changes nothing)

- **FR-3.9** Report, against the document **as imported**: total outline
  (print-mm), land area (print-mm²), and the share of the printed land the two
  versions agree on. The first two are arithmetic and follow every edit; the
  third rasterises both at **a quarter of the nozzle width** and sits behind
  **Recalculate**.
- **FR-3.10** The rasterised reading is *held*, and retired the moment the
  document or the print scale moves. A figure that no longer describes what is
  on screen must never be shown as though it did — a blank is better.
- **FR-3.11** "Could not be measured" is never rendered as agreement, and a
  coarsened pixel is declared as coarsened.
- **FR-3.12** Outline is the whole boundary: outer coastline, any shared land
  border, and every lake shore. For a province with a straight border that is
  not all coastline (23% of British Columbia's boundary is the Alberta and
  Yukon line), but the part that cannot change cancels out of a
  before-and-after, so within one session it is the right measure.

## 5. Undo / redo

- **FR-4.1** One `QUndoCommand` per operation on a single `QUndoStack`,
  **snapshot-based**: capture the document geometry before and after and swap
  between them (mirrors [app/commands.py](../app/commands.py) but over a single
  geometry rather than a region dict).
- **FR-4.2** No-op detection **by value (WKB)** so an operation that changes
  nothing does not enter the history.
- **FR-4.3** **Undo/Redo** buttons plus shortcuts **Ctrl+Z** and
  **Ctrl+Shift+Z / Ctrl+Y**, disabled when the stack has nothing to undo/redo
  (use `QUndoStack.createUndoAction` / `createRedoAction`).
- **FR-4.4** **Selection is kept across an edit** by remapping it onto the
  result through part-correspondence (representative-point containment, in the
  spirit of `_part_mapping` in [app/commands.py](../app/commands.py)): a merge
  leaves the merged landmass selected; a removed part drops out of the selection;
  undo/redo restores the selection for the direction moved.
- **FR-4.5** Save is enabled whenever a document is loaded (not only after an
  edit). The window title / status reflects unsaved changes and the current
  position in the stack.

## 6. Non-functional

- **FR-5.1** Independent of `app`/`core`; imports only `lib.LIB_*` plus
  Qt / matplotlib / geopandas, matching the harness family.
- **FR-5.2** Responsive on the heaviest real input (Nunavut, 223 parts):
  redraws hold pan/zoom. The merge/remove/lake ops are shapely-fast, so they run
  synchronously; if any proves slow it moves off the UI thread with a busy
  indicator.
- **FR-5.3** Robust to unreadable/empty files and to operations that raise:
  surface via `QMessageBox` and leave the document unchanged.
- **FR-5.4** Purely interactive, **per file** — no batch/multi-file mode.

## 7. Architecture / reuse

- **Document + command stack.** Hold the working geometry (EPSG:3347) as the
  single document. Each action builds a `GeometryCommand(before, after)` and
  pushes it onto a `QUndoStack`; `redo`/`undo` set the document to the after /
  before snapshot, re-derive island parts, remap the selection, and redraw.
- **Operation adapters** (thin wrappers over the libs, each `geometry -> geometry`):
  - merge: explode → `merge_islands(selected_parts, buffer_km)` → union with the
    unselected parts.
  - bridge: explode → `plan_bridges(selected, params, obstacles=unselected)` →
    `apply_bridges(all_parts, plan.bridges)`. The slabs are unioned onto the
    **whole** document rather than onto the selection, so a strait a bridge
    encloses is judged against the real coastline. Planning is the one slow
    operation in the editor — 8 s on a whole-province selection in British
    Columbia, over 100 s in Nunavut — so it runs on a `QThread` behind an
    indeterminate progress bar, with every control that could change the
    document locked meanwhile and the view controls deliberately left live.
  - remove selected / remove small: explode → keep the parts that are not picked
    / not below the threshold → reassemble a MultiPolygon.
  - remove lakes: `remove_lakes(document)` directly.
  - amputate: `printable(document, ThinParams(scale, width_mm))` directly, then
    re-measure with `thin_features` so the report can admit the slivers that
    survive at sharp corners instead of claiming a perfect cut.
  - scores: `outline_mm` / `land_mm2` on every state change, and
    `score_shapes(original, document, params)` on demand. The baseline is the
    document as imported and is set in exactly one place, so undo walks back
    through it and saving does not move it.
  - draw bridge: `draw_bridge(document, stroke_points, brush_km)` directly —
    the stroke's polyline buffered by half the brush width, unioned on.
  - erase: `erase_stroke(document, stroke_points, brush_km)` directly — the same
    swept region, differenced off instead. The two share the `brush_stroke`
    primitive, so what a stroke covers is identical either way.
- **Stroke preview.** While drawing, the stroke is previewed as a fat
  round-capped matplotlib line, not as the real buffered polygon: a round-capped
  line *is* the region a circular brush sweeps, so the preview is exact, while
  costing one artist update per mouse event instead of re-plotting every island.
  The true geometry is built once, on release. The preview is dropped on
  *every* exit path from a committed stroke, not only the ones that redraw — a
  result the UI refuses (an erase that emptied the document) otherwise leaves
  the stroke painted on the canvas.
- **Selection remap** on every apply: map old part index → new part index by
  representative-point containment (fall back to max-overlap), as in
  `_part_mapping`.
- **One place printed sizes cross into ground units.** `_km_per_mm` / `_to_km` /
  `_to_km2` / `_to_mm` are the only crossing points between the mm the UI states
  and the km the libs take, and every one reads the scale live — nothing caches a
  conversion the print scale could go on to invalidate.
- **Scale bars laid out at draw time.** The bars are *furniture*, so they live in
  axes coordinates pinned to the corner — a pan must not carry them off the
  canvas — while their lengths are recomputed from the live transform in a
  `draw_event` handler. That is both the only moment the numbers are exact (the
  axes keep `adjustable="datalim"`, so matplotlib widens the limits *during* the
  draw to satisfy the equal aspect, and a span read before that is a frame stale)
  and what keeps them honest on every route that moves the view — wheel, drag,
  arrow keys, the toolbar's own pan/zoom and Home/Back/Forward, a window resize —
  without each call site having to remember to ask. A draw that moves them asks
  for one more, which finds nothing left to change and stops; a *pan* moves
  neither bar, since the span is the same ground either side of it, so the
  common case costs no extra draw. `_px_per_metre` is the single place the
  map's scale is read, shared with the brush preview's true-width line.
- **One place that pans.** Drag-pan and key-pan both go through a single
  `_shift_view(dx, dy)` (ground units); the arrow keys reach it via `_pan_by`,
  which converts a fraction of the view into that delta. Redraws are issued with
  `draw_idle`, which coalesces to the event loop, so a held arrow key cannot
  queue more redraws than the canvas can serve (measured: ~17 ms per pan on the
  heaviest province, against a ~30/s key repeat).
- **Shared harness scaffolding** lifted from the existing three UIs: EPSG:3347
  working CRS with save-back to source CRS, explode-to-parts, `_island_at`,
  `_on_scroll`, `_style_axis`, status line.

## 8. Resolved decisions

1. **One file at a time** — single-document model, no multi-file concatenation.
2. **Lake removal applies to the whole document.**
3. **Single live canvas** (not before/after).
4. **Selection is kept after an edit** (remapped onto the result).
5. **Purely per file** — no batch mode.
