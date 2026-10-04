"""
UI_border_editor.py

Clean every border two provinces share, one at a time, and keep track of which
are done.

``harnesses/UI_shared_border.py`` cleans one seam between two files picked by
hand.  A country has dozens of seams, and after the third one the question is no
longer *how* to clean a border but **which ones are left**.  This window holds
the whole set of provinces, finds every border between them, and lists each
border as cleaned or not.  Pick one and it opens in the same seam editor.

Two screens
-----------
**Borders** (the main page)

1. **Import Folder …** / **Import Files …** -- the provinces to work on.  Every
   pair that shares a seam is found at once and goes on the list.  If the save
   folder already holds a file of the same name, that one is loaded instead, so
   the work done last time is the work you continue from.
2. The **Not cleaned** and **Cleaned** lists, and the map: seams are drawn red
   until cleaned and green after.  Each border also says whether its printed
   pieces will mesh -- "meshes cleanly", or what will not -- in purple when a
   tab locks and red for narrow land.  Click a province on the map to select
   it, or click a border in a list to select both of its provinces.
3. **Edit Border →** -- once the two selected provinces share a seam (or
   double-click a border in either list).

**Edit Border** (the second page) is the Shared Border editor itself: the same
parameters, the same before/after panels, the same Simplify Seam.  Two
differences:

* **Apply and Mark Cleaned** replaces *Save Both*.  It writes both provinces
  into the save folder under their own file names, in the CRS each arrived in,
  records the border as cleaned, and returns to the list.
* **← Borders** goes back without saving.

Its controls are grouped in boxes: **Border** (back, the pair, Apply),
**Simplify** (the harness's settings and Simplify Seam), and three of its own:

* **Fit check** -- where the printed pieces will not mesh (``lib.LIB_seam_fit``),
  marked on both panels: narrow land at the seam in red, fingers too thin to
  survive in amber, and tabs whose head is wider than their neck -- which lock
  the pieces together, so they only go in from above and shrinkage piles up
  at them -- in purple.  **Edge loss** is what each printed edge loses per
  side, **Min width** the narrowest land that survives, **Largest tab** the
  widest tab head looked for.  The settings are saved as they are turned, and
  the list judges every border by them.  **Zoom to Worst Fit** frames the
  worst one.
* **Auto solve** -- **Solve Tabs** repairs what the fit check found, on a
  worker thread.  Each problem is either cut off (its land goes to the other
  province) or filled round (the other province's undercuts, or a collar to
  bring thin land up to width), whichever leaves fewer problems; nothing moves
  the border further than **Max move** from where it ran, and what cannot be
  fixed within that is left for the brush.  It works on the simplified pair if
  there is one, outlines the land it moved in green, and is one undo step.
* **Push border** -- a brush that moves the border.  **Grow <A>** hands the
  land under the stroke from B to A; **Grow <B>** the other way.  Only land
  touching the province growing moves, so it moves the border rather than
  making enclaves, and the pair still covers exactly the ground it did.  It
  edits the pair as imported; if the seam had been simplified, that is run
  again on the edited pair.  **Undo** (Ctrl+Z) and **Redo** (Ctrl+Shift+Z or
  Ctrl+Y) step through the pushes and solves; the history lasts until another
  border is opened.  Esc leaves the brush.

What "cleaned" means
--------------------
The save folder keeps a ``border_ledger.json`` (see ``lib.LIB_border_ledger``)
recording the seam each cleaned border was left with.  A border is listed as
cleaned only while the files still carry that seam, so a province re-exported
from the island editor puts its borders back on the Not cleaned list by itself,
marked "changed since cleaned".

Cleaning one border does not disturb any other: the seam's endpoints are
pinned and only the land between the two provinces is re-cut, so the borders
can be done in any order.

Run with:  python UI_border_editor.py

Reuses ``lib.LIB_shared_border``, ``lib.LIB_border_ledger``,
``lib.LIB_seam_fit`` and the seam editor from ``harnesses.UI_shared_border``.
"""

import sys
from pathlib import Path

import geopandas as gpd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.collections import PolyCollection
from matplotlib.figure import Figure
from shapely.geometry import Point
from shapely.ops import unary_union

from PySide6.QtCore import QEvent, QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from harnesses.UI_shared_border import SharedBorderTester
from lib.LIB_border_ledger import LEDGER_NAME, BorderLedger, find_borders
from lib.LIB_project_config import get as cfg
from lib.LIB_project_config import update_local
from lib.LIB_seam_fit import FitParams, fit_report, push_border, solve_fit

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
GEOJSON_GLOBS = ("*.geojson", "*.json")
EPSG_METRIC = cfg("crs.working")

_ROOT = Path(__file__).resolve().parent
DEFAULT_LOAD_DIR = _ROOT / cfg("paths.provinces_out")
DEFAULT_SAVE_DIR = _ROOT / cfg("paths.provinces_bordered")

PROVINCE_COLOUR = "#D6EAF8"
SELECT_COLOUR = "#F5B041"
EDGE_COLOUR = "#1B4F72"
CLEANED_COLOUR = "#27AE60"        # green -- the harness's conditioned seam
NOT_CLEANED_COLOUR = "#C0392B"    # red -- still as imported
LABEL_COLOUR = "#2C3E50"

#: Seam strokes, in points. Wide enough to read at the zoom a whole country is
#: seen at, where a border is otherwise one pixel of province outline.
SEAM_WIDTH = 2.2
SEAM_WIDTH_SELECTED = 4.5

#: How far the pointer may move between press and release and still be a
#: click rather than a pan, in pixels.
CLICK_SLOP_PX = 5

#: The fit overlay. Narrow land in the red Show thin uses; a locking tab in
#: purple, because it is a different fault -- the land is wide enough, its
#: shape is what traps the pieces -- and a finger too thin to survive in amber.
NARROW_COLOUR = "#C0392B"
LOCK_COLOUR = "#8E44AD"
FINGER_COLOUR = "#E67E22"

#: Land Auto Solve moved, outlined on the left panel until the next edit.
SOLVED_COLOUR = "#1E8449"

#: The two push modes: grow province A into B, or B into A.
GROW_A, GROW_B = "a", "b"

#: The fit settings, as ``(config key, label, lo, hi, decimals, step, tip)``.
#: Saved to the local config as they are turned, so the list on the first page
#: -- which reads the config -- judges every border by the same numbers the
#: edit page was showing.
FIT_SETTINGS = (
    ("seam.edge_loss_mm", "Edge loss:", 0.0, 2.0, 2, 0.05,
     "What each printed edge loses or gains, per side: over-extrusion, the\n"
     "elephant's foot, slicer clearance. A feature at the seam loses twice\n"
     "this across."),
    ("thin.width_mm", "Min width:", 0.0, 20.0, 2, 0.05,
     "The narrowest land that survives as a solid -- the same setting as\n"
     "Show thin. Land at the seam needs this plus twice the edge loss."),
    ("seam.fit_tab_mm", "Largest tab:", 0.5, 50.0, 1, 0.5,
     "The widest tab head looked for. Pockets in one province narrower than\n"
     "this are closed over, and the other province's land in them is a tab.\n"
     "A bulge wider than this is just the border going somewhere."),
)

