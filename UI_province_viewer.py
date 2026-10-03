"""
UI_province_viewer.py

Every province at once, at the size it will print — and every *stage* at once,
one folder per layer.

Where ``UI_island_editor`` opens one province and changes it, this opens *many*
and changes nothing.  It answers two questions a single-file tool cannot be
asked.  Laid out together at 1:8 000 000, how big is the whole thing and do the
pieces line up?  And, given a folder per pipeline stage — originals, cleaned,
bordered — **what actually changed between them?**

The second question is answered by blinking.  Each folder loads as a layer,
Space steps to the next one, and the camera does not move: two stages drawn on
the same pixels and swapped back and forth make a changed coastline jump out the
way nothing else does.  For that to work three things have to hold still — the
view, the colour of each province, and which provinces are selected — so all
three are keyed to the province rather than to the layer being drawn.

Workflow
--------
1. **Add Layer …** — pick a folder; every GeoJSON in it becomes one layer, drawn
   in its true position (worked in EPSG:3347 metres, so provinces sit together
   exactly as the ground does).  Add a folder per pipeline stage.
   **Import GeoJSON …** adds loose files to the layer you are on.
2. **Blink** — ``Space`` for the next layer, ``Shift+Space`` for the previous,
   ``1``–``9`` to jump straight to one; or the ``◀ ▶`` buttons and the layer
   list.  The view never moves on a swap.
3. **Look** — left-drag pans, the arrow keys pan in steps (Shift for a long
   stride, Ctrl for a nudge), the wheel zooms, and a left click with no drag
   toggles a province's selection. **Selection** has All / None / Invert, as in
   the island editor, and survives a layer swap.
4. **Print scale** — the same 1:N control, km/mm readout and **Real Size** button
   the editor has, over the same two scale bars. The status line adds the one
   figure this tool exists for: how many millimetres across the loaded provinces
   come out at the current reduction.
5. **Show thin** — mark, in red, every piece of land narrower than **Min width**
   as it will print: the necks that snap when a piece is lifted, the spits that
   break off in the post, the islets that are not there at all.  A whole layer
   is measured at once, so this is the survey a single-file tool cannot give
   you — *which* provinces have a printing problem, before any of them is
   opened.  **Zoom to Worst** goes to the largest patch, because a one-
   millimetre fault is invisible at the zoom a whole country is read at.
6. **Show stress** — the other half of that question.  *Show thin* finds narrow
   land; this finds the narrow land that is **holding something up**, and says
   how much.  They are not the same survey: Nova Scotia has 338 thin patches at
   a millimetre and exactly one of them is load-bearing, and Nunavut has a
   1.0 mm neck carrying 457 mm² that passes a 1 mm width test without comment.
   Each neck is marked on the **severity** ramp, where **1.0 means the neck is
   exactly as wide as its load wants** — so the colour says sound, marginal or
   at risk rather than merely more or less.  **Min severity** filters what is
   drawn without re-measuring anything, and **Worst neck** goes to the one most
   likely to break.  Measuring a layer is about fifteen seconds for eight
   provinces, so it says where it has got to as it goes.

Nothing here writes. There is no undo because there is nothing to undo; to
change a province, open it in ``UI_island_editor``.

Run with:  python UI_province_viewer.py

Reuses ``lib.LIB_map_scale``, ``lib.LIB_thin_features``,
``lib.LIB_stress_points``,
``lib.LIB_project_config`` and ``lib.LIB_screen_calibration``; kept independent
of the main ``app`` window like the other UI_* harnesses.
"""

import logging
import sys
from pathlib import Path

import geopandas as gpd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from lib.LIB_island_merging import _polygons
from lib.LIB_map_scale import (grouped, km_per_mm, printed_size_mm, scale_bars,
                               to_km)
from lib.LIB_project_config import get as cfg
from lib.LIB_stress_points import StressParams, stress_points
from lib.LIB_thin_features import ThinParams, thin_features
from lib.LIB_screen_calibration import (MM_PER_INCH, load_saved_ppi,
                                        monitor_at, px_per_mm)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
GEOJSON_GLOBS = ("*.geojson", "*.json")
EPSG_METRIC = cfg("crs.working")

_ROOT = Path(__file__).resolve().parent
DEFAULT_LOAD_DIR = _ROOT / cfg("paths.provinces_out")

#: Name for the layer loose files land in, when there is no layer to put them on.
LOOSE_LAYER = "Files"

#: Layers reachable by a number key. Nine is what one hand covers, and a
#: pipeline with more stages than that is better stepped through with Space.
LAYER_KEYS = 9

#: Provinces are told apart by colour rather than labelled, because a label per
#: province is unreadable at the zoom this tool is for — the whole country — and
#: the status line names whatever is selected anyway.
#:
#: Six, where four would do: this is the map-colouring problem, and real
#: provinces greedy-colour in four every time (see ``_colour_map``). The spare
#: two give the greedy room and keep the map from looking like a proof.
PROVINCE_COLOURS = ["#AED6F1", "#A9DFBF", "#F9E79F", "#F5CBA7", "#D7BDE2",
                    "#F5B7B1"]
SELECT_COLOUR = "#F5B041"
EDGE_COLOUR = "#1B4F72"
SELECT_EDGE = "#B9770E"
SCALE_COLOUR = "#2C3E50"

#: Land too thin to print. A saturated red rather than the palette's softer
#: ``#E6787A``, because these patches are a millimetre across on a map of a
#: whole country: whatever marks them has to win against six pastel fills at a
#: zoom where it is only a few pixels wide.
THIN_COLOUR = "#C0392B"

#: Stroke on a thin patch, in points. The fill alone is sub-pixel at country
#: zoom -- a patch a printed millimetre across is drawn as a line rather than
#: as nothing. The mark sits exactly where the fault is; only its rendered
#: width is generous.
THIN_LINEWIDTH = 0.9

#: The minimum width to measure against, printed mm, as ``(lo, hi, default,
#: decimals, step)``. Printed sizes converted through the print scale at the
#: moment of use, as everywhere else here.
THIN_WIDTH_MM = (0.0, 50.0, cfg("thin.width_mm"), 3, 0.05)

#: What share of the view the worst patch takes up once framed. Small enough to
#: keep its surroundings on screen -- a neck means nothing without the two
#: landmasses either side of it.
THIN_ZOOM_SHARE = 0.15

#: The severity ramp for the stress layer. Severity is a **state** -- sound,
#: marginal, at risk -- rather than a plain magnitude, so it takes the reserved
#: good-to-critical scale. The two middle stops sit either side of the midpoint
#: so that, with the ramp running to twice the anchor, **severity 1.0 lands on
#: the hue change**: anything warm is a neck thinner than its load wants.
#:
#: The same stops as ``harnesses/UI_stress_points.py``, which is the other place
#: this ramp is spelled out; ``tests/test_province_viewer_stress.py`` holds the
#: two copies in step.
SEVERITY_STOPS = ((0.00, "#0ca30c"),      # good
                  (0.42, "#fab219"),      # warning
                  (0.58, "#ec835a"),      # serious
                  (1.00, "#d03b3b"))      # critical
SEVERITY_CMAP = LinearSegmentedColormap.from_list(
    "severity", [(at, hex_) for at, hex_ in SEVERITY_STOPS])

