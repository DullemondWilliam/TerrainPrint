"""
UI_province_viewer.py

Every province at once, at the size it will print.

Where ``UI_island_editor`` opens one province and changes it, this opens *many*
and changes nothing.  The question it answers is the one you cannot ask a
single-file tool: laid out together at 1:8 000 000, how big is the whole thing,
do the pieces line up, and will it fit on the paper you have.

Workflow
--------
1. **Import GeoJSON …** — pick any number of files at once, or **Import Folder …**
   to take a whole directory. Each file becomes one province, drawn in its own
   colour at its true position (worked in EPSG:3347 metres, so the provinces sit
   together exactly as the ground does).
2. **Look** — left-drag pans, the arrow keys pan in steps (Shift for a long
   stride, Ctrl for a nudge), the wheel zooms, and a left click with no drag
   toggles a province's selection. **Selection** has All / None / Invert, as in
   the island editor.
3. **Print scale** — the same 1:N control, km/mm readout and **Real Size** button
   the editor has, over the same two scale bars. The status line adds the one
   figure this tool exists for: how many millimetres across the loaded provinces
   come out at the current reduction.

Nothing here writes. There is no undo because there is nothing to undo; to
change a province, open it in ``UI_island_editor``.

Run with:  python UI_province_viewer.py

Reuses ``lib.LIB_map_scale``, ``lib.LIB_project_config`` and
``lib.LIB_screen_calibration``; kept independent of the main ``app`` window like
the other UI_* harnesses.
"""

import logging
import sys
from pathlib import Path

import geopandas as gpd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from lib.LIB_island_merging import _polygons
from lib.LIB_map_scale import (grouped, km_per_mm, printed_size_mm, scale_bars,
                               to_km)
from lib.LIB_project_config import get as cfg
from lib.LIB_screen_calibration import (MM_PER_INCH, load_saved_ppi,
                                        monitor_at, px_per_mm)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
EPSG_METRIC = cfg("crs.working")

_ROOT = Path(__file__).resolve().parent
DEFAULT_LOAD_DIR = _ROOT / cfg("paths.provinces_out")

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


class MapToolbar(NavigationToolbar):
    """Matplotlib's navigation toolbar, trimmed as in the island editor."""

    toolitems = [(text, _NAV_TIPS.get(text, tip), image, callback)
                 for text, tip, image, callback in NavigationToolbar.toolitems
                 if text in _NAV_KEEP]


class Province:
    """One loaded file: its name, its geometry, and the parts drawn from it."""

    def __init__(self, path: Path, geometry):
        self.path = path
        self.name = path.stem
        self.geometry = geometry
        self.parts = _polygons(geometry)

    @property
    def area_km2(self) -> float:
        return self.geometry.area / 1e6