PAGE_BORDERS, PAGE_EDIT = 0, 1

STATUS_TEXT = {"cleaned": "cleaned", "changed": "changed since cleaned",
               "not cleaned": "not cleaned"}


def display_name(stem: str) -> str:
    """``CAN_British Columbia`` reads as ``British Columbia``."""
    prefix, _, rest = stem.partition("_")
    return rest if rest and prefix.isupper() else stem


def geojson_in(folder) -> list:
    """Every province file in ``folder``, leaving out the ledger itself."""
    folder = Path(folder)
    found = {p for pattern in GEOJSON_GLOBS for p in folder.glob(pattern)}
    return sorted(p for p in found if p.name != LEDGER_NAME)


def _dir_or_cwd(path: Path) -> str:
    return str(path if path.is_dir() else Path.cwd())


class Province:
    """One province file, dissolved, in the working CRS."""

    def __init__(self, path: Path, geometry, source_crs=None,
                 resumed: bool = False):
        self.path = Path(path)
        self.geometry = geometry
        self.source_crs = source_crs        #: written back in this
        self.resumed = resumed              #: loaded from the save folder

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def label(self) -> str:
        return display_name(self.name)


def read_province(path: Path, resumed: bool = False):
    """One file as a Province, or None if it holds no land."""
    frame = gpd.read_file(path)
    if frame.empty:
        return None
    source_crs = frame.crs
    if source_crs is not None and str(source_crs) != str(EPSG_METRIC):
        frame = frame.to_crs(EPSG_METRIC)
    geometry = unary_union(list(frame.geometry))
    if geometry is None or geometry.is_empty:
        return None
    return Province(path, geometry, source_crs, resumed)


def write_province(geometry, source_crs, out: Path) -> Path:
    """Write one province in the CRS it arrived in, replacing any old copy."""
    frame = gpd.GeoDataFrame(geometry=[geometry], crs=EPSG_METRIC)
    if source_crs is not None:
        frame = frame.to_crs(source_crs)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The GeoJSON driver will not always write over an existing file.
    out.unlink(missing_ok=True)
    frame.to_file(out, driver="GeoJSON")
    return out


class SolveWorker(QObject):
    """Runs Auto Solve off the UI thread: Labrador/Québec takes ten seconds."""
    done = Signal(object)                  # SolveResult
    failed = Signal(str)
    progress = Signal(int, int, int)       # round, done, total

    def __init__(self, geom_a, geom_b, params, max_move_mm):
        super().__init__()
        self._a, self._b = geom_a, geom_b
        self._params = params
        self._max_move = max_move_mm

    def run(self) -> None:
        try:
            result = solve_fit(self._a, self._b, self._params, self._max_move,
                               progress=self.progress.emit)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


def _box(title: str, *rows) -> QGroupBox:
    """A titled box holding ``rows`` -- layouts or widgets -- one under another."""
    box = QGroupBox(title)
    column = QVBoxLayout(box)
    column.setContentsMargins(8, 4, 8, 6)
    for row in rows:
        if isinstance(row, QWidget):
            column.addWidget(row)
        else:
            column.addLayout(row)
    return box


def _widgets_of(layout) -> list:
    """The widgets in a layout, in order, leaving its spacers behind."""
    items = (layout.itemAt(i) for i in range(layout.count()))
    return [item.widget() for item in items if item.widget() is not None]


def _note_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("color:#555;")
    label.setWordWrap(True)
    return label


