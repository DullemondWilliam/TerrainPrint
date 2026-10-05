"""
UI_butterfly_keys.py

A standalone visual harness for ``LIB_butterfly_keys``: join two neighbouring
province STLs with butterfly (double-dovetail) keys.

Workflow
--------
1. **Import Province A …** — pick the province's GeoJSON, then its STL.  An STL
                             built by the terrain pipeline is placed from its
                             ``.frame.json``; any other is reconciled to the
                             GeoJSON by footprint, and refused if it does not fit.
2. **Import Province B …** — its neighbour.  Once both are in, the seam they
                             share is drawn in red and keys are placed along it.
3. **Place the keys**      — **Auto Place** spaces them every *Spacing* mm;
                             **click the map** near the seam to add one, or on a
                             key to remove it.  Keys that hang off the land or
                             collide are drawn red and will not be cut.
4. **Depth / Neck / Mouth / Reach / Clearance / Spacing** — the key, in print-mm.
                             Defaults are the border connectors' dovetail, so
                             every joint on the map locks the same way.
5. **Cut Slots**           — slot both STLs and build the keys (off the UI
                             thread).  The 3D panel shows them assembled: each key
                             in its slots, where a slot cut in the wrong place is
                             visible before it is printed.
6. **Save …**              — ``<A>_keyed.stl``, ``<B>_keyed.stl`` and
                             ``butterfly_keys.stl`` (every key, laid out for the
                             bed) into a folder.

The **left** panel is the plan view in EPSG:3347: both footprints, the seam, and
each key's slot outline.  The **right** panel is the 3D result -- assembly, or
either slotted piece, or the key layout on its own.

Run with:  python harnesses/UI_butterfly_keys.py

Like the other UI_* harnesses this stays independent of the main ``app`` window.

Why the loads are threaded too
------------------------------
Reconciling an STL means lifting its base footprint off every downward face --
a 250k-triangle Alberta is seconds of union -- and cutting the slots on the
Alberta/Saskatchewan pair took 15.6 s.  Both go on a ``QThread`` with an
indeterminate progress bar (BUILDING_UI_HARNESSES §11.C).
"""

import sys
from pathlib import Path

import numpy as np
import vispy
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.geometry import Point
from vispy import scene

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_butterfly_keys.py), so put
# the project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_butterfly_keys import (KeyParams, assembly, auto_place,
                                    check_keys, export, find_seam, fit_loaded,
                                    load_key_params, load_province, snap)
from lib.LIB_project_config import settings_files

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

KEEP_COLOUR = "#AED6F1"       # blue   — province A
OTHER_COLOUR = "#E5E7E9"      # grey   — province B
MARK_COLOUR = "#E6787A"       # red    — the seam; a refused key
RESULT_COLOUR = "#27AE60"     # green  — a key that will be cut
EDGE_COLOUR = "#1B4F72"       # dark blue outline
BG_COLOUR = "#FDFEFE"         # near-white 3D scene background
KEY_RGBA = (0.95, 0.61, 0.07, 1.0)   # the keys in 3D, orange
#: The pieces in 3D.  Flat colours, not the terrain ramp: the slots are seen
#: from below, where every face is the base and a height ramp is one colour.
A_RGBA = (0.68, 0.84, 0.95, 1.0)
B_RGBA = (0.80, 0.81, 0.82, 1.0)

#: What the 3D panel shows.
VIEW_CHOICES = ["Assembly", "Province A", "Province B", "Keys"]
#: A click this many screen pixels from the seam places a key there.
PICK_PX = 12
#: Padding around the seam when framing it, as a fraction of its extent.
SEAM_ZOOM_PAD = 0.08
#: The 3D camera's depth range: comfortably past a bed-sized piece, and tight
#: enough for the depth buffer to resolve the 0.1 mm a key sits under its slot.
DEPTH_RANGE_MM = 1000.0


