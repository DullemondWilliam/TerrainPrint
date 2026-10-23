# Building UI Harnesses

How to build a standalone visual harness (`UI_*.py`) for a `lib/LIB_*.py` module,
based on the ones that already exist. They all live in `harnesses/`:

| File | Wraps | Distinguishing feature |
|------|-------|------------------------|
| `UI_lake_removal.py`    | `LIB_lake_removal.remove_lakes`        | simplest — one button, no parameters |
| `UI_island_removal.py`  | `LIB_island_removal`                   | live threshold preview (spinbox) |
| `UI_island_merge.py`    | `LIB_island_merging.merge_islands`     | click-to-select islands on the map |
| `UI_border_regions.py`  | `LIB_border_regions.build_border_regions` | two typed inputs + threaded carve with a progress bar |
| `UI_border_split.py`    | `LIB_border_split.split_to_bed`        | mesh in, mesh out — two 3-D vispy panels |
| `UI_island_bridges.py`  | `LIB_island_bridges.plan_bridges`      | a *plan* before a result: eleven print-mm parameters over two rows, the scored bridges drawn and labelled on the input panel, and a report of what the spanning tree could not reach |
| `UI_terrain_trim.py`    | `LIB_terrain_trim.trim_terrain`        | mesh + GeoJSON in, mesh out: a 2-D placement overlay (slab extent vs reconciled province) beside a vispy solid coloured by elevation |
| `UI_terrain_stl.py`    | `LIB_terrain_stl.terrain_to_stl`       | raster in, **mesh out**: matplotlib DEM on the left, vispy solid on the right; live triangle/bed estimate before meshing |

The **editors stay in the project root**, not in `harnesses/`:
`UI_island_editor.py` (the island harnesses folded into one tool with undo/redo),
`UI_terrain_editor.py` (`LIB_raster_reproject` -> `LIB_geotiff_clip` ->
`LIB_terrain_stl`, the whole pipeline in one window). They borrow the patterns below, but they
are kept tools with state, not throwaways — the folder is the dividing line.

Each is a **throwaway inspection tool**, deliberately independent of the
editors, so a lib can be pointed at any GeoJSON without setting up a
project. They all follow the same shape; this guide is that shape.

---

## 1. What a harness is for

A harness answers one question: *does this lib function do the right thing to
real geometry?* It loads GeoJSON, runs one lib call, and draws **before vs
after** so the effect is visible. It is not the product — keep it small, keep it
in one file, and let the lib do the geometry.

If you find yourself adding project state, multiple pipeline steps, or export
logic beyond a single GeoJSON round-trip, it belongs in an editor, not here.

---

## 2. Naming conventions

| Thing | Pattern | Example |
|-------|---------|---------|
| File | `harnesses/UI_<feature>.py` | `harnesses/UI_island_merge.py` |
| Class | `<Feature>Tester(QMainWindow)` | `IslandMergeTester` |
| Window title | `"<Feature> — Visual Tester"` | `"Island Merge — Visual Tester"` |
| Import button | `"Import GeoJSON …"` (note the ellipsis) | |
| Action button | verb phrase, e.g. `"Remove Small Islands"`, `"Carve"` | |
| Save button | `"Save GeoJSON …"` / `"Save Result …"` | |

Buttons that open a dialog end with ` …`. Status text always names the *next*
step ("… then save.").

---

## 3. The standard stack

Every harness uses the same libraries — PySide6 for the window, matplotlib
(Qt Agg backend) embedded for the map, geopandas/shapely for geometry:

```python
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_<feature>.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_<feature> import <the_one_function>
```

