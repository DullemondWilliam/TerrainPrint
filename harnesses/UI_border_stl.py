"""
UI_border_stl.py

A standalone visual harness for ``LIB_border_stl.build_border_stl``.

Workflow
--------
1. **Import GeoJSON …** — load the border-region polygons (e.g. the
                          ``*_border.geojson`` produced by ``UI_border_regions``).
2. **Height (mm)**      — set the extrusion height of the flat STL bar.
3. **Print scale**      — optionally convert the projected metres to print-mm
                          (1 print-mm = ``scale`` mm on the ground) before
                          extruding, matching the ``LIB_border_connectors``
                          convention.  Leave off to keep raw metres.
4. **Extrude**          — run the lib (off the UI thread, with an indeterminate
                          progress bar) and preview the watertight solid.
5. **Save STL …**       — write the mesh out as an ``.stl``.

The imported footprint is drawn flat on the left ("before") with matplotlib; the
extruded watertight solid is previewed on the right ("after") in a GPU-accelerated
``vispy`` scene (drag to rotate, scroll to zoom).  The output is a 3D mesh, so the
panels do not share a view.

Run with:  python harnesses/UI_border_stl.py

Like the other UI_* harnesses this stays independent of the main ``app`` window
so it can be pointed at any GeoJSON without setting up a project.  Imported
features are reprojected into the build CRS (EPSG:3347, metres) because the lib
builds in metres before the optional print-mm conversion.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import vispy
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_border_stl.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_border_stl import build_border_stl

EPSG_SRC = "EPSG:4326"      # WGS84 – assumed source CRS when a file carries none
EPSG_BUILD = "EPSG:3347"    # Statistics Canada Lambert, metres (lib builds in metres)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

KEEP_COLOUR = "#AED6F1"     # blue  — imported footprint
RESULT_COLOUR = "#ABEBC6"   # green — the extruded solid
EDGE_COLOUR = "#1B4F72"     # dark blue outline
BG_COLOUR = "#FDFEFE"       # near-white 3D scene background


def _rgba(hex_colour: str, alpha: float = 1.0) -> tuple:
    """'#RRGGBB' -> (r, g, b, a) floats in 0..1 for vispy."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def _read_projected(paths: list[str]) -> tuple[gpd.GeoDataFrame | None, object]:
    """
    Read one or more GeoJSON files and reproject them into the build CRS.

    Returns ``(gdf_in_build_crs, source_crs)``.  ``source_crs`` is the CRS the
    first readable file carried (or WGS84 when a file has none), kept only so the
    default STL name can be derived from the source; the STL itself is written in
    the build CRS / print-mm the lib produced.  Raises ``RuntimeError`` listing
    any files that could not be read.
    """
    frames, failures, source_crs = [], [], None
    for path in paths:
        try:
            frame = gpd.read_file(path)
        except Exception as exc:
            failures.append(f"{Path(path).name}: {exc}")
            continue
        if frame.empty:
            continue
        if source_crs is None:
            source_crs = frame.crs if frame.crs is not None else EPSG_SRC
        # Assume WGS84 when a file carries no CRS, then bring everything into the
        # metre-based build CRS so the lib extrudes in real ground units.
        if frame.crs is None:
            frame = frame.set_crs(EPSG_SRC)
        frames.append(frame.to_crs(EPSG_BUILD))

    if failures:
        raise RuntimeError("\n".join(failures))
    if not frames:
        return None, None
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=EPSG_BUILD), source_crs


class ExtrudeWorker(QObject):
    """Runs ``build_border_stl`` off the UI thread (the watertight pass is slow)."""
    done = Signal(object)   # trimesh.Trimesh
    failed = Signal(str)

    def __init__(self, geom, height_mm: float, scale):
        super().__init__()
        self._geom = geom
        self._height_mm = height_mm
        self._scale = scale

    def run(self) -> None:
        try:
            # geometry_crs=None: the footprint is already in the build CRS, so
            # the lib extrudes it in place (optionally scaling metres -> print-mm).
            mesh = build_border_stl(
                self._geom, self._height_mm,
                geometry_crs=None, projection=EPSG_BUILD, scale=self._scale)
        except Exception as exc:      # surface any geometry failure to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(mesh)


class BorderStlTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Border STL — Visual Tester")
        self.resize(1200, 720)

        # -- state ----------------------------------------------------------
        self.source: gpd.GeoDataFrame | None = None   # footprint (build CRS)
        self.mesh = None                              # last extruded trimesh
        self.source_paths: list[Path] = []
        self.source_crs = None                        # only for the default name
        self._thread: QThread | None = None
        self._worker: ExtrudeWorker | None = None
        self._solid_meshes: list = []                 # vispy visuals currently shown

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)

        self.height_spin = QDoubleSpinBox()
        self.height_spin.setDecimals(2)
        self.height_spin.setRange(0.01, 1000.0)
        self.height_spin.setSingleStep(0.5)
        self.height_spin.setValue(2.0)
        self.height_spin.setSuffix(" mm")
        self.height_spin.setToolTip("Extrusion height of the flat STL bar")
        self.height_spin.valueChanged.connect(self._invalidate)

        self.scale_check = QCheckBox("Print scale 1:")
        self.scale_check.setToolTip(
            "Convert projected metres to print-mm before extruding\n"
            "(1 print-mm = <denominator> mm on the ground).")
        self.scale_check.toggled.connect(self._on_scale_toggled)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setDecimals(0)
        self.scale_spin.setRange(1.0, 100_000_000.0)
        self.scale_spin.setSingleStep(1000.0)
        self.scale_spin.setValue(250_000.0)
        self.scale_spin.setEnabled(False)
        self.scale_spin.setToolTip("Map-scale denominator (ground mm per print-mm)")
        self.scale_spin.valueChanged.connect(self._invalidate)

        self.btn_extrude = QPushButton("Extrude")
        self.btn_extrude.clicked.connect(self.extrude)
        self.btn_extrude.setEnabled(False)
        self.btn_save = QPushButton("Save STL …")
        self.btn_save.clicked.connect(self.save_stl)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(QLabel("Height:"))
        toolbar.addWidget(self.height_spin)
        toolbar.addWidget(self.scale_check)
        toolbar.addWidget(self.scale_spin)
        toolbar.addWidget(self.btn_extrude)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- viewports: flat footprint (matplotlib) | 3D solid (vispy) -----
        panels = QHBoxLayout()

        # Left: the flat footprint stays a 2-D matplotlib map, with its nav
        # toolbar and scroll-to-zoom, because it is planar geometry.
        self.figure = Figure(figsize=(5.5, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 1, 1)
        self._clear_flat(self.ax_before, "Import a border GeoJSON to begin")
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        left = QVBoxLayout()
        left.addWidget(self.mpl_toolbar)
        left.addWidget(self.canvas, stretch=1)
        panels.addLayout(left, stretch=1)

        # Right: the extruded solid is a 3-D mesh, previewed on the GPU.
        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("Extruded solid")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)

        root.addLayout(panels, stretch=1)

        # -- status + progress ---------------------------------------------
        self.status = QLabel("No file loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- flat panel helpers -------------------------------------------------
    @staticmethod
    def _clear_flat(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")   # geometry must not be distorted
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        """Zoom the flat panel toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is not self.ax_before or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    # -- 3D panel helpers ---------------------------------------------------
    @staticmethod
    def _make_scene():
        """A vispy SceneCanvas with a turntable-camera view; returns (canvas, view)."""
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    def _clear_solid(self, title: str) -> None:
        self.solid_title.setText(title)
        for v in self._solid_meshes:
            v.parent = None
        self._solid_meshes.clear()

    # -- import -------------------------------------------------------------
    def import_geojson(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import border GeoJSON (one or more)",
            str(Path.cwd()), GEOJSON_FILTER)
        if not paths:
            return
        try:
            gdf, src_crs = _read_projected(paths)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Some files could not be read", str(exc))
            return
        if gdf is None:
            QMessageBox.warning(self, "Nothing to show",
                                "No readable features in the selected file(s).")
            return

        self.source = gdf
        self.source_crs = src_crs
        self.source_paths = [Path(p) for p in paths]
        self.mesh = None
        self.btn_extrude.setEnabled(True)
        self.btn_save.setEnabled(False)
        self._draw_before()
        self._clear_solid("Not yet extruded")
        self.status.setText(
            f"Loaded {len(gdf)} border feature(s). "
            "Set the height and click ‘Extrude’.")

    # -- parameters ---------------------------------------------------------
    def _on_scale_toggled(self, on: bool) -> None:
        self.scale_spin.setEnabled(on)
        self._invalidate()

    def _invalidate(self, *_) -> None:
        """A parameter change invalidates the last mesh; disable Save."""
        if self.mesh is None:
            return
        self.mesh = None
        self.btn_save.setEnabled(False)
        self._clear_solid("(parameters changed — re-extrude)")
        if self.source is not None:
            self.status.setText("Parameters changed. Click ‘Extrude’ to rebuild.")

    def _scale_value(self):
        return self.scale_spin.value() if self.scale_check.isChecked() else None

    # -- extrude ------------------------------------------------------------
    def extrude(self) -> None:
        # Guard against launching a second extrude while one is running.
        if self.source is None or self._thread is not None:
            return
        geom = unary_union(list(self.source.geometry))
        scale = self._scale_value()

        self._set_busy(True)
        self.status.setText(f"Extruding to {self.height_spin.value():.2f} mm …")

        # The watertight pass is a single blocking geometry call, so run it off
        # the UI thread and drive an indeterminate progress bar while it works.
        self._thread = QThread(self)
        self._worker = ExtrudeWorker(geom, self.height_spin.value(), scale)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_extruded)
        self._worker.failed.connect(self._on_extrude_failed)
        self._thread.start()

    def _on_extruded(self, mesh) -> None:
        self.mesh = mesh
        self._teardown_thread()
        self._set_busy(False)
        self._draw_after()
        self.btn_save.setEnabled(True)
        watertight = getattr(mesh, "is_watertight", False)
        self.status.setText(
            f"Extruded to {self.height_spin.value():.2f} mm — "
            f"{len(mesh.vertices)} verts, watertight={watertight}. "
            "Inspect on the right, then save.")

    def _on_extrude_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Extrude failed", message)
        self.status.setText(f"Extrude failed: {message}")

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool) -> None:
        """Toggle the indeterminate progress bar and lock the controls."""
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        self.progress.setValue(0 if busy else 1)
        for w in (self.btn_import, self.height_spin, self.scale_check,
                  self.btn_extrude, self.btn_save):
            w.setEnabled(not busy)
        # Keep the scale denominator locked to its checkbox when unlocking.
        if not busy:
            self.scale_spin.setEnabled(self.scale_check.isChecked())

    # -- drawing ------------------------------------------------------------
    def _draw_before(self) -> None:
        self._clear_flat(self.ax_before, "Border footprint")
        if self.source is not None and not self.source.empty:
            self.source.plot(ax=self.ax_before, facecolor=KEEP_COLOUR,
                             edgecolor=EDGE_COLOUR, linewidth=0.6)
        self.canvas.draw_idle()

    def _draw_after(self) -> None:
        self._clear_solid("Extruded solid")
        if self.mesh is None:
            return
        vis = scene.visuals.Mesh(
            vertices=np.asarray(self.mesh.vertices, dtype=np.float32),
            faces=np.asarray(self.mesh.faces),
            color=_rgba(RESULT_COLOUR), shading="flat",
            parent=self.solid_view.scene)
        self._solid_meshes.append(vis)
        self.solid_view.camera.set_range()

    # -- save ---------------------------------------------------------------
    def save_stl(self) -> None:
        if self.mesh is None:
            return
        default = str(Path.cwd() / "borders.stl")
        if self.source_paths:
            first = self.source_paths[0]
            default = str(first.with_name(first.stem + ".stl"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save STL", default, STL_FILTER)
        if not path:
            return
        try:
            self.mesh.export(path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved STL to {Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")            # bind vispy to the running Qt app
    window = BorderStlTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