class SeamPage(SharedBorderTester):
    """
    The second screen: the Shared Border editor, fed a pair rather than files.

    Subclassed rather than copied so it *is* the same editor -- every control,
    the threaded run, the linked panels -- and stays so as the harness changes.
    The ends are different: the pair arrives from the list, and Save Both
    becomes Apply, which hands the result back instead of asking for a folder.
    The harness's two rows are regrouped into **Border** and **Simplify**
    boxes, and three of this page's own sit under them:

    * **Fit check** -- marks, on both panels, where the printed pieces will
      not mesh (``lib.LIB_seam_fit``): narrow land at the seam, fingers too
      thin to survive, and tabs that lock.
    * **Auto solve** -- repairs what the fit check found, by handing land
      across the seam, no further than *Max move* from where it ran.
    * **Push border** -- a brush that moves the border by hand.

    Pushes and solves edit the pair as imported (the left panel), and are
    steps on the same undo history; if the seam had been simplified, the
    simplification is run again on the edited pair, so the right panel stays
    live.
    """
    back_requested = Signal()
    applied = Signal(object)              # SeamResult

    def __init__(self):
        # Set before the harness builds itself: its __init__ calls
        # _invalidate, which this class extends.
        self.fit_before = None            #: FitReport of the pair as edited
        self.fit_after = None             #: FitReport of the simplified pair
        #: True from the moment a run is asked for. The harness's run() calls
        #: _set_busy before it creates the thread, so ``_thread`` alone reads
        #: idle at exactly the moment the buttons are being locked.
        self._busy = False
        super().__init__()
        # A main window by birth; here it is one page of another window.
        self.setWindowFlags(Qt.Widget)
        self.btn_a.hide()
        self.btn_b.hide()
        self.btn_save.setText("Apply and Mark Cleaned")
        self.btn_save.setToolTip(
            "Write both provinces into the save folder and record this border "
            "as cleaned.")

        self.btn_back = QPushButton("← Borders")
        self.btn_back.setToolTip("Back to the list of borders, without saving.")
        self.btn_back.clicked.connect(self.back_requested.emit)
        self.heading = QLabel("")
        self.heading.setStyleSheet("font-weight:bold;")

        self.province_a = None
        self.province_b = None
        self._status_word = ""
        #: Edit history, as ``(geom_a, geom_b, note)``: the pair before each
        #: push on the undo side, the pair after each undone push on the redo.
        self._undo: list = []
        self._redo: list = []
        self._push_mode = None            #: GROW_A, GROW_B, or None
        self._stroke = None               #: points of an in-progress stroke
        self._stroke_ax = None
        self._preview = None
        self._solve_thread = None
        self._solve_worker = None
        self._solve_marks: list = []      #: land the last solve moved

        self._regroup()

        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        # Every sequence the platform lists, plus the island editor's: on
        # Windows StandardKey.Redo is Ctrl+Y alone, and Ctrl+Shift+Z is what
        # the other window taught the hand.
        self._bind(QKeySequence.StandardKey.Undo, self.undo_push, "Ctrl+Z")
        self._bind(QKeySequence.StandardKey.Redo, self.redo_push,
                   "Ctrl+Shift+Z", "Ctrl+Y")
        self._bind(None, self._escape, "Escape")
        # …and keep the number boxes from swallowing them (see eventFilter).
        for entry in (self.tol_spin, self.seg_spin, self.angle_spin,
                      self.smooth_spin, self.nozzle_spin, self.drift_spin,
                      self.brush_spin, self.move_spin,
                      *self.fit_spins.values()):
            entry.installEventFilter(self)
        self._sync_fit_label()

    def _bind(self, standard_key, slot, *extra) -> None:
        """Wire ``slot`` to a standard key's bindings plus ``extra`` sequences,
        once each, live while focus is anywhere on this page."""
        seqs = (list(QKeySequence.keyBindings(standard_key))
                if standard_key is not None else [])
        seqs += [QKeySequence(text) for text in extra]
        seen = set()
        for seq in seqs:
            if seq.isEmpty() or seq.toString() in seen:
                continue
            seen.add(seq.toString())
            shortcut = QShortcut(seq, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    def eventFilter(self, obj, event):
        """
        Stop a focused number box from claiming undo/redo for its own text.

        A ``QDoubleSpinBox`` edits through a ``QLineEdit``, which keeps a text
        history of its own and claims Ctrl+Z for it by accepting the
        ``ShortcutOverride`` sent before a shortcut fires.  Type a brush width
        and Ctrl+Z would roll back a digit instead of the last push.  Refusing
        the override lets the page's shortcuts have it, as in the island
        editor; every other key is left alone.
        """
        if event.type() == QEvent.Type.ShortcutOverride and (
                event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)
                or event.keyCombination().toCombined()
                == QKeySequence("Ctrl+Shift+Z")[0].toCombined()):
            return True
        return super().eventFilter(obj, event)

    # -- building ---------------------------------------------------------
    def _regroup(self) -> None:
        """
        The harness's two loose rows, and this page's three, as titled boxes.

        The harness lays its toolbar and parameters out as bare rows; they are
        taken out of its layout and their widgets moved into boxes, rather than
        the harness being changed, so it stays the standalone tool it is.
        """
        root = self.centralWidget().layout()
        toolbar = root.takeAt(0).layout()
        params = root.takeAt(0).layout()
        param_widgets = _widgets_of(params)
        del toolbar                       # its widgets are re-homed below

        border_row = QHBoxLayout()
        for widget in (self.btn_back, self.heading):
            border_row.addWidget(widget)
        border_row.addStretch(1)
        border_row.addWidget(self.btn_save)

        simplify_row = QHBoxLayout()
        for widget in param_widgets:
            simplify_row.addWidget(widget)
        simplify_row.addStretch(1)
        simplify_row.addWidget(self.btn_run)
        simplify_row.addWidget(self.btn_zoom)

        self.border_box = _box("Border", border_row)
        self.simplify_box = _box("Simplify", simplify_row)
        self.fit_box = self._build_fit_box()
        self.solve_box = self._build_solve_box()
        self.push_box = self._build_push_box()
        checks = QHBoxLayout()
        checks.addWidget(self.fit_box, stretch=3)
        checks.addWidget(self.solve_box, stretch=2)
        root.insertWidget(0, self.border_box)
        root.insertWidget(1, self.simplify_box)
        root.insertLayout(2, checks)
        root.insertWidget(3, self.push_box)

    def _build_fit_box(self) -> QGroupBox:
        row = QHBoxLayout()
        self.fit_check = QCheckBox("Show fit")
        self.fit_check.setChecked(True)
        self.fit_check.setToolTip(
            "Mark where the printed pieces will not mesh: narrow land at the "
            "seam (red),\nfingers too thin to survive (amber), and tabs whose "
            "head is wider than\ntheir neck, which lock the pieces together "
            "(purple).")
        self.fit_check.toggled.connect(self._refit)
        row.addWidget(self.fit_check)
        self.fit_spins = {}
        for key, label, lo, hi, decimals, step, tip in FIT_SETTINGS:
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setSuffix(" mm")
            spin.setValue(float(cfg(key)))
            spin.setToolTip(tip)
            spin.valueChanged.connect(
                lambda value, key=key: self._on_fit_setting(key, value))
            self.fit_spins[key] = spin
            row.addWidget(QLabel(label))
            row.addWidget(spin)
        self.btn_worst = QPushButton("Zoom to Worst Fit")
        self.btn_worst.setToolTip(
            "Frame the tightest locking tab, or else the thinnest finger, or "
            "else the largest patch of narrow land.")
        self.btn_worst.clicked.connect(self.zoom_to_worst_fit)
        row.addStretch(1)
        row.addWidget(self.btn_worst)
        self.fit_label = _note_label()
        return _box("Fit check", row, self.fit_label)

    def _build_solve_box(self) -> QGroupBox:
        row = QHBoxLayout()
        self.move_spin = QDoubleSpinBox()
        self.move_spin.setDecimals(2)
        self.move_spin.setRange(0.05, 20.0)
        self.move_spin.setSingleStep(0.25)
        self.move_spin.setSuffix(" mm")
        self.move_spin.setValue(float(cfg("seam.solve_max_move_mm")))
        self.move_spin.setToolTip(
            "The furthest a repair may move the border from where it ran.\n"
            "A deep locking tab can only be cut off by moving the border its\n"
            "full depth; below that it is left for the brush.")
        self.move_spin.valueChanged.connect(
            lambda value: update_local("seam.solve_max_move_mm", value))
        row.addWidget(QLabel("Max move:"))
        row.addWidget(self.move_spin)
        row.addStretch(1)
        self.btn_solve = QPushButton("Solve Tabs")
        self.btn_solve.setToolTip(
            "Repair every locking tab, thin finger and patch of narrow land\n"
            "the fit check finds -- by cutting it off, or by filling round it\n"
            "-- whichever leaves fewer problems. Works on the simplified pair\n"
            "if there is one. One step to undo.")
        self.btn_solve.clicked.connect(self.solve)
        self.btn_solve.setEnabled(False)
        row.addWidget(self.btn_solve)
        self.solve_label = _note_label(
            "Cuts off or fills round what the fit check marks.")
        return _box("Auto solve", row, self.solve_label)

    def _build_push_box(self) -> QGroupBox:
        row = QHBoxLayout()
        self.btn_grow_a = QPushButton("Grow A")
        self.btn_grow_b = QPushButton("Grow B")
        for button, mode in ((self.btn_grow_a, GROW_A),
                             (self.btn_grow_b, GROW_B)):
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked, mode=mode: self.set_push_mode(
                    mode if checked else None))
            row.addWidget(button)
        self.brush_spin = QDoubleSpinBox()
        self.brush_spin.setDecimals(2)
        self.brush_spin.setRange(0.05, 50.0)
        self.brush_spin.setSingleStep(0.25)
        self.brush_spin.setSuffix(" mm")
        self.brush_spin.setValue(float(cfg("seam.brush_mm")))
        self.brush_spin.setToolTip(
            "Brush width on paper, measured across the stroke.")
        self.brush_spin.valueChanged.connect(
            lambda value: update_local("seam.brush_mm", value))
        row.addWidget(QLabel("Brush:"))
        row.addWidget(self.brush_spin)
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Take back the last push (Ctrl+Z).")
        self.btn_undo.clicked.connect(self.undo_push)
        self.btn_redo = QPushButton("Redo")
        self.btn_redo.setToolTip(
            "Put back the push just undone (Ctrl+Shift+Z or Ctrl+Y).")
        self.btn_redo.clicked.connect(self.redo_push)
        row.addStretch(1)
        for button in (self.btn_undo, self.btn_redo):
            button.setEnabled(False)
            row.addWidget(button)
        self.push_label = _note_label(
            "Drag across the seam to hand land from one province to the "
            "other. Esc leaves the brush.")
        return _box("Push border", row, self.push_label)

    # -- the pair -----------------------------------------------------------
    def load_pair(self, province_a: Province, province_b: Province,
                  status: str = "not cleaned") -> None:
        """Put a pair on the editor, as Import A and Import B would have."""
        self.set_push_mode(None)
        # A history belongs to one pair: undoing into another border's
        # geometry would hand this pair's land to strangers.
        self._undo, self._redo = [], []
        self._solve_marks = []
        self.province_a, self.province_b = province_a, province_b
        self.geom_a, self.geom_b = province_a.geometry, province_b.geometry
        self.path_a, self.path_b = province_a.path, province_b.path
        self.source_crs = province_a.source_crs or province_b.source_crs
        self._status_word = STATUS_TEXT.get(status, status)
        self.btn_grow_a.setText(f"Grow {province_a.label}")
        self.btn_grow_a.setToolTip(
            f"Brush: land under the stroke passes from {province_b.label} to "
            f"{province_a.label} (blue).")
        self.btn_grow_b.setText(f"Grow {province_b.label}")
        self.btn_grow_b.setToolTip(
            f"Brush: land under the stroke passes from {province_a.label} to "
            f"{province_b.label} (grey).")
        self.fit_before = None
        self._find_seam()
        self._invalidate()
        self._draw_before()
        if self.seam is not None:
            self.zoom_to_seam()
        self._sync_heading()

    def _sync_heading(self) -> None:
        if self.province_a is None:
            return
        edits = len(self._undo)
        edited = f", {edits} edit(s) not yet saved" if edits else ""
        self.heading.setText(
            f"{self.province_a.label} ⇄ {self.province_b.label} — "
            f"{self._status_word}{edited}")
        idle = not self._busy and self._thread is None
        self.btn_undo.setEnabled(bool(self._undo) and idle)
        self.btn_redo.setEnabled(bool(self._redo) and idle)

    def save_both(self) -> None:
        """Apply: hand the result back; the window decides where it goes."""
        if self.result is None:
            return
        self.applied.emit(self.result)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        super()._set_busy(busy)
        # Leaving mid-run would orphan the worker's result, and pushing would
        # edit the pair out from under it.
        for widget in (self.btn_back, self.btn_grow_a, self.btn_grow_b,
                       self.move_spin):
            widget.setEnabled(not busy)
        if busy:
            self.set_push_mode(None)
        self._sync_solve_button()
        self._sync_heading()

    def _sync_solve_button(self) -> None:
        if hasattr(self, "btn_solve"):            # not yet built on the first call
            self.btn_solve.setEnabled(not self._busy and self.seam is not None)

    # -- fit --------------------------------------------------------------
    def fit_params(self) -> FitParams:
        spins = self.fit_spins
        return FitParams(
            scale=self._params().scale,
            edge_loss_mm=spins["seam.edge_loss_mm"].value(),
            min_width_mm=spins["thin.width_mm"].value(),
            tab_mm=spins["seam.fit_tab_mm"].value())

    def _on_fit_setting(self, key: str, value: float) -> None:
        update_local(key, value)
        self._refit()

    def _refit(self, *_args) -> None:
        """Re-measure and redraw both panels, keeping the view where it is."""
        self.fit_before = self.fit_after = None
        self._redraw_keeping_view()

    def _ensure_fits(self) -> None:
        if not self.fit_check.isChecked():
            self.fit_before = self.fit_after = None
            return
        params = self.fit_params()
        if self.fit_before is None and self.seam is not None:
            self.fit_before = fit_report(self.geom_a, self.geom_b, self.seam,
                                         params)
        if self.fit_after is None and self.result is not None:
            self.fit_after = fit_report(self.result.a, self.result.b,
                                        self.result.seam, params)

    def _sync_fit_label(self) -> None:
        bits = []
        if self.fit_before is not None:
            bits.append(f"as edited: {self.fit_before.summary()}")
        if self.fit_after is not None:
            bits.append(f"simplified: {self.fit_after.summary()}")
        width = self.fit_params().required_width_mm
        self.fit_label.setText(
            f"Needs {width:g} mm — " + "; ".join(bits) if bits else "")
        self.btn_worst.setEnabled(self._worst_fit() is not None)

    def _worst_fit(self):
        for report in (self.fit_after, self.fit_before):
            if report is not None and report.worst() is not None:
                return report.worst()
        return None

    def zoom_to_worst_fit(self) -> None:
        worst = self._worst_fit()
        if worst is None:
            return
        # A fault is a millimetre across; frame it with room for the seam on
        # either side, or there is nothing to judge it against.
        minx, miny, maxx, maxy = worst.bounds
        grow = max(maxx - minx, maxy - miny, 2 * self._params().mpm) * 1.5
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        self._fit_view((cx - grow, cy - grow, cx + grow, cy + grow))

    def _draw_fit(self, ax, report) -> None:
        if report is None:
            return
        for patch in report.narrow:
            self._fill(ax, patch.geometry, NARROW_COLOUR, alpha=0.6)
        for tab in report.problem_tabs:
            colour = LOCK_COLOUR if tab.locks else FINGER_COLOUR
            self._fill(ax, tab.geometry, colour, alpha=0.35, edge=colour)

    @staticmethod
    def _fill(ax, geom, colour, alpha, edge="none") -> None:
        for poly in _polygons(geom):
            xs, ys = poly.exterior.xy
            ax.fill(xs, ys, facecolor=colour, edgecolor=edge, alpha=alpha,
                    linewidth=1.2, zorder=5)

    def _draw_before(self) -> None:
        super()._draw_before()
        self._ensure_fits()
        self._draw_fit(self.ax_before, self.fit_before)
        for moved in self._solve_marks:
            self._fill(self.ax_before, moved, SOLVED_COLOUR, alpha=0.25,
                       edge=SOLVED_COLOUR)
        if self._undo:
            self.ax_before.set_title(
                f"As edited — {len(self._undo)} edit(s), seam "
                f"{self.seam_stats().get('vertices', 0)} vertices", fontsize=11)
        self._sync_fit_label()
        self.canvas.draw_idle()

    def _draw_after(self) -> None:
        self.fit_after = None
        super()._draw_after()
        self._ensure_fits()
        self._draw_fit(self.ax_after, self.fit_after)
        self._sync_fit_label()
        self.canvas.draw_idle()

    def _invalidate(self) -> None:
        super()._invalidate()
        self.fit_after = None
        self._sync_solve_button()
        if hasattr(self, "fit_label"):          # not yet built on the first call
            self._sync_fit_label()

    def _redraw_keeping_view(self) -> None:
        """Both panels again; ``ax.clear`` would otherwise reset the view."""
        view = (self.ax_before.get_xlim(), self.ax_before.get_ylim())
        self._draw_before()
        if self.result is not None:
            self._draw_after()
        self._set_view(self.ax_before, *view)
        self.canvas.draw_idle()

    # -- push brush -------------------------------------------------------
    # Previewed as a fat round-capped line, as in the island editor: that is
    # exactly the region a circular brush sweeps, so the preview is honest, and
    # the real geometry is built once, on release.
    def set_push_mode(self, mode) -> None:
        self._cancel_stroke()
        self._push_mode = mode
        self.btn_grow_a.setChecked(mode == GROW_A)
        self.btn_grow_b.setChecked(mode == GROW_B)
        if mode is None:
            self.canvas.unsetCursor()
        else:
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def _escape(self) -> None:
        """Esc backs out one step: first the stroke, then the brush."""
        if self._stroke is not None:
            self._cancel_stroke()
        else:
            self.set_push_mode(None)

    def _cancel_stroke(self) -> None:
        self._stroke = None
        if self._preview is not None:
            try:
                self._preview.remove()
            except (ValueError, NotImplementedError):
                pass                       # its axes were already cleared
            self._preview = None
            self.canvas.draw_idle()

    def _brush_points(self, ax) -> float:
        """The brush width in typographic points, for the preview line."""
        x0, x1 = ax.get_xlim()
        px_per_m = ax.bbox.width / abs(x1 - x0) if x1 != x0 else 0.0
        width_m = self.brush_spin.value() * self._params().mpm
        return max(width_m * px_per_m * 72.0 / self.figure.dpi, 1.0)

    def _on_press(self, event) -> None:
        if (self._push_mode is None or self.mpl_toolbar.mode
                or event.button != 1 or self._thread is not None
                or event.inaxes not in (self.ax_before, self.ax_after)
                or event.xdata is None or self.geom_a is None):
            return
        self._stroke = [(event.xdata, event.ydata)]
        self._stroke_ax = event.inaxes
        colour = "#2E86C1" if self._push_mode == GROW_A else "#7F8C8D"
        self._preview, = event.inaxes.plot(
            [event.xdata], [event.ydata], color=colour, alpha=0.6,
            solid_capstyle="round", solid_joinstyle="round",
            linewidth=self._brush_points(event.inaxes), zorder=6)
        self.canvas.draw_idle()

    def _on_motion(self, event) -> None:
        if self._stroke is None or event.inaxes is not self._stroke_ax:
            return
        if event.xdata is None:
            return
        self._stroke.append((event.xdata, event.ydata))
        xs, ys = zip(*self._stroke)
        self._preview.set_data(xs, ys)
        self.canvas.draw_idle()

    def _on_release(self, event) -> None:
        if self._stroke is None:
            return
        points = self._stroke
        self._cancel_stroke()
        self.push(points)

    def push(self, points) -> bool:
        """
        Commit one stroke: move the border under it, in the current direction.

        Returns whether anything moved. A stroke that moves nothing stays out
        of the undo history.
        """
        if self._push_mode is None or self.geom_a is None:
            return False
        growing_a = self._push_mode == GROW_A
        grow, shrink = ((self.geom_a, self.geom_b) if growing_a
                        else (self.geom_b, self.geom_a))
        grown, shrunk = push_border(grow, shrink, points,
                                    self.brush_spin.value(),
                                    self._params().scale)
        if grown is grow:
            self.push_label.setText(
                "Nothing moved — the stroke has to cover land of the other "
                "province that touches the one growing.")
            return False
        moved_mm2 = (grown.area - grow.area) / self._params().mpm ** 2
        winner = self.province_a if growing_a else self.province_b
        note = f"moved {moved_mm2:.2f} mm² to {winner.label}"
        self._undo.append((self.geom_a, self.geom_b, note))
        self._redo = []                   # a new edit ends the old future
        self.geom_a, self.geom_b = ((grown, shrunk) if growing_a
                                    else (shrunk, grown))
        self.push_label.setText(f"{note[0].upper()}{note[1:]}.")
        self._after_edit()
        return True

    def undo_push(self) -> None:
        """Step back one push (Ctrl+Z)."""
        if not self._undo or self._busy or self._thread is not None:
            return
        geom_a, geom_b, note = self._undo.pop()
        self._redo.append((self.geom_a, self.geom_b, note))
        self.geom_a, self.geom_b = geom_a, geom_b
        self.push_label.setText(f"Undone: {note}.")
        self._after_edit()

    def redo_push(self) -> None:
        """Step forward again (Ctrl+Shift+Z or Ctrl+Y)."""
        if not self._redo or self._busy or self._thread is not None:
            return
        geom_a, geom_b, note = self._redo.pop()
        self._undo.append((self.geom_a, self.geom_b, note))
        self.geom_a, self.geom_b = geom_a, geom_b
        self.push_label.setText(f"Redone: {note}.")
        self._after_edit()

    def _after_edit(self, keep_marks: bool = False) -> None:
        """The pair changed: re-find the seam, re-measure, re-simplify."""
        if not keep_marks:
            self._solve_marks = []
        had_result = self.result is not None
        self._find_seam()
        self.fit_before = None
        self._invalidate()
        self._redraw_keeping_view()
        self._sync_heading()
        # Keep the right panel live: it was showing a simplification, so show
        # the simplification of what is there now.
        if had_result and self.seam is not None:
            self.run()

    # -- auto solve ---------------------------------------------------------
    def solve(self) -> None:
        """
        Repair what the fit check finds, on a worker thread.

        Works on the simplified pair when there is one -- that is what will be
        saved, and what the fit check on the right panel is judging -- and on
        the pair as imported otherwise.
        """
        if (self._busy or self._thread is not None
                or self._solve_thread is not None or self.seam is None):
            return
        if self.result is not None:
            base = (self.result.a, self.result.b)
        else:
            base = (self.geom_a, self.geom_b)
        self._set_busy(True)
        self.solve_label.setText("Solving …")
        self._solve_thread = QThread(self)
        self._solve_worker = SolveWorker(*base, self.fit_params(),
                                         self.move_spin.value())
        self._solve_worker.moveToThread(self._solve_thread)
        self._solve_thread.started.connect(self._solve_worker.run)
        self._solve_worker.progress.connect(self._on_solve_progress)
        self._solve_worker.done.connect(self._on_solved)
        self._solve_worker.failed.connect(self._on_solve_failed)
        self._solve_thread.start()

    def _on_solve_progress(self, round_no: int, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.solve_label.setText(
            f"Round {round_no}: problem {done + 1} of {total} …")

    def _on_solved(self, result) -> None:
        self._teardown_solve()
        self._set_busy(False)
        self.solve_label.setText(f"{result.summary()[0].upper()}"
                                 f"{result.summary()[1:]}.")
        if not result.changed:
            return
        note = f"auto solve, {result.summary()}"
        self._undo.append((self.geom_a, self.geom_b, note))
        self._redo = []
        self.geom_a, self.geom_b = result.a, result.b
        self._solve_marks = [fix.geometry for fix in result.fixed]
        self.push_label.setText(
            f"Auto solve moved land in {len(result.fixed)} place(s), outlined "
            "in green. Ctrl+Z takes it all back.")
        self._after_edit(keep_marks=True)

    def _on_solve_failed(self, message: str) -> None:
        self._teardown_solve()
        self._set_busy(False)
        self.solve_label.setText(f"Solve failed: {message}")

    def _teardown_solve(self) -> None:
        if self._solve_thread is not None:
            self._solve_thread.quit()
            self._solve_thread.wait()
        self._solve_thread = None
        self._solve_worker = None


class BorderEditor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1350, 900)

        # -- state ----------------------------------------------------------
        self.provinces: dict = {}           #: name -> Province
        self.borders: list = []             #: every shared seam, by key
        self.selected: list = []            #: up to two province names, in order
        self.save_dir = DEFAULT_SAVE_DIR
        self.ledger = BorderLedger.load(self.save_dir)
        self._press = None                  #: pointer position at button press
        #: Fit reports by border, kept while neither province nor the fit
        #: settings change -- cleaning one border must not re-measure thirty.
        self._fits: dict = {}

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_borders_page())
        self.seam_page = SeamPage()
        self.seam_page.back_requested.connect(self.show_borders)
        self.seam_page.applied.connect(self.apply_result)
        self.stack.addWidget(self.seam_page)
        self.setCentralWidget(self.stack)

        self._redraw()
        self._refresh_lists()
        self._sync()

    # -- building the first page -------------------------------------------
    def _build_borders_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        toolbar = QHBoxLayout()
        self.btn_folder = QPushButton("Import Folder …")
        self.btn_folder.setToolTip("Load every province in a folder.")
        self.btn_folder.clicked.connect(self.import_folder)
        self.btn_files = QPushButton("Import Files …")
        self.btn_files.setToolTip("Add province files to the set.")
        self.btn_files.clicked.connect(self.import_files)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_all)
        self.save_label = QLabel()
        self.save_label.setStyleSheet("color:#555;")
        self.btn_save_dir = QPushButton("Change …")
        self.btn_save_dir.setToolTip(
            "Where cleaned provinces are written, and where the record of "
            "which borders are cleaned is kept.")
        self.btn_save_dir.clicked.connect(self.choose_save_dir)
        provinces_row = QHBoxLayout()
        for widget in (self.btn_folder, self.btn_files, self.btn_clear):
            provinces_row.addWidget(widget)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_label, stretch=1)
        save_row.addWidget(self.btn_save_dir)
        toolbar.addWidget(_box("Provinces", provinces_row))
        toolbar.addWidget(_box("Save to", save_row), stretch=1)
        root.addLayout(toolbar)

        body = QHBoxLayout()
        map_box = QVBoxLayout()
        self.figure = Figure(figsize=(8, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self.figure.subplots_adjust(left=0.01, right=0.99, bottom=0.01,
                                    top=0.95)
        self.mpl_toolbar = NavigationToolbar(self.canvas, page)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        map_box.addWidget(self.mpl_toolbar)
        map_box.addWidget(self.canvas, stretch=1)
        self.map_box = QGroupBox("Map")
        self.map_box.setLayout(map_box)
        body.addWidget(self.map_box, stretch=1)

        side = QVBoxLayout()
        self.list_todo = QListWidget()
        self.list_done = QListWidget()
        self.box_todo = QGroupBox()
        self.box_done = QGroupBox()
        for box, widget in ((self.box_todo, self.list_todo),
                            (self.box_done, self.list_done)):
            widget.itemClicked.connect(self._on_item_clicked)
            widget.itemDoubleClicked.connect(self._on_item_double_clicked)
            inner = QVBoxLayout(box)
            inner.addWidget(widget)
            side.addWidget(box, stretch=1)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        self.btn_edit = QPushButton("Edit Border →")
        self.btn_edit.setToolTip(
            "Open the border the two selected provinces share.")
        self.btn_edit.clicked.connect(self.open_editor)
        side.addWidget(_box("Selected border", self.selection_label,
                            self.btn_edit))
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(340)
        body.addWidget(side_widget)
        root.addLayout(body, stretch=1)

        self.status = QLabel()
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)
        return page

    # -- the borders and their state ---------------------------------------
    def status_of(self, border) -> str:
        return self.ledger.status(border)

    def cleaned(self) -> list:
        return [b for b in self.borders if self.status_of(b) == "cleaned"]

    def not_cleaned(self) -> list:
        return [b for b in self.borders if self.status_of(b) != "cleaned"]

    def border_between(self, name_a, name_b):
        return next((b for b in self.borders
                     if b.involves(name_a) and b.involves(name_b)
                     and name_a != name_b), None)

    def current_border(self):
        """The border the two selected provinces share, if they share one."""
        if len(self.selected) != 2:
            return None
        return self.border_between(*self.selected)

    def _find_borders(self) -> None:
        self.borders = find_borders(
            {name: p.geometry for name, p in self.provinces.items()})

    def fit_of(self, border):
        """
        Will this border's pieces mesh? Measured with the fit settings in the
        config -- the ones the edit page saves as they are turned -- and kept
        until a province or a setting changes.
        """
        params = FitParams.from_config()
        geom_a = self.provinces[border.a].geometry
        geom_b = self.provinces[border.b].geometry
        settings = (params.edge_loss_mm, params.min_width_mm,
                    params.tab_mm, params.scale)
        cached = self._fits.get(border.key)
        # The geometries are held in the cache entry, so their ids cannot be
        # handed on to new objects while the entry still names them.
        if cached is not None and cached[0] is geom_a and cached[1] is geom_b                 and cached[2] == settings:
            return cached[3]
        report = fit_report(geom_a, geom_b, border.seam, params)
        self._fits[border.key] = (geom_a, geom_b, settings, report)
        return report

    # -- import --------------------------------------------------------------
    def import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Import every province in a folder",
            _dir_or_cwd(DEFAULT_LOAD_DIR))
        if not folder:
            return
        paths = geojson_in(folder)
        if not paths:
            QMessageBox.information(self, "Nothing to import",
                                    "No .geojson or .json files in that folder.")
            return
        self.load(paths)

    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import province GeoJSON (any number)",
            _dir_or_cwd(DEFAULT_LOAD_DIR), GEOJSON_FILTER)
        if paths:
            self.load(paths)

    def _resolve(self, path: Path):
        """
        The file to actually read for ``path``, and whether it is a resumption.

        A province already saved into the save folder carries the borders
        cleaned last time; loading the original instead would quietly throw
        them away and put every one back on the Not cleaned list.
        """
        saved = Path(self.save_dir) / path.name
        if saved.exists() and saved.resolve() != path.resolve():
            return saved, True
        return path, False

    def load(self, paths) -> None:
        """Add every readable file, reporting the ones that were not."""
        first = not self.provinces
        loaded, resumed, failed = 0, 0, []
        for raw in paths:
            path, is_resumed = self._resolve(Path(raw))
            try:
                province = read_province(path, is_resumed)
            except Exception as exc:
                failed.append(f"{path.name}: {exc}")
                continue
            if province is None:
                failed.append(f"{path.name}: no land")
                continue
            self.provinces[province.name] = province
            loaded += 1
            resumed += is_resumed

        self._find_borders()
        self.selected = [n for n in self.selected if n in self.provinces]
        self._redraw(preserve_view=not first)
        self._refresh_lists()
        self._sync()

        note = f"Loaded {loaded} province(s)"
        if resumed:
            note += (f", {resumed} of them from the save folder where they "
                     "were already cleaned")
        if failed:
            note += f"; {len(failed)} failed ({failed[0]})"
        self._set_status(f"{note}. ")

    def clear_all(self) -> None:
        self.provinces = {}
        self.borders = []
        self.selected = []
        self._redraw()
        self._refresh_lists()
        self._sync()
        self._set_status()

    def choose_save_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Save cleaned provinces into …", _dir_or_cwd(self.save_dir))
        if folder:
            self.set_save_dir(Path(folder))

    def set_save_dir(self, folder: Path) -> None:
        """A new save folder brings its own ledger with it."""
        self.save_dir = Path(folder)
        self.ledger = BorderLedger.load(self.save_dir)
        self._redraw(preserve_view=True)
        self._refresh_lists()
        self._sync()
        self._set_status(f"Saving into {self.save_dir.name}. ")

    # -- selection ----------------------------------------------------------
    def toggle_province(self, name: str) -> None:
        """Click a province: select it, or drop it if it was selected.

        Two at most -- a border has two sides -- so a third click replaces the
        older of the two, which is what reaching for the next neighbour wants.
        """
        if name in self.selected:
            self.selected.remove(name)
        else:
            self.selected.append(name)
            del self.selected[:-2]
        self._after_selection()

    def select_border(self, border) -> None:
        self.selected = [border.a, border.b]
        self._after_selection()

    def _after_selection(self) -> None:
        self._redraw(preserve_view=True)
        self._mark_current_in_lists()
        self._sync()
        self._set_status()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        border = self._border_for(item)
        if border is not None:
            self.select_border(border)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        border = self._border_for(item)
        if border is not None:
            self.select_border(border)
            self.open_editor()

    def _border_for(self, item):
        key = item.data(Qt.UserRole)
        return next((b for b in self.borders if b.key == key), None)

    # -- the two screens ------------------------------------------------------
    def open_editor(self) -> None:
        border = self.current_border()
        if border is None:
            return
        self.seam_page.load_pair(self.provinces[border.a],
                                 self.provinces[border.b],
                                 self.status_of(border))
        self.stack.setCurrentIndex(PAGE_EDIT)
        self._update_title()

    def show_borders(self) -> None:
        self.stack.setCurrentIndex(PAGE_BORDERS)
        # The fit settings may have been turned on the edit page.
        self._refresh_lists()
        self._sync()
        self._update_title()

    def apply_result(self, result) -> None:
        """
        Save the cleaned pair, record the border, and go back to the list.

        The two files are read back after writing and the ledger records the
        seam *they* share, so what the ledger checks against is exactly what a
        later session will load -- not the in-memory result, which the round
        trip through the source CRS moves by a fraction of a millimetre.
        """
        page = self.seam_page
        pair = (page.province_a, page.province_b)
        try:
            written = [write_province(geom, province.source_crs,
                                      Path(self.save_dir) / province.path.name)
                       for geom, province in zip((result.a, result.b), pair)]
            for out in written:
                province = read_province(out, resumed=True)
                self.provinces[province.name] = province
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        self._find_borders()
        border = self.border_between(pair[0].name, pair[1].name)
        if border is not None:
            # Record the seam the files now carry, keeping the result's
            # before/after figures.
            entry = self.ledger.record(border, result, page._params())
            entry["seam_wkt"] = border.seam.wkt
            try:
                self.ledger.save()
            except OSError as exc:
                QMessageBox.critical(self, "Could not save the ledger", str(exc))

        self.show_borders()
        self._redraw(preserve_view=True)
        self._refresh_lists()
        self._sync()
        self._set_status(
            f"Cleaned {pair[0].label} ⇄ {pair[1].label}: "
            f"{result.vertices_before} → {result.vertices_after} vertices, "
            f"saved both into {Path(self.save_dir).name}. ")

    # -- lists ----------------------------------------------------------------
    def _item_text(self, border) -> str:
        names = (f"{self.provinces[border.a].label} — "
                 f"{self.provinces[border.b].label}")
        status = self.status_of(border)
        fit = self.fit_of(border).summary()
        if status == "cleaned":
            entry = self.ledger.entry(border)
            return (f"{names}   {entry.get('vertices_before', '?')} → "
                    f"{border.vertices} vertices · {fit}")
        if status == "changed":
            return (f"{names}   {border.vertices} vertices · changed since "
                    f"cleaned · {fit}")
        return f"{names}   {border.vertices} vertices · {fit}"

    def _refresh_lists(self) -> None:
        for widget, borders in ((self.list_todo, self.not_cleaned()),
                                (self.list_done, self.cleaned())):
            widget.blockSignals(True)
            widget.clear()
            for border in borders:
                item = QListWidgetItem(self._item_text(border))
                item.setData(Qt.UserRole, border.key)
                if self.fit_of(border).problems:
                    item.setForeground(QColor(LOCK_COLOUR)
                                       if self.fit_of(border).locking
                                       else QColor(NARROW_COLOUR))
                widget.addItem(item)
            widget.blockSignals(False)
        self.box_todo.setTitle(f"Not cleaned ({self.list_todo.count()})")
        self.box_done.setTitle(f"Cleaned ({self.list_done.count()})")
        self.save_label.setText(str(self.save_dir))
        self._mark_current_in_lists()
        self._update_title()

    def _mark_current_in_lists(self) -> None:
        border = self.current_border()
        key = border.key if border is not None else None
        for widget in (self.list_todo, self.list_done):
            widget.blockSignals(True)
            widget.clearSelection()
            for row in range(widget.count()):
                item = widget.item(row)
                if item.data(Qt.UserRole) == key:
                    item.setSelected(True)
                    widget.scrollToItem(item)
            widget.blockSignals(False)

    # -- map ------------------------------------------------------------------
    def _redraw(self, preserve_view: bool = False) -> None:
        view = (self.ax.get_xlim(), self.ax.get_ylim()) if preserve_view \
            else None
        self.ax.clear()
        self.ax.set_aspect("equal", adjustable="datalim")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        if not self.provinces:
            self.ax.set_title("Import provinces to find their borders",
                              fontsize=11)
            self.canvas.draw_idle()
            return
        self.ax.set_title("Click provinces to select a pair — red seams are "
                          "not cleaned, green are", fontsize=10)

        for name, province in self.provinces.items():
            colour = SELECT_COLOUR if name in self.selected else PROVINCE_COLOUR
            rings = [list(poly.exterior.coords)
                     for poly in _polygons(province.geometry)]
            self.ax.add_collection(PolyCollection(
                rings, facecolor=colour, edgecolor=EDGE_COLOUR, linewidth=0.4,
                zorder=1))
            at = province.geometry.representative_point()
            self.ax.annotate(province.label, (at.x, at.y), fontsize=7,
                             color=LABEL_COLOUR, ha="center", va="center",
                             zorder=5)

        current = self.current_border()
        for border in self.borders:
            colour = (CLEANED_COLOUR if self.status_of(border) == "cleaned"
                      else NOT_CLEANED_COLOUR)
            width = SEAM_WIDTH_SELECTED if border is current else SEAM_WIDTH
            for part in border.seam.geoms:
                xs, ys = part.xy
                self.ax.plot(xs, ys, color=colour, linewidth=width,
                             solid_capstyle="round", zorder=3)

        if view is not None:
            self.ax.set_xlim(view[0])
            self.ax.set_ylim(view[1])
        else:
            minx, miny, maxx, maxy = unary_union(
                [p.geometry.envelope for p in self.provinces.values()]).bounds
            pad = 0.03 * max(maxx - minx, maxy - miny)
            self.ax.set_xlim(minx - pad, maxx + pad)
            self.ax.set_ylim(miny - pad, maxy + pad)
        self.canvas.draw_idle()

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        if event.inaxes is not self.ax or event.xdata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self.ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        self.ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    def _on_press(self, event) -> None:
        self._press = (event.x, event.y) if event.button == 1 else None

    def _on_release(self, event) -> None:
        """A left click with no drag, outside pan/zoom mode, picks a province."""
        press, self._press = self._press, None
        if (press is None or event.button != 1 or event.inaxes is not self.ax
                or event.xdata is None or self.mpl_toolbar.mode):
            return
        if abs(event.x - press[0]) + abs(event.y - press[1]) > CLICK_SLOP_PX:
            return
        name = self.province_at(event.xdata, event.ydata)
        if name is not None:
            self.toggle_province(name)

    def province_at(self, x: float, y: float):
        point = Point(x, y)
        return next((name for name, p in self.provinces.items()
                     if p.geometry.contains(point)), None)

    # -- status -----------------------------------------------------------
    def _sync(self) -> None:
        self.btn_edit.setEnabled(self.current_border() is not None)
        self.btn_clear.setEnabled(bool(self.provinces))
        self.selection_label.setText(self._selection_text())

    def _selection_text(self) -> str:
        if not self.selected:
            return "Select two neighbouring provinces, or a border in a list."
        labels = [self.provinces[n].label for n in self.selected]
        if len(labels) == 1:
            return f"{labels[0]} selected — pick a neighbour."
        border = self.current_border()
        if border is None:
            return f"{labels[0]} and {labels[1]} do not share a border."
        return (f"{labels[0]} ⇄ {labels[1]}: {border.vertices} vertices, "
                f"{STATUS_TEXT[self.status_of(border)]}; "
                f"{self.fit_of(border).summary()}.")

    def _set_status(self, prefix: str = "") -> None:
        if not self.provinces:
            self.status.setText(f"{prefix}No provinces loaded.")
            return
        self.status.setText(
            f"{prefix}{len(self.provinces)} province(s), "
            f"{len(self.borders)} border(s): {len(self.cleaned())} cleaned, "
            f"{len(self.not_cleaned())} to go.")

    def _update_title(self) -> None:
        if self.stack.currentIndex() == PAGE_EDIT:
            page = self.seam_page
            self.setWindowTitle(
                f"Border Editor — {page.province_a.label} ⇄ "
                f"{page.province_b.label}")
            return
        if not self.provinces:
            self.setWindowTitle("Border Editor — (nothing loaded)")
            return
        self.setWindowTitle(
            f"Border Editor — {len(self.cleaned())} of {len(self.borders)} "
            "borders cleaned")

    def closeEvent(self, event) -> None:
        self.seam_page._teardown_thread()
        self.seam_page._teardown_solve()
        super().closeEvent(event)


def _polygons(geometry) -> list:
    """The polygons of a geometry, whatever kind it is."""
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [g for g in getattr(geometry, "geoms", [])
            if g.geom_type == "Polygon"]


def main() -> int:
    app = QApplication(sys.argv)
    window = BorderEditor()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
