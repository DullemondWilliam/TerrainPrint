"""
UI_stress_points.py

A standalone visual harness for ``LIB_stress_points``.

Workflow
--------
1. **Import GeoJSON …**   — load a province (worked in EPSG:3347 metres, so the
                            printed millimetres below are real ground distances).
2. **What counts**        — the pixel the width is measured at, the width above
                            which a join stops being a neck, the band the throat
                            is resolved to, and how much land has to hang on each
                            side before a join is worth reporting.
3. **Find Stress Points** — off the UI thread with a progress bar: 0.4 s on Nova
                            Scotia, 1.7 s on British Columbia, 6.7 s on Nunavut.
4. **Zoom to Worst**      — a 0.2 mm neck is a third of a pixel at province
                            extent. This is the only way to see one.
5. **Brush**              — drag on the left panel to add or erase land, and on
                            release the report is asked again. That is the whole
                            reason the lib hands its raster back: an edit costs a
                            fifth to a seventh of a full run, and the status line
                            says how many tiles it actually had to redo.
6. **Save Necks …**       — the necks as GeoJSON: a throat bar and a blob each,
                            carrying the throat, the load, the width it wants and
                            its severity.
7. **Save Settings**      — keep the parameters as the defaults, here and in the
                            Province Viewer's stress layer, so a whole country is
                            surveyed with what you tuned on one province rather
                            than with the shipped numbers — and the two windows
                            cannot drift into disagreeing about what a neck is.
                            Written to the gitignored ``project_config.local.json``;
                            **Reload** puts the boxes back to it.

What the panels show
--------------------
The **left** panel is the answer, in three layers:

* the **width field**, faint, underneath — *where is the land narrow at all?*
  One blue hue, light for narrow, dark for wide. Optional, and off by default,
  because it competes with the layer that matters.
* the **necks**, each shaded by **severity** on the reserved good → critical
  scale, with **1.0 landing on the hue change** between warning and serious.
  That is the anchor the whole module is built to give: 1.0 means the neck is
  exactly as wide as its load wants, so the colour says sound / marginal / at
  risk rather than merely darker / lighter.
* a **bar across each throat**, as long as the throat is wide, which is the line
  the piece would break along — and a severity label on the worst few. The marks
  are not decoration: a status colour must never carry the meaning on its own,
  and at province extent the shading of a 0.2 mm neck is invisible anyway.

The **right** panel is the selected neck's **branch**: the land that falls off if
it goes. "This 0.2 mm neck holds *that*" is the sentence the overlay exists to
say, and it is more convincing than any number.

Run with:  python harnesses/UI_stress_points.py

A throwaway inspection tool, deliberately kept independent of the main ``app``
window, and styled after ``UI_island_bridges.py`` and ``UI_shared_border.py``.
"""

import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.figure import Figure

from shapely.ops import unary_union

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_stress_points.py), so put
# the project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_project_config import (LOCAL_CONFIG_PATH, settings_files,
                                    update_local)
from lib.LIB_stress_points import (EPSG_METRIC, StressParams, branch_mask,
                                   load_stress_params, restress,
                                   severity_field, stress_points, width_field)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"

LAND_COLOUR = "#AED6F1"       # blue   — the province as imported
EDGE_COLOUR = "#1B4F72"       # dark blue outline
BRANCH_COLOUR = "#ABEBC6"     # green  — the land a neck holds
THROAT_COLOUR = "#17202A"     # ink    — the bar across a throat

#: The severity ramp. Severity is a **state** -- sound, marginal, at risk -- not
#: a plain magnitude, so it takes the reserved good → critical scale rather than
#: a hue of its own, and the stops are documented values rather than eyeballed
#: ones. The two middle stops sit either side of the midpoint so that **severity
#: 1.0 lands on the hue change**: with the ramp running 0 to twice the anchor,
#: anything warm is a neck thinner than its load wants.
SEVERITY_STOPS = ((0.00, "#0ca30c"),      # good
                  (0.42, "#fab219"),      # warning
                  (0.58, "#ec835a"),      # serious
                  (1.00, "#d03b3b"))      # critical
SEVERITY_CMAP = LinearSegmentedColormap.from_list(
    "severity", [(at, hex_) for at, hex_ in SEVERITY_STOPS])

#: The width layer's ramp: one blue hue, light to dark, from the documented
#: sequential steps. One hue and not a second colour, because this layer sits
#: *under* the severity it must not compete with.
#:
#: What it encodes is **narrowness**, ``ceiling - width``, rather than width. The
#: convention is light for low and dark for high, and the end worth seeing here
#: is the narrow one -- plotted the other way round a province is a solid dark
#: slab of perfectly printable interior with its faults picked out in white.
WIDTH_CMAP = LinearSegmentedColormap.from_list(
    "narrowness", ["#cde2fb", "#86b6ef", "#3987e5", "#184f95"])

#: How far the width layer is faded. It is context, not the answer; at full
#: strength it reads as the subject of the picture.
WIDTH_ALPHA = 0.55