class CallWorker(QObject):
    """Runs one lib call off the UI thread: a province load, or the cut."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self) -> None:
        try:
            result = self._fn(*self._args)
        except Exception as exc:          # any mesh/geometry failure reaches the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class ButterflyKeysTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Butterfly Keys — Visual Tester")
        self.resize(1300, 760)

        # -- state ----------------------------------------------------------
        self.provinces = {"a": None, "b": None}   #: side -> lib Province
        self.seam = None                          #: shared seam, metres
        self.placements: list = []                #: where the keys go
        self.checks: list = []                    #: KeyCheck per placement
        self.result = None                        #: last KeyResult, to save
        self._thread: QThread | None = None
        self._worker: CallWorker | None = None
        self._solid_meshes = []                   #: vispy visuals on screen
        self._busy = False                        #: a lib call is running

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_a = QPushButton("Import Province A …")
        self.btn_a.clicked.connect(lambda: self.import_province("a"))
        self.btn_b = QPushButton("Import Province B …")
        self.btn_b.clicked.connect(lambda: self.import_province("b"))
        self.btn_auto = QPushButton("Auto Place")
        self.btn_auto.setToolTip("Replace the keys with one every Spacing mm "
                                 "along the seam, clear of its ends.")
        self.btn_auto.clicked.connect(self.auto_place)
        self.btn_clear = QPushButton("Clear Keys")
        self.btn_clear.clicked.connect(self.clear_keys)
        self.btn_zoom = QPushButton("Zoom to Seam")
        self.btn_zoom.clicked.connect(self.zoom_to_seam)
        self.btn_cut = QPushButton("Cut Slots")
        self.btn_cut.clicked.connect(self.cut)
        self.btn_save = QPushButton("Save …")
        self.btn_save.clicked.connect(self.save)
        for widget in (self.btn_auto, self.btn_clear, self.btn_zoom,
                       self.btn_cut, self.btn_save):
            widget.setEnabled(False)
        for widget in (self.btn_a, self.btn_b, self.btn_auto, self.btn_clear,
                       self.btn_zoom, self.btn_cut, self.btn_save):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- second row: the key --------------------------------------------
        defaults = load_key_params(*settings_files())
        self.depth_spin = self._mm_spin(
            defaults.depth_mm, 0.2, 10.0,
            "Slot depth into each piece's underside (print.dovetail_mm).\n"
            "Must stay under the piece's base, or the slot opens on top.")
        self.neck_spin = self._mm_spin(
            defaults.neck_mm, 0.2, 20.0, "Key width at the seam (the waist).")
        self.mouth_spin = self._mm_spin(
            defaults.mouth_mm, 0.4, 30.0,
            "Key width at each end. Wider than the neck, or nothing is\n"
            "captured behind the waist and the pieces slide apart.")
        self.reach_spin = self._mm_spin(
            defaults.reach_mm, 0.5, 30.0,
            "How far each half reaches into its province from the seam.")
        self.clear_spin = self._mm_spin(
            defaults.clearance_mm, 0.0, 2.0,
            "Slot oversize per side, so a printed key presses in.")
        self.spacing_spin = self._mm_spin(
            defaults.spacing_mm, 2.0, 200.0,
            "Pitch Auto Place uses along the seam.")
        for spin in (self.depth_spin, self.neck_spin, self.mouth_spin,
                     self.reach_spin, self.clear_spin):
            spin.valueChanged.connect(self._params_changed)
        params_bar = QHBoxLayout()
        for label, widget in (("Depth:", self.depth_spin),
                              ("Neck:", self.neck_spin),
                              ("Mouth:", self.mouth_spin),
                              ("Reach:", self.reach_spin),
                              ("Clearance:", self.clear_spin),
                              ("Spacing:", self.spacing_spin)):
            params_bar.addWidget(QLabel(label))
            params_bar.addWidget(widget)
        params_bar.addWidget(QLabel("3D:"))
        self.view_combo = QComboBox()
        self.view_combo.addItems(VIEW_CHOICES)
        self.view_combo.currentTextChanged.connect(self._refresh_3d)
        params_bar.addWidget(self.view_combo)
        params_bar.addStretch(1)
        root.addLayout(params_bar)

        # -- viewports: plan (matplotlib) | 3D result (vispy) ---------------
        panels = QHBoxLayout()
        self.figure = Figure(figsize=(6, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 1, 1)
        self._clear_flat(self.ax_before, "Import two neighbouring provinces")
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_click)
        left = QVBoxLayout()
        left.addWidget(self.mpl_toolbar)
        left.addWidget(self.canvas, stretch=1)
        panels.addLayout(left, stretch=1)

        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("Not yet cut")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)
        root.addLayout(panels, stretch=1)

        self.status = QLabel("No provinces loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- widget helpers -----------------------------------------------------
    @staticmethod
    def _mm_spin(value, lo, hi, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setSingleStep(0.1)
        spin.setSuffix(" mm")
        spin.setToolTip(tip)
        return spin

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        # vispy's orthographic depth range defaults to +/-1e6, which leaves the
        # depth buffer too coarse to tell a key from the slot ceiling 0.1 mm
        # above it: each piece then drew over the half of the key in its slot.
        view.camera.depth_value = DEPTH_RANGE_MM
        return canvas, view

    @staticmethod
    def _clear_flat(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        # datalim, not box: the panel keeps the size the window gives it
        # whatever shape the seam is (BUILDING_UI_HARNESSES §5).
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        """Zoom the plan toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is not self.ax_before or event.xdata is None \
                or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    def params(self) -> KeyParams:
        return load_key_params(*settings_files(),
                               depth_mm=self.depth_spin.value(),
                               neck_mm=self.neck_spin.value(),
                               mouth_mm=self.mouth_spin.value(),
                               reach_mm=self.reach_spin.value(),
                               clearance_mm=self.clear_spin.value(),
                               spacing_mm=self.spacing_spin.value())

    # -- import -------------------------------------------------------------
    def import_province(self, side: str) -> None:
        geojson, _ = QFileDialog.getOpenFileName(
            self, f"Province {side.upper()}: GeoJSON", str(Path.cwd()),
            GEOJSON_FILTER)
        if not geojson:
            return
        stl, _ = QFileDialog.getOpenFileName(
            self, f"Province {side.upper()}: its STL",
            str(Path(geojson).parent), STL_FILTER)
        if not stl:
            return
        self.load(side, Path(stl), Path(geojson))

    def load(self, side: str, stl, geojson) -> None:
        """Load one side off the UI thread (reconciling can take seconds)."""
        if self._thread is not None:
            return
        self._set_busy(True)
        self.status.setText(f"Loading and aligning {Path(str(stl)).name} …")
        self._launch(CallWorker(load_province, stl, geojson, self.params()),
                     lambda province: self._on_loaded(side, province))

    def _on_loaded(self, side: str, province) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.set_province(side, province)

    def set_province(self, side: str, province) -> None:
        """Adopt a loaded province; a new pair gets a fresh seam and keys."""
        self.provinces[side] = province
        a, b = self.provinces["a"], self.provinces["b"]
        self.seam = find_seam(a.geom_m, b.geom_m, self.params()) \
            if a is not None and b is not None else None
        self.placements = auto_place(self.seam, self.params()) \
            if self.seam is not None else []
        self._keys_changed()
        if self.seam is not None:
            self.zoom_to_seam()

    # -- keys ---------------------------------------------------------------
    def auto_place(self) -> None:
        if self.seam is not None:
            self.placements = auto_place(self.seam, self.params())
            self._keys_changed()

    def clear_keys(self) -> None:
        self.placements = []
        self._keys_changed()

    def add_key(self, x: float, y: float) -> None:
        if self.seam is not None:
            self.placements.append(snap(self.seam, x, y))
            self._keys_changed()

    def remove_key_at(self, x: float, y: float) -> bool:
        """Remove the key whose slot contains (x, y); True if one was."""
        point = Point(x, y)
        for i, check in enumerate(self.checks):
            if check.slot.contains(point):
                del self.placements[i]
                self._keys_changed()
                return True
        return False

    def _on_click(self, event) -> None:
        """On a key: remove it.  Near the seam: add one there."""
        if self.mpl_toolbar.mode or event.inaxes is not self.ax_before:
            return                                # pan/zoom owns the mouse
        if event.xdata is None or self.seam is None or event.button != 1:
            return
        if self.remove_key_at(event.xdata, event.ydata):
            return
        if self.seam.distance(Point(event.xdata, event.ydata)) \
                <= self._pick_distance():
            self.add_key(event.xdata, event.ydata)

    def _pick_distance(self) -> float:
        """``PICK_PX`` screen pixels, in map metres at the current zoom."""
        x0, x1 = self.ax_before.get_xlim()
        width_px = max(self.ax_before.bbox.width, 1.0)
        return PICK_PX * (x1 - x0) / width_px

    def _params_changed(self) -> None:
        """The key's shape changed: re-check every key where it stands."""
        self._keys_changed()

    def _keys_changed(self) -> None:
        """Keys moved or changed shape: re-check them; the cut is stale."""
        self.result = None
        a, b = self.provinces["a"], self.provinces["b"]
        self.checks = []
        error = None
        if self.seam is not None and self.placements:
            try:
                self.params().validate()
                self.checks = check_keys(self.seam, self.placements,
                                         a.geom_m.union(b.geom_m),
                                         self.params())
            except ValueError as exc:     # a key that cannot lock: say why
                error = f"Key shape refused: {exc}"
        self._sync_buttons()
        self._draw_plan(preserve_view=True)
        self._refresh_3d()
        self.status.setText(error or self._status_text())

    def _sync_buttons(self) -> None:
        busy = self._busy
        has_seam = self.seam is not None
        for widget in (self.btn_auto, self.btn_clear, self.btn_zoom):
            widget.setEnabled(has_seam and not busy)
        self.btn_cut.setEnabled(any(c.ok for c in self.checks) and not busy)
        self.btn_save.setEnabled(self.result is not None and not busy)

    def _status_text(self) -> str:
        a, b = self.provinces["a"], self.provinces["b"]
        loaded = [f"{p.name} ({p.method}, fit {p.score:.2f})"
                  for p in (a, b) if p is not None]
        if a is None or b is None:
            return ("Loaded: " + ", ".join(loaded) + ". " if loaded else "") \
                + "Import both provinces."
        if self.seam is None:
            return (f"{a.name} and {b.name} share no seam — butterfly keys "
                    "join neighbours.")
        ok = sum(c.ok for c in self.checks)
        refused = [c.reason for c in self.checks if not c.ok]
        text = (f"{', '.join(loaded)}. Seam "
                f"{self.seam.length / self.params().mpm:.0f} mm; {ok} key(s)")
        if refused:
            text += f", {len(refused)} refused ({'; '.join(sorted(set(refused)))})"
        return text + (". Click near the seam to add a key, on one to remove "
                       "it, then ‘Cut Slots’.")

    # -- cut ----------------------------------------------------------------
    def cut(self) -> None:
        a, b = self.provinces["a"], self.provinces["b"]
        if self._thread is not None or a is None or b is None:
            return
        self._set_busy(True)
        self.status.setText("Cutting the slots and building the keys …")
        self._launch(CallWorker(fit_loaded, a, b, self.params(),
                                list(self.placements)), self._on_cut)

    def _on_cut(self, result) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.apply_result(result)

    def apply_result(self, result) -> None:
        self.result = result
        self._sync_buttons()
        self._refresh_3d()
        removed_a = result.a.mesh.volume - result.keyed_a.volume
        removed_b = result.b.mesh.volume - result.keyed_b.volume
        text = (f"Cut {len(result.keys)} slot pair(s): {removed_a:.1f} mm³ "
                f"from {result.a.name}, {removed_b:.1f} mm³ from "
                f"{result.b.name}; {len(result.keys)} key(s) "
                f"{result.params.key_height_mm:.2f} mm tall, all watertight.")
        if result.skipped:
            text += f" {len(result.skipped)} refused key(s) not cut."
        self.status.setText(text + " Inspect the assembly, then save.")

    def save(self) -> None:
        if self.result is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save keyed pieces and keys into …", str(Path.cwd()))
        if not folder:
            return
        try:
            paths = export(self.result, folder)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText("Saved " + ", ".join(p.name for p in paths) + ".")

    # -- threading ----------------------------------------------------------
    def _launch(self, worker, on_done) -> None:
        self._thread = QThread(self)
        self._worker = worker
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.done.connect(on_done)
        worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Failed", message)
        self.status.setText(f"Failed: {message}")

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        for widget in (self.btn_a, self.btn_b, self.depth_spin,
                       self.neck_spin, self.mouth_spin, self.reach_spin,
                       self.clear_spin, self.spacing_spin):
            widget.setEnabled(not busy)
        self._sync_buttons()

    # -- drawing ------------------------------------------------------------
    def _draw_plan(self, preserve_view: bool = False) -> None:
        keep = preserve_view and self.ax_before.has_data()
        if keep:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        names = [p.name for p in self.provinces.values() if p is not None]
        self._clear_flat(self.ax_before, " | ".join(names) or
                         "Import two neighbouring provinces")
        for side, colour in (("b", OTHER_COLOUR), ("a", KEEP_COLOUR)):
            province = self.provinces[side]
            if province is not None:
                self._fill(self.ax_before, province.geom_m, colour)
        if self.seam is not None:
            for part in self.seam.geoms:
                xs, ys = part.xy
                self.ax_before.plot(xs, ys, color=MARK_COLOUR, linewidth=1.4,
                                    zorder=3)
        for check in self.checks:
            colour = RESULT_COLOUR if check.ok else MARK_COLOUR
            xs, ys = check.slot.exterior.xy
            self.ax_before.fill(xs, ys, facecolor=colour, alpha=0.55,
                                edgecolor=colour, linewidth=0.8, zorder=4)
        if keep:
            self.ax_before.set_xlim(xlim)
            self.ax_before.set_ylim(ylim)
        self.canvas.draw_idle()

    @staticmethod
    def _fill(ax, geom, colour) -> None:
        polygons = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for poly in polygons:
            xs, ys = poly.exterior.xy
            ax.fill(xs, ys, facecolor=colour, edgecolor=EDGE_COLOUR,
                    linewidth=0.4, zorder=1)
            for ring in poly.interiors:
                xs, ys = ring.xy
                ax.fill(xs, ys, facecolor="white", edgecolor=EDGE_COLOUR,
                        linewidth=0.3, zorder=2)

    def zoom_to_seam(self) -> None:
        """Frame the seam, grown to the panel's shape so nothing is cropped."""
        if self.seam is None:
            return
        minx, miny, maxx, maxy = self.seam.bounds
        reach = self.params().reach_mm * self.params().mpm * 3
        minx, miny, maxx, maxy = minx - reach, miny - reach, maxx + reach, \
            maxy + reach
        fig_w, fig_h = self.figure.get_size_inches()
        pos = self.ax_before.get_subplotspec().get_position(self.figure)
        target = (pos.width * fig_w) / (pos.height * fig_h)
        width, height = maxx - minx, maxy - miny
        if width / height < target:
            width = height * target
        else:
            height = width / target
        grow = 1.0 + 2.0 * SEAM_ZOOM_PAD
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        self.ax_before.set_xlim(cx - width * grow / 2, cx + width * grow / 2)
        self.ax_before.set_ylim(cy - height * grow / 2, cy + height * grow / 2)
        self.canvas.draw_idle()

    def _refresh_3d(self) -> None:
        """Draw what the 3D selector asks for from the last cut."""
        if not hasattr(self, "view_combo"):
            return
        for visual in self._solid_meshes:
            visual.parent = None
        self._solid_meshes.clear()
        if self.result is None:
            self.solid_title.setText("Not yet cut")
            self.solid_canvas.update()
            return
        view = self.view_combo.currentText()
        if view == "Assembly":
            parts = assembly(self.result)
            meshes = [(parts[0][1], A_RGBA), (parts[1][1], B_RGBA)] + \
                [(m, KEY_RGBA) for _, m in parts[2:]]
            title = (f"Assembly — {len(parts) - 2} key(s) in their slots, "
                     "seen from below")
        elif view == "Keys":
            meshes = [(self.result.keys_mesh, KEY_RGBA)]
            title = f"{len(self.result.keys)} key(s), laid out for the bed"
        else:
            side = self.result.a if view == "Province A" else self.result.b
            mesh = self.result.keyed_a if view == "Province A" \
                else self.result.keyed_b
            meshes = [(mesh, A_RGBA if view == "Province A" else B_RGBA)]
            title = f"{side.name}, slotted — seen from below"
        for mesh, colour in meshes:
            # Keys unlit: seen from below their faces point away from the
            # light and shade as dark as the slot ceiling behind them, which
            # makes a seated key look like an empty slot.
            self._solid_meshes.append(scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces), color=colour,
                shading=None if colour is KEY_RGBA else "flat",
                parent=self.solid_view.scene))
        self.solid_title.setText(title)
        # The slots are in the underside: look up at it unless it is the keys.
        self.solid_view.camera.elevation = 28 if view == "Keys" else -60
        self.solid_view.camera.set_range()
        self.solid_canvas.update()

    def closeEvent(self, event) -> None:
        self._teardown_thread()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")          # before the window builds its scene
    window = ButterflyKeysTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
