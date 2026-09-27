"""
UI_terrain_stl.py

A standalone visual harness for ``LIB_terrain_stl.terrain_to_stl``.

Workflow
--------
1. **Import GeoTIFF …**   — a DEM already clipped, reprojected and coarsened;
                            ``UI_terrain_editor.py`` produces exactly that.  Only
                            metadata and a decimated overview are read here.
2. **Import Boundary …**  — optional.  With one, the coastline is cut directly
                            and the rectangular skirt never exists; without one
                            you get the DEMto3D-style slab, which is useful for
                            comparing against older pieces.
3. **Scale / Exaggeration / Base / Floor / Buffer** — the solid's parameters.
                            The status line live-costs each change (triangle
                            count, printed size, bed fit) *before* meshing.
4. **Build Solid**        — run the lib off the UI thread with a progress bar.
5. **Save STL …**         — write the STL and its ``.frame.json`` sidecar.

The **left** panel is the DEM the solid is sampled from, with the boundary drawn
over it in red; the **right** panel is the finished solid in 3D (a GPU vispy
scene), coloured by height.

Run with:  python harnesses/UI_terrain_stl.py

Like the other UI_* harnesses this stays independent of the main ``app`` window.

What this harness is actually for
---------------------------------
Three failures are invisible in a number and obvious in a picture, so the panels
are aimed at them:

*Is the coastline right?*  The cut is a boolean against the boundary polygon, so
it either follows the red outline on the left or it does not.

*Is the exaggeration sane?*  At 1:8,000,000 true scale is 0.20 mm of relief on
Newfoundland — invisible.  Spin **Exaggeration** and watch the solid inflate;
the status line reports the relief in millimetres as you go.

*Will it print?*  Bed fit and watertightness are reported on every build, and a
mesh that fails the watertight gate never reaches the Save button.

Resolution is deliberately **not** a control here.  The sampling grid is the
raster's grid, so coarsening belongs upstream in ``UI_terrain_editor`` where it
can be done nodata-aware; this harness reports the spacing it inherited and
refuses (via ``max_triangles``) a grid too fine to print.
"""

import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import vispy
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from rasterio.enums import Resampling
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_terrain_stl.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_terrain_stl import (DEFAULT_EXAGGERATION, DEFAULT_SCALE,
                                 EPSG_METRIC, TerrainParams, _triangle_count,
                                 terrain_to_stl)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
GEOTIFF_FILTER = "GeoTIFF (*.tif *.tiff);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

MARK_COLOUR = "#E6787A"     # red — the boundary the cut will follow
DEM_CMAP = "terrain"
BG_COLOUR = "#FDFEFE"       # near-white 3D scene background

#: Pixels in the DEM overview drawn on the left.
OVERVIEW_PX = 900


def _height_colors(mesh):
    """Per-vertex RGBA (float32) from a terrain ramp over the mesh's z."""
    z = np.asarray(mesh.vertices, dtype=float)[:, 2]
    lo, hi = float(z.min()), float(z.max())
    t = (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)
    return colormaps[DEM_CMAP](t).astype(np.float32)