#: Where the ramp tops out, as a multiple of the anchor. Clipped on purpose:
#: Nunavut's worst neck is severity 4.3, and letting one province set the top of
#: the scale would flatten every other one to the same green.
STRESS_VMAX = 2.0

#: Ink under every stress mark, so a severity colour stays legible wherever it
#: lands -- including on top of the thin layer's red, which is a different claim
#: about the same place and may well be switched on beside it.
STRESS_CASING = "#17202A"

#: The ring on a neck that is at risk, in points, and the stroke on a throat
#: bar. Screen units, as with the thin layer's stroke and for the same reason: a
#: neck is a *place*, and at the zoom a whole country is read at the honest
#: extent of the worst of them is a fraction of a pixel.
STRESS_MARKER_PT = 9.0
STRESS_LINEWIDTH = 1.6

#: The smallest severity worth drawing, as ``(lo, hi, default, decimals,
#: step)``. **1.0 by default**, which is the anchor: this tool is a survey of
#: thirteen provinces, and the question it is here to answer is which of them
#: will break, not where every sound neck is.
STRESS_SEVERITY = (0.0, 20.0, 1.0, 2, 0.25)

#: How much of the map to show when framing a neck, in printed millimetres. A
#: floor rather than a share of the neck: a throat is a fraction of a millimetre
#: and framing it tightly would show nothing but the throat, where what makes it
#: a neck is the land either side.
STRESS_ZOOM_MM = 16.0

DEFAULT_PRINT_SCALE = cfg("print.scale")
PRINT_SCALE_MIN, PRINT_SCALE_MAX = 1_000, 500_000_000
PRINT_SCALE_STEP = 500_000

SCALE_X = 0.02
SCALE_Y = (0.105, 0.045)
SCALE_SPAN = 0.22
SCALE_CAP = 0.012
SCALE_GAP = 0.008
SCALE_FONT_PT = 8

PAN_SLOP_PX = 4
STATUS_POINT_BUMP = 2
PAN_STEP = 0.10
PAN_STEP_COARSE = 0.50
PAN_STEP_FINE = 0.02

# As in the editor: ``aspect="equal", adjustable="datalim"`` makes matplotlib
# widen the view rather than shrink the axes box, and log a warning every time.
# That is the intended behaviour here, so drop just that message.
logging.getLogger("matplotlib.axes._base").addFilter(
    lambda record: "to fulfill fixed data aspect" not in record.getMessage())

_NAV_KEEP = ("Home", "Back", "Forward", "Pan", "Zoom")
_NAV_TIPS = {"Back": "Back to the previous view", "Forward": "Forward again"}


def _dir_or_cwd(path: Path) -> str:
    """Directory as a string if it exists, else fall back to the cwd."""
    return str(path if path.is_dir() else Path.cwd())


def geojson_in(folder) -> list:
    """Every GeoJSON in a folder, in a stable order."""
    folder = Path(folder)
    return sorted(p for pattern in GEOJSON_GLOBS for p in folder.glob(pattern))


def stage_suffix(names) -> str:
    """
    The ``_word`` that *every* file in a folder ends with, or ``""``.

    Pipeline stages stamp their output: ``JSonProvinces-Bordered`` holds
    ``CAN_Alberta_simplified.geojson`` where ``JSonProvinces-Cleaned`` holds
    ``CAN_Alberta.geojson``.  Stripping the suffix the whole folder shares is
    what lets the viewer see those two as the same Alberta, and so hold its
    colour and its selection still while you blink between the layers.

    Only a suffix the whole folder shares counts, and only when there is more
    than one file to share it: ``CAN_Alberta`` on its own would otherwise read
    as the province ``CAN`` at stage ``Alberta``, and a folder of
    ``CAN_Alberta`` and ``CAN_Manitoba`` would keep its two province names
    precisely because they disagree.
    """
    names = list(names)
    if len(names) < 2:
        return ""
    suffix = "_" + names[0].rpartition("_")[2]
    # One ``endswith`` settles three questions. The suffix is shared; the
    # underscore it starts with is the last one in every name, so there is no
    # need to re-split them; and a name carrying no underscore at all is
    # rejected too, since ``rpartition`` hands back the whole of it and nothing
    # can end with a string longer than itself. The length test is the one
    # thing left over: a name that *is* the suffix has no province left in it.
    if any(not name.endswith(suffix) or len(name) <= len(suffix)
           for name in names):
        return ""
    return suffix


class MapToolbar(NavigationToolbar):
    """Matplotlib's navigation toolbar, trimmed as in the island editor."""

    toolitems = [(text, _NAV_TIPS.get(text, tip), image, callback)
                 for text, tip, image, callback in NavigationToolbar.toolitems
                 if text in _NAV_KEEP]


class Province:
    """One loaded file: its name, its geometry, and the parts drawn from it."""

    def __init__(self, path: Path, geometry, key: str = ""):
        self.path = path
        self.name = path.stem
        self._thin_key = None            # radius the cached report used
        self._thin = None                # last ThinReport, if any
        self._stress_key = None          # the measurement the cached necks used
        self._stress = None              # last StressReport, if any
        #: What identifies this province *across* layers: the file's name with
        #: the layer's stage suffix taken off. Colour and selection hang off it.
        self.key = key or path.stem
        self.geometry = geometry
        self.parts = _polygons(geometry)

    @property
    def area_km2(self) -> float:
        return self.geometry.area / 1e6

    def thin_report(self, params: ThinParams):
        """The too-thin land, measured once and kept.

        Keyed on the ground radius rather than on the printed width, because
        that is what the measurement actually used: turning the print scale
        changes the answer exactly as turning the width does, and a cache keyed
        on the width alone would keep serving the old verdict.

        Kept because the viewer redraws on every layer swap and every click,
        and measuring Nunavut is 0.6 s -- fine on a button press, not fine
        thirty times a second.
        """
        key = round(params.radius_m, 6)
        if self._thin_key != key:
            self._thin = thin_features(self.geometry, params)
            self._thin_key = key
        return self._thin

    def stress_report(self, params: StressParams):
        """The load-bearing necks, measured once and kept.

        **The raster is thrown away.**  ``stress_points`` hands back the grid it
        worked on so that an edit can reuse it, which is exactly what the island
        editor wants and exactly what a viewer does not: nothing here writes, and
        the layer is drawn from each neck's own geometry.  Keeping the grids
        would cost 116 MB for the eight cleaned provinces -- 51 MB of it Nunavut
        -- against nothing at all for the necks themselves.

        The key leaves out ``report_above`` on purpose.  That one only decides
        which necks are *reported*, so the viewer measures with it at zero and
        filters as it draws; turning Min severity is then instant rather than a
        fresh fifteen seconds.
        """
        key = (round(params.scale, 6), round(params.pixel_mm, 6),
               round(params.ceiling_mm, 6), round(params.band_mm, 6),
               round(params.min_lobe_mm2, 6), round(params.lever_power, 6))
        if self._stress_key != key:
            report = stress_points(self.geometry, params)
            report.grid = None
            self._stress = report
            self._stress_key = key
        return self._stress


class Layer:
    """One folder, loaded whole: a single stage of the pipeline."""

    def __init__(self, name: str, path, provinces: list):
        self.name = name
        self.path = path                 # None for a layer of loose files
        self.provinces = provinces

    def __len__(self) -> int:
        return len(self.provinces)


