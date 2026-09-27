"""
UI_terrain_trim.py

A standalone visual harness for ``LIB_terrain_trim.trim_terrain``.

Workflow
--------
1. **Import Slab STL …** — load a DEMto3D terrain slab (rectangular, with the
                           province raised on a flat nodata skirt).
2. **Import Province …** — the province GeoJSON the DEM was clipped by; the cut
                           follows its coastline.
3. **Scale / Build CRS / Buffer / Min fit** — the map scale the slab was printed
                           at, the projected CRS DEMto3D built it in (auto-guessed
                           from the footprints on load), an optional coastal lip,
                           and the minimum reconciliation overlap to accept.
4. **Trim to Coastline** — reconcile the slab's frame and intersect it with the
                           province polygon (off the UI thread, with a progress
                           bar).
5. **Save …**            — write the trimmed STL.

The **left** panel shows placement: the reconciled province polygon (blue) laid
inside the original slab extent (grey), so you can see how much rectangular skirt
is removed and that the cut sits where it should; the title reports the fit
score.  The **right** panel previews the trimmed solid in 3D (a GPU vispy scene),
coloured by elevation.

Run with:  python harnesses/UI_terrain_trim.py

Like the other UI_* harnesses this stays independent of the main ``app`` window.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import trimesh
import vispy
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.affinity import affine_transform
from shapely.geometry import box
from vispy import scene

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_terrain_trim.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_border_connectors import ConnectorParams, DEFAULT_SCALE
from lib.LIB_terrain_trim import trim_terrain, _load_union_crs

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

#: Projected metric CRSes DEMto3D commonly builds in for this project.
CRS_CHOICES = ["EPSG:3347", "EPSG:3979"]

GEOJSON_COLOUR = "#AED6F1"   # blue — the reconciled province polygon (the cut)
SLAB_COLOUR = "#B3B6B7"      # grey — the original rectangular slab extent
EDGE_COLOUR = "#1B4F72"      # dark blue outline
BG_COLOUR = "#FDFEFE"        # near-white 3D scene background


def _matrix_from_result(result, mpm):
    """Rebuild the metres->STL-mm affine from a TrimResult (mirrors the lib)."""
    x0, y0 = result.origin_m
    e = (-1.0 / mpm) if result.flipped_y else (1.0 / mpm)
    xoff = -x0 / mpm
    yoff = (y0 / mpm) if result.flipped_y else (-y0 / mpm)
    return [1.0 / mpm, 0.0, 0.0, e, xoff, yoff]


def _elevation_colors(mesh):
    """Per-vertex RGBA (float32) from a terrain colormap over the mesh's z."""
    z = np.asarray(mesh.vertices, dtype=float)[:, 2]
    lo, hi = float(z.min()), float(z.max())
    t = (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)
    return colormaps["terrain"](t).astype(np.float32)


