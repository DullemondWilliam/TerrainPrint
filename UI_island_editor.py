"""
UI_island_editor.py

A combined visual editor for province island cleanup, folding the three
single-purpose harnesses (``UI_island_merge``, ``UI_island_removal``,
``UI_lake_removal``) into one tool with **undo/redo**.

Workflow
--------
1. **Import GeoJSON …** — load one province file; every polygon part becomes a
   selectable island (worked in EPSG:3347 metres so km / km² are ground units).
2. **Edit** — on a single live canvas (left-drag pans, the arrow keys pan in
   steps — Shift for a long stride, Ctrl for a nudge — and the wheel zooms at
   all times; a left click with no drag toggles an island's selection):
   - **Merge Selected** — bridge two or more clicked islands with necks of land;
     **From Gap** first sets the buffer to the least that bridges them.
   - **Draw Bridge** — paint a land bridge by hand at a chosen brush width, for
     the gaps no workable buffer closes (left-drag draws while the mode is on;
     Esc cancels a stroke, or leaves the mode).
   - **Erase** — the same brush in reverse: rub land out to trim a coastline or
     cut a landmass in two, where removal by island or by area cannot reach.
   - **Remove Selected** — drop the clicked islands.
   - **Remove Small** — drop every island below the *Min area* threshold.
   - **Remove Lakes** — strip interior rings (lakes) from the whole province.
3. **Undo / Redo** (Ctrl+Z / Ctrl+Shift+Z) — every edit is one step on a stack.
4. **Save GeoJSON …** — write the province back out, CRS preserved.

Everything is sized **as it will print**. An editable **Print scale**
(1:8 000 000 to start) sits in the top row with a read-only **km/mm** readout
beside it, and the three editing parameters — *Buffer*, *Min area*, *Brush* —
are printed millimetres, converted to ground units through that scale at the
moment they are used. The constraint they express belongs to the paper (the
finest neck a cutter will hold, the smallest island worth printing), so changing
the scale leaves the numbers alone and changes what they are worth. The map
carries two scale bars to match, bottom left: one in km of ground, one in mm of
paper.

The document is a single geometry; each operation is a ``geometry -> geometry``
transform recorded as a snapshot command, and the selection is remapped onto the
result so a merge leaves the merged landmass selected.

Run with:  python UI_island_editor.py

Reuses ``lib.LIB_island_merging``, ``lib.LIB_island_bridging``,
``lib.LIB_island_erasing``, ``lib.LIB_island_removal`` and
``lib.LIB_lake_removal``; kept independent of the main ``app`` window like the
other UI_* harnesses.
"""

import logging
import math
import sys
from pathlib import Path

import geopandas as gpd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.geometry import MultiPolygon, Point
from shapely.ops import unary_union

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from lib.LIB_island_bridging import draw_bridge
from lib.LIB_island_erasing import erase_stroke
from lib.LIB_island_merging import merge_islands, _polygons
from lib.LIB_island_metrics import (bridging_buffer_km, closest_pair,
                                    gap_range_km, landmass_count)
from lib.LIB_island_removal import is_below_area, remove_small_islands
from lib.LIB_lake_removal import remove_lakes

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
#: Project print CRS (Statistics Canada Lambert, metres). Islands are worked in
#: metres so the km buffer / km² threshold are real ground units, then
#: reprojected back to the source CRS on save.
EPSG_METRIC = "EPSG:3347"

#: Where the file dialogs start, resolved next to this script (raw provinces
#: in, island-edited outputs out).
_ROOT = Path(__file__).resolve().parent
DEFAULT_LOAD_DIR = _ROOT / "Data" / "JSon-Provinces-Orig"
DEFAULT_SAVE_DIR = _ROOT / "Data" / "JSonProvinces-Cleaned"

ISLAND_COLOUR = "#AED6F1"     # ordinary islands
SELECT_COLOUR = "#F5B041"     # clicked / selected islands
REMOVE_COLOUR = "#E6787A"     # land on its way out: below the area threshold,
                              # or under the eraser's stroke
BRIDGE_COLOUR = "#7D6608"     # a brush stroke being drawn
EDGE_COLOUR = "#1B4F72"
SELECT_EDGE = "#B9770E"
REMOVE_EDGE = "#943126"
SCALE_COLOUR = "#2C3E50"      # the map's scale bars, darker than any island

#: The map carries two scale bars: what a distance measures on the ground, and
#: what it comes out as on paper once the province is printed at the reduction
#: below. 1:8 000 000 fits a whole province on a desk-sized sheet, which is the
#: project's working figure; the toolbar's spin box moves it.
DEFAULT_PRINT_SCALE = 8_000_000
PRINT_SCALE_MIN, PRINT_SCALE_MAX = 1_000, 500_000_000
PRINT_SCALE_STEP = 500_000

#: Millimetres in a kilometre, which is the whole of the km/mm readout: a 1:N map
#: puts N mm of ground under every mm of paper, so N / this is the ground
#: kilometres one printed millimetre covers.
MM_PER_KM = 1_000_000

#: The editing parameters, as ``(lo, hi, default, decimals, step)`` for ``_spin``.
#:
#: They are stated as **printed** sizes and converted to ground units through the
#: print scale at the moment of use, because that is what the constraint actually
#: is: the finest neck a cutter will hold, the smallest island worth a piece of
#: paper.  A constraint of that kind does not move when the scale does — the
#: ground distance standing behind it is what moves — so the scale never rewrites
#: these boxes, it only changes what they are worth.
#:
#: The defaults are what the tool shipped with in km, converted at 1:8 000 000:
#: a 0.25 mm buffer is 2 km there and a 0.125 mm brush is 1 km.  Three decimals
#: resolve 0.001 mm, which at that scale is 8 m — finer than the 10 m the old km
#: boxes could state; area gets a fourth because a squared unit needs it.
BUFFER_MM = (0.0, 50.0, 0.25, 3, 0.05)
BRUSH_MM = (0.001, 25.0, 0.125, 3, 0.05)
AREA_MM2 = (0.0, 10_000.0, 0.02, 4, 0.01)

#: Scale-bar geometry, in fractions of the axes. The bars sit in the bottom-left
#: corner — the km bar above the mm one — and each aims to span ``SCALE_SPAN`` of
#: the view before being rounded down to a whole number of its own unit, which
#: leaves it somewhere between half that and all of it.
SCALE_X = 0.02
SCALE_Y = (0.105, 0.045)
SCALE_SPAN = 0.22
SCALE_CAP = 0.012             # half-height of a bar's end ticks
SCALE_GAP = 0.008             # bar to label
SCALE_FONT_PT = 8