`harnesses/` is one level below the project root, so `python
harnesses/UI_<feature>.py` puts *that* folder on `sys.path`, not the root, and
`from lib.…` would fail. The three-line bootstrap above — verbatim in every
harness, immediately before the `lib` imports — is what keeps running a harness
by its path (and the editor's run button) working. Tests import them as a
package instead: `from harnesses import UI_<feature> as U`.

When a lib's output is a **3-D mesh** (a `trimesh`/STL) rather than flat
geometry, that panel is rendered with `vispy` on the GPU instead of matplotlib —
see §11.D. Everything else (toolbar, import, threading, save) is unchanged.

### Module constants

```python
GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
EPSG_METRIC = "EPSG:3347"     # Statistics Canada Lambert, metres

# Colour semantics are shared across the harnesses — reuse them:
KEEP_COLOUR   = "#AED6F1"   # blue   — input / kept geometry
RESULT_COLOUR = "#ABEBC6"   # green  — the successful result
MARK_COLOUR   = "#E6787A"   # red    — marked-for-removal / a carved strip
SELECT_COLOUR = "#F5B041"   # orange — user selection
EDGE_COLOUR   = "#1B4F72"   # dark blue outline
```

---

## 4. Window skeleton

```python
class FeatureTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Feature — Visual Tester")
        self.resize(1100, 640)

        # -- state (type-hinted, with #: doc comments) ----------------------
        self.source: gpd.GeoDataFrame | None = None   # the imported input
        self.result: gpd.GeoDataFrame | None = None   # last run, ready to save
        self.source_paths: list[Path] = []
        self.source_crs = None                        # save back into this CRS

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar (single row of controls) -------------------------------
        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)
        self.btn_run = QPushButton("Do The Thing")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)                # nothing to run yet
        self.btn_save = QPushButton("Save GeoJSON …")
        self.btn_save.clicked.connect(self.save_geojson)
        self.btn_save.setEnabled(False)
        for b in (self.btn_import, self.btn_run, self.btn_save):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- map: before | after, linked by hand ---------------------------
        # Deliberately NOT sharex/sharey: the panels keep the size the window
        # gives them, and matplotlib refuses a fixed axes box on shared axes.
        # See §5.
        self._linking = False
        self.figure = Figure(figsize=(10, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import a GeoJSON to begin", "")

        self.toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        root.addWidget(self.toolbar)
        root.addWidget(self.canvas, stretch=1)

        # -- status line ----------------------------------------------------
        self.status = QLabel("No file loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.setCentralWidget(central)
```

Layout order top to bottom: **toolbar → matplotlib nav toolbar → canvas
(stretch=1) → status**. The canvas is the only widget that stretches.

---

## 5. The reusable helpers

These are near-identical across the harnesses — copy them verbatim.

### A panel sized by the window, not by the data

`set_aspect("equal")` is not optional — a distorted map is a wrong map — but
matplotlib offers two ways to honour it, and the default is the wrong one here.
`adjustable="box"` keeps the limits and reshapes the **axes box**: a
north-south province turns the panel into a vertical hairline (measured: 0.4%
of the figure width at 1:1000), an east-west one into a letterbox, and the
window rearranges itself around whatever file was imported last.
`adjustable="datalim"` keeps the box the window gave it and widens the
**limits** instead. Always use `datalim`.

Two consequences follow, and neither is optional either:

1. **The panels cannot be `sharex`/`sharey`.** matplotlib refuses `datalim` on
   shared axes, and it refuses at *draw* time, so a harness with both looks
   fine until something is plotted. Link them by hand instead.
2. **Limits set by hand must already fit the panel.** Handed a view of the
   wrong shape, `datalim` reaches the aspect by *cropping* — ask for 20 units
   of height and you get 1.2, with your subject off screen. Anything that
   frames a region goes through `_fit_view` (§5, below); an autoscaled draw is
   safe, because matplotlib grows the limits rather than shrinking them.

```python
def _reset_axes(self, left_title: str, right_title: str) -> None:
    for ax, title in ((self.ax_before, left_title),
                      (self.ax_after, right_title)):
        self._style_axis(ax, title)
    self.canvas.draw_idle()

def _style_axis(self, ax, title: str) -> None:
    ax.clear()
    ax.set_title(title, fontsize=11)
    # Fixed panel, not a fixed view: the default adjustable="box" would shrink
    # the axes box to the data's own shape instead.
    ax.set_aspect("equal", adjustable="datalim")   # geometry must not be distorted
    ax.set_xticks([])
    ax.set_yticks([])
    self._watch_limits(ax)        # ax.clear() threw the callbacks away
```

Note it is an instance method, not the `@staticmethod` it used to be: it has to
put the limit callbacks back.

### Linking the two panels

```python
def _watch_limits(self, ax) -> None:
    """(Re)connect the callbacks that keep the two panels together."""
    ax.callbacks.connect("xlim_changed", self._link_views)
    ax.callbacks.connect("ylim_changed", self._link_views)

def _set_view(self, ax, xlim, ylim) -> None:
    """Move a panel to a new view, both limits at once."""
    self._linking = True          # hold the callbacks off in between
    try:
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)
    finally:
        self._linking = False
    self._link_views(ax)

def _link_views(self, changed=None) -> None:
    if self._linking:
        return                    # our own write coming back as a callback
    self._linking = True
    try:
        source = changed if changed is self.ax_after else self.ax_before
        target = self.ax_after if source is self.ax_before else self.ax_before
        source.apply_aspect()     # settle datalim's widening before copying
        target.set_xlim(source.get_xlim())
        target.set_ylim(source.get_ylim())
    finally:
        self._linking = False
```

`self._linking = False` goes in `__init__` before the first `_style_axis`.

Three details, each of which was a bug first:

* **The callbacks, not a call at each redraw.** matplotlib's own toolbar (pan,
  zoom-to-rectangle, Home) reaches the axes directly, never through
  `_on_scroll`. Without the callback, dragging one panel leaves the other
  behind — which sharing used to make impossible.