class TrimWorker(QObject):
    """Runs ``trim_terrain`` off the UI thread (the boolean cut is slow)."""
    done = Signal(object)   # TrimResult
    failed = Signal(str)

    def __init__(self, stl, gj, params, build_crs, buffer_mm, min_fit):
        super().__init__()
        self._stl = stl
        self._gj = gj
        self._params = params
        self._build_crs = build_crs
        self._buffer = buffer_mm
        self._min_fit = min_fit

    def run(self) -> None:
        try:
            result = trim_terrain(self._stl, self._gj, params=self._params,
                                  build_crs=self._build_crs,
                                  buffer_mm=self._buffer,
                                  min_fit_score=self._min_fit)
        except Exception as exc:      # surface any failure to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class TerrainTrimTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Terrain Trim — Visual Tester")
        self.resize(1320, 760)

        # -- state ----------------------------------------------------------
        self.slab_path: Path | None = None
        self.slab_mesh = None              # loaded DEMto3D slab (Trimesh)
        self.gj_path: Path | None = None
        self.result = None                 # TrimResult after a run
        self._thread: QThread | None = None
        self._worker: TrimWorker | None = None
        self._solid_meshes: list = []      # vispy visuals currently shown

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_slab = QPushButton("Import Slab STL …")
        self.btn_slab.clicked.connect(self.import_slab)
        self.btn_gj = QPushButton("Import Province …")
        self.btn_gj.clicked.connect(self.import_province)

        self.scale_spin = self._spin(1.0, 100_000_000.0, float(DEFAULT_SCALE), 0,
                                     "Map-scale denominator the slab was printed at")
        self.scale_spin.setSingleStep(500_000.0)
        self.scale_spin.valueChanged.connect(self._invalidate)

        self.crs_combo = QComboBox()
        self.crs_combo.setEditable(True)
        self.crs_combo.addItems(CRS_CHOICES)
        self.crs_combo.setToolTip("Projected CRS DEMto3D built the model in")
        self.crs_combo.currentTextChanged.connect(self._invalidate)

        self.buf_spin = self._spin(0.0, 20.0, 0.0, 2, "Coastal lip added to the cut")
        self.buf_spin.setSuffix(" mm")
        self.buf_spin.valueChanged.connect(self._invalidate)
        self.fit_spin = self._spin(0.5, 1.0, 0.9, 2,
                                   "Minimum footprint overlap to accept a fit")
        self.fit_spin.setSingleStep(0.05)
        self.fit_spin.valueChanged.connect(self._invalidate)

        self.btn_trim = QPushButton("Trim to Coastline")
        self.btn_trim.clicked.connect(self.trim)
        self.btn_trim.setEnabled(False)
        self.btn_save = QPushButton("Save …")
        self.btn_save.clicked.connect(self.save)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_slab)
        toolbar.addWidget(self.btn_gj)
        toolbar.addWidget(QLabel("Scale 1:"))
        toolbar.addWidget(self.scale_spin)
        toolbar.addWidget(QLabel("Build CRS:"))
        toolbar.addWidget(self.crs_combo)
        toolbar.addWidget(QLabel("Buffer:"))
        toolbar.addWidget(self.buf_spin)
        toolbar.addWidget(QLabel("Min fit:"))
        toolbar.addWidget(self.fit_spin)
        toolbar.addWidget(self.btn_trim)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- panels: placement (matplotlib) | 3D (vispy) -------------------
        panels = QHBoxLayout()

        self.figure = Figure(figsize=(5.5, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._clear_flat(self.ax, "Import a slab STL and a province GeoJSON")
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        mid = QVBoxLayout()
        mid.addWidget(self.mpl_toolbar)
        mid.addWidget(self.canvas, stretch=1)
        panels.addLayout(mid, stretch=1)

        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("3D preview")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)

        root.addLayout(panels, stretch=1)

        # -- status + progress ---------------------------------------------
        self.status = QLabel("No slab loaded.")
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
    def _show_solid(self, title: str, mesh) -> None:
        self.solid_title.setText(title)
        for v in self._solid_meshes:
            v.parent = None
        self._solid_meshes.clear()
        if mesh is not None and not mesh.is_empty:
            vis = scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces),
                vertex_colors=_elevation_colors(mesh), shading="flat",
                parent=self.solid_view.scene)
            self._solid_meshes.append(vis)
            self.solid_view.camera.set_range()
        self.solid_canvas.update()

    # -- import -------------------------------------------------------------
    def import_slab(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import DEMto3D slab STL", str(Path.cwd()), STL_FILTER)
        if not path:
            return
        try:
            mesh = trimesh.load(path, force="mesh")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
            QMessageBox.warning(self, "Not a mesh",
                                "That file did not load as a single mesh.")
            return
        self.slab_path = Path(path)
        self.slab_mesh = mesh
        self._invalidate()
        self._show_solid(f"Slab: {self.slab_path.name}", mesh)
        self._suggest_crs()
        self._draw_placement()
        self._sync_trim_enabled()
        self.status.setText(
            f"Slab loaded from {self.slab_path.name} "
            f"({len(mesh.vertices):,} verts). Import the province GeoJSON.")

    def import_province(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Province GeoJSON", str(Path.cwd()), GEOJSON_FILTER)
        if not path:
            return
        self.gj_path = Path(path)
        self._invalidate()
        self._suggest_crs()
        self._draw_placement()
        self._sync_trim_enabled()
        self.status.setText(
            f"Province {self.gj_path.name} loaded. "
            "Check the scale / CRS and click ‘Trim to Coastline’.")

    def _sync_trim_enabled(self) -> None:
        self.btn_trim.setEnabled(self.slab_mesh is not None
                                 and self.gj_path is not None)

    def _suggest_crs(self) -> None:
        """Pre-select the CRS whose scaled polygon bbox best matches the slab."""
        if self.slab_mesh is None or self.gj_path is None:
            return
        b = self.slab_mesh.bounds
        sw, sh = float(b[1][0] - b[0][0]), float(b[1][1] - b[0][1])
        if sw <= 0 or sh <= 0:
            return
        mpm = self.scale_spin.value() / 1000.0
        best = None
        for crs in CRS_CHOICES:
            try:
                geom = _load_union_crs(str(self.gj_path), crs)
            except Exception:
                continue
            if geom is None or geom.is_empty:
                continue
            minx, miny, maxx, maxy = geom.bounds
            pw, ph = (maxx - minx) / mpm, (maxy - miny) / mpm
            err = abs(pw - sw) / sw + abs(ph - sh) / sh
            if best is None or err < best[0]:
                best = (err, crs)
        if best is not None and best[0] < 0.25:
            self.crs_combo.blockSignals(True)
            self.crs_combo.setCurrentText(best[1])
            self.crs_combo.blockSignals(False)
            self.status.setText(f"Guessed build CRS {best[1]} "
                                f"(footprint match, err {best[0]:.2f}).")

    # -- parameters ---------------------------------------------------------
    def _params(self) -> ConnectorParams:
        return ConnectorParams(scale=self.scale_spin.value())

    def _invalidate(self, *_) -> None:
        """Any input/parameter change invalidates the last trim."""
        if self.result is None:
            return
        self.result = None
        self.btn_save.setEnabled(False)
        if self.slab_mesh is not None:
            self._show_solid(f"Slab: {self.slab_path.name}", self.slab_mesh)
        self._draw_placement()

    # -- trim ---------------------------------------------------------------
    def trim(self) -> None:
        if self.slab_mesh is None or self.gj_path is None or self._thread is not None:
            return
        self._set_busy(True)
        self.status.setText("Trimming slab to the coastline …")

        self._thread = QThread(self)
        self._worker = TrimWorker(str(self.slab_path), str(self.gj_path),
                                  self._params(), self.crs_combo.currentText().strip(),
                                  self.buf_spin.value(), self.fit_spin.value())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_trim)
        self._worker.failed.connect(self._on_trim_failed)
        self._thread.start()

    def _on_trim(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        self.btn_save.setEnabled(True)
        self._show_solid(
            f"Trimmed — fit {result.score:.2f}, kept {result.kept_fraction*100:.0f}%"
            + ("  (Y-flipped)" if result.flipped_y else ""),
            result.mesh)
        self._draw_placement()
        self.status.setText(
            f"Trimmed: fit {result.score:.2f}, "
            f"kept {result.kept_fraction*100:.1f}% of the slab volume, "
            f"watertight={result.mesh.is_watertight}. Save when satisfied.")

    def _on_trim_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Trim failed", message)
        self.status.setText(f"Trim failed: {message}")

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
        for w in (self.btn_slab, self.btn_gj, self.scale_spin, self.crs_combo,
                  self.buf_spin, self.fit_spin, self.btn_trim, self.btn_save):
            w.setEnabled(not busy)
        if not busy:
            self._sync_trim_enabled()

    # -- drawing ------------------------------------------------------------
    def _draw_placement(self) -> None:
        """The reconciled province polygon inside the original slab extent."""
        if self.slab_mesh is None:
            self._clear_flat(self.ax, "Import a slab STL and a province GeoJSON")
            self.canvas.draw_idle()
            return

        title = "Placement"
        self._clear_flat(self.ax, title)

        # Original slab footprint (print-mm) as a grey rectangle.
        b = self.slab_mesh.bounds
        rect = box(float(b[0][0]), float(b[0][1]), float(b[1][0]), float(b[1][1]))
        gpd.GeoSeries([rect]).plot(ax=self.ax, facecolor=SLAB_COLOUR,
                                   edgecolor=EDGE_COLOUR, linewidth=0.5, alpha=0.35)

        # The province polygon, mapped into the slab's own print-mm frame.
        poly_mm = self._reconciled_polygon()
        if poly_mm is not None:
            gpd.GeoSeries([poly_mm]).plot(ax=self.ax, facecolor=GEOJSON_COLOUR,
                                          edgecolor=EDGE_COLOUR, linewidth=0.6,
                                          alpha=0.7)
        if self.result is not None:
            self.ax.set_title(
                f"Placement — reconcile fit {self.result.score:.2f}"
                + ("  (Y-flipped)" if self.result.flipped_y else ""),
                fontsize=11)
        elif poly_mm is None:
            self.ax.set_title("Placement (run Trim to reconcile the polygon)",
                              fontsize=11)
        self.canvas.draw_idle()

    def _reconciled_polygon(self):
        """Province polygon in the slab's print-mm frame, if it can be placed.

        After a trim the exact recovered transform is used; before one, the
        polygon is centred on the slab as a rough preview.
        """
        if self.gj_path is None:
            return None
        crs = self.crs_combo.currentText().strip()
        try:
            geom_m = _load_union_crs(str(self.gj_path), crs)
        except Exception:
            return None
        if geom_m is None or geom_m.is_empty:
            return None
        mpm = self._params().mpm

        if self.result is not None:
            return affine_transform(geom_m, _matrix_from_result(self.result, mpm))

        # No fit yet: scale and centre on the slab for a rough placement preview.
        scaled = affine_transform(geom_m, [1.0 / mpm, 0, 0, 1.0 / mpm, 0, 0])
        b = self.slab_mesh.bounds
        scx, scy = (float(b[0][0] + b[1][0]) / 2, float(b[0][1] + b[1][1]) / 2)
        pc = scaled.centroid
        return affine_transform(scaled, [1, 0, 0, 1, scx - pc.x, scy - pc.y])

    # -- save ---------------------------------------------------------------
    def save(self) -> None:
        if self.result is None:
            return
        default = str((self.slab_path.parent / f"{self.slab_path.stem}_trimmed.stl")
                      if self.slab_path else Path.cwd() / "trimmed.stl")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save trimmed STL", default, STL_FILTER)
        if not path:
            return
        try:
            self.result.mesh.export(path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved trimmed STL to {Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")
    window = TerrainTrimTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