class ProvinceViewer(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1280, 800)

        # -- state ----------------------------------------------------------
        self.provinces: list[Province] = []
        self.selected: list[int] = []       # indices into ``provinces``
        self._colours: list = []            # one per province, no two neighbours alike
        self._grab = None
        self._dragged = False
        self._scale_artists: list = []
        self._scale_key = None

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
        self.caption.setStyleSheet("color:#555;")
        self.figure = Figure(figsize=(11, 7))
        self.figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._style_axis("Import province GeoJSON files to begin")
        self.nav = MapToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("draw_event", self._on_draw)

        # -- row 1: the documents and the view -------------------------------
        top = QHBoxLayout()
        self.btn_import = self._button("Import GeoJSON …", self.import_files)
        self.btn_folder = self._button("Import Folder …", self.import_folder)
        self.btn_clear_all = self._button("Clear", self.clear_all, enabled=False)
        self.btn_clear_all.setToolTip("Unload every province.")
        for widget in (self.btn_import, self.btn_folder, self.btn_clear_all,
                       self._separator(), self.nav, self._separator(),
                       QLabel("Print scale"), self.scale_spin, self.perkm_box,
                       self.btn_real):
            top.addWidget(widget)
        top.addStretch(1)
        root.addLayout(top)

        # -- row 2: selection ------------------------------------------------
        # The only group there is: this tool looks, it does not act, so there is
        # nothing for a selection to be the subject *of* beyond reading it off.
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

        row = QHBoxLayout()
        row.addWidget(self.grp_selection)
        row.addStretch(1)
        root.addLayout(row)

        root.addWidget(self.caption)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No provinces loaded.")
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
                self._bind_pan(modifier | key.value, dx * step, dy * step)

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
    def _bind_pan(self, sequence, fx: float, fy: float) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self.canvas,
                             activated=lambda: self._pan_by(fx, fy))
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

    def _button(self, text, slot, enabled=True):
        b = QPushButton(text)
        b.clicked.connect(slot)
        b.clicked.connect(lambda *_: self.canvas.setFocus())
        b.setEnabled(enabled)
        return b

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

    def _colour_map(self) -> list:
        """
        A colour per province, such that no two that touch are given the same one.

        This is the map-colouring problem, and the classic answer holds: greedy
        colouring in descending-adjacency order gets real provinces down to four.
        Cycling the palette by load order instead would sooner or later put two
        yellows either side of a border, which is the one thing a map of many
        provinces must not do — and load order is alphabetical, so it would do it
        on a whim.  Costs about 0.2 s for 27 provinces, paid once per load rather
        than once per draw.
        """
        geometries = [p.geometry for p in self.provinces]
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
        return [PROVINCE_COLOURS[chosen[i]] for i in range(len(geometries))]

    def _redraw(self, preserve_view: bool = False) -> None:
        if preserve_view:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._style_axis(
            "Provinces at scale  (click to select, drag to pan, wheel to zoom)")
        picked = set(self.selected)
        for index, province in enumerate(self.provinces):
            if not province.parts:
                continue
            chosen = index in picked
            gpd.GeoSeries(province.parts).plot(
                ax=self.ax,
                facecolor=SELECT_COLOUR if chosen else self._colours[index],
                edgecolor=SELECT_EDGE if chosen else EDGE_COLOUR,
                linewidth=1.0 if chosen else 0.6,
                zorder=3 if chosen else 2)
        if preserve_view:
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
        self.canvas.draw_idle()

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
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.append(index)
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

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
    def select_all(self) -> None:
        self.selected = list(range(len(self.provinces)))
        self._after_selection_change()

    def clear_selection(self) -> None:
        self.selected = []
        self._after_selection_change()

    def invert_selection(self) -> None:
        """Select exactly the provinces that were not, and vice versa."""
        picked = set(self.selected)
        self.selected = [i for i in range(len(self.provinces)) if i not in picked]
        self._after_selection_change()

    def _after_selection_change(self) -> None:
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def _sync_buttons(self) -> None:
        has = bool(self.provinces)
        picked = len(self.selected)
        self.grp_selection.setEnabled(has)
        self.btn_select_all.setEnabled(has)
        self.btn_clear.setEnabled(bool(self.selected))
        self.btn_invert.setEnabled(has)
        self.btn_real.setEnabled(has)
        self.btn_clear_all.setEnabled(has)
        self.lbl_count.setText(
            f"{picked} of {len(self.provinces)} selected" if has
            else "none loaded")

    # -- printed size / real size -------------------------------------------
    def _perkm_text(self) -> str:
        return f"{km_per_mm(self.scale_spin.value()):g} km/mm"

    def _on_print_scale(self, _denom: int) -> None:
        self.perkm_box.setText(self._perkm_text())
        self.canvas.draw_idle()
        self._update_status("Print scale changed — ")

    def _extent(self):
        """``(minx, miny, maxx, maxy)`` over every loaded province, or None."""
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

    def import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Import every GeoJSON in a folder", _dir_or_cwd(DEFAULT_LOAD_DIR))
        if not folder:
            return
        found = sorted(p for pattern in ("*.geojson", "*.json")
                       for p in Path(folder).glob(pattern))
        if not found:
            QMessageBox.information(self, "Nothing to import",
                                    "No .geojson or .json files in that folder.")
            return
        self._load(found)

    def _load(self, paths) -> None:
        """
        Add every readable file, reporting the ones that were not.

        One bad file among forty must not cost the other thirty-nine: this is a
        bulk tool, so a failure is a line in the status rather than a dialog that
        stops the run.
        """
        if not paths:
            return
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

        self.provinces.extend(loaded)
        self.selected = []
        self._colours = self._colour_map()
        self._redraw(preserve_view=False)
        self._sync_buttons()
        self._update_title()

        note = f"Loaded {len(loaded)} file(s)"
        if failed:
            note += f"; {len(failed)} failed ({failed[0]})"
        self._update_status(f"{note} — ")

    def _read(self, path: Path):
        """One file as a Province in the working CRS, or None if it has no land."""
        gdf = gpd.read_file(path)
        if gdf.empty:
            return None
        work = gdf.to_crs(EPSG_METRIC) if gdf.crs is not None else gdf
        geometry = unary_union(list(work.geometry))
        if geometry.is_empty or not _polygons(geometry):
            return None
        return Province(path, geometry)

    def clear_all(self) -> None:
        self.provinces = []
        self.selected = []
        self._colours = []
        self._style_axis("Import province GeoJSON files to begin")
        self._sync_buttons()
        self._update_title()
        self._update_status()

    # -- status / title -----------------------------------------------------
    def _update_status(self, prefix: str = "") -> None:
        if not self.provinces:
            self.status.setText(f"{prefix}No provinces loaded.")
            return
        bits = [f"{len(self.provinces)} province(s)"]
        if self.selected:
            names = [self.provinces[i].name for i in sorted(self.selected)]
            shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
            area = sum(self.provinces[i].area_km2 for i in self.selected)
            bits.append(f"{len(names)} selected ({shown}; {area:,.0f} km²)")
        bits.append(self._printed_extent_text())
        bits.append(f"at 1:{grouped(self.scale_spin.value())}")
        self.status.setText(f"{prefix}{', '.join(b for b in bits if b)}.")

    def _update_title(self) -> None:
        count = len(self.provinces)
        self.setWindowTitle(
            f"Province Viewer — {count} province(s)" if count
            else "Province Viewer — (nothing loaded)")


def main() -> int:
    app = QApplication(sys.argv)
    window = ProvinceViewer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