#: Where the severity ramp tops out, as a multiple of the anchor. Clipped on
#: purpose: Nunavut's worst neck is severity 4.3 and letting it set the top of
#: the scale would flatten every other province to the same green.
DEFAULT_VMAX = 2.0

#: Label this many necks with their severity. More than this and the labels
#: overlap each other rather than naming anything.
MAX_NECK_LABELS = 12

#: The ring on a neck that is at risk, in points. Screen units on purpose, as
#: with the anchor marks in UI_island_bridges: a neck is a *place*, and the
#: thing that has to survive being read at province extent is knowing it is
#: there. The bar beneath it is the measurement and keeps its true length.
RISK_MARKER_PT = 7.0

#: What **Save Settings** writes, as ``config key -> the control holding it``.
#:
#: These are the parameters that decide what a neck *is*, so they are the ones
#: worth sharing: saved here, they are what ``UI_province_viewer`` measures a
#: whole country with next time it is opened, and the two windows cannot drift
#: into disagreeing about the same coast.  They go to the **local** config, which
#: is gitignored — the tracked file stays the project's shipped defaults, and a
#: setting typed into a window is a fact about this desk rather than a change
#: everyone should inherit through a diff.
#:
#: ``report_above`` travels with them.  It only decides what is *drawn* from a
#: measurement already made, so it was left out at first on the grounds that the
#: viewer had its own reason for its own floor -- and that floor, at 1.0, hid a
#: real neck at Rupert Inlet so completely that it read as a measurement
#: failure.  A filter the two windows disagree about is a filter that makes one
#: of them look broken.
SAVED_SETTINGS = (
    ("print.scale", "scale_spin"),
    ("stress.pixel_mm", "pixel_spin"),
    ("stress.ceiling_mm", "ceiling_spin"),
    ("stress.band_mm", "band_spin"),
    ("stress.tile_px", "tile_spin"),
    ("stress.min_lobe_mm2", "min_lobe_spin"),
    ("stress.lever_power", "lever_spin"),
    ("stress.report_above", "above_spin"),
)

#: Which ``StressParams`` field each saved key lands in, for reading them back.
_FIELD_OF = {"print.scale": "scale", "stress.pixel_mm": "pixel_mm",
             "stress.ceiling_mm": "ceiling_mm", "stress.band_mm": "band_mm",
             "stress.tile_px": "tile_px",
             "stress.min_lobe_mm2": "min_lobe_mm2",
             "stress.lever_power": "lever_power",
             "stress.report_above": "report_above"}

#: Padding around a neck when zooming to it, as a fraction of the span shown.
NECK_ZOOM_PAD = 0.4
#: How much of the province to show when framing a single neck, in print-mm.
NECK_ZOOM_MM = 12.0


class StressWorker(QObject):
    """Runs the analysis off the UI thread (6.7 s on Nunavut).

    Two jobs, not one, because they are different questions: measure this
    geometry from scratch, or measure it again after an edit reusing the raster.
    The second is the point of the lib handing its grid back, so the harness has
    to be able to ask it.
    """
    done = Signal(object, float)        # StressReport, seconds
    failed = Signal(str)

    def __init__(self, geom, params, grid=None, land=None):
        super().__init__()
        self._geom = geom
        self._params = params
        self._grid = grid
        self._land = land

    def run(self) -> None:
        try:
            started = time.perf_counter()
            if self._grid is not None and self._land is not None:
                # dirty is left to the lib, which derives it by XOR against the
                # raster it already holds -- a superset by construction, and the
                # harness has no better claim about what it painted.
                report = restress(self._grid, land=self._land,
                                  params=self._params)
            else:
                report = stress_points(self._geom, self._params)
        except Exception as exc:                        # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.done.emit(report, time.perf_counter() - started)


class StressPointsTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stress Points — Visual Tester")
        self.resize(1300, 820)

        # -- state ----------------------------------------------------------
        self.geom = None                 #: the province, in the working CRS
        self.report = None               #: last StressReport
        self.land = None                 #: the raster as the brush has left it
        self.source_path: Path | None = None
        self.source_crs = None
        self._thread: QThread | None = None
        self._worker: StressWorker | None = None
        self._painting = False
        self._painted = 0                #: cells the current stroke has flipped

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)
        self.btn_import.setToolTip(
            "Load a province, replacing whatever is on screen.\nReprojected to "
            "EPSG:3347 on the way in, so every millimetre below is a\nreal "
            "ground distance rather than a distance on a lon/lat grid.\nAll the "
            "parts of the file are measured as one piece — a province\nis "
            "printed on its own, so it is its own outline that has to survive.")
        self.btn_run = QPushButton("Find Stress Points")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)
        self.btn_run.setToolTip(
            "Measure the province against every parameter on the row below.\n"
            "Finds each neck with its own throat width — no width is chosen for "
            "it —\nweighs what falls off if the neck goes, and scores that "
            "against what a\nneck that wide can carry. Off the UI thread: about "
            "0.4 s on Nova Scotia,\n1.7 s on British Columbia, 6.7 s on "
            "Nunavut.")
        self.btn_zoom = QPushButton("Zoom to Worst")
        self.btn_zoom.clicked.connect(self.zoom_to_worst)
        self.btn_zoom.setEnabled(False)
        self.btn_zoom.setToolTip(
            "Frame the neck with the highest severity.\nThe only way to see "
            "one: a 0.20 mm throat is a fiftieth of a cell at\nprovince extent, "
            "so the shading of the worst fault on the map is\nsmaller than the "
            "pixel drawn for it.")
        self.btn_whole = QPushButton("Whole Province")
        self.btn_whole.clicked.connect(self.zoom_out)
        self.btn_whole.setEnabled(False)
        self.btn_whole.setToolTip(
            "Back out to the whole province, after a zoom or a Worst.\nThe "
            "survey view: how many necks there are and where they fall.")
        self.btn_save = QPushButton("Save Necks …")
        self.btn_save.clicked.connect(self.save_necks)
        self.btn_save.setEnabled(False)
        self.btn_save.setToolTip(
            "Write the necks out as GeoJSON — a throat bar and a blob each,\n"
            "carrying the throat width, the load, the lever arm, the width the "
            "neck\nwants and its severity. In the CRS the file arrived in, so "
            "it drops\nstraight back onto the map it came from.")
        for widget in (self.btn_import, self.btn_run, self.btn_zoom,
                       self.btn_whole, self.btn_save):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        defaults = load_stress_params(*settings_files())

        # -- second row: what counts as a neck ------------------------------
        counts_bar = QHBoxLayout()
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setDecimals(0)
        self.scale_spin.setRange(1000.0, 100_000_000.0)
        self.scale_spin.setSingleStep(1_000_000.0)
        self.scale_spin.setValue(float(defaults.scale))
        self.scale_spin.setGroupSeparatorShown(True)
        self.scale_spin.setPrefix("1:")
        self.scale_spin.setToolTip(
            "Map scale denominator.\nEvery millimetre below is multiplied by "
            "this/1000 to reach ground metres,\nso 1 mm at 1:8,000,000 is 8 km.")
        self.pixel_spin = self._spin(
            0.01, 2.0, defaults.pixel_mm, 3,
            "The pixel the width field is measured at.\nNothing narrower than "
            "two pixels exists to the distance transform,\nso this is the floor "
            "on any throat that can be reported.\nFiner costs time as the "
            "square.")
        self.ceiling_spin = self._spin(
            0.1, 50.0, defaults.ceiling_mm, 2,
            "Above this width a join is not a stress point.\nThere is no "
            "printing problem at a 50 mm throat — and the clamp is\nalso what "
            "makes an edit cheap, since the width field only changes\nwithin "
            "half of this of a change.")
        self.band_spin = self._spin(
            0.005, 1.0, defaults.band_mm, 3,
            "Throat resolution — an accuracy knob, not only a speed one.\nTwo "
            "joins landing in one band are simultaneous, so one neck's load\n"
            "can be split across both: at 0.05 mm British Columbia reports "
            "three\nnecks where the pixel-by-pixel flood finds four. 0.025 "
            "finds all four.")
        self.band_spin.setSingleStep(0.005)
        self.tile_spin = QSpinBox()
        self.tile_spin.setRange(16, 1024)
        self.tile_spin.setSingleStep(32)
        self.tile_spin.setValue(int(defaults.tile_px))
        self.tile_spin.setToolTip(
            "The unit of reuse after an edit, in grid cells.\nAt 128 the graph "
            "that has to be reassembled is 3% of the pixel graph;\nbigger tiles "
            "reassemble faster but cost more to re-summarise.")
        self.min_lobe_spin = self._spin(
            0.0, 100_000.0, defaults.min_lobe_mm2, 1,
            "How much land must hang on each side before a join is a neck.\n"
            '"Two larger areas", in numbers. A spit holds nothing up and is\n'
            "not a stress point however thin it is.")
        self.min_lobe_spin.setSingleStep(5.0)
        self.min_lobe_spin.setSuffix(" mm²")
        self.lever_spin = self._spin(
            0.0, 4.0, defaults.lever_power, 1,
            "How much the lever arm counts.\n0 weighs a load by its mass alone, "
            "which is exactly the bridge\nplanner's model; 1 also weighs how far "
            "out the mass sits, which is\nthe physics. Measured on four "
            "provinces the ranking barely moves.")
        self.lever_spin.setSingleStep(0.25)
        self.above_spin = self._spin(
            0.0, 100.0, defaults.report_above, 2,
            "Measure everything, report only necks at or above this severity.\n"
            "1.0 shows only the ones that are thinner than their load wants.")
        self.above_spin.setSingleStep(0.1)
        for spin in (self.pixel_spin, self.ceiling_spin, self.band_spin):
            spin.setSuffix(" mm")
        self.btn_save_settings = QPushButton("Save Settings")
        self.btn_save_settings.clicked.connect(self.save_settings)
        self.btn_save_settings.setToolTip(
            "Keep these parameters as the defaults, for this window and for the "
            "Province\nViewer's stress layer — so a whole country is surveyed "
            "with the settings\nyou tuned here rather than with the shipped "
            "ones, and the two cannot\ndisagree about what a neck is — nor about "
            "which part of the answer is\nworth showing, since the severity "
            "floor travels with them. Written to\nproject_config.local.json, "
            "which is not tracked: a number typed into a\nwindow is a fact "
            "about this desk.")
        self.btn_reload_settings = QPushButton("Reload")
        self.btn_reload_settings.clicked.connect(self.reload_settings)
        self.btn_reload_settings.setToolTip(
            "Put the controls back to the saved defaults, discarding whatever "
            "is\nin the boxes now. Reads the same file Save Settings writes.")

        for label, widget in (("Scale:", self.scale_spin),
                              ("Pixel:", self.pixel_spin),
                              ("Ceiling:", self.ceiling_spin),
                              ("Band:", self.band_spin),
                              ("Tile:", self.tile_spin),
                              ("Min area:", self.min_lobe_spin),
                              ("Lever:", self.lever_spin),
                              ("Report above:", self.above_spin)):
            counts_bar.addWidget(self._caption(label, widget))
            counts_bar.addWidget(widget)
        counts_bar.addSpacing(12)
        counts_bar.addWidget(self.btn_save_settings)
        counts_bar.addWidget(self.btn_reload_settings)
        counts_bar.addStretch(1)
        root.addLayout(counts_bar)

        # Every parameter above invalidates the report: the picture must never be
        # the answer to a question that is no longer on screen.
        for widget in (self.scale_spin, self.pixel_spin, self.ceiling_spin,
                       self.band_spin, self.min_lobe_spin, self.lever_spin,
                       self.above_spin):
            widget.valueChanged.connect(self._invalidate)
        self.tile_spin.valueChanged.connect(self._invalidate)

        # -- third row: what is drawn, and the brush ------------------------
        draw_bar = QHBoxLayout()
        self.width_check = QCheckBox("Show width field")
        self.width_check.setToolTip(
            "Shade all the land by how wide it is, light for narrow.\nContext "
            "for the necks rather than the answer, so it is off by default:\n"
            "it competes with the layer that matters.")
        self.region_check = QCheckBox("Show necks")
        self.region_check.setChecked(True)
        self.region_check.setToolTip(
            "Shade each neck by its severity, with 1.0 on the hue change.\n"
            "This is the layer the tool is for.")
        self.bars_check = QCheckBox("Show throats")
        self.bars_check.setChecked(True)
        self.bars_check.setToolTip(
            "A bar across each throat, as long as the throat is wide — the "
            "line\nthe piece would break along. At province extent this is the "
            "only\npart of a 0.2 mm neck big enough to see.")
        self.label_check = QCheckBox("Label severity")
        self.label_check.setChecked(True)
        self.label_check.setToolTip(
            "Print the severity beside the worst necks.\nA colour must never "
            "carry the meaning on its own.")
        self.vmax_spin = self._spin(
            0.5, 20.0, DEFAULT_VMAX, 1,
            "Where the ramp tops out.\nClipped on purpose: Nunavut's worst neck "
            "is severity 4.3, and letting\nit set the top of the scale flattens "
            "every other province to one green.")
        self.vmax_spin.setSingleStep(0.5)
        for widget in (self.width_check, self.region_check, self.bars_check,
                       self.label_check):
            widget.stateChanged.connect(lambda *_: self._draw(preserve=True))
        self.vmax_spin.valueChanged.connect(lambda *_: self._draw(preserve=True))

        self.brush_check = QCheckBox("Brush")
        self.brush_check.setToolTip(
            "Drag on the left panel to add land, or with Shift to erase it.\n"
            "On release the report is asked again through restress(), reusing "
            "the\nraster and every tile the edit could not reach — which is "
            "what the\nlib hands its grid back for.")
        self.brush_check.setEnabled(False)
        self.brush_spin = self._spin(
            0.025, 20.0, 0.5, 3,
            "Brush diameter, in printed millimetres.\nThe island editor's own "
            "brush is 0.125 mm.")
        self.brush_spin.setSuffix(" mm")
        self.brush_spin.setSingleStep(0.125)
        self.neck_box = QComboBox()
        self.neck_box.setMinimumWidth(330)
        self.neck_box.setToolTip(
            "Which neck the right panel shows the branch of.\nWorst first.")
        self.neck_box.currentIndexChanged.connect(lambda *_: self._draw_branch())

        draw_bar.addWidget(self.width_check)
        draw_bar.addWidget(self.region_check)
        draw_bar.addWidget(self.bars_check)
        draw_bar.addWidget(self.label_check)
        draw_bar.addWidget(self._caption("Ramp to:", self.vmax_spin))
        draw_bar.addWidget(self.vmax_spin)
        draw_bar.addSpacing(16)
        draw_bar.addWidget(self.brush_check)
        draw_bar.addWidget(self.brush_spin)
        draw_bar.addSpacing(16)
        draw_bar.addWidget(self._caption("Branch of:", self.neck_box))
        draw_bar.addWidget(self.neck_box)
        draw_bar.addStretch(1)
        root.addLayout(draw_bar)

        # -- map: the overlay | what one neck holds -------------------------
        self.figure = Figure(figsize=(12, 6.2))
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Deliberately NOT sharex/sharey, for the same reason as
        # UI_island_bridges: the panels must keep the size the window gives them
        # whatever shape the province is, which needs adjustable="datalim", and
        # matplotlib refuses that on shared axes at draw time.
        self.ax_map = self.figure.add_subplot(1, 2, 1)
        self.ax_branch = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import a GeoJSON to begin", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        root.addWidget(self.mpl_toolbar)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No file loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    @staticmethod
    def _spin(lo, hi, value, decimals, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setToolTip(tip)
        return spin

    @staticmethod
    def _caption(text: str, widget) -> QLabel:
        """A label for a control, carrying the control's own explanation.

        ``Band:`` on its own tells you nothing, and the caption is the half of
        the pair the pointer lands on first. Taking the tooltip from the widget
        rather than repeating it keeps one wording for one parameter.
        """
        label = QLabel(text)
        label.setToolTip(widget.toolTip())
        return label

    # -- settings -----------------------------------------------------------
    def save_settings(self) -> None:
        """Keep the parameters on screen as everyone's defaults.

        Written one key at a time through ``update_local``, which merges into
        the gitignored local config.  ``StressParams`` reads its defaults as the
        block is *built* rather than at import, so the next window to ask --
        here or in the Province Viewer -- measures with these without anything
        having to be passed between them.
        """
        saved = []
        for key, control in SAVED_SETTINGS:
            value = getattr(self, control).value()
            update_local(key, int(value) if key.endswith("tile_px")
                         or key == "print.scale" else float(value))
            saved.append(key.rpartition(".")[2])
        self.status.setText(
            f"Saved {len(saved)} settings to {Path(LOCAL_CONFIG_PATH).name} — "
            "the Province Viewer's stress layer will measure with them too.")

    def reload_settings(self) -> None:
        """Put the controls back to the saved defaults."""
        defaults = load_stress_params(*settings_files())
        for key, control in SAVED_SETTINGS:
            getattr(self, control).setValue(getattr(defaults,
                                                    _FIELD_OF[key]))
        self.status.setText(
            f"Reloaded the settings from {Path(LOCAL_CONFIG_PATH).name}.")

    # -- parameters ---------------------------------------------------------
    def params(self) -> StressParams:
        """The parameter block exactly as the controls now read it."""
        return load_stress_params(
            *settings_files(),
            scale=self.scale_spin.value(),
            pixel_mm=self.pixel_spin.value(),
            ceiling_mm=self.ceiling_spin.value(),
            band_mm=self.band_spin.value(),
            tile_px=int(self.tile_spin.value()),
            min_lobe_mm2=self.min_lobe_spin.value(),
            lever_power=self.lever_spin.value(),
            report_above=self.above_spin.value())

    def _invalidate(self, *_) -> None:
        """A changed question makes the last answer stale; drop it."""
        if self.report is None:
            return
        self.report = None
        self.land = None
        self.btn_save.setEnabled(False)
        self.btn_zoom.setEnabled(False)
        self.brush_check.setEnabled(False)
        self.brush_check.setChecked(False)
        self.neck_box.clear()
        self._draw(preserve=True)
        self.status.setText("Parameters changed — find the stress points again.")

    # -- loading and saving -------------------------------------------------
    def import_geojson(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import GeoJSON", "", GEOJSON_FILTER)
        if not path:
            return
        try:
            frame = gpd.read_file(path)
            if frame.empty:
                raise ValueError("the file has no features in it")
            self.source_crs = frame.crs
            geom = unary_union(frame.to_crs(EPSG_METRIC).geometry.values)
        except Exception as exc:                        # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self.geom = geom
        self.source_path = Path(path)
        self.report = None
        self.land = None
        self.neck_box.clear()
        self.btn_run.setEnabled(True)
        self.btn_whole.setEnabled(True)
        self.btn_zoom.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.brush_check.setEnabled(False)
        self._draw()
        self.status.setText(
            f"{self.source_path.name} — {geom.area / self._mpm() ** 2:.0f} mm² "
            f"of land at 1:{self.scale_spin.value():,.0f}. "
            "Find the stress points, then brush at them.")

    def save_necks(self) -> None:
        """The necks as GeoJSON: a bar and a blob each, with their numbers."""
        if self.report is None or not self.report.necks:
            return
        suggested = ""
        if self.source_path is not None:
            suggested = str(self.source_path.with_name(
                self.source_path.stem + "_necks.geojson"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Necks", suggested, GEOJSON_FILTER)
        if not path:
            return
        rows, geoms = [], []
        for rank, neck in enumerate(self.report.necks, start=1):
            for kind, geom in (("throat", neck.throat), ("region", neck.region)):
                if geom is None or geom.is_empty:
                    continue
                rows.append(dict(rank=rank, kind=kind,
                                 throat_mm=neck.throat_mm,
                                 load_mm2=neck.load_mm2,
                                 other_mm2=neck.other_mm2,
                                 lever_mm=neck.lever_mm,
                                 needed_mm=neck.needed_mm,
                                 severity=neck.severity,
                                 at_risk=bool(neck.at_risk),
                                 severed=bool(neck.severed)))
                geoms.append(geom)
        try:
            frame = gpd.GeoDataFrame(rows, geometry=geoms, crs=EPSG_METRIC)
            if self.source_crs is not None:
                frame = frame.to_crs(self.source_crs)
            frame.to_file(path, driver="GeoJSON")
        except Exception as exc:                        # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Wrote {len(rows)} features to {Path(path).name}.")

    # -- running ------------------------------------------------------------
    def run(self) -> None:
        if self.geom is None or self._thread is not None:
            return
        self._start(StressWorker(self.geom, self.params()),
                    "Measuring the width field and flooding the land …")

    def _rerun_after_edit(self) -> None:
        """Ask again after a brush stroke, reusing the raster and the tiles."""
        if self.report is None or self.land is None or self._thread is not None:
            return
        self._start(StressWorker(self.geom, self.params(),
                                 grid=self.report.grid, land=self.land),
                    "Re-checking the tiles the edit could reach …")

    def _start(self, worker: StressWorker, message: str) -> None:
        self._busy(True, message)
        self._thread = QThread(self)
        self._worker = worker
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self._thread.start()

    def _busy(self, busy: bool, message: str = "") -> None:
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if busy else 1)
        for widget in (self.btn_import, self.btn_run, self.btn_save,
                       self.btn_zoom, self.btn_whole):
            widget.setEnabled(not busy and self._enabled_when_idle(widget))
        if message:
            self.status.setText(message)

    def _enabled_when_idle(self, widget) -> bool:
        if widget is self.btn_run or widget is self.btn_whole:
            return self.geom is not None
        if widget is self.btn_import:
            return True
        return self.report is not None and bool(self.report.necks)

    def _finish_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

    def _on_failed(self, message: str) -> None:
        self._finish_thread()
        self._busy(False)
        QMessageBox.critical(self, "Measuring failed", message)
        self.status.setText("Measuring failed — see the message.")

    def _on_done(self, report, seconds: float) -> None:
        self._finish_thread()
        self.report = report
        self.land = report.grid.land.copy() if report.grid is not None else None
        self._busy(False)
        self._fill_neck_box()
        self.btn_zoom.setEnabled(bool(report.necks))
        self.btn_save.setEnabled(bool(report.necks))
        self.brush_check.setEnabled(report.grid is not None)
        self._draw(preserve=True)
        self.status.setText(self._report_line(seconds))

    def _report_line(self, seconds: float) -> str:
        """The verdict, and what it cost. Never a bare count.

        The cost is on screen because the incremental path is invisible when it
        works, and that is exactly when a stale overlay would go unnoticed.
        """
        report = self.report
        if report is None:
            return ""
        how = ("measured from scratch" if report.rebuilt else
               f"re-checked {report.tiles_recomputed} of "
               f"{report.tiles_total} tiles")
        extra = []
        if report.repaired:
            extra.append("geometry repaired first")
        if report.coarsened:
            extra.append(f"pixel coarsened to {report.pixel_mm:.3f} mm")
        unsevered = sum(1 for n in report.necks if not n.severed)
        if unsevered:
            extra.append(f"{unsevered} load(s) are undercounts — the cut did "
                         f"not part the sides")
        tail = f" ({'; '.join(extra)})" if extra else ""
        return f"{report.verdict()} — {how} in {seconds:.2f} s{tail}"

    def _fill_neck_box(self) -> None:
        self.neck_box.blockSignals(True)
        self.neck_box.clear()
        for rank, neck in enumerate(self.report.necks if self.report else [],
                                    start=1):
            mark = "!" if neck.at_risk else " "
            self.neck_box.addItem(
                f"{mark} {rank}. {neck.throat_mm:.2f} mm holds "
                f"{neck.load_mm2:.0f} mm² — severity {neck.severity:.2f}")
        self.neck_box.blockSignals(False)

    # -- the brush ----------------------------------------------------------
    def _mpm(self) -> float:
        return self.scale_spin.value() / 1000.0

    def _cell_at(self, x: float, y: float):
        """Which grid cell a point in the working CRS falls in, or None."""
        grid = self.report.grid if self.report else None
        if grid is None:
            return None
        transform = grid.transform
        col = int((x - transform.c) / transform.a)
        row = int((y - transform.f) / transform.e)
        if 0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]:
            return row, col
        return None

    def _paint(self, event) -> None:
        """Flip a brush-sized disc of cells. Shift erases, otherwise it adds."""
        cell = self._cell_at(event.xdata, event.ydata)
        if cell is None or self.land is None:
            return
        grid = self.report.grid
        row, col = cell
        radius = max(0, int(round(self.brush_spin.value()
                                  / grid.pixel_mm / 2.0)))
        r0, r1 = max(0, row - radius), min(grid.shape[0], row + radius + 1)
        c0, c1 = max(0, col - radius), min(grid.shape[1], col + radius + 1)
        rows = np.arange(r0, r1)[:, None] - row
        cols = np.arange(c0, c1)[None, :] - col
        disc = rows ** 2 + cols ** 2 <= max(radius, 0.5) ** 2
        erase = bool(event.key and "shift" in str(event.key))
        before = int(self.land[r0:r1, c0:c1].sum())
        self.land[r0:r1, c0:c1] = np.where(disc, not erase,
                                           self.land[r0:r1, c0:c1])
        self._painted += abs(int(self.land[r0:r1, c0:c1].sum()) - before)

    def _on_press(self, event) -> None:
        """Start a stroke on the map panel, if the brush is on.

        The nav toolbar is given right of way while it has a mode: a pan or a
        zoom-rectangle starts with exactly this event and must not also edit the
        province.
        """
        if (not self.brush_check.isChecked() or self._thread is not None
                or event.inaxes is not self.ax_map or event.button != 1
                or str(self.mpl_toolbar.mode)
                or event.xdata is None or event.ydata is None):
            return
        self._painting = True
        self._painted = 0
        self._paint(event)
        self._draw_land_only()

    def _on_motion(self, event) -> None:
        if not self._painting or event.inaxes is not self.ax_map:
            return
        if event.xdata is None or event.ydata is None:
            return
        self._paint(event)
        self._draw_land_only()

    def _on_release(self, event) -> None:
        if not self._painting:
            return
        self._painting = False
        if not self._painted:
            return
        self.status.setText(f"{self._painted} cells edited — re-checking …")
        self._rerun_after_edit()

    # -- drawing ------------------------------------------------------------
    def _reset_axes(self, left_title: str, right_title: str) -> None:
        for ax, title in ((self.ax_map, left_title),
                          (self.ax_branch, right_title)):
            self._style_axis(ax, title)

    def _style_axis(self, ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        # Fixed panel, not a fixed view: with the default adjustable="box"
        # matplotlib satisfies "equal" by shrinking the axes box to the data's
        # own shape, so a long thin province collapses the panel to a hairline
        # and the window rearranges itself on every import.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])

    def _view(self, ax):
        return ax.get_xlim(), ax.get_ylim()

    def _set_view(self, ax, xlim, ylim) -> None:
        """Move a panel, both limits at once.

        On an equal-aspect panel a view can only be put back both limits
        together: set x alone and ``apply_aspect`` settles the aspect against a
        y that has not moved yet, which pulls x straight back.
        """
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

    def _extent(self, grid):
        minx, miny, maxx, maxy = grid.bounds
        return (minx, maxx, miny, maxy)

    def _draw(self, preserve: bool = False) -> None:
        """The map panel: land, then whichever layers are switched on.

        The title is settled before the axes are styled, because styling clears
        them -- one pass through the draw, so there is no second place for the
        layer order to be decided.
        """
        keep = self._view(self.ax_map) if preserve else None
        if self.geom is None:
            self._reset_axes("Import a GeoJSON to begin", "")
            self.canvas.draw_idle()
            return

        report = self.report
        grid = report.grid if report else None
        necks = report.necks if report else []
        if necks:
            title = (f"Stress points — {len(report.at_risk)} at risk of "
                     f"{len(necks)}")
        elif report is not None:
            title = "Stress points — none found"
        else:
            title = "Stress points — not yet measured"
        self._style_axis(self.ax_map, title)

        widths = None
        if grid is not None and self.width_check.isChecked():
            widths, _ = width_field(report, grid)
        if widths is not None:
            self.ax_map.imshow(
                np.ma.masked_where(~grid.land, grid.ceiling_mm - widths),
                extent=self._extent(grid), origin="upper",
                cmap=WIDTH_CMAP, vmin=0.0, vmax=grid.ceiling_mm,
                alpha=WIDTH_ALPHA, interpolation="nearest", zorder=1)
            gpd.GeoSeries([self.geom]).boundary.plot(
                ax=self.ax_map, color=EDGE_COLOUR, linewidth=0.4, zorder=2)
        else:
            gpd.GeoSeries([self.geom]).plot(
                ax=self.ax_map, facecolor=LAND_COLOUR, edgecolor=EDGE_COLOUR,
                linewidth=0.4, zorder=1)

        if necks and self.region_check.isChecked():
            field, _ = severity_field(report, grid)
            if field is not None:
                self.ax_map.imshow(
                    np.ma.masked_where(field <= 0.0, field),
                    extent=self._extent(grid), origin="upper",
                    cmap=SEVERITY_CMAP,
                    norm=Normalize(0.0, self.vmax_spin.value()),
                    interpolation="nearest", zorder=3)
        self._draw_throats()

        self.ax_map.relim()
        self.ax_map.autoscale_view()
        if keep is not None:
            self._set_view(self.ax_map, *keep)
        self._draw_branch(redraw=False)
        self.canvas.draw_idle()

    def _draw_throats(self) -> None:
        """A bar across each throat, in its own severity, and a number beside it.

        **The bar is where the colour has to live.** The shaded blob is the
        honest extent of a neck, and the honest extent of the worst ones is
        sub-pixel: Nova Scotia's 0.20 mm throat is a fiftieth of a cell at
        province extent, so at the zoom the map is read at its shading cannot be
        seen at all and the picture would say "nothing here is red". The bar is
        drawn in screen units, so it is legible at every zoom, and it carries the
        severity colour over an ink casing that keeps it legible on any
        background.

        Colour still never carries the meaning alone: the bar thickens when a
        neck is at risk, the number is printed beside it, and the verdict under
        the panel counts them.
        """
        if self.report is None or not self.bars_check.isChecked():
            return
        vmax = max(self.vmax_spin.value(), 1e-6)
        for rank, neck in enumerate(self.report.necks, start=1):
            if neck.throat is None or neck.throat.is_empty:
                continue
            xs, ys = zip(*neck.throat.coords)
            colour = SEVERITY_CMAP(min(neck.severity / vmax, 1.0))
            width = 3.0 if neck.at_risk else 1.8
            self.ax_map.plot(xs, ys, color=THROAT_COLOUR,
                             linewidth=width + 1.6, solid_capstyle="round",
                             zorder=4)
            self.ax_map.plot(xs, ys, color=colour, linewidth=width,
                             solid_capstyle="round", zorder=5)
            if neck.at_risk:
                self.ax_map.plot(
                    [neck.point.x], [neck.point.y], marker="o",
                    markersize=RISK_MARKER_PT, markerfacecolor="none",
                    markeredgecolor=colour, markeredgewidth=1.8, zorder=6)
            if self.label_check.isChecked() and rank <= MAX_NECK_LABELS:
                self.ax_map.annotate(
                    f"{neck.severity:.2f}",
                    (neck.point.x, neck.point.y),
                    textcoords="offset points", xytext=(6, 5), fontsize=7.5,
                    color=THROAT_COLOUR, zorder=6,
                    fontweight="bold" if neck.at_risk else "normal")

    def _draw_land_only(self) -> None:
        """Mid-stroke feedback: the brushed raster, nothing measured.

        The necks are deliberately *not* redrawn here. They belong to the
        geometry as it was before the stroke, and leaving them on screen while
        the land moves under them would be the one thing this harness must not
        do: show an answer next to a question it does not belong to.
        """
        if self.land is None or self.report is None:
            return
        grid = self.report.grid
        keep = self._view(self.ax_map)
        self._style_axis(self.ax_map, "Stress points — editing …")
        self.ax_map.imshow(
            np.ma.masked_where(~self.land, self.land.astype(float)),
            extent=self._extent(grid), origin="upper",
            cmap=LinearSegmentedColormap.from_list("land", [LAND_COLOUR,
                                                            LAND_COLOUR]),
            interpolation="nearest", zorder=1)
        self._set_view(self.ax_map, *keep)
        self.canvas.draw_idle()

    def _draw_branch(self, redraw: bool = True) -> None:
        """The right panel: the land the selected neck holds up."""
        report = self.report
        index = self.neck_box.currentIndex()
        if report is None or not report.necks or index < 0:
            self._style_axis(self.ax_branch, "")
            if redraw:
                self.canvas.draw_idle()
            return
        neck = report.necks[min(index, len(report.necks) - 1)]
        grid = report.grid
        self._style_axis(
            self.ax_branch,
            f"Held by a {neck.throat_mm:.2f} mm throat — {neck.load_mm2:.0f} mm²"
            + ("" if neck.severed else "  (undercount: the cut did not part it)"))
        gpd.GeoSeries([self.geom]).plot(
            ax=self.ax_branch, facecolor="#EAEDED", edgecolor=EDGE_COLOUR,
            linewidth=0.4, zorder=1)
        mask = branch_mask(report, neck, grid)
        if mask is not None:
            self.ax_branch.imshow(
                np.ma.masked_where(~mask, mask.astype(float)),
                extent=self._extent(grid), origin="upper",
                cmap=LinearSegmentedColormap.from_list(
                    "branch", [BRANCH_COLOUR, BRANCH_COLOUR]),
                interpolation="nearest", zorder=2)
        if neck.throat is not None and not neck.throat.is_empty:
            xs, ys = zip(*neck.throat.coords)
            self.ax_branch.plot(xs, ys, color=THROAT_COLOUR, linewidth=2.0,
                                solid_capstyle="butt", zorder=3)
        self.ax_branch.relim()
        self.ax_branch.autoscale_view()
        if redraw:
            self.canvas.draw_idle()

    # -- framing ------------------------------------------------------------
    def zoom_to_worst(self) -> None:
        """Frame the worst neck. A 0.2 mm throat is invisible at full extent."""
        if self.report is None or not self.report.necks:
            return
        neck = self.report.necks[0]
        half = NECK_ZOOM_MM * self._mpm() * (1.0 + NECK_ZOOM_PAD) / 2.0
        self._set_view(self.ax_map,
                       (neck.point.x - half, neck.point.x + half),
                       (neck.point.y - half, neck.point.y + half))
        self.canvas.draw_idle()
        self.status.setText(f"Worst neck: {neck}.")

    def zoom_out(self) -> None:
        if self.geom is None:
            return
        minx, miny, maxx, maxy = self.geom.bounds
        pad = 0.03 * max(maxx - minx, maxy - miny)
        self._set_view(self.ax_map, (minx - pad, maxx + pad),
                       (miny - pad, maxy + pad))
        self.canvas.draw_idle()


def main() -> int:
    app = QApplication(sys.argv)
    window = StressPointsTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