* **Reconnect after every clear.** `ax.clear()` replaces the axes' callback
  registry, so a link connected once in `__init__` is gone after the first
  redraw. That is why `_style_axis` ends with `_watch_limits`.
* **`apply_aspect()` before copying.** `datalim` widens the source's limits
  during the draw, so reading them beforehand hands the other panel a
  data-shaped view it then has to override; matplotlib logs *"Ignoring fixed y
  limits to fulfill fixed data aspect"* and picks the frame itself.

### Framing a region

Anything that frames a region by hand — Zoom to Seam, Zoom to Bridges — grows
the view to the panel's shape first, so matplotlib has nothing left to crop:

```python
def _panel_shape(self) -> float:
    """Width / height of one panel's slot, in inches."""
    fig_w, fig_h = self.figure.get_size_inches()
    try:
        pos = self.ax_before.get_subplotspec().get_position(self.figure)
        width, height = pos.width * fig_w, pos.height * fig_h
    except Exception:                     # no layout yet; a sane fallback
        width, height = fig_w / 2.0, fig_h
    return width / height if height else 1.0

def _fit_view(self, bounds, pad: float) -> None:
    minx, miny, maxx, maxy = bounds
    width = max(maxx - minx, 1e-9)
    height = max(maxy - miny, 1e-9)
    target = self._panel_shape()
    if width / height < target:
        width = height * target           # too tall for the panel: widen
    else:
        height = width / target           # too wide: heighten
    # Proportional breathing room: the same padding added to a wide half-width
    # and a narrow half-height would change the ratio again.
    grow = 1.0 + 2.0 * pad
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    self._set_view(self.ax_before,
                   (cx - width * grow / 2.0, cx + width * grow / 2.0),
                   (cy - height * grow / 2.0, cy + height * grow / 2.0))
```

### Scroll-to-zoom (toward the pointer)

```python
def _on_scroll(self, event) -> None:
    ax = event.inaxes
    if ax is None or event.xdata is None or event.ydata is None:
        return
    scale = 1 / 1.2 if event.button == "up" else 1.2
    (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
    xd, yd = event.xdata, event.ydata
    # Both limits at once, via _set_view: a link fired on the x change alone
    # would settle the aspect against a y that has not moved yet, and
    # apply_aspect would pull x straight back — cancelling the zoom entirely.
    self._set_view(ax,
                   (xd - (xd - x0) * scale, xd + (x1 - xd) * scale),
                   (yd - (yd - y0) * scale, yd + (y1 - yd) * scale))
    self.canvas.draw_idle()
```

The matplotlib `NavigationToolbar` already gives pan, zoom-to-rectangle, and
Home; scroll-to-zoom is added on top for quick inspection.

`tests/test_harness_viewport.py` holds every harness to all of this, so a new
one is covered by adding it to that file's `HARNESSES` list.

---

## 6. Import: the standard loader

Every harness loads the same way: read many files, collect per-file failures,
warn but continue, then unify into one CRS.