class BuildWorker(QObject):
    """Runs ``terrain_to_stl`` off the UI thread (the boolean cut is slow)."""
    done = Signal(object)   # TerrainResult
    failed = Signal(str)

    def __init__(self, raster, boundary, out_path, params):
        super().__init__()
        self._raster = raster
        self._boundary = boundary
        self._out_path = out_path
        self._params = params

    def run(self) -> None:
        try:
            result = terrain_to_stl(self._raster, self._boundary,
                                    self._out_path, self._params)
        except Exception as exc:      # any raster/geometry/mesh failure
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class TerrainStlTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Terrain STL — Visual Tester")
        self.resize(1250, 720)

        # -- state ----------------------------------------------------------
        self.raster_path: Path | None = None
        self.raster_crs = None
        self.raster_shape = (0, 0)           #: (cols, rows)
        self.raster_res = (0.0, 0.0)
        self.overview = None
        self.overview_extent = None
        self.boundary: gpd.GeoDataFrame | None = None
        self.boundary_path: Path | None = None
        self.result = None                   #: last TerrainResult, ready to save
        self._tempdir = Path(tempfile.mkdtemp(prefix="ui_terrain_stl_"))
        self._thread: QThread | None = None
        self._worker: BuildWorker | None = None
        self._solid_meshes = []

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_raster = QPushButton("Import GeoTIFF …")
        self.btn_raster.clicked.connect(self.import_geotiff)
        self.btn_boundary = QPushButton("Import Boundary …")
        self.btn_boundary.clicked.connect(self.import_boundary)

        self.scale_spin = self._spin(1.0, 100_000_000.0, float(DEFAULT_SCALE), 0,
                                     "Map-scale denominator. Must match the "
                                     "scale the border corridor was built at,\n"
                                     "or the dovetails will not fit.")
        self.scale_spin.setSingleStep(500_000.0)
        self.scale_spin.valueChanged.connect(self._invalidate)

        self.exag_spin = self._spin(1.0, 200.0, DEFAULT_EXAGGERATION, 1,
                                    "Vertical exaggeration.\nAt 1:8,000,000 true "
                                    "scale (x1) is 0.2 mm of relief on "
                                    "Newfoundland — invisible.")
        self.exag_spin.setSingleStep(5.0)
        self.exag_spin.valueChanged.connect(self._invalidate)

        self.base_spin = self._spin(0.1, 50.0, 2.0, 2,
                                    "Solid base under the terrain.\nMust exceed "
                                    "the dovetail pocket depth (1.0 mm) or the "
                                    "pocket\ncuts through the bottom.")
        self.base_spin.setSingleStep(0.5)
        self.base_spin.setSuffix(" mm")
        self.base_spin.valueChanged.connect(self._invalidate)

        self.floor_spin = self._spin(-1000.0, 9000.0, 0.0, 0,
                                     "Elevation the base measures from.\nShare "
                                     "one value across every piece of a map, or "
                                     "neighbours\nwill not meet at the seam.")
        self.floor_spin.setSingleStep(50.0)
        self.floor_spin.setSuffix(" m")
        self.floor_spin.valueChanged.connect(self._invalidate)

        self.buf_spin = self._spin(0.0, 20.0, 0.0, 2,
                                   "Outward lip on the coastline cut")
        self.buf_spin.setSingleStep(0.25)
        self.buf_spin.setSuffix(" mm")
        self.buf_spin.valueChanged.connect(self._invalidate)

        self.chk_cut = QCheckBox("Cut coastline")
        self.chk_cut.setChecked(True)
        self.chk_cut.setToolTip(
            "Cut the solid to the boundary.\nUntick for the rectangular slab "
            "DEMto3D produces, to compare old and new pieces.")
        self.chk_cut.toggled.connect(self._invalidate)

        self.btn_build = QPushButton("Build Solid")
        self.btn_build.clicked.connect(self.build)
        self.btn_build.setEnabled(False)
        self.btn_save = QPushButton("Save STL …")
        self.btn_save.clicked.connect(self.save_stl)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_raster)
        toolbar.addWidget(self.btn_boundary)
        toolbar.addWidget(QLabel("Scale 1:"))
        toolbar.addWidget(self.scale_spin)
        toolbar.addWidget(QLabel("Exag:"))
        toolbar.addWidget(self.exag_spin)
        toolbar.addWidget(QLabel("Base:"))
        toolbar.addWidget(self.base_spin)
        toolbar.addWidget(QLabel("Floor:"))
        toolbar.addWidget(self.floor_spin)
        toolbar.addWidget(QLabel("Buffer:"))
        toolbar.addWidget(self.buf_spin)
        toolbar.addWidget(self.chk_cut)
        toolbar.addWidget(self.btn_build)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- panels: DEM (matplotlib) | solid (vispy) -----------------------
        # Not a shared view: one panel is a map in metres, the other a solid in
        # print-mm seen through a turntable camera. They share nothing but the
        # ground they describe.
        panels = QHBoxLayout()

        self.figure = Figure(figsize=(6, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_dem = self.figure.add_subplot(1, 1, 1)
        self._clear_ax(self.ax_dem, "Import a GeoTIFF to begin")
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        left = QVBoxLayout()
        left.addWidget(self.mpl_toolbar)
        left.addWidget(self.canvas, stretch=1)
        panels.addLayout(left, stretch=1)

        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("3D preview")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)

        root.addLayout(panels, stretch=1)

        self.status = QLabel("No DEM loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- construction helpers ----------------------------------------------
    @staticmethod
    def _spin(lo, hi, value, decimals, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setToolTip(tip)
        return spin

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    @staticmethod
    def _clear_ax(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    # -- imports ------------------------------------------------------------
    def import_geotiff(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import prepared DEM GeoTIFF", str(Path.cwd()), GEOTIFF_FILTER)
        if not path:
            return
        try:
            self._load_raster(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Could not open the GeoTIFF", str(exc))
            return
        self._invalidate()
        self._draw_dem()

    def _load_raster(self, path: Path) -> None:
        """Metadata and one decimated overview — never the whole file."""
        with rasterio.open(str(path)) as ds:
            self.raster_crs = ds.crs
            self.raster_shape = (ds.width, ds.height)
            self.raster_res = ds.res
            bounds = tuple(ds.bounds)
            factor = max(ds.width / OVERVIEW_PX, ds.height / OVERVIEW_PX, 1.0)
            shape = (max(1, int(ds.height / factor)),
                     max(1, int(ds.width / factor)))
            self.overview = ds.read(1, out_shape=shape,
                                    resampling=Resampling.nearest, masked=True)
        self.raster_path = path
        left, bottom, right, top = bounds
        self.overview_extent = (left, right, bottom, top)

    def import_boundary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import boundary GeoJSON", str(Path.cwd()), GEOJSON_FILTER)
        if not path:
            return
        try:
            frame = gpd.read_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not read the boundary", str(exc))
            return
        if frame.empty:
            QMessageBox.warning(self, "Nothing to cut with",
                                "That file has no features.")
            return
        self.boundary = frame
        self.boundary_path = Path(path)
        self._invalidate()
        self._draw_dem()

    def _boundary_geom(self):
        """The boundary dissolved into the raster's CRS, or None."""
        if self.boundary is None or self.raster_crs is None:
            return None
        frame = self.boundary
        if frame.crs is not None and frame.crs != self.raster_crs:
            frame = frame.to_crs(self.raster_crs)
        return unary_union(frame.geometry.values)

    def cut_boundary(self):
        """The boundary the build will actually cut with, honouring the tick."""
        if not self.chk_cut.isChecked():
            return None
        return self._boundary_geom()

    # -- parameters and the live cost --------------------------------------
    def _params(self) -> TerrainParams:
        return TerrainParams(
            scale=self.scale_spin.value(),
            z_exaggeration=self.exag_spin.value(),
            base_height_mm=self.base_spin.value(),
            floor_m=self.floor_spin.value(),
            buffer_mm=self.buf_spin.value(),
            expect_crs=EPSG_METRIC)

    def _cost_text(self) -> str:
        """
        Triangles, printed size and bed fit — before meshing anything.

        All of it is arithmetic over the raster's shape and the scale, so it
        costs nothing and can run on every spin of a control.  It uses the lib's
        own ``_triangle_count`` so the estimate cannot drift from the mesh.
        """
        if self.raster_path is None:
            return "Import a prepared DEM to begin."
        params = self._params()
        cols, rows = self.raster_shape
        triangles = _triangle_count(cols, rows)
        mpm = params.mpm
        width = (cols - 1) * self.raster_res[0] / mpm
        height = (rows - 1) * self.raster_res[1] / mpm
        spacing = self.raster_res[0] / mpm
        usable = params.bed_mm - 2 * params.bed_margin_mm
        fits = width <= usable and height <= usable
        note = "" if fits else "  DOES NOT FIT THE BED —"
        if triangles > params.max_triangles:
            return (f"{cols}x{rows} would make {triangles:,} triangles, over the "
                    f"{params.max_triangles:,} cap. Coarsen the DEM first "
                    "(UI_terrain_editor).")
        if self.cut_boundary() is not None:
            # The grid count is what gets *meshed*; the coastline cut then throws
            # most of it away (65% on Newfoundland). Quoting it as the answer
            # would overstate the result threefold, so quote it as the ceiling.
            count = f"up to {triangles:,} triangles (before the coastline cut)"
            cut = "cut to the boundary"
        else:
            count = f"{triangles:,} triangles"
            cut = "rectangular slab"
        return (f"Would make {count}, "
                f"{width:.0f} x {height:.0f} mm at {spacing:.2f} mm spacing, "
                f"{cut}.{note} Click ‘Build Solid’.")

    def _invalidate(self) -> None:
        """Any input or parameter change makes the last solid stale."""
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_build.setEnabled(self.raster_path is not None)
        self._show_solid("Not yet built", None)
        self.status.setText(self._cost_text())

    # -- build --------------------------------------------------------------
    def build(self) -> None:
        if self._thread is not None or self.raster_path is None:
            return
        out_path = self._tempdir / f"{self._stem()}.stl"

        self._set_busy(True)
        self.status.setText("Meshing …")

        self._thread = QThread(self)
        self._worker = BuildWorker(str(self.raster_path), self.cut_boundary(),
                                   str(out_path), self._params())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_built)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_built(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        w, h, z = result.extent_mm
        self._show_solid(
            f"{result.triangles:,} triangles — {w:.0f} x {h:.0f} x {z:.1f} mm",
            result.mesh)
        self.btn_save.setEnabled(True)
        relief = z - result.base_height_mm
        self.status.setText(
            f"Built {result.triangles:,} triangles, {w:.0f} x {h:.0f} x {z:.1f} mm "
            f"({relief:.1f} mm of relief on a {result.base_height_mm:.1f} mm base), "
            f"{'fits' if result.bed_fits else 'DOES NOT FIT'} the bed, "
            f"watertight={result.watertight}. Inspect, then save.")

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Build failed", message)
        self.status.setText(f"Build failed: {message}")

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
        for w in (self.btn_raster, self.btn_boundary, self.btn_build,
                  self.btn_save, self.scale_spin, self.exag_spin,
                  self.base_spin, self.floor_spin, self.buf_spin, self.chk_cut):
            w.setEnabled(not busy)
        if not busy:      # re-apply the preconditions the blanket enable ignored
            self.btn_build.setEnabled(self.raster_path is not None)
            self.btn_save.setEnabled(self.result is not None)

    # -- drawing ------------------------------------------------------------
    def _draw_dem(self) -> None:
        title = "DEM"
        if self.overview is not None:
            title = (f"DEM {self.raster_shape[0]}x{self.raster_shape[1]} "
                     f"@ {self.raster_res[0]:.0f} m — {self.raster_crs}")
        self._clear_ax(self.ax_dem, title)
        if self.overview is not None:
            self.ax_dem.imshow(self.overview, cmap=DEM_CMAP,
                               extent=self.overview_extent, origin="upper")
        geom = self._boundary_geom()
        if geom is not None:
            self._plot_outline(self.ax_dem, geom)
        self.canvas.draw_idle()

    @staticmethod
    def _plot_outline(ax, geom) -> None:
        """Rings only, so the DEM stays visible through the boundary.

        Bare ``ax.plot`` rather than geopandas: ``.plot()`` reaches for
        ``pyplot``, which this harness never uses, and warns about GUI threads.
        """
        polygons = (geom.geoms if geom.geom_type.startswith("Multi")
                    else [geom])
        for poly in polygons:
            for ring in (poly.exterior, *poly.interiors):
                xs, ys = ring.xy
                ax.plot(xs, ys, color=MARK_COLOUR, linewidth=1.2)

    def _show_solid(self, title: str, mesh) -> None:
        self.solid_title.setText(title)
        for visual in self._solid_meshes:
            visual.parent = None
        self._solid_meshes.clear()
        if mesh is not None and not mesh.is_empty:
            visual = scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces),
                vertex_colors=_height_colors(mesh), shading="flat",
                parent=self.solid_view.scene)
            self._solid_meshes.append(visual)
            self.solid_view.camera.set_range()
        self.solid_canvas.update()

    # -- save ---------------------------------------------------------------
    def _stem(self) -> str:
        if self.boundary_path is not None:
            return self.boundary_path.stem
        return self.raster_path.stem if self.raster_path else "terrain"

    def save_stl(self) -> None:
        if self.result is None or self.result.path is None:
            return
        default = str(Path.cwd() / f"{self._stem()}.stl")
        path, _ = QFileDialog.getSaveFileName(self, "Save STL", default,
                                              STL_FILTER)
        if not path:
            return
        import shutil
        target = Path(path)
        try:
            # The lib already wrote both files; copying keeps what you inspected
            # byte-for-byte, sidecar included.
            shutil.copyfile(self.result.path, target)
            shutil.copyfile(self.result.frame_path,
                            target.with_suffix(".frame.json"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {self.result.triangles:,} triangles to {target.name}, "
            f"with its frame in {target.with_suffix('.frame.json').name}.")

    def closeEvent(self, event) -> None:
        import shutil
        self._teardown_thread()
        shutil.rmtree(self._tempdir, ignore_errors=True)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")
    window = TerrainStlTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
