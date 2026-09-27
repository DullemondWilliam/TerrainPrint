"""
UI_dovetail_fitter.py

A standalone visual harness for ``LIB_dovetail_fitter.fit_dovetails``.

Workflow
--------
1. **Import Border …** — load the carved border GeoJSON (the corridor).
2. **Add Province …**  — pick a province GeoJSON and its printed STL; repeat for
                         each printed piece.  The name is taken from the GeoJSON.
3. **Scale / Dovetail / Min fit** — the map scale the STLs were printed at, the
                         pocket depth, and the minimum reconciliation overlap to
                         accept.
4. **Fit Dovetails**   — reconcile every STL to its GeoJSON and carve the pockets
                         (off the UI thread, with a progress bar), then build the
                         matching border tabs.
5. **Save …**          — write the keyed province STLs and the border tabs.

Selecting a province in the list shows, on the **left**, its reconciliation: the
STL's base footprint (orange outline) laid over the GeoJSON footprint mapped
through the recovered transform (blue) — if they coincide, the fit is good, and
the title reports the overlap score.  On the **right**, the notched province is
previewed in 3D (a GPU vispy scene); pick "◆ Border tabs" to see the tab piece.

Run with:  python harnesses/UI_dovetail_fitter.py

Like the other UI_* harnesses this stays independent of the main ``app`` window.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import trimesh
import vispy
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.affinity import affine_transform
from vispy import scene

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_dovetail_fitter.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_border_connectors import ConnectorParams, EPSG_METRIC, _load_union_m
from lib.LIB_dovetail_fitter import fit_dovetails, _base_footprint, _safe_name
from lib.LIB_project_config import get as _cfg

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

GEOJSON_COLOUR = "#AED6F1"   # blue   — the GeoJSON footprint (ground truth)
STL_COLOUR = "#E67E22"       # orange — the STL base footprint (as reconciled)
BORDER_COLOUR = "#E74C3C"    # red    — the border corridor
EDGE_COLOUR = "#1B4F72"      # dark blue outline
BG_COLOUR = "#FDFEFE"        # near-white 3D scene background
PIECE_COLOURS = ["#ABEBC6", "#F9E79F", "#F5B7B1", "#D7BDE2"]

_ROLE = Qt.UserRole
_BORDER_KEY = "◆ Border tabs"   # ◆ Border tabs


def _rgba(hex_colour: str, alpha: float = 1.0) -> tuple:
    """'#RRGGBB' -> (r, g, b, a) floats in 0..1 for vispy."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def _matrix_from_report(report, mpm):
    """Rebuild the metres->STL-mm affine from a ReconcileReport (see the lib)."""
    x0, y0 = report.origin_m
    e = (-1.0 / mpm) if report.flipped_y else (1.0 / mpm)
    xoff = -x0 / mpm
    yoff = (y0 / mpm) if report.flipped_y else (-y0 / mpm)
    return [1.0 / mpm, 0.0, 0.0, e, xoff, yoff]