```python
def import_geojson(self) -> None:
    paths, _ = QFileDialog.getOpenFileNames(
        self, "Import GeoJSON (one or more)", str(Path.cwd()), GEOJSON_FILTER)
    if not paths:
        return

    frames, failures = [], []
    for path in paths:
        try:
            frame = gpd.read_file(path)
        except Exception as exc:
            failures.append(f"{Path(path).name}: {exc}")
            continue
        if not frame.empty:
            frames.append(frame)

    if failures:
        QMessageBox.warning(self, "Some files could not be read",
                            "\n".join(failures))
    if not frames:
        QMessageBox.warning(self, "Nothing to show",
                            "No readable features in the selected file(s).")
        return

    # Reproject every file into the first file's CRS so they overlay.
    base_crs = frames[0].crs
    if base_crs is not None:
        frames = [f.to_crs(base_crs) if f.crs is not None else f for f in frames]
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=base_crs)

    self.source_paths = [Path(p) for p in paths]
    self.source_crs = base_crs
    self.source = gdf
    self.result = None
    # ... draw, enable btn_run, disable btn_save, set status
```

---

## 7. CRS: work in metres, save in the source CRS

The golden rule when a parameter is a **real-world distance or area** (km, km²):

1. Load in whatever CRS the files carry (`source_crs`).
2. Reproject the working copy to `EPSG:3347` (metres) so a km buffer / km²
   threshold means real ground units — `gdf = gdf.to_crs(EPSG_METRIC)`.
3. If the input has **no CRS**, you can't do this: warn the user that the value
   will act in raw data units, and carry on.
4. On save, reproject the result **back** to `source_crs` so the output matches
   the input frame.

`UI_lake_removal` skips all of this — removing interior rings needs no distance,
so it never leaves the source CRS. Only introduce the metric working CRS when a
parameter demands it.

### One polygon per row

For per-feature operations (per-island filtering, selection), explode first:

```python
islands = gdf.explode(index_parts=False, ignore_index=True)
```

---

## 8. Running the lib + drawing the result

Wrap the single lib call in `try/except` and surface failures as a dialog — a
bad geometry should never crash the harness:

```python
def run(self) -> None:
    if self.source is None:
        return
    try:
        out = the_one_function(list(self.source.geometry), self.param_spin.value())
    except Exception as exc:
        QMessageBox.critical(self, "Run failed", str(exc))
        return
    self.result = gpd.GeoDataFrame(..., crs=self.source.crs)
    self._draw_result()
    self.btn_save.setEnabled(True)
    self.status.setText("… done. Inspect on the right, then save.")
```

Draw with geopandas straight onto the axes, colour-coded by role:

```python
def _draw_result(self) -> None:
    self._style_axis(self.ax_after, "Result")
    if self.result is not None and not self.result.empty:
        self.result.plot(ax=self.ax_after, facecolor=RESULT_COLOUR,
                         edgecolor=EDGE_COLOUR, linewidth=0.6)
    self.canvas.draw_idle()
```

Draw the input the same way onto `ax_before`. A single shapely geometry (not a
GeoDataFrame) plots via `gpd.GeoSeries([geom], crs=...)`.

---

## 9. Save: default name from the source

```python
def save_geojson(self) -> None:
    if self.result is None or self.result.empty:
        return
    default = ""
    if self.source_paths:
        first = self.source_paths[0]
        default = str(first.with_name(first.stem + "_<suffix>.geojson"))
    path, _ = QFileDialog.getSaveFileName(
        self, "Save result", default, GEOJSON_FILTER)
    if not path:
        return
    out = (self.result.to_crs(self.source_crs)
           if self.source_crs is not None else self.result)
    out = out.drop(columns="area_km2", errors="ignore")   # drop helper columns
    try:
        out.to_file(path, driver="GeoJSON")
    except Exception as exc:
        QMessageBox.critical(self, "Save failed", str(exc))
        return
    self.status.setText(f"Saved {len(out)} feature(s) to {Path(path).name}.")
```

Suffixes in use: `_no_lakes`, `_pruned`, `_merged`, `_border`. Drop any helper
columns you added (e.g. `area_km2`) before writing.

---

## 10. `main()` boilerplate

Identical in all four:

