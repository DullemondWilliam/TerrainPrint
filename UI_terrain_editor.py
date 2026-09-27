"""
UI_terrain_editor.py

A standalone visual harness for the whole terrain pipeline:
``LIB_raster_reproject`` -> ``LIB_geotiff_clip`` -> ``LIB_terrain_stl``.

Workflow
--------
1. **Import GeoTIFF …**   — open the DEM.  Only its metadata and a small
                            decimated overview are read, never the whole file,
                            so pointing this at the 7.3 GB ``Canada120m.tif``
                            takes well under a second.
2. **Import Boundary …**  — load the province GeoJSON.  It is drawn over the DEM
                            in red, so you can see what is about to be cut
                            before cutting.
3. **Target CRS**         — the projection the output is built in.  Leave it at
                            EPSG:3347 (the print CRS) unless you know why not;
                            see "Why the projection matters" below.
4. **Resolution / Buffer / Resample / All-touched / Crop** — the sampling and
                            cut parameters.  The status line live-costs each
                            change ("453 x 328 px, 0.6 MB") *before* you commit.
                            **Res** above the DEM's native pitch reduces terrain
                            resolution, and **Resample** decides what survives
                            it: coarsening Labrador 120 m -> 3200 m keeps
                            1578 m of Mount Caubvick with ``max`` and 1175 m
                            with ``bilinear``, against a true 1652 m.  Both
                            apply whether or not the CRS changes.
5. **Reproject + Crop**   — run the raster stage off the UI thread.
6. **Exag / Base / Floor / Cut** — the solid's parameters.  Defaults come from
                            ``project_config.json``'s ``terrain`` section, so
                            every piece of a map is built the same way.
7. **Build STL**          — mesh the cropped DEM into a watertight solid.
8. **Save GeoTIFF … / Save STL …** — keep either stage's output.

The whole run is here: national DEM in, printable province solid out, without
leaving the window or touching QGIS.

Run with:  python UI_terrain_editor.py

Like the other UI_* harnesses this stays independent of the main ``app`` window
so the libs can be pointed at any DEM/GeoJSON pair without setting up a project.

Why the projection matters
--------------------------
``Canada120m.tif`` is EPSG:3979; every border corridor and dovetail in this
project is built in EPSG:3347.  The same province is 175.7 x 137.0 print-mm in
one and 181.1 x 131.1 in the other — a 5.4 x 5.9 mm disagreement against a 1.6 mm
dovetail neck.  Terrain cropped in the DEM's own CRS cannot key into a border
built in the print CRS, which is the failure this harness exists to make visible:
switch **Target CRS** between the two and watch the result change shape.

Order of operations
-------------------
Warp first, cut second.  The coastline cut then happens in the frame that gets
printed, at the resolution that gets printed, so it is exact there rather than
being a source-CRS cut dragged through a warp afterwards.  When a warp runs, the
warp is given the boundary's bounds, so it *is* the bbox crop and the clip only
has to apply the polygon mask.

Two stages, two results
-----------------------
The raster stage (reproject + crop) and the mesh stage (build) are separate
buttons on purpose: the intermediate raster is worth inspecting before spending
seconds meshing it, and changing an exaggeration should not re-warp 7 GB.  So a
raster-parameter change invalidates **both** results, while a mesh-parameter
change invalidates only the solid.

Three things differ from the 2-D geometry harnesses, all forced by the input
being a **very large raster**:

*Nothing is ever read whole.*  The before panel is a decimated overview (GDAL
seeks past the scanlines it does not need, so a full-Canada overview costs
~0.4 s); the warp streams through a ``WarpedVRT`` and the clip in row bands.

*The run writes the file, and Save copies it.*  The libs are what write
GeoTIFFs, so the harness runs straight to a temporary file and Save copies it.
What you inspect is byte-for-byte what you save.

*The after panel may be showing you less than you saved.*  A native-resolution
province is a 400 MB array; matplotlib is handed a strided-down copy above
``MAX_DISPLAY_PX``.  The panel title says so whenever it happens, because a
harness that quietly showed a coarser image than it wrote would be lying about
the thing it exists to check.
"""

import shutil
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
from rasterio.crs import CRS
from rasterio.enums import Resampling
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from lib.LIB_geotiff_clip import ClipParams, clip_geotiff
# The cost preview must agree with what the run actually does, so it asks the
# libs for their grids rather than reimplementing the window/warp arithmetic.
from lib.LIB_geotiff_clip import _output_grid as _clip_grid
from lib.LIB_geotiff_clip import _read_window
from lib.LIB_raster_reproject import (ReprojectParams, bounds_for,
                                      reproject_geotiff)