class ProvinceViewer(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1280, 800)

        # -- state ----------------------------------------------------------
        self.layers: list[Layer] = []
        self.active = 0                     # index into ``layers``
        #: Selected province *keys*, not indices: index 5 is a different
        #: province in a layer with a different file count, so an index-based
        #: selection would quietly jump to another province on a swap.
        self.selected: set = set()
        self._colours: dict = {}            # key -> colour, one map for all layers
        self._grab = None
        self._dragged = False
        self._scale_artists: list = []
        self._scale_key = None
        #: Set while the stress layer is being measured. The measurement lets
        #: the event loop through so the window keeps painting, which also lets
        #: the button be pressed again; this is what stops the second press.
        self._measuring = False

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 4)
        root.setSpacing(4)

        # Built before the canvas: the map's furniture reads it on every draw,
        # and the first draw is scheduled from ``_style_axis`` below.
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(PRINT_SCALE_MIN, PRINT_SCALE_MAX)
        self.scale_spin.setSingleStep(PRINT_SCALE_STEP)
        self.scale_spin.setValue(DEFAULT_PRINT_SCALE)
        self.scale_spin.setGroupSeparatorShown(True)
        self.scale_spin.setPrefix("1 : ")
        self.scale_spin.setToolTip(
            "Reduction the provinces are printed at, as 1:N. It sets the "
            "millimetre scale bar and the printed size in the status line.")
        self.perkm_box = QLineEdit(self._perkm_text())
        self.perkm_box.setReadOnly(True)
        self.perkm_box.setToolTip(
            "Ground kilometres under one printed millimetre, at the scale on "
            "the left. Read-only — set the scale to change it.")
        self.perkm_box.setFixedWidth(
            self.perkm_box.fontMetrics().horizontalAdvance("000.00 km/mm") + 16)
        self.scale_spin.valueChanged.connect(self._on_print_scale)

        self.btn_real = self._button("Real Size", self.zoom_to_real_size,
                                     enabled=False)
        self.btn_real.setToolTip(
            "Zoom so the map is its printed size: a millimetre on screen "
            "becomes a millimetre on paper, at the scale on the left. Needs "
            "this monitor measured first — run ScreenRuler.py to do that.")

        # -- canvas ---------------------------------------------------------
        self.caption = QLabel()
        self._emphasise(self.caption)       # names the layer you are looking at
        self.figure = Figure(figsize=(11, 7))
        self.figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._style_axis(self._caption_text())
        self.nav = MapToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("draw_event", self._on_draw)

        # -- row 1: the documents and the view -------------------------------
        top = QHBoxLayout()
        self.btn_layer = self._button("Add Layer …", self.add_layer)
        self.btn_layer.setToolTip(
            "Load every GeoJSON in a folder as one layer — one stage of the "
            "pipeline. Add a folder per stage, then blink between them.")
        self.btn_import = self._button("Import GeoJSON …", self.import_files)
        self.btn_import.setToolTip("Add loose files to the layer you are on.")
        self.btn_clear_all = self._button("Clear", self.clear_all, enabled=False)
        self.btn_clear_all.setToolTip("Unload every layer.")
        for widget in (self.btn_layer, self.btn_import, self.btn_clear_all,
                       self._separator(), self.nav, self._separator(),
                       QLabel("Print scale"), self.scale_spin, self.perkm_box,
                       self.btn_real):
            top.addWidget(widget)
        top.addStretch(1)
        root.addLayout(top)

        # -- row 2: layers, then selection -----------------------------------
        self.btn_prev = self._button("◀", self.previous_layer, enabled=False)
        self.btn_prev.setToolTip("Previous layer  (Shift+Space)")
        self.btn_prev.setFixedWidth(28)
        self.btn_next = self._button("▶", self.next_layer, enabled=False)
        self.btn_next.setToolTip("Next layer  (Space) — the blink key")
        self.btn_next.setFixedWidth(28)
        self.layer_box = QComboBox()
        self.layer_box.setMinimumWidth(240)
        self.layer_box.setToolTip(
            "The layer on show. Number keys 1–9 jump straight to one.")
        self.layer_box.currentIndexChanged.connect(self._on_layer_box)
        self.btn_drop_layer = self._button("Remove", self.remove_layer,
                                           enabled=False)
        self.btn_drop_layer.setToolTip("Unload the layer on show.")
        self.grp_layers = self._group(
            "Layers", self.btn_prev, self.layer_box, self.btn_next,
            self.btn_drop_layer)

        # The only other group there is: this tool looks, it does not act, so
        # there is nothing for a selection to be the subject *of* beyond reading
        # it off — and beyond following one province between the layers.
        self.btn_select_all = self._button("All", self.select_all, enabled=False)
        self.btn_clear = self._button("None", self.clear_selection, enabled=False)
        self.btn_invert = self._button("Invert", self.invert_selection,
                                       enabled=False)
        self.btn_invert.setToolTip(
            "Swap selected for unselected: click the provinces to keep in mind, "
            "then invert to have every other one selected instead.")
        self.lbl_count = QLabel()
        self.grp_selection = self._group(
            "Selection", self.btn_select_all, self.btn_clear, self.btn_invert,
            self.lbl_count)

        # Printability. The one thing here that is a *judgement* about the
        # provinces rather than a way of looking at them -- and it belongs in a
        # viewer rather than an editor because the question is which of thirteen
        # provinces has a problem, which is not a question you can ask one file
        # at a time.
        self.thin_spin = self._spin(*THIN_WIDTH_MM,
            "Land narrower than this, as it will print (mm), is marked in red. "
            "Below about a millimetre a neck snaps when the piece is lifted and "
            "a spit breaks off in the post. The km/mm box up beside the print "
            "scale says what one millimetre is worth on the ground.")
        self.thin_spin.valueChanged.connect(self._on_thin_width)
        self.btn_thin = self._button("Show thin", self._toggle_thin,
                                     enabled=False)
        self.btn_thin.setCheckable(True)
        self.btn_thin.setToolTip(
            "Mark every piece of land narrower than the width on the left. "
            "Measured by asking whether a disc that wide fits inside the land, "
            "so it catches necks, spits and whole islets alike — and says which "
            "are which. The whole layer is measured at once; Nunavut takes "
            "about half a second.")
        self.btn_worst = self._button("Zoom to Worst", self.zoom_to_worst,
                                      enabled=False)
        self.btn_worst.setToolTip(
            "Go to the largest thin patch in this layer. A one-millimetre "
            "fault is a few pixels wide on a map of the whole country, so it "
            "has to be gone to rather than looked for.")
        self.grp_thin = self._group(
            "Printability", "Min width (mm)", self.thin_spin, self.btn_thin,
            self.btn_worst)

        # Stress. The same family of question as Printability and deliberately
        # its own group, because the answers disagree and the disagreement is
        # the point: thin land is a fault wherever it is, and a neck is only a
        # fault for what it is holding up.
        self.stress_spin = self._spin(*STRESS_SEVERITY,
            "Draw only necks at or above this severity. Severity is the width "
            "the neck wants over the width it has, so 1.0 is marginal and "
            "anything above it is thinner than its load. Filtering only — "
            "turning this re-draws, it does not re-measure.")
        # The caption rides the box rather than sitting beside it: four groups
        # of label-and-control is more toolbar than a 1920 screen has, and this
        # one says what it is in two characters.
        self.stress_spin.setPrefix("≥ ")
        self.stress_spin.valueChanged.connect(self._on_stress_severity)
        self.btn_stress = self._button("Show stress", self._toggle_stress,
                                       enabled=False)
        self.btn_stress.setCheckable(True)
        self.btn_stress.setToolTip(
            "Mark the narrow land that is holding something up, coloured by how "
            "badly. Finds every neck with its own throat width, weighs what "
            "falls off if it goes, and scores that against what a neck of that "
            "width can carry. Measuring is about two seconds a province, so the "
            "status line counts them off.")
        self.btn_worst_neck = self._button("Worst neck", self.zoom_to_worst_neck,
                                           enabled=False)
        self.btn_worst_neck.setToolTip(
            "Go to the neck most likely to break in this layer. A 0.2 mm throat "
            "is a fraction of a pixel on a map of the whole country, so it has "
            "to be gone to rather than looked for.")
        self.grp_stress = self._group(
            "Stress", self.stress_spin, self.btn_stress, self.btn_worst_neck)

        row = QHBoxLayout()
        row.addWidget(self.grp_layers)
        row.addWidget(self.grp_selection)
        row.addWidget(self.grp_thin)
        row.addWidget(self.grp_stress)
        row.addStretch(1)
        root.addLayout(row)

        root.addWidget(self.caption)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No layers loaded.")
        self._emphasise(self.status)
        root.addWidget(self.status)

        self.setCentralWidget(central)

        for key, (dx, dy) in ((Qt.Key.Key_Left, (-1, 0)),
                              (Qt.Key.Key_Right, (1, 0)),
                              (Qt.Key.Key_Down, (0, -1)),
                              (Qt.Key.Key_Up, (0, 1))):
            for modifier, step in ((0, PAN_STEP),
                                   (Qt.Modifier.SHIFT.value, PAN_STEP_COARSE),
                                   (Qt.Modifier.CTRL.value, PAN_STEP_FINE)):
                self._bind(modifier | key.value,
                           lambda fx=dx * step, fy=dy * step: self._pan_by(fx, fy))

        # Space is the blink key. It sits under the thumb, so one stage can be
        # compared against the one before it as fast as the eye re-registers —
        # which is the only way a change of two printed millimetres in a
        # coastline is ever going to be visible on a map of the whole country.
        self._bind(Qt.Key.Key_Space.value, self.next_layer)
        self._bind(Qt.Modifier.SHIFT.value | Qt.Key.Key_Space.value,
                   self.previous_layer)
        for offset in range(LAYER_KEYS):
            self._bind(Qt.Key.Key_1.value + offset,
                       lambda index=offset: self.show_layer(index))

        self.scale_spin.installEventFilter(self)
        self.perkm_box.installEventFilter(self)
        self._sync_buttons()
        self._update_title()

    def eventFilter(self, obj, event):
        """
        Keep a focused text entry from swallowing the arrow keys' siblings.

        There is no undo stack here, but a spin box still claims Ctrl+Z for its
        own text; dropping the override costs nothing and keeps the two windows
        behaving alike.
        """
        if event.type() == QEvent.Type.ShortcutOverride and (
                event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            return True
        return super().eventFilter(obj, event)

    # -- construction helpers ----------------------------------------------
    def _bind(self, sequence, slot) -> None:
        """A canvas-scoped shortcut, so the map's keys only fire on the map."""
        shortcut = QShortcut(QKeySequence(sequence), self.canvas, activated=slot)
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    @staticmethod
    def _emphasise(label: QLabel) -> None:
        """Make a label carry: larger than the chrome, at full palette contrast."""
        font = label.font()
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(size + STATUS_POINT_BUMP)
        else:
            font.setPixelSize(font.pixelSize() + STATUS_POINT_BUMP * 2)
        label.setFont(font)

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    @staticmethod
    def _group(title: str, *contents) -> QGroupBox:
        box = QGroupBox(title)
        row = QHBoxLayout(box)
        row.setContentsMargins(8, 2, 8, 4)
        row.setSpacing(4)
        for item in contents:
            row.addWidget(QLabel(item) if isinstance(item, str) else item)
        return box

    @staticmethod
    def _spin(lo, hi, val, decimals, step, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setValue(val)
        spin.setToolTip(tip)
        return spin

    def _button(self, text, slot, enabled=True):
        b = QPushButton(text)
        b.clicked.connect(slot)
        b.clicked.connect(lambda *_: self.canvas.setFocus())
        b.setEnabled(enabled)
        return b

    # -- layers -------------------------------------------------------------
    @property
    def layer(self):
        """The layer on show, or None."""
        return self.layers[self.active] if self.layers else None

    @property
    def provinces(self) -> list:
        """The active layer's provinces — what is drawn, clicked and measured."""
        return self.layers[self.active].provinces if self.layers else []

    def show_layer(self, index: int) -> None:
        """
        Put one layer on show, without moving the camera.

        Holding the camera still is the requirement, not a courtesy.  Comparing
        two stages of the pipeline is a blink comparison, and a blink only reads
        if the two frames are registered: re-fitting the view on a swap would
        show every province shift at once and tell you nothing about which ones
        actually moved.
        """
        if not (0 <= index < len(self.layers)) or index == self.active:
            return
        self.active = index
        self._sync_layer_box()
        if self.btn_stress.isChecked():
            # The new layer has not been measured, and measuring it is two
            # seconds a province. Through the toggle rather than letting the
            # draw do it province by province, so the swap counts them off
            # instead of appearing to hang in the middle of a redraw.
            self._toggle_stress(True)
        else:
            self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_title()
        self._update_status()

    def next_layer(self) -> None:
        """Step to the next layer, wrapping — so two layers blink on one key."""
        if len(self.layers) > 1:
            self.show_layer((self.active + 1) % len(self.layers))

    def previous_layer(self) -> None:
        if len(self.layers) > 1:
            self.show_layer((self.active - 1) % len(self.layers))

    def _on_layer_box(self, index: int) -> None:
        self.show_layer(index)
        self.canvas.setFocus()          # Space must go on meaning "next layer"

    def _sync_layer_box(self) -> None:
        """Rebuild the layer list, without it answering back."""
        blocked = self.layer_box.blockSignals(True)
        try:
            self.layer_box.clear()
            for index, layer in enumerate(self.layers):
                mark = f"{index + 1}. " if index < LAYER_KEYS else ""
                self.layer_box.addItem(f"{mark}{layer.name}  ({len(layer)})")
            self.layer_box.setCurrentIndex(self.active if self.layers else -1)
        finally:
            self.layer_box.blockSignals(blocked)

    def remove_layer(self) -> None:
        """Unload the layer on show, keeping the view and the selection."""
        if not self.layers:
            return
        self.layers.pop(self.active)
        if not self.layers:
            self.clear_all()
            return
        self.active = min(self.active, len(self.layers) - 1)
        self._colours = self._colour_map()
        self._sync_layer_box()
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_title()
        self._update_status()

    # -- canvas -------------------------------------------------------------
    def _px_per_metre(self) -> float:
        """Screen pixels per ground metre, from the live data transform."""
        origin = self.ax.transData.transform((0.0, 0.0))
        unit = self.ax.transData.transform((1.0, 0.0))
        return abs(unit[0] - origin[0])

    def _style_axis(self, title: str) -> None:
        self.ax.clear()
        self.caption.setText(title)
        self.ax.set_aspect("equal", adjustable="datalim")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.ax.set_frame_on(False)
        self.ax.margins(0.01)
        self._make_scale_bars()
        self.canvas.draw_idle()

    def _make_scale_bars(self) -> None:
        """Build the empty bar/label artists, one pair per scale."""
        self._scale_artists = []
        self._scale_key = None
        for _ in SCALE_Y:
            line, = self.ax.plot([], [], transform=self.ax.transAxes,
                                 color=SCALE_COLOUR, linewidth=1.3,
                                 solid_capstyle="butt", zorder=5, visible=False)
            label = self.ax.text(0, 0, "", transform=self.ax.transAxes,
                                 color=SCALE_COLOUR, fontsize=SCALE_FONT_PT,
                                 va="center", ha="left", zorder=5, visible=False)
            self._scale_artists.append((line, label))

    def _on_draw(self, _event) -> None:
        """Re-lay the scale bars once a draw has settled the view."""
        if self._update_scales():
            self.canvas.draw_idle()

    def _update_scales(self) -> bool:
        """Lay out both bars for the current view; True if anything moved."""
        if not self._scale_artists:
            return False
        px_per_metre = self._px_per_metre()
        span = (self.ax.get_window_extent().width / px_per_metre
                if px_per_metre > 0 else 0.0)
        live = bool(self.provinces) and span > 0
        denom = self.scale_spin.value()

        key = (live, span, denom)
        if key == self._scale_key:
            return False
        self._scale_key = key

        if not live:
            for artist in (a for pair in self._scale_artists for a in pair):
                artist.set_visible(False)
            return True

        for (line, label), y, (ground, text) in zip(
                self._scale_artists, SCALE_Y, scale_bars(span, denom, SCALE_SPAN)):
            right = SCALE_X + ground / span
            line.set_data([SCALE_X, SCALE_X, SCALE_X, right, right, right],
                          [y - SCALE_CAP, y + SCALE_CAP, y,
                           y, y - SCALE_CAP, y + SCALE_CAP])
            label.set_position((right + SCALE_GAP, y))
            label.set_text(text)
            line.set_visible(True)
            label.set_visible(True)
        return True

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        if event.inaxes is not self.ax or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self.ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        self.ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    def _colour_map(self) -> dict:
        """
        A colour per province *key*, such that no two that touch are given the
        same one — and such that a province keeps its colour across every layer.

        Keeping still is the point.  Colour only the layer on show, and the
        greedy order would come out differently for a stage holding a different
        set of files: Alberta would go blue in one layer and green in the next,
        and blinking between the two would report that every province in Canada
        had changed.  So the colouring is computed once, over all the layers
        together, and looked up by key at draw time.

        This is the map-colouring problem, and the classic answer holds: greedy
        colouring in descending-adjacency order gets real provinces down to
        four.  Cycling the palette by load order instead would sooner or later
        put two yellows either side of a border, which is the one thing a map of
        many provinces must not do — and load order is alphabetical, so it would
        do it on a whim.  Costs about 0.2 s for 27 provinces, paid once per load
        rather than once per draw.

        One geometry per key, from the first layer that has it, rather than the
        union over the layers: the stages of this pipeline differ by fractions
        of a printed millimetre, so any one of them answers "who touches whom"
        the same way, and taking the first keeps this the size of job it was
        before layers existed.
        """
        representative: dict = {}
        for layer in self.layers:
            for province in layer.provinces:
                representative.setdefault(province.key, province.geometry)

        keys = list(representative)
        geometries = [representative[k] for k in keys]
        neighbours = {i: set() for i in range(len(geometries))}
        if len(geometries) > 1:
            tree = STRtree(geometries)
            for i, geometry in enumerate(geometries):
                for j in (int(k) for k in tree.query(geometry)):
                    if i != j and geometries[i].intersects(geometries[j]):
                        neighbours[i].add(j)
                        neighbours[j].add(i)

        chosen: dict = {}
        for index in sorted(neighbours, key=lambda k: -len(neighbours[k])):
            taken = {chosen[n] for n in neighbours[index] if n in chosen}
            free = [c for c in range(len(PROVINCE_COLOURS)) if c not in taken]
            # A province ringed by more neighbours than the palette has colours
            # has to repeat one; better a repeat than a crash.
            chosen[index] = free[0] if free else len(taken) % len(PROVINCE_COLOURS)
        return {keys[i]: PROVINCE_COLOURS[chosen[i]] for i in range(len(keys))}

    def _caption_text(self) -> str:
        """What is on show, said plainly — the one thing a blink has to tell you."""
        if not self.layers:
            return "Add a folder as a layer to begin"
        where = (f"layer {self.active + 1} of {len(self.layers)}"
                 if len(self.layers) > 1 else "the only layer")
        blink = "   —   Space for the next layer" if len(self.layers) > 1 else ""
        return f"{self.layer.name}   ({where}){blink}"

    def _redraw(self, preserve_view: bool = False) -> None:
        if preserve_view:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._style_axis(self._caption_text())
        for province in self.provinces:
            if not province.parts:
                continue
            chosen = province.key in self.selected
            gpd.GeoSeries(province.parts).plot(
                ax=self.ax,
                facecolor=SELECT_COLOUR if chosen
                else self._colours.get(province.key, PROVINCE_COLOURS[0]),
                edgecolor=SELECT_EDGE if chosen else EDGE_COLOUR,
                linewidth=1.0 if chosen else 0.6,
                zorder=3 if chosen else 2)
        self._draw_thin()
        self._draw_stress()
        if preserve_view:
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
        self.canvas.draw_idle()

    def _draw_thin(self) -> None:
        """Mark the land too narrow to print, over the provinces it is in."""
        if not self.btn_thin.isChecked():
            return
        params = self._thin_params()
        for province in self.provinces:
            report = province.thin_report(params)
            if report.thin is None or report.thin.is_empty:
                continue
            # One artist for the whole province's thin land, not one per patch:
            # Nunavut has 7 250 of them and geopandas would make 7 250 artists.
            gpd.GeoSeries([report.thin]).plot(
                ax=self.ax, facecolor=THIN_COLOUR, edgecolor=THIN_COLOUR,
                linewidth=THIN_LINEWIDTH, zorder=4)

    def _draw_stress(self) -> None:
        """Mark each load-bearing neck, in the colour of how badly it is loaded.

        Three marks, not one, and the reason is the zoom this tool is read at.
        The shaded blob is the neck's honest extent, and the honest extent of
        the worst necks is sub-pixel -- Nova Scotia's 0.20 mm throat is a
        fiftieth of a cell -- so on its own it would say nothing is wrong
        precisely where something is.  The bar across the throat and the ring on
        a neck at risk are drawn in screen units, so they survive any zoom, and
        both carry an ink casing because the thin layer may well be switched on
        underneath them in its own red.

        Colour never carries the meaning alone: the ring says *at risk* by being
        there at all, and the status line counts them.
        """
        if not self.btn_stress.isChecked():
            return
        params = self._stress_params()
        floor = self.stress_spin.value()
        for province in self.provinces:
            report = province.stress_report(params)
            for neck in report.necks:
                if neck.severity < floor:
                    continue
                colour = SEVERITY_CMAP(min(neck.severity / STRESS_VMAX, 1.0))
                if neck.region is not None and not neck.region.is_empty:
                    # Outlined, never filled. Show thin paints whole swathes of
                    # coast in its own red -- 327 patches on Nova Scotia alone --
                    # and a filled neck lands in the middle of that as one more
                    # red shape, which is the one reading this layer must not
                    # have: the two are different claims about the same place.
                    gpd.GeoSeries([neck.region]).plot(
                        ax=self.ax, facecolor="none", edgecolor=colour,
                        linewidth=STRESS_LINEWIDTH, zorder=5)
                if neck.throat is not None and not neck.throat.is_empty:
                    xs, ys = zip(*neck.throat.coords)
                    self.ax.plot(xs, ys, color=STRESS_CASING,
                                 linewidth=STRESS_LINEWIDTH + 1.4,
                                 solid_capstyle="round", zorder=6)
                    self.ax.plot(xs, ys, color=colour,
                                 linewidth=STRESS_LINEWIDTH,
                                 solid_capstyle="round", zorder=7)
                if neck.at_risk:
                    self.ax.plot([neck.point.x], [neck.point.y], marker="o",
                                 markersize=STRESS_MARKER_PT,
                                 markerfacecolor="none",
                                 markeredgecolor=STRESS_CASING,
                                 markeredgewidth=3.0, zorder=8)
                    self.ax.plot([neck.point.x], [neck.point.y], marker="o",
                                 markersize=STRESS_MARKER_PT,
                                 markerfacecolor="none", markeredgecolor=colour,
                                 markeredgewidth=1.8, zorder=9)

    # -- pan / selection ----------------------------------------------------
    def _shift_view(self, dx: float, dy: float) -> None:
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self.ax.set_xlim(x0 + dx, x1 + dx)
        self.ax.set_ylim(y0 + dy, y1 + dy)
        self.canvas.draw_idle()

    def _pan_by(self, fx: float, fy: float) -> None:
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self._shift_view((x1 - x0) * fx, (y1 - y0) * fy)

    def _on_press(self, event) -> None:
        if self.nav.mode:
            return
        if event.button != 1 or event.inaxes is not self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return
        self._grab = (event.xdata, event.ydata, event.x, event.y)
        self._dragged = False

    def _on_motion(self, event) -> None:
        if self._grab is None or event.xdata is None or event.ydata is None:
            return
        gx, gy, px, py = self._grab
        if not self._dragged:
            if abs(event.x - px) < PAN_SLOP_PX and abs(event.y - py) < PAN_SLOP_PX:
                return
            self._dragged = True
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._shift_view(gx - event.xdata, gy - event.ydata)

    def _on_release(self, event) -> None:
        if self._grab is None:
            return
        dragged, self._grab, self._dragged = self._dragged, None, False
        if dragged:
            self.canvas.unsetCursor()
            return
        if event.inaxes is not self.ax or not self.provinces:
            return
        if event.xdata is None or event.ydata is None:
            return
        index = self._province_at(event.xdata, event.ydata)
        if index is None:
            return
        self.selected ^= {self.provinces[index].key}
        self._after_selection_change()

    def _province_at(self, x: float, y: float):
        """
        The province under a point, latest-loaded first.

        Provinces can overlap where their coastlines were cleaned apart, so the
        search runs backwards: the one drawn on top is the one clicked.
        """
        point = Point(x, y)
        for index in range(len(self.provinces) - 1, -1, -1):
            if any(part.contains(point) for part in self.provinces[index].parts):
                return index
        return None

    # -- selection ----------------------------------------------------------
    @property
    def visible_keys(self) -> set:
        """The keys the layer on show carries. Selection acts on these."""
        return {p.key for p in self.provinces}

    @property
    def picked(self) -> list:
        """Indices into the active layer whose province is selected."""
        return [i for i, p in enumerate(self.provinces) if p.key in self.selected]

    def select_all(self) -> None:
        self.selected |= self.visible_keys
        self._after_selection_change()

    def clear_selection(self) -> None:
        self.selected = set()
        self._after_selection_change()

    def invert_selection(self) -> None:
        """
        Select exactly the provinces that were not, and vice versa.

        Over what is on show only: a province another layer has and this one
        does not is left as it was, so that a swap and a swap back gives you the
        selection you had rather than the complement of it.
        """
        self.selected ^= self.visible_keys
        self._after_selection_change()

    def _after_selection_change(self) -> None:
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def _sync_buttons(self) -> None:
        has = bool(self.provinces)
        many = len(self.layers) > 1
        self.grp_selection.setEnabled(has)
        self.btn_select_all.setEnabled(has)
        self.btn_clear.setEnabled(bool(self.selected))
        self.btn_invert.setEnabled(has)
        self.btn_real.setEnabled(has)
        self.btn_clear_all.setEnabled(bool(self.layers))
        self.btn_drop_layer.setEnabled(bool(self.layers))
        self.btn_prev.setEnabled(many)
        self.btn_next.setEnabled(many)
        self.grp_thin.setEnabled(has)
        self.btn_thin.setEnabled(has)
        if not has and self.btn_thin.isChecked():
            self.btn_thin.setChecked(False)      # nothing left to measure
        self.btn_worst.setEnabled(has and self.btn_thin.isChecked())
        self.grp_stress.setEnabled(has)
        self.btn_stress.setEnabled(has)
        if not has and self.btn_stress.isChecked():
            self.btn_stress.setChecked(False)
        self.btn_worst_neck.setEnabled(has and self.btn_stress.isChecked())
        self.lbl_count.setText(
            f"{len(self.picked)} of {len(self.provinces)} selected" if has
            else "none loaded")

    # -- printability -------------------------------------------------------
    def _thin_params(self) -> ThinParams:
        """What counts as too thin, as the controls now read it."""
        return ThinParams(scale=self.scale_spin.value(),
                          width_mm=self.thin_spin.value())

    def _toggle_thin(self, checked: bool) -> None:
        """Measure the layer and mark it, or take the marks away."""
        if checked and self.provinces:
            # First look at a width costs a measurement per province; after
            # that they are kept. Half a second on Nunavut, so the cursor says
            # so rather than the window appearing to stall.
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                params = self._thin_params()
                for province in self.provinces:
                    province.thin_report(params)
            finally:
                QApplication.restoreOverrideCursor()
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()
        self.canvas.setFocus()

    def _on_thin_width(self, _value: float) -> None:
        """A new width is a new question; the old marks are not its answer."""
        if not self.btn_thin.isChecked():
            return
        self._toggle_thin(True)

    def thin_reports(self) -> list:
        """``(province, report)`` for the layer on show, measuring as needed."""
        params = self._thin_params()
        return [(province, province.thin_report(params))
                for province in self.provinces]

    def _worst_patch(self):
        """The largest thin patch anywhere in this layer, or None."""
        best = None
        for _, report in self.thin_reports():
            for patch in report.worst:
                if best is None or patch.area > best.area:
                    best = patch
        return best

    def zoom_to_worst(self) -> None:
        """Take the view to the largest thin patch in the layer."""
        if not self.btn_thin.isChecked() or not self.provinces:
            return
        patch = self._worst_patch()
        if patch is None:
            self._update_status("Nothing too thin to go to — ")
            return
        minx, miny, maxx, maxy = patch.bounds
        centre_x, centre_y = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        wanted = max(maxx - minx, maxy - miny, 1.0) / THIN_ZOOM_SHARE
        # One factor on both spans, as Real Size does it and for the same
        # reason: the limits matplotlib holds have already been through
        # apply_aspect, and scaling them together keeps that agreement where
        # fresh limits would fight it.
        factor = wanted / max(x1 - x0, 1e-9)
        half_x, half_y = (x1 - x0) * factor / 2.0, (y1 - y0) * factor / 2.0
        self.ax.set_xlim(centre_x - half_x, centre_x + half_x)
        self.ax.set_ylim(centre_y - half_y, centre_y + half_y)
        self.canvas.draw_idle()
        params = self._thin_params()
        self._update_status(
            f"At the worst patch: {patch.area / params.mpm ** 2:.2f} mm² of "
            f"land under {params.width_mm:g} mm — ")

    def _thin_fragment(self) -> str:
        """The status line's printability segment, or empty when not showing."""
        if not self.btn_thin.isChecked() or not self.provinces:
            return ""
        reports = self.thin_reports()
        width = self.thin_spin.value()
        unmeasured = [name for name, report in
                      ((p.name, r) for p, r in reports) if not report.measured]
        flagged = [p.name for p, report in reports if not report.clean]
        patches = sum(report.patches for _, report in reports)
        area = sum(report.thin_area_mm2 for _, report in reports)
        whole = sum(report.whole_parts for _, report in reports)
        if not flagged and not unmeasured:
            return f"nothing under {width:g} mm"
        bits = []
        if flagged:
            bits.append(f"{patches} thin patch(es) in {len(flagged)} "
                        f"province(s), {area:.1f} mm² under {width:g} mm")
            if whole:
                # A different problem with a different fix, so it is said
                # separately: these want removing, not widening.
                bits.append(f"{whole} island(s) too thin end to end")
        if unmeasured:
            # Never folded into "nothing found": not measured is not clean.
            bits.append(f"{len(unmeasured)} could not be measured")
        return "; ".join(bits)

    # -- stress -------------------------------------------------------------
    def _stress_params(self) -> StressParams:
        """What the necks are measured against, as the controls now read it.

        ``report_above`` is held at zero whatever the spin box says, so that the
        measurement is of the province and not of the filter: Min severity then
        decides what is *drawn* from an answer already in hand.
        """
        return StressParams(scale=self.scale_spin.value(), report_above=0.0)

    def _toggle_stress(self, checked: bool) -> None:
        """Measure the layer and mark it, or take the marks away.

        Fifteen seconds for eight provinces, which is long enough that a frozen
        window would read as a crash, so each province is named as it is reached
        and the event loop is let breathe between them -- the same bargain
        ``app.main_window`` strikes with its progress bar. The guard is because
        letting events through means the button can be pressed again mid-run.
        """
        if self._measuring:
            return
        if checked and self.provinces:
            params = self._stress_params()
            self._measuring = True
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                total = len(self.provinces)
                for number, province in enumerate(self.provinces, start=1):
                    self.status.setText(
                        f"Measuring stress {number} of {total}: {province.name} …")
                    QApplication.processEvents()
                    province.stress_report(params)
            finally:
                QApplication.restoreOverrideCursor()
                self._measuring = False
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()
        self.canvas.setFocus()

    def _on_stress_severity(self, _value: float) -> None:
        """A new floor is a new picture of the same measurement, so just redraw."""
        if not self.btn_stress.isChecked():
            return
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def stress_reports(self) -> list:
        """``(province, report)`` for the layer on show, measuring as needed."""
        params = self._stress_params()
        return [(province, province.stress_report(params))
                for province in self.provinces]

    def shown_necks(self) -> list:
        """``(province, neck)`` for every neck the Min severity floor lets through."""
        floor = self.stress_spin.value()
        return [(province, neck)
                for province, report in self.stress_reports()
                for neck in report.necks if neck.severity >= floor]

    def _worst_neck(self):
        """The neck most likely to break in this layer, or None."""
        shown = self.shown_necks()
        if not shown:
            return None
        return max(shown, key=lambda pair: pair[1].severity)

    def zoom_to_worst_neck(self) -> None:
        """Take the view to the neck most likely to break."""
        if not self.btn_stress.isChecked() or not self.provinces:
            return
        found = self._worst_neck()
        if found is None:
            self._update_status("No neck above that severity to go to — ")
            return
        province, neck = found
        wanted = STRESS_ZOOM_MM * self._stress_params().mpm
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        # One factor on both spans, as Zoom to Worst and Real Size do it: the
        # limits matplotlib holds have already been through apply_aspect, and
        # scaling them together keeps that agreement where fresh limits fight it.
        factor = wanted / max(x1 - x0, 1e-9)
        half_x, half_y = (x1 - x0) * factor / 2.0, (y1 - y0) * factor / 2.0
        self.ax.set_xlim(neck.point.x - half_x, neck.point.x + half_x)
        self.ax.set_ylim(neck.point.y - half_y, neck.point.y + half_y)
        self.canvas.draw_idle()
        self._update_status(f"At the worst neck, in {province.name}: {neck} — ")

    def _stress_fragment(self) -> str:
        """The status line's stress segment, or empty when not showing."""
        if not self.btn_stress.isChecked() or not self.provinces:
            return ""
        reports = self.stress_reports()
        unmeasured = [p.name for p, report in reports if not report.measured]
        shown = self.shown_necks()
        floor = self.stress_spin.value()
        bits = []
        if shown:
            risky = [neck for _, neck in shown if neck.at_risk]
            provinces = {province.name for province, _ in shown}
            worst_province, worst = max(shown, key=lambda pair: pair[1].severity)
            bits.append(f"{len(shown)} neck(s) in {len(provinces)} province(s)"
                        + (f", {len(risky)} at risk" if risky else ""))
            bits.append(f"worst {worst.throat_mm:.2f} mm holding "
                        f"{worst.load_mm2:.0f} mm² in {worst_province.name} "
                        f"(wants {worst.needed_mm:.2f} mm)")
        else:
            bits.append(f"no neck at severity {floor:g} or worse")
        if unmeasured:
            # Never folded into "nothing found": not measured is not sound.
            bits.append(f"{len(unmeasured)} could not be measured")
        return "; ".join(bits)

    # -- printed size / real size -------------------------------------------
    def _perkm_text(self) -> str:
        return f"{km_per_mm(self.scale_spin.value()):g} km/mm"

    def _on_print_scale(self, _denom: int) -> None:
        self.perkm_box.setText(self._perkm_text())
        if self.btn_thin.isChecked():
            # The width is printed millimetres, so a new reduction is a new
            # question about the same land: re-measure rather than leave the
            # old marks sitting under a scale that no longer produced them.
            self._toggle_thin(True)
        if self.btn_stress.isChecked():
            # Same again, and more so: a neck's throat, its load and what that
            # load is allowed to weigh are all printed quantities, so every
            # severity on screen belongs to the old reduction.
            self._toggle_stress(True)
        self.canvas.draw_idle()
        self._update_status("Print scale changed — ")

    def _extent(self):
        """``(minx, miny, maxx, maxy)`` over the layer on show, or None."""
        if not self.provinces:
            return None
        return unary_union([p.geometry for p in self.provinces]).bounds

    def _printed_extent_text(self) -> str:
        """How big everything loaded comes out on paper — the tool's whole point."""
        bounds = self._extent()
        if bounds is None:
            return ""
        minx, miny, maxx, maxy = bounds
        wide, tall = printed_size_mm(maxx - minx, maxy - miny,
                                     self.scale_spin.value())
        return f"prints {wide:,.0f} x {tall:,.0f} mm"

    def _screen_calibration(self):
        """``(pixels per screen mm, monitor)``, or ``(None, monitor)`` if unmeasured."""
        centre = self.frameGeometry().center()
        screen = self.screen() or QApplication.primaryScreen()
        size = ((screen.geometry().width(), screen.geometry().height())
                if screen is not None else None)
        monitor = monitor_at(centre.x(), centre.y(), size)
        ppi = load_saved_ppi(monitor)
        return (px_per_mm(ppi) if ppi else None), monitor

    def zoom_to_real_size(self) -> None:
        """
        Put the map on the glass at the size it will print.

        Scales the *current* view by one factor rather than deriving limits from
        the axes box: the limits matplotlib holds have already been through
        ``apply_aspect`` (the axes keep ``adjustable="datalim"``), and one factor
        on both spans keeps that agreement where fresh limits could fight it.
        """
        screen_px_per_mm, monitor = self._screen_calibration()
        if screen_px_per_mm is None:
            self._update_status(
                f"No screen measurement for {monitor.label} — run "
                "ScreenRuler.py to measure it, then Real Size can work — ")
            return
        wanted = screen_px_per_mm / to_km(1.0, self.scale_spin.value()) / 1000.0
        current = self._px_per_metre()
        if current <= 0 or wanted <= 0:
            return
        factor = current / wanted
        for get, set_ in ((self.ax.get_xlim, self.ax.set_xlim),
                          (self.ax.get_ylim, self.ax.set_ylim)):
            low, high = get()
            middle, half = (low + high) / 2, (high - low) * factor / 2
            set_(middle - half, middle + half)
        self.canvas.draw_idle()
        self._update_status(
            f"Real size at 1:{grouped(self.scale_spin.value())} — measured at "
            f"{screen_px_per_mm * MM_PER_INCH:.1f} px/in on {monitor.label} — ")

    # -- load ---------------------------------------------------------------
    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import province GeoJSON (any number)",
            _dir_or_cwd(DEFAULT_LOAD_DIR), GEOJSON_FILTER)
        self._load(paths)

    def add_layer(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Add a folder as a layer", _dir_or_cwd(DEFAULT_LOAD_DIR))
        if not folder:
            return
        found = geojson_in(folder)
        if not found:
            QMessageBox.information(self, "Nothing to import",
                                    "No .geojson or .json files in that folder.")
            return
        self._load_layer(Path(folder), found)

    def _layer_name(self, folder: Path) -> str:
        """
        The folder's own name, qualified only as far as it has to be.

        Two stages of a pipeline are easily ``.../v1/provinces`` and
        ``.../v2/provinces``; calling both of those "provinces" would leave the
        blink with no way of saying which one you are looking at.
        """
        name = folder.name or str(folder)
        if any(layer.name == name for layer in self.layers):
            name = f"{folder.parent.name}/{name}"
        return name

    def _load_layer(self, folder: Path, paths) -> None:
        """
        A folder as one layer: one stage of the pipeline, ready to blink against.

        The camera moves for the first layer and never again.  Fitting the view
        to each new folder would undo the registration the whole comparison
        rests on — you would add the stage you meant to compare and lose the
        framing you meant to compare it in.
        """
        suffix = stage_suffix([p.stem for p in paths])
        provinces, failed = [], []
        for path in paths:
            key = path.stem[:-len(suffix)] if suffix else path.stem
            try:
                province = self._read(path, key)
            except Exception as exc:
                failed.append(f"{path.name}: {exc}")
                continue
            if province is not None:
                provinces.append(province)

        first = not self.layers
        self.layers.append(Layer(self._layer_name(folder), folder, provinces))
        self.active = len(self.layers) - 1
        self._colours = self._colour_map()
        self._sync_layer_box()
        self._redraw(preserve_view=not first)
        self._sync_buttons()
        self._update_title()

        note = f"Layer '{self.layer.name}': {len(provinces)} province(s)"
        if suffix:
            note += f", matched across layers by dropping '{suffix}'"
        if failed:
            note += f"; {len(failed)} failed ({failed[0]})"
        self._update_status(f"{note} — ")

    def _load(self, paths) -> None:
        """
        Add every readable file to the layer on show, reporting the ones that
        were not.

        One bad file among forty must not cost the other thirty-nine: this is a
        bulk tool, so a failure is a line in the status rather than a dialog that
        stops the run.
        """
        if not paths:
            return
        first = not self.layers
        if first:
            self.layers.append(Layer(LOOSE_LAYER, None, []))
            self.active = 0

        loaded, failed = [], []
        known = {p.path for p in self.provinces}
        for raw in paths:
            path = Path(raw)
            if path in known:
                continue                   # already on the map
            try:
                province = self._read(path)
            except Exception as exc:
                failed.append(f"{path.name}: {exc}")
                continue
            if province is not None:
                loaded.append(province)

        self.layer.provinces.extend(loaded)
        self._colours = self._colour_map()
        self._sync_layer_box()
        self._redraw(preserve_view=not first)
        self._sync_buttons()
        self._update_title()

        note = f"Loaded {len(loaded)} file(s)"
        if failed:
            note += f"; {len(failed)} failed ({failed[0]})"
        self._update_status(f"{note} — ")

    def _read(self, path: Path, key: str = ""):
        """One file as a Province in the working CRS, or None if it has no land."""
        gdf = gpd.read_file(path)
        if gdf.empty:
            return None
        work = gdf.to_crs(EPSG_METRIC) if gdf.crs is not None else gdf
        geometry = unary_union(list(work.geometry))
        if geometry.is_empty or not _polygons(geometry):
            return None
        return Province(path, geometry, key)

    def clear_all(self) -> None:
        self.layers = []
        self.active = 0
        self.selected = set()
        self._colours = {}
        self._sync_layer_box()
        self._style_axis(self._caption_text())
        self._sync_buttons()
        self._update_title()
        self._update_status()

    # -- status / title -----------------------------------------------------
    def _update_status(self, prefix: str = "") -> None:
        if not self.provinces:
            self.status.setText(f"{prefix}No provinces loaded.")
            return
        bits = [f"{self.layer.name}: {len(self.provinces)} province(s)"]
        if len(self.layers) > 1:
            bits.append(f"layer {self.active + 1} of {len(self.layers)}")
        picked = self.picked
        if picked:
            names = [self.provinces[i].name for i in picked]
            shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
            area = sum(self.provinces[i].area_km2 for i in picked)
            bits.append(f"{len(names)} selected ({shown}; {area:,.0f} km²)")
        bits.append(self._printed_extent_text())
        bits.append(f"at 1:{grouped(self.scale_spin.value())}")
        bits.append(self._thin_fragment())
        bits.append(self._stress_fragment())
        self.status.setText(f"{prefix}{', '.join(b for b in bits if b)}.")

    def _update_title(self) -> None:
        if not self.layers:
            self.setWindowTitle("Province Viewer — (nothing loaded)")
            return
        where = (f" ({self.active + 1} of {len(self.layers)})"
                 if len(self.layers) > 1 else "")
        self.setWindowTitle(
            f"Province Viewer — {self.layer.name}{where} — "
            f"{len(self.provinces)} province(s)")


def main() -> int:
    app = QApplication(sys.argv)
    window = ProvinceViewer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