```python
def main() -> int:
    app = QApplication(sys.argv)
    window = FeatureTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## 11. Optional patterns (add only when the lib needs them)

### A. Live parameter preview (`UI_island_removal`)

A `QDoubleSpinBox` whose `valueChanged` re-highlights the *before* panel, so the
user sees what a threshold will do before committing:

```python
self.area_spin = QDoubleSpinBox()
self.area_spin.setDecimals(3); self.area_spin.setRange(0.0, 1_000_000.0)
self.area_spin.setValue(1.0)
self.area_spin.setToolTip("Islands smaller than this (km²) are removed.")
self.area_spin.valueChanged.connect(self._preview)
```

Changing the value **invalidates the last result**: null `self.result`, disable
Save, and reset the after-panel title to "(not yet run)".

**Preserve the view on redraw.** A parameter/selection redraw must not fling the
map back to full extent — hold and restore the limits:

```python
def _draw_original(self, preserve_view: bool = False) -> None:
    if preserve_view:
        xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
    self._style_axis(self.ax_before, "…")
    # ... plot ...
    if preserve_view:
        self._set_view(self.ax_before, xlim, ylim)   # both limits at once, §5
    else:
        self._link_views()
    self.canvas.draw_idle()
```

### B. Click-to-select on the map (`UI_island_merge`)

Connect `button_press_event`, ignore clicks while a pan/zoom tool owns the mouse
(`self.toolbar.mode`), hit-test with a shapely `Point`, toggle selection, redraw
with `preserve_view=True`:

```python
self.canvas.mpl_connect("button_press_event", self._on_click)

def _on_click(self, event) -> None:
    if self.toolbar.mode:                       # pan/zoom active
        return
    if event.inaxes is not self.ax_before or self.source is None:
        return
    if event.xdata is None or event.ydata is None:
        return
    hits = self.source.index[self.source.geometry.contains(
        Point(event.xdata, event.ydata))]
    if not len(hits):
        return
    idx = int(hits[0])
    self.selected.remove(idx) if idx in self.selected else self.selected.append(idx)
    # invalidate result, redraw preserve_view=True, sync button enablement
```

Provide **Select All** / **Clear Selection** buttons and a `_sync_buttons()`
that enables the action only once the selection is valid (e.g. `>= 2`).

### C. Heavy lib call off the UI thread + progress bar (`UI_border_regions`)

When the lib call is a **single blocking geometry call** that can take seconds,
run it on a `QThread` so the window stays responsive, and drive an
**indeterminate** progress bar. The lib is pure geometry (no Qt), so it is safe
on a worker thread; only marshal the result back to the UI thread via signals.

```python
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QProgressBar

class RunWorker(QObject):
    done = Signal(object)
    failed = Signal(str)
    def __init__(self, *args):
        super().__init__(); self._args = args
    def run(self) -> None:
        try:
            self.done.emit(the_one_function(*self._args))
        except Exception as exc:
            self.failed.emit(str(exc))
```

In the window: a `QProgressBar` at the very bottom (below the status), hidden
when idle. Start it before launching the thread and hide it on completion:

```python
self.progress = QProgressBar()
self.progress.setTextVisible(False)
self.progress.setVisible(False)
root.addWidget(self.progress)          # after self.status

def run(self) -> None:
    if self.source is None or self._thread is not None:   # no double-launch
        return
    self._set_busy(True)
    self._thread = QThread(self)
    self._worker = RunWorker(list(self.source.geometry), self.param_spin.value())
    self._worker.moveToThread(self._thread)
    self._thread.started.connect(self._worker.run)
    self._worker.done.connect(self._on_done)
    self._worker.failed.connect(self._on_failed)
    self._thread.start()

def _set_busy(self, busy: bool) -> None:
    self.progress.setVisible(busy)
    self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
    for w in (self.btn_import, self.btn_run, self.btn_save, self.param_spin):
        w.setEnabled(not busy)         # lock controls while running

def _teardown_thread(self) -> None:
    if self._thread is not None:
        self._thread.quit(); self._thread.wait()
    self._worker = self._thread = None