from lib.LIB_raster_reproject import _output_grid as _warp_grid
from lib.LIB_terrain_stl import (DEFAULT_BASE_MM, DEFAULT_EXAGGERATION,
                                 DEFAULT_FLOOR_M, EPSG_METRIC, TerrainParams,
                                 _triangle_count, terrain_to_stl)
from lib.LIB_project_config import get as _cfg

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
GEOTIFF_FILTER = "GeoTIFF (*.tif *.tiff);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

EDGE_COLOUR = "#1B4F72"     # dark blue outline
MARK_COLOUR = "#E6787A"     # red — the boundary about to be cut
DEM_CMAP = "terrain"

#: Target CRS choices. EPSG:3347 (Statistics Canada Lambert) is the print CRS
#: the borders and dovetails are built in; EPSG:3979 is the DEM's own, offered
#: so the difference can be seen rather than argued about.
CRS_CHOICES = ["EPSG:3347", "EPSG:3979"]
#: Warp resampling offered, best-for-terrain first.  ``average`` is the neutral
#: downsample; ``max`` recovers summits a heavy downsample would otherwise lose
#: (1578 m vs 1165 m on Labrador) at the cost of biasing the surface upward.
RESAMPLING_CHOICES = ["average", "max", "nearest", "bilinear", "cubic"]
#: Pixels in the whole-raster overview drawn on the before panel.
OVERVIEW_PX = 1100
#: Above this, the after panel is strided down for display only.
MAX_DISPLAY_PX = 2_000_000
BG_COLOUR = "#FDFEFE"       # near-white 3D scene background
#: Sampling pitch on paper; the Res control is seeded from it via the scale.
DEFAULT_SPACING_MM = _cfg("terrain.spacing_mm")
DEFAULT_RESAMPLING = _cfg("terrain.resampling")


def _height_colors(mesh):
    """Per-vertex RGBA (float32) from a terrain ramp over the mesh's z."""
    z = np.asarray(mesh.vertices, dtype=float)[:, 2]
    lo, hi = float(z.min()), float(z.max())
    t = (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)
    return colormaps[DEM_CMAP](t).astype(np.float32)


class RunWorker(QObject):
    """Runs the warp and the clip off the UI thread (seconds, on a real DEM)."""
    done = Signal(object)   # ClipResult
    failed = Signal(str)

    def __init__(self, raster, geom, dst_crs, warp_params, clip_params,
                 warp_path, out_path):
        super().__init__()
        self._raster = raster
        self._geom = geom
        self._dst_crs = dst_crs
        self._warp = warp_params
        self._clip = clip_params
        self._warp_path = warp_path
        self._out_path = out_path

    def run(self) -> None:
        try:
            source = self._raster
            if self._warp is not None:
                reproject_geotiff(self._raster, self._dst_crs, self._warp_path,
                                  self._warp)
                source = self._warp_path
            # The boundary arrives as a geometry already in the destination CRS,
            # so neither lib re-reads or re-projects the GeoJSON.
            result = clip_geotiff(source, self._geom, self._out_path,
                                  self._clip, keep_array=True)
        except Exception as exc:      # any raster/geometry failure reaches the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class MeshWorker(QObject):
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


class TerrainEditorTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Terrain Editor — Visual Tester")
        self.resize(1250, 720)

        # -- state ----------------------------------------------------------
        self.raster_path: Path | None = None
        self.raster_crs = None
        self.raster_bounds = None            #: (l, b, r, t) in the raster CRS
        self.raster_shape = (0, 0)           #: (width, height) in pixels
        self.raster_res = (0.0, 0.0)         #: native metres per pixel
        self.overview = None                 #: masked array for the before panel
        self.overview_extent = None
        self.boundary: gpd.GeoDataFrame | None = None
        self.boundary_path: Path | None = None
        self.result = None                   #: last ClipResult (raster stage)
        self.stl_result = None               #: last TerrainResult (mesh stage)
        self._tempdir = Path(tempfile.mkdtemp(prefix="ui_terrain_editor_"))
        self._thread: QThread | None = None
        self._worker = None                  #: RunWorker or MeshWorker
        self._colorbar = None
        self._before_drawn = False   #: has the DEM panel been painted yet?
        self._solid_meshes = []      #: vispy visuals, detached on each rebuild

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_raster = QPushButton("Import GeoTIFF …")
        self.btn_raster.clicked.connect(self.import_geotiff)
        self.btn_boundary = QPushButton("Import Boundary …")
        self.btn_boundary.clicked.connect(self.import_boundary)

        self.crs_combo = QComboBox()
        self.crs_combo.setEditable(True)
        self.crs_combo.addItems(CRS_CHOICES)
        self.crs_combo.setToolTip(
            "CRS the output is built in.\nEPSG:3347 is this project's print CRS "
            "— the one the borders and dovetails\nare built in. Leaving the DEM "
            "in EPSG:3979 misplaces a province by ~5 mm.")
        self.crs_combo.currentTextChanged.connect(self._invalidate_raster)

        # 0 m means "native resolution" — the libs' own convention.
        # Seeded from terrain.spacing_mm through the scale: 0.4 mm at 1:8M is
        # 3200 m, so the default Res is the configured print pitch, not a magic
        # number that could drift from the meshing stage's idea of spacing.
        self.res_spin = self._spin(
            0.0, 100_000.0, DEFAULT_SPACING_MM * _cfg("print.scale") / 1000.0, 0,
                                   "Metres per pixel; 0 = the DEM's native "
                                   "resolution.\n1:8,000,000 with a 0.4 mm "
                                   "nozzle wants ~3200 m.")
        self.res_spin.setSingleStep(100.0)
        self.res_spin.setSpecialValueText("native")
        self.res_spin.setSuffix(" m/px")
        self.res_spin.valueChanged.connect(self._invalidate_raster)

        self.buf_spin = self._spin(0.0, 100_000.0, 0.0, 0,
                                   "Grow the boundary outward before cutting, "
                                   "to keep a coastal lip")
        self.buf_spin.setSingleStep(100.0)
        self.buf_spin.setSuffix(" m")
        self.buf_spin.valueChanged.connect(self._invalidate_raster)

        self.resample_combo = QComboBox()
        self.resample_combo.addItems(RESAMPLING_CHOICES)
        self.resample_combo.setCurrentText(DEFAULT_RESAMPLING)
        self.resample_combo.setToolTip(
            "How the warp resamples.\n"
            "Warping straight to print resolution is a big downsample, so the "
            "choice matters:\n"
            "  max       keeps summits (1578 m on Labrador)\n"
            "  average   the neutral downsample (1265 m)\n"
            "  bilinear  loses the most (1165 m; true peak is 1652 m)\n"
            "Applies whenever the output is coarser than the DEM, whether or "
            "not the CRS changes.")
        self.resample_combo.currentTextChanged.connect(self._invalidate_raster)

        self.chk_touched = QCheckBox("All-touched")
        self.chk_touched.setToolTip(
            "Keep pixels the boundary merely touches, not only those it covers")
        self.chk_touched.toggled.connect(self._invalidate_raster)
        self.chk_crop = QCheckBox("Crop")
        self.chk_crop.setChecked(True)
        self.chk_crop.setToolTip(
            "Crop to the boundary's bounding box.\nUntick to keep the DEM's "
            "full extent, which shows where the province sits in it.")
        self.chk_crop.toggled.connect(self._invalidate_raster)

        self.cache_spin = self._spin(0.0, 8192.0, 0.0, 0,
                                     "Cap GDAL's block cache for the run.\n"
                                     "0 leaves GDAL's default (5% of RAM); on "
                                     "a striped DEM that is the difference\n"
                                     "between a ~1.9 GB and a ~256 MB peak.")
        self.cache_spin.setSingleStep(64.0)
        self.cache_spin.setSpecialValueText("default")
        self.cache_spin.setSuffix(" MB")

        self.btn_run = QPushButton("Reproject + Crop")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)
        self.btn_save = QPushButton("Save GeoTIFF …")
        self.btn_save.clicked.connect(self.save_geotiff)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_raster)
        toolbar.addWidget(self.btn_boundary)
        toolbar.addWidget(QLabel("CRS:"))
        toolbar.addWidget(self.crs_combo)
        toolbar.addWidget(QLabel("Res:"))
        toolbar.addWidget(self.res_spin)
        toolbar.addWidget(QLabel("Buffer:"))
        toolbar.addWidget(self.buf_spin)
        toolbar.addWidget(QLabel("Resample:"))
        toolbar.addWidget(self.resample_combo)
        toolbar.addWidget(self.chk_touched)
        toolbar.addWidget(self.chk_crop)
        toolbar.addWidget(QLabel("Cache:"))
        toolbar.addWidget(self.cache_spin)
        toolbar.addWidget(self.btn_run)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- second row: the mesh stage -------------------------------------
        # A second row rather than one long one: these controls act on a
        # different stage with a different invalidation rule, and putting them
        # beside the raster controls would imply they re-warp the DEM.
        mesh_bar = QHBoxLayout()
        self.exag_spin = self._spin(
            1.0, 200.0, DEFAULT_EXAGGERATION, 1,
            "Vertical exaggeration (terrain.exaggeration).\n"
            "At 1:8,000,000 true scale (x1) is 0.20 mm of relief on\n"
            "Newfoundland — invisible. Share one value across a whole map.")
        self.exag_spin.setSingleStep(5.0)
        self.exag_spin.valueChanged.connect(self._invalidate_mesh)

        self.base_spin = self._spin(
            0.1, 50.0, DEFAULT_BASE_MM, 2,
            "Solid base under the terrain (terrain.base_mm).\n"
            "Must exceed the dovetail pocket depth, or the pocket cuts\n"
            "through the bottom of the piece.")
        self.base_spin.setSingleStep(0.5)
        self.base_spin.setSuffix(" mm")
        self.base_spin.valueChanged.connect(self._invalidate_mesh)

        self.floor_spin = self._spin(
            -1000.0, 9000.0, DEFAULT_FLOOR_M, 0,
            "Elevation the base measures from (terrain.floor_m).\n"
            "Share one value across a map, or neighbouring pieces step\n"
            "at the seam.")
        self.floor_spin.setSingleStep(50.0)
        self.floor_spin.setSuffix(" m")
        self.floor_spin.valueChanged.connect(self._invalidate_mesh)

        self.chk_cut = QCheckBox("Cut coastline")
        self.chk_cut.setChecked(True)
        self.chk_cut.setToolTip(
            "Cut the solid to the boundary.\n"
            "Untick for the rectangular slab DEMto3D produces, to compare\n"
            "old and new pieces.")
        self.chk_cut.toggled.connect(self._invalidate_mesh)

        self.btn_build = QPushButton("Build STL")
        self.btn_build.clicked.connect(self.build_stl)
        self.btn_build.setEnabled(False)
        self.btn_save_stl = QPushButton("Save STL …")
        self.btn_save_stl.clicked.connect(self.save_stl)
        self.btn_save_stl.setEnabled(False)

        mesh_bar.addWidget(QLabel("Mesh:"))
        mesh_bar.addWidget(QLabel("Exag:"))
        mesh_bar.addWidget(self.exag_spin)
        mesh_bar.addWidget(QLabel("Base:"))
        mesh_bar.addWidget(self.base_spin)
        mesh_bar.addWidget(QLabel("Floor:"))
        mesh_bar.addWidget(self.floor_spin)
        mesh_bar.addWidget(self.chk_cut)
        mesh_bar.addWidget(self.btn_build)
        mesh_bar.addWidget(self.btn_save_stl)
        mesh_bar.addStretch(1)
        root.addLayout(mesh_bar)

        # -- map: before | after --------------------------------------------
        # Deliberately NOT sharex/sharey. Once a warp is in play the panels are
        # in different CRSs — Newfoundland sits at x ~1.5e6 in EPSG:3979 and
        # ~7.1e6 in EPSG:3347 — so shared limits would drive the result clean off
        # the canvas. They are linked explicitly instead, and only when the two
        # panels really are in the same frame (see _link_views).
        self.figure = Figure(figsize=(11, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import a GeoTIFF to begin", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)

        # DEM + cropped raster on the left, the finished solid on the right.
        # The 3-D panel is vispy, not mplot3d: a province is ~200k triangles and
        # matplotlib re-sorts every one of them on each rotate.
        panels = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.mpl_toolbar)
        left.addWidget(self.canvas, stretch=1)
        panels.addLayout(left, stretch=2)

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

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    @staticmethod
    def _spin(lo, hi, value, decimals, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setToolTip(tip)
        return spin

    # -- CRS ----------------------------------------------------------------
    def target_crs(self):
        """The chosen destination CRS, or None if it is not parseable yet."""
        text = self.crs_combo.currentText().strip()
        if not text:
            return None
        try:
            return CRS.from_user_input(text)
        except Exception:
            return None          # half-typed EPSG code; the preview just waits

    def needs_warp(self) -> bool:
        """Whether the output lands in a different CRS than the DEM.

        This is the *frame* question, so it is what the panel-linking and the
        CRS labels key off — not what decides which code path runs.
        """
        dst = self.target_crs()
        return (dst is not None and self.raster_crs is not None
                and dst != self.raster_crs)

    def reduction(self) -> float:
        """Requested output resolution as a multiple of the DEM's native one."""
        native = self.raster_res[0]
        target = self.res_spin.value()
        if not native or target <= 0:
            return 1.0
        return max(1.0, target / native)

    def needs_resample(self) -> bool:
        """Whether the run goes through the warper rather than plain decimation.

        A reprojection obviously needs it — but so does a pure **downsample**.
        ``LIB_geotiff_clip`` can only decimate nearest-neighbour (GDAL's RasterIO
        path cannot skip nodata), which costs 1363 m of Mount Caubvick against
        the 1578 m ``max`` recovers.  Routing every coarsening through the
        warper is what keeps the Resample control meaningful instead of
        silently inert whenever the target CRS happened to match the DEM's.
        """
        return self.needs_warp() or self.reduction() > 1.001

    # -- axes helpers -------------------------------------------------------
    def _reset_axes(self, before_title: str, after_title: str) -> None:
        for ax, title in ((self.ax_before, before_title),
                          (self.ax_after, after_title)):
            self._clear_ax(ax, title)
        self.canvas.draw_idle()

    @staticmethod
    def _clear_ax(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

    def _link_views(self) -> None:
        """Match the after panel to the before panel, when both are one frame.

        Replaces the ``sharex``/``sharey`` the other harnesses use, which cannot
        express "aligned only while the projections agree".
        """
        if self.needs_warp() or self.result is None:
            return
        self.ax_after.set_xlim(self.ax_before.get_xlim())
        self.ax_after.set_ylim(self.ax_before.get_ylim())

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
        if ax is self.ax_before:
            self._link_views()
        self.canvas.draw_idle()

    # -- imports ------------------------------------------------------------
    def import_geotiff(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import DEM GeoTIFF", str(Path.cwd()), GEOTIFF_FILTER)
        if not path:
            return
        try:
            self._load_raster(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Could not open the GeoTIFF", str(exc))
            return

        self._invalidate()
        self._draw_before()
        self.status.setText(
            f"{self.raster_path.name}: {self.raster_shape[0]}x"
            f"{self.raster_shape[1]} @ {self.raster_res[0]:.0f} m, "
            f"{self.raster_crs}. Import a boundary to crop against.")

    def _load_raster(self, path: Path) -> None:
        """
        Read the DEM's metadata and one decimated overview — never the file.

        A whole-raster overview is cheap even at 7.3 GB: the source is striped,
        so GDAL seeks straight past the scanlines the decimation skips.
        """
        with rasterio.open(str(path)) as ds:
            self.raster_crs = ds.crs
            self.raster_bounds = tuple(ds.bounds)
            self.raster_shape = (ds.width, ds.height)
            self.raster_res = ds.res
            factor = max(ds.width / OVERVIEW_PX, ds.height / OVERVIEW_PX, 1.0)
            shape = (max(1, int(ds.height / factor)),
                     max(1, int(ds.width / factor)))
            data = ds.read(1, out_shape=shape, resampling=Resampling.nearest,
                           masked=True)
        self.raster_path = path
        self.overview = data
        left, bottom, right, top = self.raster_bounds
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
            QMessageBox.warning(self, "Nothing to crop against",
                                "That file has no features.")
            return

        self.boundary = frame
        self.boundary_path = Path(path)
        self._invalidate()
        self._draw_before()
        self.status.setText(f"Boundary: {self.boundary_path.name} "
                            f"({len(frame)} feature(s)). " + self._cost_text())

    def _boundary_geom(self, crs=None):
        """The boundary dissolved to one geometry in ``crs``, buffered."""
        crs = crs if crs is not None else self.raster_crs
        if self.boundary is None or crs is None:
            return None
        frame = self.boundary
        # The province GeoJSON is WGS84 while the DEM is EPSG:3979 and the print
        # CRS is EPSG:3347; without this the read window comes out empty.
        if frame.crs is not None and frame.crs != crs:
            frame = frame.to_crs(crs)
        geom = unary_union(frame.geometry.values)
        buffer_m = self.buf_spin.value()
        return geom.buffer(buffer_m) if buffer_m else geom

    def _output_geom(self):
        """The boundary in whichever CRS the output will be built in."""
        return self._boundary_geom(self.target_crs() if self.needs_warp()
                                   else self.raster_crs)

    # -- the plan both the preview and the run follow -----------------------
    def _plan(self):
        """
        The (warp_params | None, clip_params) the current settings imply.

        Preview and run share this so they cannot disagree.  When a warp runs it
        is handed the boundary's bounds, so the warp *is* the bbox crop and the
        clip is left to apply only the polygon mask — which is also why the
        final grid equals the warped grid exactly, and the preview can promise
        the number the run produces.
        """
        res = self.res_spin.value()
        cache = int(self.cache_spin.value())
        if not self.needs_resample():
            return None, ClipParams(crop=self.chk_crop.isChecked(),
                                    all_touched=self.chk_touched.isChecked(),
                                    target_res_m=res, gdal_cache_mb=cache)
        # On a pure downsample this is the raster's own CRS, so the warp
        # changes only scale — exactly what downsample_geotiff does.
        dst = self.target_crs()
        bounds = (bounds_for(self._boundary_geom(dst), dst)
                  if self.chk_crop.isChecked() else None)
        warp = ReprojectParams(dst_res_m=res, bounds=bounds,
                               resampling=Resampling[
                                   self.resample_combo.currentText()],
                               gdal_cache_mb=cache)
        # crop=False: the warp already cut to the bbox, and target_res_m=0
        # because the warp already produced the requested resolution.
        clip = ClipParams(crop=False, all_touched=self.chk_touched.isChecked(),
                          target_res_m=0.0, gdal_cache_mb=cache)
        return warp, clip

    def _cost_text(self) -> str:
        """
        What the current settings would cost, asked of the libs' own grid math.

        Re-deriving the grids here would let the preview drift from the run it
        is previewing, so this opens the dataset (metadata only, ~20 ms) and
        calls the same helpers the libs use.
        """
        if self.raster_path is None or self.boundary is None:
            return "Set the parameters, then click ‘Reproject + Crop’."
        dst = self.target_crs()
        if dst is None:
            return "Target CRS not recognised yet."
        warp, clip = self._plan()
        try:
            with rasterio.open(str(self.raster_path)) as ds:
                if warp is not None:
                    grid = _warp_grid(ds, dst, warp)
                else:
                    window = _read_window(ds, self._boundary_geom(),
                                          clip.crop)
                    grid = _clip_grid(ds, window, clip)
        except ValueError as exc:          # disjoint boundary, usually a CRS slip
            return f"Cannot run: {exc}"
        except Exception:
            return "Set the parameters, then click ‘Reproject + Crop’."
        mb = grid.width * grid.height * 4 / 2 ** 20
        factor = self.reduction()
        if warp is None:
            step = f"crop in {self.raster_crs} at native resolution"
        elif self.needs_warp():
            step = f"warp {self.raster_crs} → {dst}, then crop"
            if factor > 1.001:
                step += (f" (also {factor:.0f}x coarser, "
                         f"{self.resample_combo.currentText()})")
        else:
            step = (f"downsample {self.raster_res[0]:.0f} m → "
                    f"{self.res_spin.value():.0f} m ({factor:.0f}x, "
                    f"{self.resample_combo.currentText()}), then crop")
        return (f"Would produce {grid.width} x {grid.height} px "
                f"@ {abs(grid.transform.a):.0f} m ({mb:.1f} MB) — {step}. "
                "Click ‘Reproject + Crop’.")

    def _invalidate_mesh(self) -> None:
        """A mesh-parameter change stales the solid but keeps the raster.

        Re-warping 7 GB because someone nudged the exaggeration would be absurd,
        and the cropped raster is unaffected by anything on the mesh row.
        """
        self.stl_result = None
        self.btn_save_stl.setEnabled(False)
        self.btn_build.setEnabled(self.result is not None)
        self._show_solid("Not yet built", None)
        if self.raster_path is not None:
            self.status.setText(self._cost_text())

    def _invalidate_raster(self) -> None:
        """A raster-parameter change stales both stages.

        The solid is meshed *from* the cropped raster, so a new crop makes the
        old solid describe ground that is no longer on the table.
        """
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_run.setEnabled(
            self.raster_path is not None and self.boundary is not None)
        self._clear_ax(self.ax_after, "Not yet run")
        self.canvas.draw_idle()
        self._invalidate_mesh()

    #: Kept as the name the imports call, which stale both stages.
    _invalidate = _invalidate_raster

    # -- run ----------------------------------------------------------------
    def run(self) -> None:
        # Guard against launching a second run while one is in flight.
        if self._thread is not None:
            return
        geom = self._output_geom()
        if geom is None:
            return
        warp, clip = self._plan()
        stem = self._stem()

        self._set_busy(True)
        self.status.setText("Reprojecting and cropping …" if warp is not None
                            else "Cropping …")

        self._thread = QThread(self)
        self._worker = RunWorker(
            str(self.raster_path), geom, self.target_crs(), warp, clip,
            str(self._tempdir / f"{stem}_warp.tif"),
            str(self._tempdir / f"{stem}_crop.tif"))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        # Before panel first: the result's range now drives the shared colour
        # ramp so the DEM has to be repainted onto it (keeping wherever the user
        # panned to), and _draw_after's _link_views reads this panel's limits --
        # repainting it afterwards would leave the link one draw out of date.
        self._draw_before(preserve_view=True)
        self._draw_after()
        self.btn_save.setEnabled(True)
        z = ("no valid pixels" if result.zmin is None
             else f"{result.zmin:.0f}..{result.zmax:.0f} m")
        crs_note = (f" in {self.target_crs()}" if self.needs_warp()
                    else f" in {self.raster_crs}")
        self.btn_build.setEnabled(True)
        self.status.setText(
            f"Cropped: {result.width} x {result.height} px @ "
            f"{result.res[0]:.0f} m{crs_note}, {result.coverage * 100:.1f}% "
            f"covered, elevation {z}. Save it, or click 'Build STL'.")

    # -- mesh stage ---------------------------------------------------------
    def _terrain_params(self) -> TerrainParams:
        return TerrainParams(
            scale=_cfg("print.scale"),
            z_exaggeration=self.exag_spin.value(),
            base_height_mm=self.base_spin.value(),
            floor_m=self.floor_spin.value(),
            buffer_mm=self.buf_spin.value(),
            # The cropped raster is in whatever CRS the raster stage produced,
            # which may deliberately be the DEM's own; the guard belongs to the
            # stage that chose it, so it is waived rather than duplicated here.
            expect_crs=None)

    def cut_geom(self):
        """The boundary the build will cut with, honouring the tick.

        A named method rather than an expression inside :meth:`build_stl`, so a
        test can ask the window what it would cut with instead of restating the
        rule — a restated rule is one the tests stop actually checking.
        """
        return self._output_geom() if self.chk_cut.isChecked() else None

    def build_stl(self) -> None:
        """Mesh the cropped raster — the second half of the pipeline."""
        if self._thread is not None or self.result is None:
            return
        out_path = self._tempdir / f"{self._stem()}.stl"
        boundary = self.cut_geom()

        self._set_busy(True)
        self.status.setText("Meshing …")

        self._thread = QThread(self)
        self._worker = MeshWorker(self.result.path, boundary, str(out_path),
                                  self._terrain_params())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_meshed)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_meshed(self, result) -> None:
        self.stl_result = result
        self._teardown_thread()
        self._set_busy(False)
        w, h, z = result.extent_mm
        self._show_solid(
            f"{result.triangles:,} triangles — {w:.0f} x {h:.0f} x {z:.1f} mm",
            result.mesh)
        self.btn_save_stl.setEnabled(True)
        relief = z - result.base_height_mm
        self.status.setText(
            f"Built {result.triangles:,} triangles, {w:.0f} x {h:.0f} x "
            f"{z:.1f} mm ({relief:.1f} mm of relief on a "
            f"{result.base_height_mm:.1f} mm base), "
            f"{'fits' if result.bed_fits else 'DOES NOT FIT'} the bed, "
            f"watertight={result.watertight}. Inspect in 3D, then save.")

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

    def save_stl(self) -> None:
        if self.stl_result is None or self.stl_result.path is None:
            return
        default = str(Path.cwd() / f"{self._stem()}.stl")
        path, _ = QFileDialog.getSaveFileName(self, "Save STL", default,
                                              STL_FILTER)
        if not path:
            return
        target = Path(path)
        try:
            # The lib wrote both files; copying keeps what you inspected
            # byte-for-byte, frame sidecar included.
            shutil.copyfile(self.stl_result.path, target)
            shutil.copyfile(self.stl_result.frame_path,
                            target.with_suffix(".frame.json"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {self.stl_result.triangles:,} triangles to {target.name}, "
            f"with its frame in {target.with_suffix('.frame.json').name}.")

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Run failed", message)
        self.status.setText(f"Run failed: {message}")

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
        for w in (self.btn_raster, self.btn_boundary, self.btn_run,
                  self.btn_save, self.crs_combo, self.res_spin, self.buf_spin,
                  self.cache_spin, self.chk_touched, self.chk_crop,
                  self.resample_combo, self.exag_spin, self.base_spin,
                  self.floor_spin, self.chk_cut, self.btn_build,
                  self.btn_save_stl):
            w.setEnabled(not busy)
        if not busy:      # re-apply the preconditions the blanket enable ignored
            self.btn_run.setEnabled(
                self.raster_path is not None and self.boundary is not None)
            self.btn_save.setEnabled(self.result is not None)
            self.btn_build.setEnabled(self.result is not None)
            self.btn_save_stl.setEnabled(self.stl_result is not None)

    # -- drawing ------------------------------------------------------------
    def _colour_limits(self):
        """
        One elevation ramp for both panels, so they can actually be compared.

        The panels show the same ground, so scaling them independently would
        paint identical elevations different colours — the before panel would
        also be crushed into the bottom of the ramp, because the whole-Canada
        overview runs to Mount Logan's 5500 m while a province rarely clears
        1500 m.  So once there is a result, its range drives both panels and the
        rest of the DEM is allowed to saturate.  Before that, percentiles keep
        the overview readable without a few summits flattening everything else.
        """
        if self.result is not None and self.result.zmin is not None:
            return self.result.zmin, self.result.zmax
        if self.overview is None:
            return None, None
        valid = self.overview.compressed()
        if valid.size == 0:
            return None, None
        return float(np.percentile(valid, 2)), float(np.percentile(valid, 98))

    def _draw_before(self, preserve_view: bool = False) -> None:
        # Only a panel that has actually been drawn has a view worth keeping.
        # Preserving a never-drawn axis would restore matplotlib's default
        # (0, 1) limits over an extent measured in millions of metres, leaving
        # the panel zoomed into a one-metre square of flat colour.
        preserve_view = preserve_view and self._before_drawn
        if preserve_view:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        title = "DEM overview"
        if self.overview is not None:
            title = (f"DEM overview ({self.overview.shape[1]}x"
                     f"{self.overview.shape[0]} of {self.raster_shape[0]}x"
                     f"{self.raster_shape[1]}) — {self.raster_crs}")
        self._clear_ax(self.ax_before, title)
        vmin, vmax = self._colour_limits()
        if self.overview is not None:
            self.ax_before.imshow(self.overview, cmap=DEM_CMAP,
                                  extent=self.overview_extent, origin="upper",
                                  vmin=vmin, vmax=vmax)
        # Drawn in the DEM's CRS, because that is the panel's frame — the after
        # panel is where the target CRS shows up.
        geom = self._boundary_geom(self.raster_crs)
        if geom is not None:
            self._plot_outline(self.ax_before, geom)
        if preserve_view:
            self.ax_before.set_xlim(xlim)
            self.ax_before.set_ylim(ylim)
        self._before_drawn = self.overview is not None
        self.canvas.draw_idle()

    def _draw_after(self) -> None:
        if self.result is None or self.result.data is None:
            self._clear_ax(self.ax_after, "Not yet run")
            self.canvas.draw_idle()
            return

        data, step = self._for_display(self.result.data)
        crs = self.target_crs() if self.needs_warp() else self.raster_crs
        title = (f"{self.result.width}x{self.result.height} @ "
                 f"{self.result.res[0]:.0f} m — {crs}")
        if step > 1:
            # Never let the panel imply the saved file is this coarse.
            title += f"  (shown 1:{step} — saved file is full resolution)"
        self._clear_ax(self.ax_after, title)

        left, bottom, right, top = self.result.bounds_m
        masked = np.ma.masked_equal(data, self.result.nodata)
        vmin, vmax = self._colour_limits()
        image = self.ax_after.imshow(masked, cmap=DEM_CMAP,
                                     extent=(left, right, bottom, top),
                                     origin="upper", vmin=vmin, vmax=vmax)
        if self._colorbar is not None:
            self._colorbar.remove()
        self._colorbar = self.figure.colorbar(
            image, ax=self.ax_after, fraction=0.046, label="elevation (m)")
        self._link_views()
        self.canvas.draw_idle()

    @staticmethod
    def _plot_outline(ax, geom) -> None:
        """
        Draw a boundary as rings only, so the DEM stays visible through it.

        Drawn with bare ``ax.plot`` rather than geopandas: ``.plot()`` reaches
        for ``pyplot``, which this harness never uses (it drives a bare
        ``Figure``), and warns about GUI threads for its trouble.
        """
        polygons = (geom.geoms if geom.geom_type.startswith("Multi")
                    else [geom])
        for poly in polygons:
            for ring in (poly.exterior, *poly.interiors):
                xs, ys = ring.xy
                ax.plot(xs, ys, color=MARK_COLOUR, linewidth=1.2)

    @staticmethod
    def _for_display(data):
        """Stride a large result down for matplotlib; returns (array, step)."""
        pixels = data.size
        if pixels <= MAX_DISPLAY_PX:
            return data, 1
        step = int(np.ceil(np.sqrt(pixels / MAX_DISPLAY_PX)))
        return data[::step, ::step], step

    # -- save ---------------------------------------------------------------
    def _stem(self) -> str:
        return self.boundary_path.stem if self.boundary_path else "terrain"

    def save_geotiff(self) -> None:
        if self.result is None or self.result.path is None:
            return
        default = str(Path.cwd() / f"{self._stem()}_crop.tif")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save GeoTIFF", default, GEOTIFF_FILTER)
        if not path:
            return
        try:
            # The libs already wrote the real file; copying it means the saved
            # raster is exactly the one just inspected.
            shutil.copyfile(self.result.path, path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {self.result.width} x {self.result.height} px to "
            f"{Path(path).name}.")

    def closeEvent(self, event) -> None:
        self._teardown_thread()
        shutil.rmtree(self._tempdir, ignore_errors=True)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")          # before the window builds its scene
    window = TerrainEditorTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