#: The two freehand brush modes, and the value meaning neither. A stroke is
#: always either laying land down or lifting it off, so the buttons are a radio
#: pair in all but name: entering one mode leaves the other.
DRAW, ERASE = "draw", "erase"

#: How far the mouse may travel between press and release and still count as a
#: click rather than a pan — a few pixels of hand tremor must not eat a
#: selection, and no real drag is this short.
PAN_SLOP_PX = 4

#: How many points larger than the interface default the status line runs. It
#: carries the landmass counts and the result of every operation, and is read
#: mid-edit from across the desk, so it is the one label worth setting above the
#: size of the surrounding chrome.
STATUS_POINT_BUMP = 2

#: How far one arrow-key press pans, as a fraction of the visible span. A
#: fraction rather than a ground distance keeps the step honest at every zoom —
#: it moves the same amount of *screen* whether the view holds a whole province
#: or a single strait. Shift crosses the view in two presses; Ctrl nudges.
PAN_STEP = 0.10
PAN_STEP_COARSE = 0.50
PAN_STEP_FINE = 0.02

#: Above this many selected landmasses the gap is left unmeasured. Comparing
#: every pair is quadratic — 50 selected is ~1 200 pairs and about 55 ms, but
#: the 223 landmasses of Nunavut are ~25 000 pairs and over two seconds, and
#: stalling the status line on ‘Select All’ would be worse than staying quiet.
GAP_MAX_SELECTION = 50

# The canvas keeps ``aspect="equal", adjustable="datalim"`` so the map always
# fills the widget: matplotlib widens the view rather than shrinking the axes
# box. It logs a warning each time it does that, which here is the intended
# behaviour on every zoom / resize, so drop just that message.
logging.getLogger("matplotlib.axes._base").addFilter(
    lambda record: "to fulfill fixed data aspect" not in record.getMessage())


def _dir_or_cwd(path: Path) -> str:
    """Directory as a string if it exists, else fall back to the cwd."""
    return str(path if path.is_dir() else Path.cwd())


def _as_multi(geom):
    """Normalise any polygonal geometry to a MultiPolygon (None if empty)."""
    parts = _polygons(geom)
    return MultiPolygon(parts) if parts else None


def _nice(value: float) -> float:
    """
    The largest 1 / 2 / 2.5 / 5 x 10ⁿ at or below ``value``.

    A scale bar has to be read off, not measured, so its length is a round
    number of its own unit and the *bar* takes whatever length that works out
    to — never the other way round.  Rounding down keeps the bar inside the
    span it was budgeted; the sequence's widest step is 2x, so it never falls
    below half of it either.
    """
    if value <= 0:
        return 0.0
    power = 10.0 ** math.floor(math.log10(value))
    for step in (5.0, 2.5, 2.0):
        if value >= step * power:
            return step * power
    return power


def _grouped(value: int) -> str:
    """A large integer with space-separated thousands (8000000 -> '8 000 000')."""
    return f"{value:,}".replace(",", " ")


def _differs(a, b) -> bool:
    """Whether two geometries differ by value (cheap identity check first)."""
    if a is b:
        return False
    try:
        return a.wkb != b.wkb
    except Exception:
        return True


#: The navigation tools worth keeping, and the tooltips worth rewording. Module
#: level because a class-body comprehension cannot see its own class attributes.
_NAV_KEEP = ("Home", "Back", "Forward", "Pan", "Zoom")
_NAV_TIPS = {"Back": "Back to the previous view (this is not Undo)",
             "Forward": "Forward to the next view (this is not Redo)"}


class MapToolbar(NavigationToolbar):
    """
    Matplotlib's navigation toolbar, minus the tools that fight this window.

    ``Subplots`` and ``Customize`` edit the axes margins and aspect that the
    editor sets deliberately — they can undo the edge-to-edge map that
    ``subplots_adjust`` and ``_style_axis`` exist to produce.  ``Save`` writes a
    PNG of the figure, which sits one button away from **Save GeoJSON …** and
    does something entirely different with the same word.

    ``Back``/``Forward`` step the *view* history, not the edit history; their
    tooltips say so, because two arrows next to Undo/Redo invite the reading.
    """

    toolitems = [(text, _NAV_TIPS.get(text, tip), image, callback)
                 for text, tip, image, callback in NavigationToolbar.toolitems
                 if text in _NAV_KEEP]


class EditCommand(QUndoCommand):
    """
    One undoable edit: swap the editor's document (and selection) between the
    geometry snapshots captured before and after the operation.

    Snapshot-based like ``app.commands`` — the lib operations always return new
    geometry, so holding references is cheap and exact and undo never recomputes.
    """

    def __init__(self, editor, text, before_doc, after_doc, before_sel, after_sel):
        super().__init__(text)
        self._e = editor
        self._before_doc = before_doc
        self._after_doc = after_doc
        self._before_sel = list(before_sel)
        self._after_sel = list(after_sel)

    # QUndoStack.push() calls redo() immediately, which applies the result.
    def redo(self) -> None:
        self._e._set_state(self._after_doc, self._after_sel)

    def undo(self) -> None:
        self._e._set_state(self._before_doc, self._before_sel)