```

`setRange(0, 0)` gives the bar its busy animation. Both `_on_done` and
`_on_failed` must call `_teardown_thread()` then `_set_busy(False)`.

### D. 3-D mesh preview with vispy (`UI_terrain_stl`, `UI_border_split`)

When the lib returns a **`trimesh` mesh** (an STL), preview it in a
GPU-accelerated `vispy` scene, **not** matplotlib. matplotlib's `mplot3d`
(`Poly3DCollection`) is a CPU painter's-algorithm renderer — it re-sorts every
triangle on each rotate and bogs down on dense meshes; vispy uploads the mesh to
the GPU once and rotates it for free.

Stack additions, and bind vispy to Qt once in `main()` **before** the window is
built:

```python
import numpy as np
import vispy
from vispy import scene

def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")
    ...
```

A scene's `.native` is an ordinary `QWidget` you drop into the layout; a
`TurntableCamera` gives drag-to-rotate and scroll-to-zoom for free, so a 3-D
panel needs neither the matplotlib nav toolbar nor the `_on_scroll` helper:

```python
@staticmethod
def _make_scene():
    canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR, show=False)
    view = canvas.central_widget.add_view()
    view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
    return canvas, view
```

Draw by handing trimesh's arrays straight to a `Mesh` visual. Keep a list of the
added visuals so the next redraw can detach them (`vis.parent = None`), then frame
the camera with `view.camera.set_range()`:

```python
def _add_mesh(self, view, mesh, colour, store):
    vis = scene.visuals.Mesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float32),
        faces=np.asarray(mesh.faces),
        color=colour, shading="flat", parent=view.scene)
    store.append(vis)
```

vispy wants RGBA floats, so convert the shared hex colours (§3):

```python
def _rgba(hex_colour, alpha=1.0):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)
```

**Mixing 2-D and 3-D.** A harness whose *input* is flat but *output* is a mesh
(`UI_terrain_stl`) keeps a matplotlib view of its input on the left and a vispy scene on
the right, side by side in a `QHBoxLayout`; the panels do **not** share a view.
When both panels are meshes (`UI_border_split`), use two vispy scenes. Invalidate
a stale mesh panel by detaching its visuals, exactly as a 2-D harness clears its
`ax_after`.

Headless (offscreen) construction prints GL-context warnings but still builds;
exercise drawing by feeding a mesh into the window's state and calling the draw
method, just as for the 2-D harnesses.

---

## 12. Checklist for a new harness

- [ ] Module docstring: one-line purpose, numbered **Workflow**, `Run with:` line,
      the "throwaway / independent of `app`" note.
- [ ] `harnesses/UI_<feature>.py`, class `<Feature>Tester`, title
      `"<Feature> — Visual Tester"`.
- [ ] The `sys.path` bootstrap from §3, just above the `lib` imports.
- [ ] Toolbar: Import (enabled), Action (disabled), Save (disabled), `addStretch(1)`.
- [ ] `ax_before` / `ax_after` **unshared**, `adjustable="datalim"`, linked by
      hand (§5); nav toolbar; scroll-to-zoom; status label.
- [ ] Added to `HARNESSES` in `tests/test_harness_viewport.py`, which holds
      every harness to the panel rules in §5.
- [ ] Buttons start disabled and are enabled only when their precondition holds;
      any input/parameter change invalidates `result` and disables Save.
- [ ] Import loop tolerates per-file failures and unifies CRS.
- [ ] Metric working CRS **only** if a parameter is a real distance/area; save
      back in `source_crs`; drop helper columns.
- [ ] Lib call wrapped in `try/except` → `QMessageBox.critical`.
- [ ] `main()` + `raise SystemExit(main())`.
- [ ] Status text always points at the next action.
- [ ] Add threading + progress bar only if the lib call is slow; add click-select
      / live-preview only if the lib’s parameters call for it.
- [ ] If the lib outputs a 3-D mesh, preview it with vispy (§11.D), not mplot3d;
      `vispy.use(app="pyside6")` in `main()` before building the window.

---

## 13. Verifying without a display

Syntax-check and headless-construct before running for real:

```bash
python -c "import ast; ast.parse(open('UI_<feature>.py',encoding='utf-8').read()); print('syntax OK')"
QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -c \
  "from PySide6.QtWidgets import QApplication; import UI_<feature> as U; \
   app=QApplication([]); w=U.FeatureTester(); print('constructed OK')"
```

For a threaded harness, drive one carve to completion under a `QEventLoop`,
feeding geometry directly into the window's state to bypass the file dialog.