class FitWorker(QObject):
    """Runs ``fit_dovetails`` off the UI thread (booleans per province are slow)."""
    done = Signal(object)   # FitResult
    failed = Signal(str)

    def __init__(self, border_path, specs, params, min_fit_score):
        super().__init__()
        self._border = border_path
        self._specs = specs
        self._params = params
        self._min_fit = min_fit_score

    def run(self) -> None:
        try:
            result = fit_dovetails(self._border, self._specs,
                                   params=self._params,
                                   min_fit_score=self._min_fit)
        except Exception as exc:      # surface any failure to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class DovetailFitterTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dovetail Fitter — Visual Tester")
        self.resize(1320, 760)

        # -- state ----------------------------------------------------------
        self.border_path: Path | None = None
        self.border_geom_m = None
        self.specs: list[dict] = []        # {name, geojson, stl, geom_m}
        self.result = None                 # FitResult after a run
        self._mesh_cache: dict = {}        # name -> loaded input STL (for overlay)
        self._thread: QThread | None = None
        self._worker: FitWorker | None = None
        self._solid_meshes: list = []      # vispy visuals currently shown

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_border = QPushButton("Import Border …")
        self.btn_border.clicked.connect(self.import_border)
        self.btn_add = QPushButton("Add Province …")
        self.btn_add.clicked.connect(self.add_province)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self.remove_province)

        self.scale_spin = self._spin(1.0, 100_000_000.0, float(_cfg("print.scale")), 0,
                                     "Map-scale denominator the STLs were printed at")
        self.scale_spin.setSingleStep(500_000.0)
        self.dove_spin = self._spin(0.1, 20.0, 1.0, 2, "Dovetail pocket depth (mm)")
        self.dove_spin.setSuffix(" mm")
        self.fit_spin = self._spin(0.5, 1.0, 0.9, 2,
                                   "Minimum footprint overlap to accept a fit")
        self.fit_spin.setSingleStep(0.05)
        for s in (self.scale_spin, self.dove_spin, self.fit_spin):
            s.valueChanged.connect(self._invalidate)

        self.btn_fit = QPushButton("Fit Dovetails")
        self.btn_fit.clicked.connect(self.fit)
        self.btn_fit.setEnabled(False)
        self.btn_save = QPushButton("Save …")
        self.btn_save.clicked.connect(self.save)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_border)
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(QLabel("Scale 1:"))
        toolbar.addWidget(self.scale_spin)
        toolbar.addWidget(QLabel("Dovetail:"))
        toolbar.addWidget(self.dove_spin)
        toolbar.addWidget(QLabel("Min fit:"))
        toolbar.addWidget(self.fit_spin)
        toolbar.addWidget(self.btn_fit)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- panels: list | reconciliation (matplotlib) | 3D (vispy) -------
        panels = QHBoxLayout()

        self.plist = QListWidget()
        self.plist.setMaximumWidth(240)
        self.plist.currentItemChanged.connect(lambda *_: self._refresh_selection())
        panels.addWidget(self.plist)

        # Middle: the flat reconciliation overlay is a 2-D matplotlib map.
        self.figure = Figure(figsize=(5.5, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._clear_flat(self.ax, "Import a border and add provinces")
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        mid = QVBoxLayout()
        mid.addWidget(self.mpl_toolbar)
        mid.addWidget(self.canvas, stretch=1)
        panels.addLayout(mid, stretch=1)

        # Right: the notched solid / border tabs, previewed on the GPU.
        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("3D preview")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)

        root.addLayout(panels, stretch=1)

        # -- status + progress ---------------------------------------------
        self.status = QLabel("No border loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- construction helpers ----------------------------------------------
    @staticmethod
    def _spin(lo, hi, val, decimals, tip):
        s = QDoubleSpinBox()
        s.setDecimals(decimals)
        s.setRange(lo, hi)
        s.setValue(val)
        s.setToolTip(tip)
        return s

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    # -- flat panel helpers -------------------------------------------------
    @staticmethod
    def _clear_flat(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        ax = event.inaxes
        if ax is not self.ax or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    # -- 3D panel helpers ---------------------------------------------------
    def _show_solid(self, title: str, mesh, colour) -> None:
        self.solid_title.setText(title)
        for v in self._solid_meshes:
            v.parent = None
        self._solid_meshes.clear()
        if mesh is not None and not mesh.is_empty:
            vis = scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces), color=colour, shading="flat",
                parent=self.solid_view.scene)
            self._solid_meshes.append(vis)
            self.solid_view.camera.set_range()

    # -- import / add / remove ---------------------------------------------
    def import_border(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import border GeoJSON", str(Path.cwd()), GEOJSON_FILTER)
        if not path:
            return
        try:
            geom = _load_union_m(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        if geom is None or geom.is_empty:
            QMessageBox.warning(self, "Nothing to show",
                                "No border geometry in that file.")
            return
        self.border_path = Path(path)
        self.border_geom_m = geom
        self._invalidate()
        self._draw_inputs()
        self.status.setText(
            f"Border loaded from {self.border_path.name}. Add province pieces.")

    def add_province(self) -> None:
        gj, _ = QFileDialog.getOpenFileName(
            self, "Province GeoJSON", str(Path.cwd()), GEOJSON_FILTER)
        if not gj:
            return
        name = self._name_from(gj)
        stl, _ = QFileDialog.getOpenFileName(
            self, f"STL for {name}", str(Path(gj).parent), STL_FILTER)
        if not stl:
            return
        try:
            geom_m = _load_union_m(gj)
        except Exception as exc:
            QMessageBox.critical(self, "GeoJSON failed", str(exc))
            return
        if geom_m is None or geom_m.is_empty:
            QMessageBox.warning(self, "Empty province", f"No geometry in {name}.")
            return

        self.specs.append({"name": name, "geojson": gj, "stl": stl,
                           "geom_m": geom_m})
        item = QListWidgetItem(name)
        item.setData(_ROLE, ("province", name))
        self.plist.addItem(item)
        self._invalidate()
        self._draw_inputs()
        self._sync_fit_enabled()
        self.status.setText(
            f"Added {name} ({len(self.specs)} province(s)). "
            "Set the scale and click ‘Fit Dovetails’.")

    def remove_province(self) -> None:
        item = self.plist.currentItem()
        if item is None:
            return
        kind, name = item.data(_ROLE)
        if kind != "province":
            return
        self.specs = [s for s in self.specs if s["name"] != name]
        self.plist.takeItem(self.plist.row(item))
        self._invalidate()
        self._draw_inputs()
        self._sync_fit_enabled()

    @staticmethod
    def _name_from(geojson_path: str) -> str:
        stem = Path(geojson_path).stem
        if stem.startswith("name_"):
            stem = stem[len("name_"):]
        return stem.replace("_", " ")

    def _sync_fit_enabled(self) -> None:
        self.btn_fit.setEnabled(self.border_geom_m is not None and bool(self.specs))

    # -- parameters ---------------------------------------------------------
    def _params(self) -> ConnectorParams:
        return ConnectorParams(scale=self.scale_spin.value(),
                               dovetail_mm=self.dove_spin.value())

    def _invalidate(self, *_) -> None:
        """Any input/parameter change invalidates the last fit."""
        if self.result is None and not self._has_border_row():
            return
        self.result = None
        self._mesh_cache.clear()
        self.btn_save.setEnabled(False)
        self._remove_border_row()
        self._show_solid("3D preview", None, None)
        self.solid_canvas.update()

    # -- fit ----------------------------------------------------------------
    def fit(self) -> None:
        if self.border_geom_m is None or not self.specs or self._thread is not None:
            return
        specs = [(s["name"], s["geojson"], s["stl"]) for s in self.specs]

        self._set_busy(True)
        self.status.setText(f"Fitting dovetails for {len(specs)} province(s) …")

        # A single blocking call (booleans per province), so run it off the UI
        # thread and drive an indeterminate progress bar meanwhile.
        self._thread = QThread(self)
        self._worker = FitWorker(str(self.border_path), specs, self._params(),
                                 self.fit_spin.value())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_fit)
        self._worker.failed.connect(self._on_fit_failed)
        self._thread.start()

    def _on_fit(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        if result.border is not None and not self._has_border_row():
            item = QListWidgetItem(_BORDER_KEY)
            item.setData(_ROLE, ("border", None))
            self.plist.addItem(item)
        self.btn_save.setEnabled(True)
        worst = min((r.score for r in result.reports), default=1.0)
        self.status.setText(
            f"Keyed {len(result.provinces)} province(s); worst fit {worst:.2f}. "
            "Select a piece to inspect, then save.")
        self._refresh_selection()

    def _on_fit_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Fit failed", message)
        self.status.setText(f"Fit failed: {message}")

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        self.progress.setValue(0 if busy else 1)
        for w in (self.btn_border, self.btn_add, self.btn_remove, self.scale_spin,
                  self.dove_spin, self.fit_spin, self.btn_fit, self.btn_save):
            w.setEnabled(not busy)
        if not busy:
            self._sync_fit_enabled()

    # -- list-row bookkeeping ----------------------------------------------
    def _has_border_row(self) -> bool:
        return any(self.plist.item(i).data(_ROLE)[0] == "border"
                   for i in range(self.plist.count()))

    def _remove_border_row(self) -> None:
        for i in range(self.plist.count()):
            if self.plist.item(i).data(_ROLE)[0] == "border":
                self.plist.takeItem(i)
                return

    # -- drawing ------------------------------------------------------------
    def _draw_inputs(self) -> None:
        """Overview of the loaded GeoJSON footprints (metres), before fitting."""
        self._clear_flat(self.ax, "Inputs (metres)")
        for spec in self.specs:
            gpd.GeoSeries([spec["geom_m"]]).plot(
                ax=self.ax, facecolor=GEOJSON_COLOUR, edgecolor=EDGE_COLOUR,
                linewidth=0.5, alpha=0.7)
        if self.border_geom_m is not None:
            gpd.GeoSeries([self.border_geom_m]).plot(
                ax=self.ax, facecolor=BORDER_COLOUR, edgecolor="none", alpha=0.85)
        self.canvas.draw_idle()

    def _refresh_selection(self) -> None:
        item = self.plist.currentItem()
        if item is None or self.result is None:
            if self.result is None:
                self._draw_inputs()
            return
        kind, name = item.data(_ROLE)
        if kind == "border":
            self._clear_flat(self.ax, "Border corridor (metres)")
            if self.border_geom_m is not None:
                gpd.GeoSeries([self.border_geom_m]).plot(
                    ax=self.ax, facecolor=BORDER_COLOUR, edgecolor=EDGE_COLOUR,
                    linewidth=0.4, alpha=0.85)
            self.canvas.draw_idle()
            self._show_solid("Border tabs", self.result.border, _rgba(BORDER_COLOUR))
        else:
            self._draw_overlay(name)
            colour = PIECE_COLOURS[self._province_index(name) % len(PIECE_COLOURS)]
            self._show_solid(f"{name} (keyed)", self.result.provinces.get(name),
                             _rgba(colour))

    def _draw_overlay(self, name: str) -> None:
        """STL base footprint over the reconciled GeoJSON footprint (STL frame)."""
        spec = self._spec(name)
        report = self._report(name)
        if spec is None or report is None:
            return
        mpm = self._params().mpm
        gfp = affine_transform(spec["geom_m"], _matrix_from_report(report, mpm))
        mesh = self._input_mesh(name)
        sfp = _base_footprint(mesh) if mesh is not None else None

        self._clear_flat(self.ax, f"{name} — reconcile fit {report.score:.2f}"
                                  + ("  (Y-flipped)" if report.flipped_y else ""))
        gpd.GeoSeries([gfp]).plot(ax=self.ax, facecolor=GEOJSON_COLOUR,
                                  edgecolor=EDGE_COLOUR, linewidth=0.4, alpha=0.55)
        if sfp is not None and not sfp.is_empty:
            gpd.GeoSeries([sfp]).plot(ax=self.ax, facecolor="none",
                                      edgecolor=STL_COLOUR, linewidth=1.3)
        self.canvas.draw_idle()

    # -- lookups ------------------------------------------------------------
    def _spec(self, name):
        return next((s for s in self.specs if s["name"] == name), None)

    def _province_index(self, name):
        return next((i for i, s in enumerate(self.specs) if s["name"] == name), 0)

    def _report(self, name):
        if self.result is None:
            return None
        return next((r for r in self.result.reports if r.name == name), None)

    def _input_mesh(self, name):
        """The original (un-notched) STL, loaded on demand for the overlay."""
        if name not in self._mesh_cache:
            spec = self._spec(name)
            if spec is None:
                return None
            try:
                self._mesh_cache[name] = trimesh.load(spec["stl"], force="mesh")
            except Exception:
                self._mesh_cache[name] = None
        return self._mesh_cache[name]

    # -- save ---------------------------------------------------------------
    def save(self) -> None:
        if self.result is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save keyed pieces into folder", str(Path.cwd()))
        if not folder:
            return
        folder = Path(folder)
        try:
            for name, mesh in self.result.provinces.items():
                mesh.export(str(folder / f"{_safe_name(name)}_keyed.stl"))
            if self.result.border is not None:
                self.result.border.export(str(folder / "border_connectors.stl"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {len(self.result.provinces)} keyed province(s) + border "
            f"to {folder.name}.")


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")
    window = DovetailFitterTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