class IslandEditor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1180, 720)

        # -- state ----------------------------------------------------------
        self.doc = None                    # the province, one MultiPolygon (EPSG:3347)
        self.parts: list = []              # the document's polygon parts (islands)
        self.selected: list[int] = []      # indices into ``parts``
        self.source_path: Path | None = None
        self.source_crs = None             # CRS to save back into
        self.source_attrs: dict = {}       # non-geometry fields of the source feature
        self.undo_stack = QUndoStack(self)
        self._grab = None                  # anchor of an in-progress left-drag
        self._dragged = False              # whether that drag has passed PAN_SLOP_PX
        self._brush_mode = None            # DRAW, ERASE, or None (neither)
        self._stroke = None                # points of an in-progress brush stroke
        self._preview = None               # the artist previewing that stroke
        self._gap_doc = None               # document the cached gap was measured on
        self._gap_sel: tuple = ()          # selection it was measured for
        self._gap_text = ""                # its status-line fragment
        self._scale_artists: list = []     # (bar, label) pairs, one per scale
        self._scale_key = None             # what those were last laid out for

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 4)   # give the canvas the spare pixels
        root.setSpacing(4)

        # The print scale belongs in the top row with the other view controls,
        # but it is built here, ahead of the canvas: the map's scale bars read
        # it on every draw, and the first draw is scheduled from ``_style_axis``
        # below.
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(PRINT_SCALE_MIN, PRINT_SCALE_MAX)
        self.scale_spin.setSingleStep(PRINT_SCALE_STEP)
        self.scale_spin.setValue(DEFAULT_PRINT_SCALE)
        self.scale_spin.setGroupSeparatorShown(True)
        self.scale_spin.setPrefix("1 : ")
        self.scale_spin.setToolTip(
            "Reduction the province is printed at, as 1:N. It sets the map's "
            "millimetre scale bar — what a ground distance measures on paper — "
            "and changes nothing about the geometry.")
        # The ratio says everything but says it in the units of neither end of
        # the work — this is the same figure in the ones actually reached for
        # while editing, where the brush and the gaps are already in km.
        self.perkm_box = QLineEdit(self._perkm_text())
        self.perkm_box.setReadOnly(True)
        self.perkm_box.setToolTip(
            "Ground kilometres under one printed millimetre, at the scale on "
            "the left. Read-only — set the scale to change it.")
        # Sized from the font rather than a pixel count, so it still fits the
        # widest reading this box can hold on whatever theme is in force.
        self.perkm_box.setFixedWidth(
            self.perkm_box.fontMetrics().horizontalAdvance("000.00 km/mm") + 16)
        self.scale_spin.valueChanged.connect(self._on_print_scale)

        # -- single live canvas ---------------------------------------------
        # Built before the toolbars because the view tools belong to the canvas
        # and ride in the top row with the other document-level controls.
        # The caption lives in a label rather than an axes title so the axes can
        # take the whole figure; the map then fills the window edge to edge.
        self.caption = QLabel()
        self.caption.setStyleSheet("color:#555;")
        self.figure = Figure(figsize=(10, 6))
        self.figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Matplotlib only gives the canvas a focus policy in its own managed
        # (pyplot) window; embedded like this it keeps Qt's default NoFocus, so
        # clicking the map would move no keyboard focus at all and leave it
        # parked wherever it last was — a spin box, typically, which then eats
        # the undo/redo shortcuts wired below.
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._style_axis("Import a province GeoJSON to begin")
        self.nav = MapToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("draw_event", self._on_draw)

        # -- row 1: the document and the view --------------------------------
        # Nothing here changes geometry: it opens, saves, steps the history,
        # moves the camera, or says how the map is to be read.
        top = QHBoxLayout()
        self.btn_import = self._button("Import GeoJSON …", self.import_geojson)
        self.btn_save = self._button("Save GeoJSON …", self.save_geojson, enabled=False)
        self.btn_undo = self._button("Undo", self.undo_stack.undo, enabled=False)
        self.btn_redo = self._button("Redo", self.undo_stack.redo, enabled=False)
        for widget in (self.btn_import, self.btn_save, self._separator(),
                       self.btn_undo, self.btn_redo, self._separator(), self.nav,
                       self._separator(), QLabel("Print scale"), self.scale_spin,
                       self.perkm_box):
            top.addWidget(widget)
        top.addStretch(1)
        root.addLayout(top)

        # -- row 2: editing, grouped by what each control acts on ------------
        # The recurring question in this tool is whether an action hits the
        # islands you clicked or the whole province, so that is what the group
        # titles answer — and it lets the buttons drop the qualifier from their
        # own labels ("Merge", not "Merge Selected").
        self.btn_select_all = self._button("All", self.select_all, enabled=False)
        self.btn_select_all.setToolTip("Select every island.")
        self.btn_clear = self._button("None", self.clear_selection, enabled=False)
        self.btn_clear.setToolTip("Clear the selection.")
        # Picking out the few islands to keep is often far quicker than clicking
        # the many to drop, and this turns the one into the other.
        self.btn_invert = self._button("Invert", self.invert_selection, enabled=False)
        self.btn_invert.setToolTip(
            "Swap selected for unselected: click the islands to keep, then "
            "invert to have every other one selected instead.")
        # Left unstyled deliberately: it inherits the palette, so it stays
        # legible on a dark theme. The group title carries the hierarchy.
        self.lbl_count = QLabel()

        self.buffer_spin = self._spin(*BUFFER_MM,
            "Merge closing distance, as it will print (mm). Islands closer than "
            "twice this bridge. The km/mm box up beside the print scale says "
            "what one millimetre is worth on the ground.")
        self.btn_gap = self._button("From Gap", self.buffer_from_gap, enabled=False)
        self.btn_gap.setToolTip(
            "Set the buffer to the least that actually bridges the closest pair "
            "of selected islands, measured against their coastlines.")
        self.btn_merge = self._button("Merge", self.merge_selected, enabled=False)
        self.btn_merge.setToolTip("Bridge the selected islands with necks of land.")
        self.btn_remove_sel = self._button("Remove", self.remove_selected, enabled=False)
        self.btn_remove_sel.setToolTip("Drop the selected islands.")

        self.area_spin = self._spin(*AREA_MM2,
            "Islands whose printed area falls below this (mm² at the print "
            "scale) are marked and removed by ‘Remove Small’.")
        self.area_spin.valueChanged.connect(self._on_threshold)
        self.btn_remove_small = self._button("Remove Small", self.remove_small, enabled=False)
        self.btn_lakes = self._button("Remove Lakes", self.remove_lakes_action, enabled=False)

        # The brush is the manual counterpart to the two heuristics either side
        # of it. Merge only ever adds necks spanning two islands, so it cannot
        # reach a gap no workable buffer closes, nor pick which pair to bridge
        # when several are in range; the removals only drop islands *whole*, by
        # click or by area, so neither can trim a spit or cut a landmass in two.
        # Both are *modes*, so they sit in their own group rather than among the
        # verbs, and they share one brush width — it is one brush, used to add
        # land or to take it away.
        self.btn_draw = self._button("Draw Bridge", self._toggle_draw, enabled=False)
        self.btn_draw.setCheckable(True)
        self.btn_draw.setToolTip(
            "Drag on the map to paint a land bridge at the brush width. "
            "While this is on, left-drag draws instead of panning — pan with "
            "the toolbar's pan tool, and zoom with the wheel as usual. "
            "Esc cancels a stroke, or leaves the mode.")
        self.btn_erase = self._button("Erase", self._toggle_erase, enabled=False)
        self.btn_erase.setCheckable(True)
        self.btn_erase.setToolTip(
            "Drag on the map to rub land out at the brush width — for trimming "
            "a coastline or cutting a landmass in two, which removing whole "
            "islands cannot do. Holes the stroke opens are kept (use ‘Remove "
            "Lakes’ for one you did not want). Esc cancels a stroke, or leaves "
            "the mode.")
        self.brush_spin = self._spin(*BRUSH_MM,
            "Width of the freehand stroke as it will print (mm), for both "
            "brushes, measured across the stroke — not a radius.")

        self.grp_selection = self._group(
            "Selection", self.btn_select_all, self.btn_clear, self.btn_invert,
            self.lbl_count)
        self.grp_selected = self._group(
            "Selected islands", "Buffer (mm)", self.buffer_spin, self.btn_gap,
            self.btn_merge, self.btn_remove_sel)
        self.grp_province = self._group(
            "Whole province", "Min area (mm²)", self.area_spin,
            self.btn_remove_small, self.btn_lakes)
        self.grp_freehand = self._group(
            "Freehand", self.btn_draw, self.btn_erase, "Brush (mm)",
            self.brush_spin)

        edit = QHBoxLayout()
        for group in (self.grp_selection, self.grp_selected,
                      self.grp_province, self.grp_freehand):
            edit.addWidget(group)
        edit.addStretch(1)
        root.addLayout(edit)

        root.addWidget(self.caption)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No file loaded.")
        self._emphasise(self.status)
        root.addWidget(self.status)

        self.setCentralWidget(central)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._escape)

        # Arrow keys pan the map. They are bound to the *canvas*, not the window,
        # so they are live only while the canvas holds the keyboard focus: a
        # window-level shortcut would fire over the spin boxes too and swallow
        # the Up/Down that steps their value. Buttons hand the focus back to the
        # canvas when clicked (see ``_button``), so this is the resting state.
        for key, (dx, dy) in ((Qt.Key.Key_Left, (-1, 0)),
                              (Qt.Key.Key_Right, (1, 0)),
                              (Qt.Key.Key_Down, (0, -1)),
                              (Qt.Key.Key_Up, (0, 1))):
            for modifier, step in ((0, PAN_STEP),
                                   (Qt.Modifier.SHIFT.value, PAN_STEP_COARSE),
                                   (Qt.Modifier.CTRL.value, PAN_STEP_FINE)):
                self._bind_pan(modifier | key.value, dx * step, dy * step)

        # -- undo/redo wiring ----------------------------------------------
        self.undo_stack.canUndoChanged.connect(self.btn_undo.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.btn_redo.setEnabled)
        self.undo_stack.cleanChanged.connect(lambda *_: self._update_title())
        # Bind every sequence the platform lists for undo/redo, not just the
        # first one: on Windows StandardKey.Redo resolves to Ctrl+Y alone, so
        # Ctrl+Shift+Z would otherwise do nothing.
        self._bind(QKeySequence.StandardKey.Undo, self.undo_stack.undo, "Ctrl+Z")
        self._bind(QKeySequence.StandardKey.Redo, self.undo_stack.redo,
                   "Ctrl+Shift+Z", "Ctrl+Y")
        # …and keep the text entries from swallowing them (see ``eventFilter``).
        for entry in (self.buffer_spin, self.area_spin, self.scale_spin,
                      self.perkm_box):
            entry.installEventFilter(self)
        self._sync_buttons()      # start with the groups greyed and the count set
        self._update_title()

    def eventFilter(self, obj, event):
        """
        Stop a focused text entry from claiming undo/redo for its own content.

        Every entry in the window is or contains a ``QLineEdit`` (a
        ``QDoubleSpinBox`` edits through one), which owns a private text-undo
        history and claims Ctrl+Z for it by accepting the ``ShortcutOverride``
        Qt sends before dispatching a shortcut.  That beats the window shortcuts
        whatever context they are given, so with one focused Ctrl+Z would
        silently roll back a typed digit instead of the last edit.  Dropping the
        override here leaves the event unaccepted and normal shortcut dispatch
        continues to the undo stack; every other key (typing, arrow stepping,
        select-all, copy) is untouched.
        """
        if event.type() == QEvent.Type.ShortcutOverride and (
                event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            return True
        return super().eventFilter(obj, event)

    # -- construction helpers ----------------------------------------------
    def _bind(self, standard_key, slot, *extra):
        """Wire ``slot`` to the platform bindings for ``standard_key`` plus
        ``extra`` literal sequences, skipping duplicates."""
        seqs = list(QKeySequence.keyBindings(standard_key))
        seqs += [QKeySequence(s) for s in extra]
        seen = set()
        for seq in seqs:
            text = seq.toString()
            if text in seen:
                continue
            seen.add(text)
            QShortcut(seq, self, activated=slot)

    def _bind_pan(self, sequence, fx: float, fy: float) -> None:
        """Pan by ``(fx, fy)`` of the view on a key, while the canvas has focus."""
        shortcut = QShortcut(QKeySequence(sequence), self.canvas,
                             activated=lambda: self._pan_by(fx, fy))
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    @staticmethod
    def _emphasise(label: QLabel) -> None:
        """
        Make a label carry: larger than the chrome, at full palette contrast.

        No colour is set at all, deliberately.  A hardcoded grey can only ever
        be right for one theme — ``#555`` is legible on a light desktop and
        nearly invisible on a dark one — whereas leaving the colour alone takes
        whatever the palette uses for ordinary text, which is by construction
        the highest-contrast pairing the theme offers against its own
        background.
        """
        font = label.font()
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(size + STATUS_POINT_BUMP)
        else:                            # a font defined in pixels, not points
            font.setPixelSize(font.pixelSize() + STATUS_POINT_BUMP * 2)
        label.setFont(font)

    @staticmethod
    def _separator() -> QFrame:
        """A vertical rule, so the top row's groups are divided, not just spaced."""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    @staticmethod
    def _group(title: str, *contents) -> QGroupBox:
        """
        A titled box of controls, the title naming what they act on.

        Bare strings become labels, so a group reads in source the way it reads
        on screen. The box is also the enable handle for everything inside it.
        """
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
        # Hand the keyboard back to the map once the button has done its work.
        # Qt walks the focus along the toolbar on an arrow press, so without
        # this the arrow keys would step between buttons instead of panning.
        b.clicked.connect(lambda *_: self.canvas.setFocus())
        b.setEnabled(enabled)
        return b

    @staticmethod
    def _spin(lo, hi, val, decimals, step, tip):
        s = QDoubleSpinBox()
        s.setDecimals(decimals)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setValue(val)
        s.setToolTip(tip)
        return s

    # -- canvas -------------------------------------------------------------
    def _px_per_metre(self) -> float:
        """
        Screen pixels per ground metre, from the live data transform.

        The one place the map's scale is read: the brush preview needs it to
        draw a stroke at its true width, and the scale bars to size themselves.
        The axes keep ``aspect="equal"``, so one factor serves both directions.
        """
        origin = self.ax.transData.transform((0.0, 0.0))
        unit = self.ax.transData.transform((1.0, 0.0))
        return abs(unit[0] - origin[0])

    def _style_axis(self, title: str) -> None:
        self.ax.clear()
        self.caption.setText(title)
        # Equal scaling is non-negotiable for a map, but ``datalim`` pays for it
        # by widening the view instead of shrinking the axes box, so zooming in
        # reveals more ground rather than leaving empty sides on the canvas.
        self.ax.set_aspect("equal", adjustable="datalim")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.ax.set_frame_on(False)
        self.ax.margins(0.01)          # hug the province instead of matplotlib's 5%
        self._make_scale_bars()        # ``clear`` above took the old ones with it
        self.canvas.draw_idle()

    # -- scale bars ---------------------------------------------------------
    # Two bars, both measuring the same map: one in km of ground, one in mm of
    # paper at the print scale. They are furniture rather than features, so they
    # live in *axes* coordinates, pinned to the corner — a pan must not carry
    # them off the canvas — while their lengths are recomputed from the ground
    # each draw.
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

    # -- printed sizes to ground units --------------------------------------
    # The three editing parameters are printed millimetres; the libs underneath
    # take kilometres. These are the only crossing points, and every one of them
    # reads the scale live, so nothing caches a conversion that the print scale
    # could go on to invalidate.
    def _km_per_mm(self) -> float:
        """Ground kilometres under one printed millimetre, at the current scale."""
        return self.scale_spin.value() / MM_PER_KM

    def _to_km(self, mm: float) -> float:
        """A printed length (mm) as the ground distance it stands for (km)."""
        return mm * self._km_per_mm()

    def _to_km2(self, mm2: float) -> float:
        """A printed area (mm²) as the ground area it stands for (km²)."""
        return mm2 * self._km_per_mm() ** 2

    def _to_mm(self, km: float) -> float:
        """A ground distance (km) as the length it prints at (mm)."""
        return km / self._km_per_mm()

    def _perkm_text(self) -> str:
        """The km/mm readout for the current print scale."""
        return f"{self._km_per_mm():g} km/mm"

    def _on_print_scale(self, _denom: int) -> None:
        """Carry a new print scale to everything that reads it."""
        self.perkm_box.setText(self._perkm_text())
        # Every editing parameter is now worth a different ground amount, which
        # moves the *Min area* mask — so the map needs replotting, not just the
        # scale bars repositioning. ``_redraw`` asks for the draw either way.
        if self.parts:
            self._redraw(preserve_view=True)
            self._update_status("Print scale changed — ")
        else:
            self.canvas.draw_idle()

    def _on_draw(self, _event) -> None:
        """
        Re-lay the scale bars once a draw has settled the view, if it moved them.

        Doing it here rather than at each call site is what keeps the bars
        honest on *every* route that moves the view — the wheel and drag-pan
        below, the arrow keys, the toolbar's own pan/zoom and Home/Back/Forward,
        and a window resize — without each having to remember to ask.  It is
        also the only moment the numbers are exact: the axes keep
        ``adjustable="datalim"``, so matplotlib widens the limits *during* the
        draw to satisfy the equal aspect, and any span read before that is a
        frame stale.

        A draw that does move them asks for one more, and that one finds nothing
        left to change, so it stops there.  A pan changes neither bar (the span
        is the same ground either side of it), so the common case costs nothing.
        """
        if self._update_scales():
            self.canvas.draw_idle()

    def _update_scales(self) -> bool:
        """Lay out both bars for the current view; True if anything moved."""
        if not self._scale_artists:
            return False
        px_per_metre = self._px_per_metre()
        span = (self.ax.get_window_extent().width / px_per_metre
                if px_per_metre > 0 else 0.0)
        # With no province loaded the view is matplotlib's own unit square, and
        # a ground scale over it would be a fiction.
        live = bool(self.parts) and span > 0
        denom = self.scale_spin.value()

        key = (live, span, denom)
        if key == self._scale_key:
            return False
        self._scale_key = key

        if not live:
            for artist in (a for pair in self._scale_artists for a in pair):
                artist.set_visible(False)
            return True

        # Each bar is rounded in its own unit, so both read as whole numbers;
        # their lengths therefore differ, which is the point — one says how far,
        # the other how big it comes out.
        budget = span * SCALE_SPAN
        km = _nice(budget / 1000.0)
        mm = _nice(budget * 1000.0 / denom)
        bars = ((km * 1000.0, f"{km:g} km"),
                (mm * denom / 1000.0, f"{mm:g} mm at 1:{_grouped(denom)}"))

        for (line, label), y, (ground, text) in zip(self._scale_artists,
                                                    SCALE_Y, bars):
            right = SCALE_X + ground / span
            # One path for the whole bar: left tick, the run, right tick.
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

    def _redraw(self, preserve_view: bool = False) -> None:
        if preserve_view:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        self._style_axis("Province islands  (click to select, drag to pan, "
                         "wheel to zoom; red = below the minimum area)")
        if self.parts:
            gs = gpd.GeoSeries(self.parts)
            thr = self._to_km2(self.area_spin.value())
            sel = set(self.selected)
            below = [is_below_area(p, thr) for p in self.parts]
            base = [i for i in range(len(self.parts)) if i not in sel and not below[i]]
            mark = [i for i in range(len(self.parts)) if i not in sel and below[i]]
            if base:
                gs.iloc[base].plot(ax=self.ax, facecolor=ISLAND_COLOUR,
                                   edgecolor=EDGE_COLOUR, linewidth=0.6)
            if mark:
                gs.iloc[mark].plot(ax=self.ax, facecolor=REMOVE_COLOUR,
                                   edgecolor=REMOVE_EDGE, linewidth=0.9)
            if sel:
                gs.iloc[sorted(sel)].plot(ax=self.ax, facecolor=SELECT_COLOUR,
                                          edgecolor=SELECT_EDGE, linewidth=1.0)
        if preserve_view:
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
        self.canvas.draw_idle()

    # -- pan / selection ----------------------------------------------------
    # Left-drag pans and a left click selects, so panning needs no toolbar mode
    # and never costs a selection: which one happened is only decided on
    # release, by whether the mouse travelled further than PAN_SLOP_PX.
    def _shift_view(self, dx: float, dy: float) -> None:
        """Slide the view by a ground distance — the one place panning happens."""
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self.ax.set_xlim(x0 + dx, x1 + dx)
        self.ax.set_ylim(y0 + dy, y1 + dy)
        # draw_idle coalesces to the event loop, so a held arrow key cannot
        # queue up more redraws than the canvas can serve.
        self.canvas.draw_idle()

    def _pan_by(self, fx: float, fy: float) -> None:
        """Pan by a fraction of the visible span (the arrow keys' step)."""
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        self._shift_view((x1 - x0) * fx, (y1 - y0) * fy)

    def _on_press(self, event) -> None:
        if self.nav.mode:               # a toolbar pan/zoom tool owns the mouse
            return
        if event.button != 1 or event.inaxes is not self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return
        if self._brush_mode is not None:
            self._start_stroke(event)
            return
        # Anchor in data coordinates: the pan keeps this point under the cursor.
        self._grab = (event.xdata, event.ydata, event.x, event.y)
        self._dragged = False

    def _on_motion(self, event) -> None:
        if self._stroke is not None:
            self._extend_stroke(event)
            return
        if self._grab is None or event.xdata is None or event.ydata is None:
            return
        gx, gy, px, py = self._grab
        if not self._dragged:
            if abs(event.x - px) < PAN_SLOP_PX and abs(event.y - py) < PAN_SLOP_PX:
                return                  # still within click slop
            self._dragged = True
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
        # Shifting the limits by the anchor's drift re-pins it under the cursor,
        # so the pan cannot accumulate error however far it is dragged.
        self._shift_view(gx - event.xdata, gy - event.ydata)

    def _on_release(self, event) -> None:
        if self._stroke is not None:
            self._finish_stroke()
            return
        if self._grab is None:
            return
        dragged, self._grab, self._dragged = self._dragged, None, False
        if dragged:
            self.canvas.unsetCursor()
            return                      # that was a pan, not a selection
        if event.inaxes is not self.ax or not self.parts:
            return
        if event.xdata is None or event.ydata is None:
            return
        idx = self._island_at(event.xdata, event.ydata)
        if idx is None:
            return
        if idx in self.selected:
            self.selected.remove(idx)
        else:
            self.selected.append(idx)
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def _island_at(self, x: float, y: float):
        point = Point(x, y)
        for i, part in enumerate(self.parts):
            if part.contains(point):
                return i
        return None

    # -- brush --------------------------------------------------------------
    # A stroke is previewed as a fat round-capped line rather than as the real
    # buffered polygon, because that *is* the region a circular brush sweeps —
    # so the preview is exact, and costs one artist update per mouse event
    # instead of re-plotting 223 islands. The true geometry is built once, on
    # release, where paying for a buffer and a union is affordable.
    def _toggle_draw(self, checked: bool) -> None:
        self._set_brush_mode(DRAW if checked else None)

    def _toggle_erase(self, checked: bool) -> None:
        self._set_brush_mode(ERASE if checked else None)

    def _set_brush_mode(self, mode) -> None:
        """Switch the brush to ``mode`` (or None), dropping any half-drawn stroke."""
        self._cancel_stroke()
        self._brush_mode = mode
        # ``setChecked`` emits no ``clicked``, so setting both buttons from the
        # one mode is what makes the pair exclusive: turning one on releases the
        # other without re-entering here.
        self.btn_draw.setChecked(mode == DRAW)
        self.btn_erase.setChecked(mode == ERASE)
        if mode is None:
            self.canvas.unsetCursor()
            self._update_status()
            return
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self._update_status("Draw mode — drag to paint a bridge; " if mode == DRAW
                            else "Erase mode — drag to rub land out; ")

    def _escape(self) -> None:
        """Esc backs out one step: first the stroke, then the mode itself."""
        if self._stroke is not None:
            self._cancel_stroke()
            self._update_status("Stroke cancelled — ")
        elif self._brush_mode is not None:
            self._set_brush_mode(None)

    def _cancel_stroke(self) -> None:
        self._stroke = None
        if self._preview is not None:
            self._preview.remove()
            self._preview = None
            self.canvas.draw_idle()

    def _brush_points(self) -> float:
        """
        The brush width in typographic points, for the preview line.

        Matplotlib linewidths are points (1/72 inch), so the ground width has to
        be carried through the data transform and the figure's dpi.
        """
        width_m = self._to_km(self.brush_spin.value()) * 1000.0
        # Never thinner than a hairline, or a fine brush zoomed out would draw
        # land the stroke did not appear to cover.
        return max(width_m * self._px_per_metre() * 72.0 / self.figure.dpi, 1.0)

    def _start_stroke(self, event) -> None:
        self._stroke = [(event.xdata, event.ydata)]
        # The preview takes the colour of what the stroke will do: land being
        # laid down, or — in the same red the area threshold marks doomed
        # islands with — land about to go.
        colour = BRIDGE_COLOUR if self._brush_mode == DRAW else REMOVE_COLOUR
        self._preview, = self.ax.plot(
            [event.xdata], [event.ydata], color=colour, alpha=0.7,
            solid_capstyle="round", solid_joinstyle="round",
            linewidth=self._brush_points())
        self.canvas.draw_idle()

    def _extend_stroke(self, event) -> None:
        if event.xdata is None or event.ydata is None:
            return          # pointer left the axes; skip the sample, keep the stroke
        self._stroke.append((event.xdata, event.ydata))
        xs, ys = zip(*self._stroke)
        self._preview.set_data(xs, ys)
        self.canvas.draw_idle()

    @staticmethod
    def _draw_note(before: int, after: int) -> str:
        """What a committed draw stroke did, read off the landmass count."""
        if after < before:
            return f"{before - after + 1} landmasses joined into one"
        if after > before:
            return "it touched no land, so that is a new island"
        return "one landmass extended"

    @staticmethod
    def _erase_note(before: int, after: int) -> str:
        """What a committed erase stroke did, read off the landmass count."""
        if after > before:
            return f"cut apart — {after} landmasses where there were {before}"
        if after < before:
            return f"{before - after} landmass(es) erased entirely"
        return "coastline trimmed"

    def _finish_stroke(self) -> None:
        """Commit the stroke as one undoable edit, drawn or erased."""
        points, erasing = self._stroke, self._brush_mode == ERASE
        # Drop the preview artist here, not on the way out: every path below
        # ends the stroke, and some (a refused empty result) do not redraw.
        self._cancel_stroke()
        width = self.brush_spin.value()
        width_km = self._to_km(width)
        try:
            result = (erase_stroke(self.doc, points, width_km) if erasing
                      else draw_bridge(self.doc, points, width_km))
        except Exception as exc:
            QMessageBox.critical(self, "Erase failed" if erasing else "Draw failed",
                                 str(exc))
            self._redraw(preserve_view=True)
            return

        # Both libs answer a stroke that changes nothing by identity, which is
        # the only honest no-op test here: the overlay rebuilds the rings, so
        # the result compares unequal by value while being the same land.
        if result is self.doc:
            self._redraw(preserve_view=True)
            self._update_status("Nothing there to erase — " if erasing
                                else "Already land there — ")
            return

        before, after = landmass_count(self.doc), landmass_count(result)
        note = (self._erase_note(before, after) if erasing
                else self._draw_note(before, after))
        verb = "Erased" if erasing else "Bridge drawn"
        self._apply(f"{'Erase' if erasing else 'Draw bridge'} at {width:g} mm",
                    _as_multi(result), f"{verb} at {width:g} mm — {note}.")

    def select_all(self) -> None:
        self.selected = list(range(len(self.parts)))
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def clear_selection(self) -> None:
        self.selected = []
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def invert_selection(self) -> None:
        """Select exactly the islands that were not, and vice versa."""
        sel = set(self.selected)
        self.selected = [i for i in range(len(self.parts)) if i not in sel]
        self._redraw(preserve_view=True)
        self._sync_buttons()
        self._update_status()

    def _sync_buttons(self) -> None:
        has = bool(self.parts)
        picked = len(self.selected)
        # A group is only offered when there is a document to act on; within it
        # the members keep their own rules, so "needs a selection" still shows
        # per button rather than greying out the parameters beside them.
        for group in (self.grp_selection, self.grp_selected,
                      self.grp_province, self.grp_freehand):
            group.setEnabled(has)
        self.btn_draw.setEnabled(has)
        self.btn_erase.setEnabled(has)
        if not has and self._brush_mode is not None:
            self._set_brush_mode(None)     # nothing to draw on, or rub out
        self.btn_merge.setEnabled(picked >= 2)
        # Same ceiling as the status line's gap: past it, measuring would stall.
        self.btn_gap.setEnabled(2 <= picked <= GAP_MAX_SELECTION)
        self.btn_remove_sel.setEnabled(picked >= 1)
        self.btn_remove_small.setEnabled(has)
        self.btn_lakes.setEnabled(has)
        self.btn_select_all.setEnabled(has)
        self.btn_clear.setEnabled(bool(self.selected))
        # Unlike All/None this stays live at both extremes: inverting nothing
        # selects everything, and inverting everything clears it.
        self.btn_invert.setEnabled(has)
        self.btn_save.setEnabled(has)
        self.lbl_count.setText(f"{picked} selected" if picked else "none selected")

    # -- operations ---------------------------------------------------------
    def buffer_from_gap(self) -> None:
        """
        Set the merge buffer to the least that bridges the closest pair.

        The value is *verified* against the real coastlines rather than derived
        from the gap, because no fixed multiple of the gap holds: half the gap
        only suffices for islands large relative to it, and the provinces here
        need anywhere from 0.59x to 1.74x.  Probing costs a couple of buffer
        passes over real geometry, hence the wait cursor.
        """
        chosen = [self.parts[i] for i in self.selected
                  if 0 <= i < len(self.parts)]
        pair = closest_pair(chosen)
        if pair is None:
            return
        a, b, gap = pair
        if gap == 0.0:
            self._update_status("Already touching, no buffer needed — ")
            return

        # Hand the spin box's own precision down, so what comes back is the
        # exact value that lands in the box — the merge is knife-edge near its
        # threshold, and rounding a verified figure afterwards can break it.
        # The box is in printed millimetres and the probe works in ground km, so
        # the ceiling and the quantum are converted rather than the answer: one
        # increment of the box becomes the step, which puts every value the probe
        # tries on the box's own grid, and the one it settles on converts back to
        # a millimetre the box can hold exactly.
        top = self.buffer_spin.maximum()
        step_mm = 10 ** -self.buffer_spin.decimals()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            needed = bridging_buffer_km(a, b, max_km=self._to_km(top),
                                        step=self._to_km(step_mm))
        finally:
            QApplication.restoreOverrideCursor()

        if needed is None:
            self._update_status(
                f"No buffer up to {top:g} mm bridges the {self._mm(gap)} mm "
                "gap — the islands are too small for it — ")
            return
        needed_mm = self._to_mm(needed)
        self.buffer_spin.setValue(needed_mm)
        self._update_status(
            f"Buffer {self.buffer_spin.value():g} mm bridges the closest "
            f"{self._mm(gap)} mm gap — ")

    def merge_selected(self) -> None:
        if len(self.selected) < 2:
            return
        sel = set(self.selected)
        chosen = [self.parts[i] for i in self.selected]
        others = [p for i, p in enumerate(self.parts) if i not in sel]
        buffer_mm = self.buffer_spin.value()
        try:
            merged = merge_islands(chosen, self._to_km(buffer_mm))
        except Exception as exc:
            QMessageBox.critical(self, "Merge failed", str(exc))
            return
        new_doc = _as_multi(unary_union([merged, *others]) if others else merged)
        groups = len(_polygons(merged))
        msg = (f"Merged {len(chosen)} islands at {buffer_mm:g} mm — "
               + ("all bridged into one landmass." if groups == 1 else
                  f"{groups} group(s) remain; raise the buffer to bridge them."))
        self._apply(f"Merge {len(chosen)} islands at {buffer_mm:g} mm", new_doc, msg)

    def remove_selected(self) -> None:
        if not self.selected:
            return
        sel = set(self.selected)
        keep = [p for i, p in enumerate(self.parts) if i not in sel]
        new_doc = _as_multi(unary_union(keep)) if keep else None
        self._apply(f"Remove {len(sel)} island(s)", new_doc,
                    f"Removed {len(sel)} selected island(s).")

    def remove_small(self) -> None:
        thr = self.area_spin.value()
        keep = remove_small_islands(self.parts, self._to_km2(thr))
        removed = len(self.parts) - len(keep)
        if removed == 0:
            self._update_status(f"Nothing below {thr:g} mm² — ")
            return
        new_doc = _as_multi(unary_union(keep)) if keep else None
        self._apply(f"Remove {removed} small island(s) < {thr:g} mm²", new_doc,
                    f"Removed {removed} island(s) below {thr:g} mm²; {len(keep)} kept.")

    def remove_lakes_action(self) -> None:
        try:
            stripped = remove_lakes(self.doc)
        except Exception as exc:
            QMessageBox.critical(self, "Remove lakes failed", str(exc))
            return
        self._apply("Remove lakes", _as_multi(stripped),
                    "Removed lakes (interior rings).")

    def _apply(self, text: str, new_doc, ok_msg: str) -> None:
        """Validate an operation's result and push it as one undoable command."""
        if new_doc is None or new_doc.is_empty:
            QMessageBox.warning(
                self, "Empty result",
                "That would remove the whole province; nothing was changed.")
            return
        if not _differs(new_doc, self.doc):
            self._update_status("No change — ")
            return
        new_sel = self._remap(new_doc)
        self.undo_stack.push(
            EditCommand(self, text, self.doc, new_doc, self.selected, new_sel))
        self.status.setText(ok_msg)

    def _remap(self, new_doc) -> list:
        """Carry the selection onto the result by representative-point cover."""
        new_parts = _polygons(new_doc)
        out, seen = [], set()
        for i in self.selected:
            if not 0 <= i < len(self.parts):
                continue
            probe = self.parts[i].representative_point()
            for j, part in enumerate(new_parts):
                if part.covers(probe):
                    if j not in seen:
                        seen.add(j)
                        out.append(j)
                    break
        return out

    # -- state application (redo/undo land here) ---------------------------
    def _set_state(self, doc, selected, preserve_view: bool = True) -> None:
        self.doc = doc
        self.parts = _polygons(doc)
        self.selected = [i for i in selected if 0 <= i < len(self.parts)]
        self._redraw(preserve_view=preserve_view)
        self._sync_buttons()
        self._update_status()

    # -- load / save --------------------------------------------------------
    def import_geojson(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import province GeoJSON", _dir_or_cwd(DEFAULT_LOAD_DIR),
            GEOJSON_FILTER)
        if not path:
            return
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        if gdf.empty:
            QMessageBox.warning(self, "Nothing to show",
                                "No features in that file.")
            return

        self.source_crs = gdf.crs
        geom_col = gdf.geometry.name
        self.source_attrs = (gdf.drop(columns=[geom_col]).iloc[0].to_dict()
                             if len(gdf) else {})

        work = gdf.to_crs(EPSG_METRIC) if gdf.crs is not None else gdf
        if gdf.crs is None:
            QMessageBox.warning(
                self, "No CRS on input",
                "The file has no coordinate system, so there is no ground "
                "distance to convert: the buffer / brush / area act in the raw "
                "data units, and the print scale does not apply to them.")
        doc = _as_multi(unary_union(list(work.geometry)))
        if doc is None:
            QMessageBox.warning(self, "Nothing to show",
                                "No polygon geometry in that file.")
            return

        self.source_path = Path(path)
        self.undo_stack.clear()
        # Frame the whole province on load (subsequent redraws preserve the view).
        self._set_state(doc, [], preserve_view=False)
        self.undo_stack.setClean()
        self._update_title()
        self.status.setText(
            f"Loaded {self.source_path.name}: "
            f"{landmass_count(self.doc)} landmass(es). "
            "Click islands to select; edit; undo/redo as needed.")

    def save_geojson(self) -> None:
        if self.doc is None:
            return
        DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        stem = self.source_path.stem if self.source_path else "province"
        default = str(DEFAULT_SAVE_DIR / f"{stem}.geojson")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save province GeoJSON", default, GEOJSON_FILTER)
        if not path:
            return
        row = dict(self.source_attrs)
        row["geometry"] = self.doc
        out = gpd.GeoDataFrame(
            [row], geometry="geometry",
            crs=(EPSG_METRIC if self.source_crs is not None else None))
        if self.source_crs is not None:
            out = out.to_crs(self.source_crs)
        try:
            out.to_file(path, driver="GeoJSON")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.undo_stack.setClean()
        self.status.setText(f"Saved {len(self.parts)} island(s) to {Path(path).name}.")

    # -- status / title -----------------------------------------------------
    def _on_threshold(self, *_) -> None:
        if self.parts:
            self._redraw(preserve_view=True)
            self._update_status("Threshold changed — ")

    def _mm(self, distance_km: float) -> str:
        """
        A ground distance as a printed one, at a readable precision (bare number).

        The gaps are reported in the unit that closes them: the buffer beside
        them is printed millimetres, and ‘From Gap’ answers one with the other,
        so a status line quoting kilometres would leave the two incomparable.
        """
        mm = self._to_mm(distance_km)
        if mm < 1:
            return f"{mm:.3f}"
        return f"{mm:.2f}" if mm < 10 else f"{mm:.1f}"

    def _gap_fragment(self) -> str:
        """
        The status line's gap segment for the current selection, cached.

        ``_update_status`` runs on every click, threshold tweak and undo, but the
        gap only moves when the selection or the document does — so measuring is
        keyed on both, and a threshold change costs nothing.  The document is
        held by reference (not by id) so a freed snapshot can never alias a new
        one into a stale hit.

        The print scale joins the key because the *reported* gap is in printed
        millimetres: the ground measurement would be unchanged, but the sentence
        it is rendered into is not.
        """
        sel = (tuple(sorted(self.selected)), self.scale_spin.value())
        if self._gap_doc is self.doc and self._gap_sel == sel:
            return self._gap_text
        self._gap_doc, self._gap_sel = self.doc, sel
        self._gap_text = self._measure_gap()
        return self._gap_text

    def _measure_gap(self) -> str:
        if len(self.selected) < 2:
            return ""                   # nothing to measure against
        if len(self.selected) > GAP_MAX_SELECTION:
            return f"gaps not measured (>{GAP_MAX_SELECTION} selected)"
        chosen = [self.parts[i] for i in self.selected
                  if 0 <= i < len(self.parts)]
        span = gap_range_km(chosen)
        if span is None:
            return ""
        closest, widest = span
        if len(chosen) == 2:
            return "touching" if closest == 0 else f"gap {self._mm(closest)} mm"
        return f"gaps {self._mm(closest)}–{self._mm(widest)} mm"

    def _update_status(self, prefix: str = "") -> None:
        if not self.parts:
            self.status.setText("No file loaded.")
            return
        thr = self.area_spin.value()
        thr_km2 = self._to_km2(thr)
        below = sum(is_below_area(p, thr_km2) for p in self.parts)
        bits = [f"{landmass_count(self.doc)} landmass(es)",
                f"{len(self.selected)} selected"]
        gap = self._gap_fragment()
        if gap:
            bits.append(gap)
        bits.append(f"{below} below {thr:g} mm²")
        self.status.setText(f"{prefix}{', '.join(bits)}.")

    def _update_title(self) -> None:
        name = self.source_path.name if self.source_path else "(no file)"
        dirty = "" if self.undo_stack.isClean() else " *"
        self.setWindowTitle(f"Island Editor — {name}{dirty}")


def main() -> int:
    app = QApplication(sys.argv)
    window = IslandEditor()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
