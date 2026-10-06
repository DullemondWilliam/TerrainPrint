"""
UI_join_editor.py

Join every province on the map to its neighbours with butterfly keys.

``harnesses/UI_butterfly_keys.py`` keys one pair of STLs picked by hand.  A map
has a dozen borders and every province sits on several, so this window holds
the whole set: it finds every border between the loaded pieces, lets the keys
be placed along each, and cuts each province once with every slot it needs.

Workflow
--------
1. **Provinces …** / **STLs …** -- the bordered GeoJSONs (what the Border
   Editor saved) and the terrain STLs cut from them (what the terrain editor
   saved, with their ``.frame.json``).  Files are paired by name, ignoring the
   ``_simplified`` / ``_trimmed`` / ``_keyed`` endings the tools add.
   **Load** reads them off the UI thread.  Anything that could not be paired
   or placed is listed under the borders, with the reason.
2. **Key (print-mm)** -- the key's size, one size for the whole map so that any
   key fits any slot: *Neck* (width at the seam), *Mouth* (width at each end),
   *Reach* (seam to each end), *Depth* (slot depth into the base, which is
   ``print.dovetail_mm``), *Clearance* (slot oversize per side), *Spacing*
   (Auto Place's pitch) and *Spares* (extra keys on the plates).  Saved to the
   local config as they are turned, and every key re-checked at once.
3. **Place the keys** -- pick a border in the list or click near its seam.
   **Auto Place** spaces keys along it (**Auto Place All** along every border);
   on the selected border, **click** near the seam to add a key, click a key
   to remove it, **drag** a key to slide it along the seam.  Refused keys are
   red, with the reason on the status line when the border is selected.
   **Undo** / **Redo** (Ctrl+Z / Ctrl+Y) step through placements.
4. **Cut and Save All** -- slot every piece and write, into the save folder,
   ``<province>_keyed.stl`` with its frame, ``butterfly_keys_01.stl`` … (every
   key plus the spares, on bed-sized plates) and ``join_plan.json``.  A piece
   whose slots have not changed since the last cut is not cut again.
5. **3D** -- the selected border's two pieces, cropped round the seam, with
   their keys in the slots, seen from below; or a key plate.

Reopening: the save folder's ``join_plan.json`` is read on every load, so the
keys placed last time come back -- snapped to today's seams, and flagged if a
border has moved since.

A border is drawn grey with no keys, green when every key passes, amber when
some are refused, and red when an STL on it is out of date -- cut from a
province the Border Editor has changed since.  Its keys are refused until the
STL is rebuilt.

Run with:  python UI_join_editor.py

Reuses ``lib.LIB_province_joins``, ``lib.LIB_butterfly_keys``,
``lib.LIB_border_ledger`` and ``lib.LIB_seam_fit``.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import vispy
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.collections import PolyCollection
from matplotlib.figure import Figure
from shapely.geometry import Point
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

from lib.LIB_border_ledger import BorderLedger
from lib.LIB_butterfly_keys import KeyParams, load_key_params, snap
from lib.LIB_project_config import get as cfg
from lib.LIB_project_config import load_files, settings_files, update_local
from lib.LIB_province_joins import (PLAN_NAME, JoinPlan, auto_plan,
                                    check_plan, cut_all, export, find_joins,
                                    join_assembly, key_point, load_pieces,
                                    pair_folders)
from lib.LIB_seam_fit import fit_report, load_fit_params

_ROOT = Path(__file__).resolve().parent
DEFAULT_GEOJSON_DIR = _ROOT / cfg("paths.provinces_bordered")
DEFAULT_STL_DIR = _ROOT / cfg("paths.stl")
DEFAULT_SAVE_DIR = _ROOT / cfg("paths.stl_keyed")

PROVINCE_COLOUR = "#D6EAF8"
EDGE_COLOUR = "#1B4F72"
LABEL_COLOUR = "#2C3E50"
#: Border states, as seams on the map and as text in the list.
STATE_COLOURS = {"none": "#95A5A6", "ok": "#27AE60", "refused": "#E67E22",
                 "stale": "#C0392B"}
SLOT_OK = "#27AE60"
SLOT_REFUSED = "#E6787A"
DRAG_COLOUR = "#D35400"
BG_COLOUR = "#FDFEFE"
KEY_RGBA = (0.95, 0.61, 0.07, 1.0)
A_RGBA = (0.68, 0.84, 0.95, 1.0)
B_RGBA = (0.80, 0.81, 0.82, 1.0)

SEAM_WIDTH = 2.2
SEAM_WIDTH_SELECTED = 4.5
#: A click this many pixels from a seam is on it.
PICK_PX = 12
#: How far the pointer may move between press and release and still be a
#: click rather than a pan or a drag, in pixels.
CLICK_SLOP_PX = 5
#: Padding round a border when framing it, as a fraction of its extent.
ZOOM_PAD = 0.08
#: The 3D camera's depth range (see UI_butterfly_keys: the key sits 0.1 mm
#: under its slot ceiling, and the default range cannot resolve that).
DEPTH_RANGE_MM = 1000.0
#: The most problems listed under the borders; the rest are counted.
MAX_PROBLEMS_SHOWN = 6

#: The key settings: ``(field, config key, label, lo, hi, decimals, tip)``.
KEY_SETTINGS = (
    ("neck_mm", "keys.neck_mm", "Neck:", 0.2, 30.0, 2,
     "Key width at the seam -- the waist."),
    ("mouth_mm", "keys.mouth_mm", "Mouth:", 0.4, 40.0, 2,
     "Key width at each end. Wider than the neck, or nothing is captured\n"
     "behind the waist and the pieces slide apart."),
    ("reach_mm", "keys.reach_mm", "Reach:", 0.5, 40.0, 2,
     "How far each half reaches into its province from the seam."),
    ("depth_mm", "print.dovetail_mm", "Depth:", 0.2, 10.0, 2,
     "Slot depth into each piece's underside. Must stay under the terrain\n"
     "base (terrain.base_mm), or the slot opens on the terrain side."),
    ("clearance_mm", "keys.clearance_mm", "Clearance:", 0.0, 2.0, 2,
     "Slot oversize per side, so a printed key presses in."),
    ("spacing_mm", "keys.spacing_mm", "Spacing:", 2.0, 300.0, 1,
     "Pitch Auto Place spaces keys at along a border."),
)

VIEW_BORDER = "Selected border"


@dataclass
class Session:
    """What a load produces: the pieces, their borders, and what went wrong."""
    pieces: dict = field(default_factory=dict)
    joins: list = field(default_factory=list)
    fits: dict = field(default_factory=dict)      #: border key -> FitReport
    problems: list = field(default_factory=list)


def load_session(geojson_dir, stl_dir, params, config=None, fit_params=None,
                 progress=None) -> Session:
    """Pair, load and measure everything in two folders (the Load button)."""
    pairs, unpaired = pair_folders(geojson_dir, stl_dir)
    pieces, problems = load_pieces(pairs, params, config, progress)
    ledger = BorderLedger.load(geojson_dir)
    joins = find_joins(pieces, params, ledger)
    fits = {}
    for i, join in enumerate(joins):
        if progress is not None:
            progress(i, len(joins), f"fit check {join.label}")
        fits[join.key] = fit_report(pieces[join.a].geom, pieces[join.b].geom,
                                    join.seam, fit_params)
    return Session(pieces, joins, fits, unpaired + problems)


def cut_and_export(plan, pieces, joins, params, fits, base_mm, previous,
                   out_dir, progress=None):
    """Cut every piece and write the lot (the Cut and Save All button)."""
    result = cut_all(plan, pieces, joins, params, fits=fits, base_mm=base_mm,
                     previous=previous, progress=progress)
    return result, export(result, pieces, out_dir, plan)


class Task(QObject):
    """Runs one lib call off the UI thread, reporting its progress."""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)       # done, total, what

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self) -> None:
        try:
            result = self._fn(*self._args, progress=self.progress.emit)
        except Exception as exc:          # any geometry/mesh failure reaches the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


def _box(title: str, *rows) -> QGroupBox:
    box = QGroupBox(title)
    column = QVBoxLayout(box)
    column.setContentsMargins(8, 4, 8, 6)
    for row in rows:
        if isinstance(row, QWidget):
            column.addWidget(row)
        else:
            column.addLayout(row)
    return box


def _note_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("color:#555;")
    label.setWordWrap(True)
    return label


def _dir_or_cwd(path) -> str:
    return str(path if path is not None and Path(path).is_dir() else Path.cwd())


def _polygons(geometry) -> list:
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [g for g in getattr(geometry, "geoms", [])
            if g.geom_type == "Polygon"]


class JoinEditor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1500, 920)

        # -- state ----------------------------------------------------------
        self.geojson_dir = DEFAULT_GEOJSON_DIR if DEFAULT_GEOJSON_DIR.is_dir() \
            else None
        self.stl_dir = DEFAULT_STL_DIR if DEFAULT_STL_DIR.is_dir() else None
        self.save_dir = DEFAULT_SAVE_DIR
        self.pieces: dict = {}
        self.joins: list = []
        self.fits: dict = {}
        self.problems: list = []
        self.plan = JoinPlan()
        self.checks: dict = {}               #: border key -> [KeyCheck]
        self.params_error = None             #: why the key cannot be used
        self.selected = None                 #: border key
        self.result = None                   #: last JoinResult
        self.result_current = False          #: the plan has not moved since
        self._undo: list = []
        self._redo: list = []
        self._press = None                   #: (pixels, data) at button press
        self._drag = None                    #: index of the key being dragged
        self._drag_artist = None
        self._thread: QThread | None = None
        self._task: Task | None = None
        self._busy = False
        self._solid_meshes = []

        central = QWidget()
        root = QVBoxLayout(central)
        root.addLayout(self._build_toolbar())

        body = QHBoxLayout()
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_map())
        splitter.addWidget(self._build_3d())
        splitter.setSizes([700, 420])
        body.addWidget(splitter, stretch=1)
        body.addWidget(self._build_side())
        root.addLayout(body, stretch=1)

        bottom = QHBoxLayout()
        self.btn_cut = QPushButton("Cut and Save All")
        self.btn_cut.setToolTip(
            "Cut every piece's slots and write the keyed STLs, the key plates\n"
            "and the plan into the save folder.")
        self.btn_cut.clicked.connect(self.cut_and_save)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = _note_label()
        bottom.addWidget(self.btn_cut)
        bottom.addWidget(self.progress)
        bottom.addWidget(self.status, stretch=1)
        root.addLayout(bottom)
        self.setCentralWidget(central)

        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)

        self.recheck()
        self._set_status()

    # -- building -------------------------------------------------------------
    def _build_toolbar(self) -> QHBoxLayout:
        self.geojson_label = _note_label()
        self.stl_label = _note_label()
        self.btn_geojson = QPushButton("Provinces …")
        self.btn_geojson.setToolTip(
            "The folder of bordered province GeoJSONs the Border Editor saved.")
        self.btn_geojson.clicked.connect(self.choose_geojson_dir)
        self.btn_stl = QPushButton("STLs …")
        self.btn_stl.setToolTip(
            "The folder of province terrain STLs, each with its .frame.json.")
        self.btn_stl.clicked.connect(self.choose_stl_dir)
        self.btn_load = QPushButton("Load")
        self.btn_load.clicked.connect(self.start_load)
        inputs = QGridLayout()
        inputs.addWidget(self.btn_geojson, 0, 0)
        inputs.addWidget(self.geojson_label, 0, 1)
        inputs.addWidget(self.btn_stl, 1, 0)
        inputs.addWidget(self.stl_label, 1, 1)
        inputs.addWidget(self.btn_load, 0, 2, 2, 1)
        inputs.setColumnStretch(1, 1)

        self.save_label = _note_label()
        self.btn_save_dir = QPushButton("Change …")
        self.btn_save_dir.setToolTip(
            "Where the keyed STLs, the key plates and the plan are written; the\n"
            "plan found here is reopened on every load.")
        self.btn_save_dir.clicked.connect(self.choose_save_dir)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_label, stretch=1)
        save_row.addWidget(self.btn_save_dir)

        toolbar = QHBoxLayout()
        toolbar.addWidget(_box("Inputs", inputs), stretch=2)
        toolbar.addWidget(_box("Save to", save_row), stretch=1)
        return toolbar

    def _build_map(self) -> QWidget:
        self.figure = Figure(figsize=(8, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self.figure.subplots_adjust(left=0.01, right=0.99, bottom=0.01,
                                    top=0.95)
        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        column = QVBoxLayout()
        column.addWidget(self.mpl_toolbar)
        column.addWidget(self.canvas, stretch=1)
        box = QGroupBox("Map")
        box.setLayout(column)
        return box

    def _build_3d(self) -> QWidget:
        self.solid_canvas = scene.SceneCanvas(keys="interactive",
                                              bgcolor=BG_COLOUR, show=False)
        self.solid_view = self.solid_canvas.central_widget.add_view()
        self.solid_view.camera = scene.TurntableCamera(fov=0, elevation=-60,
                                                       azimuth=-55)
        self.solid_view.camera.depth_value = DEPTH_RANGE_MM
        self.view_combo = QComboBox()
        self.view_combo.addItem(VIEW_BORDER)
        self.btn_show_3d = QPushButton("Show")
        self.btn_show_3d.setToolTip(
            "Draw the selected border's pieces and keys as last cut, or a key "
            "plate.")
        self.btn_show_3d.clicked.connect(self.show_3d)
        row = QHBoxLayout()
        row.addWidget(self.view_combo, stretch=1)
        row.addWidget(self.btn_show_3d)
        self.solid_title = _note_label("Cut to see the result in 3D.")
        column = QVBoxLayout()
        column.addLayout(row)
        column.addWidget(self.solid_title)
        column.addWidget(self.solid_canvas.native, stretch=1)
        box = QGroupBox("3D")
        box.setLayout(column)
        return box

    def _build_side(self) -> QWidget:
        self.border_list = QListWidget()
        self.border_list.itemClicked.connect(self._on_item_clicked)
        self.problems_label = _note_label()
        self.problems_label.setStyleSheet(
            f"color:{STATE_COLOURS['stale']};")
        self.btn_auto = QPushButton("Auto Place")
        self.btn_auto.setToolTip(
            "Replace the selected border's keys with one every Spacing mm.")
        self.btn_auto.clicked.connect(self.auto_place_selected)
        self.btn_auto_all = QPushButton("Auto Place All")
        self.btn_auto_all.setToolTip(
            "Replace the keys on every border with one every Spacing mm.")
        self.btn_auto_all.clicked.connect(self.auto_place_all)
        self.btn_clear = QPushButton("Clear Keys")
        self.btn_clear.clicked.connect(self.clear_selected)
        self.btn_zoom = QPushButton("Zoom to Border")
        self.btn_zoom.clicked.connect(self.zoom_to_border)
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("Redo")
        self.btn_redo.clicked.connect(self.redo)
        place_row = QHBoxLayout()
        for widget in (self.btn_auto, self.btn_auto_all, self.btn_clear):
            place_row.addWidget(widget)
        view_row = QHBoxLayout()
        for widget in (self.btn_zoom, self.btn_undo, self.btn_redo):
            view_row.addWidget(widget)
        self.selection_label = _note_label()
        borders_box = _box("Borders", self.border_list, self.problems_label,
                           place_row, view_row, self.selection_label)

        defaults = load_key_params(*settings_files())
        grid = QGridLayout()
        self.key_spins = {}
        for i, (name, key, label, lo, hi, decimals, tip) in \
                enumerate(KEY_SETTINGS):
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setRange(lo, hi)
            spin.setSingleStep(0.1 if decimals > 1 else 1.0)
            spin.setSuffix(" mm")
            spin.setValue(getattr(defaults, name))
            spin.setToolTip(tip)
            spin.valueChanged.connect(
                lambda value, key=key: self._on_key_setting(key, value))
            self.key_spins[name] = spin
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(spin, i // 2, (i % 2) * 2 + 1)
        self.spares_spin = QSpinBox()
        self.spares_spin.setRange(0, 100)
        self.spares_spin.setValue(int(defaults.spares))
        self.spares_spin.setToolTip(
            "Extra keys printed on the plates: a small key is easy to lose.")
        self.spares_spin.valueChanged.connect(
            lambda value: self._on_key_setting("keys.spares", value))
        row = len(KEY_SETTINGS) // 2
        grid.addWidget(QLabel("Spares:"), row, 0)
        grid.addWidget(self.spares_spin, row, 1)
        self.key_label = _note_label()
        key_box = _box("Key (print-mm)", grid, self.key_label)

        side = QVBoxLayout()
        side.addWidget(borders_box, stretch=1)
        side.addWidget(key_box)
        widget = QWidget()
        widget.setLayout(side)
        widget.setFixedWidth(380)
        return widget

    # -- the key ---------------------------------------------------------------
    def params(self) -> KeyParams:
        """The key as the spin boxes have it (scale from the config)."""
        values = {name: spin.value() for name, spin in self.key_spins.items()}
        values["spares"] = self.spares_spin.value()
        return load_key_params(*settings_files(), **values)

    def base_mm(self) -> float:
        return cfg("terrain.base_mm",
                   config=load_files(*settings_files()))

    def _on_key_setting(self, key: str, value) -> None:
        update_local(key, value)
        self.recheck()

    def _params_problem(self, params):
        try:
            params.validate()
        except ValueError as exc:
            return str(exc)
        if params.depth_mm >= self.base_mm():
            return (f"Depth {params.depth_mm:g} mm would cut through the "
                    f"{self.base_mm():g} mm terrain base.")
        return None

    # -- checking --------------------------------------------------------------
    def recheck(self) -> None:
        """Re-judge every key against the current key size, and redraw."""
        params = self.params()
        self.params_error = self._params_problem(params)
        if self.params_error is None and self.joins:
            self.checks = check_plan(self.plan, self.pieces, self.joins,
                                     params, self.fits)
        else:
            self.checks = {}
        self.result_current = False
        self._redraw(preserve_view=True)
        self._refresh_list()
        self._sync()

    def state_of(self, join) -> str:
        if join.stale:
            return "stale"
        checks = self.checks.get(join.key, [])
        if not checks:
            return "none"
        return "refused" if any(not c.ok for c in checks) else "ok"

    def key_count(self) -> int:
        return sum(c.ok for checks in self.checks.values() for c in checks)

    def current_join(self):
        return next((j for j in self.joins if j.key == self.selected), None)

    # -- folders and loading ---------------------------------------------------
    def choose_geojson_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Bordered province GeoJSONs", _dir_or_cwd(self.geojson_dir))
        if folder:
            self.geojson_dir = Path(folder)
            self._sync()

    def choose_stl_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Province terrain STLs", _dir_or_cwd(self.stl_dir))
        if folder:
            self.stl_dir = Path(folder)
            self._sync()

    def choose_save_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Save keyed pieces and key plates into …",
            _dir_or_cwd(self.save_dir if Path(self.save_dir).is_dir()
                        else self.save_dir.parent))
        if folder:
            self.save_dir = Path(folder)
            self._sync()

    def start_load(self) -> None:
        if self.geojson_dir is None or self.stl_dir is None or self._busy:
            return
        config = load_files(*settings_files())
        self._launch(Task(load_session, self.geojson_dir, self.stl_dir,
                          self.params(), config,
                          load_fit_params(*settings_files())),
                     self._on_loaded, "Loading")

    def _on_loaded(self, session) -> None:
        self._finish_task()
        self.apply_session(session)

    def apply_session(self, session: Session) -> None:
        """Take a loaded set of pieces, and reopen the plan in the save folder."""
        first = not self.pieces
        self.pieces = session.pieces
        self.joins = session.joins
        self.fits = session.fits
        self.problems = session.problems
        self.plan = JoinPlan.load(Path(self.save_dir) / PLAN_NAME)
        self.selected = None
        self.result = None
        self._undo.clear()
        self._redo.clear()
        self._redraw_full(preserve_view=not first)
        self.recheck()
        moved = [j for j in self.joins if self.plan.seam_moved(j)]
        note = (f"Loaded {len(self.pieces)} piece(s) and "
                f"{len(self.joins)} border(s)")
        if self.plan.count:
            note += f"; reopened {self.plan.count} key(s) from {PLAN_NAME}"
        if moved:
            note += f" ({len(moved)} border(s) moved since -- check their keys)"
        if self.problems:
            note += f"; {len(self.problems)} problem(s) listed under the borders"
        self._set_status(note + ".")

    # -- editing ---------------------------------------------------------------
    def _commit(self, plan: JoinPlan) -> None:
        """Make ``plan`` the plan, as one undoable step."""
        self._undo.append(self.plan)
        self._redo.clear()
        self.plan = plan
        self.recheck()

    def _edited(self, join, points) -> None:
        plan = self.plan.copy()
        plan.set_points(join, points)
        self._commit(plan)

    def undo(self) -> None:
        if self._undo and not self._busy:
            self._redo.append(self.plan)
            self.plan = self._undo.pop()
            self.recheck()

    def redo(self) -> None:
        if self._redo and not self._busy:
            self._undo.append(self.plan)
            self.plan = self._redo.pop()
            self.recheck()

    def auto_place_selected(self) -> None:
        join = self.current_join()
        if join is not None:
            self._commit(auto_plan(self.joins, self.params(), self.plan,
                                   only={join.key}))
            self._set_status(f"Placed {len(self.plan.points(join.key))} "
                             f"key(s) along {join.label}.")

    def auto_place_all(self) -> None:
        if self.joins:
            self._commit(auto_plan(self.joins, self.params(), self.plan))
            self._set_status(f"Placed {self.plan.count} key(s) on "
                             f"{len(self.joins)} border(s).")

    def clear_selected(self) -> None:
        join = self.current_join()
        if join is not None and self.plan.points(join.key):
            self._edited(join, [])

    def add_key(self, x: float, y: float) -> None:
        join = self.current_join()
        if join is None:
            return
        point = key_point(join.seam, snap(join.seam, x, y))
        self._edited(join, self.plan.points(join.key) + [point])

    def remove_key(self, index: int) -> None:
        join = self.current_join()
        points = self.plan.points(join.key)
        del points[index]
        self._edited(join, points)

    def move_key(self, index: int, x: float, y: float) -> None:
        join = self.current_join()
        points = self.plan.points(join.key)
        points[index] = key_point(join.seam, snap(join.seam, x, y))
        self._edited(join, points)

    def key_at(self, x: float, y: float):
        """Index of the selected border's key under (x, y), or None."""
        join = self.current_join()
        if join is None:
            return None
        params = self.params()
        reach = max(self._pick_distance(), params.reach_mm * params.mpm)
        best, best_d = None, None
        for i, (kx, ky) in enumerate(self.plan.points(join.key)):
            d = np.hypot(kx - x, ky - y)
            if d <= reach and (best_d is None or d < best_d):
                best, best_d = i, d
        return best

    def join_near(self, x: float, y: float):
        """The border whose seam passes within a pick of (x, y), nearest first."""
        point = Point(x, y)
        near = [(j.seam.distance(point), j) for j in self.joins]
        near = [(d, j) for d, j in near if d <= self._pick_distance()]
        return min(near, key=lambda pair: pair[0])[1] if near else None

    def click(self, x: float, y: float) -> None:
        """
        A click on the map: on the selected border, remove the key under the
        pointer or add one; anywhere else, select the border clicked near.
        """
        if self._busy:
            return
        join = self.current_join()
        if join is not None:
            index = self.key_at(x, y)
            if index is not None:
                self.remove_key(index)
                return
            if join.seam.distance(Point(x, y)) <= self._pick_distance():
                self.add_key(x, y)
                return
        other = self.join_near(x, y)
        if other is not None:
            self.select(other.key)

    def select(self, key) -> None:
        self.selected = key
        self._redraw(preserve_view=True)
        self._mark_current_in_list()
        self._sync()
        self._set_status()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.select(item.data(Qt.UserRole))

    # -- map events --------------------------------------------------------------
    def _on_scroll(self, event) -> None:
        if event.inaxes is not self.ax or event.xdata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self.ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        self.ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    def _on_press(self, event) -> None:
        if (event.button != 1 or event.inaxes is not self.ax
                or event.xdata is None or self.mpl_toolbar.mode or self._busy):
            self._press = None
            return
        self._press = (event.x, event.y)
        self._drag = self.key_at(event.xdata, event.ydata)

    def _moved(self, event) -> bool:
        return self._press is not None and \
            abs(event.x - self._press[0]) + abs(event.y - self._press[1]) \
            > CLICK_SLOP_PX

    def _on_motion(self, event) -> None:
        if self._drag is None or event.xdata is None or not self._moved(event):
            return
        join = self.current_join()
        x, y = key_point(join.seam, snap(join.seam, event.xdata, event.ydata))
        if self._drag_artist is None:
            (self._drag_artist,) = self.ax.plot(
                [x], [y], marker="o", markersize=9, color=DRAG_COLOUR,
                zorder=10)
        else:
            self._drag_artist.set_data([x], [y])
        self.canvas.draw_idle()

    def _on_release(self, event) -> None:
        press, drag = self._press, self._drag
        self._press = self._drag = None
        if self._drag_artist is not None:
            self._drag_artist.remove()
            self._drag_artist = None
        if press is None or event.button != 1 or event.xdata is None \
                or event.inaxes is not self.ax:
            self.canvas.draw_idle()
            return
        moved = abs(event.x - press[0]) + abs(event.y - press[1]) \
            > CLICK_SLOP_PX
        if drag is not None and moved:
            self.move_key(drag, event.xdata, event.ydata)
        elif not moved:
            self.click(event.xdata, event.ydata)

    def _pick_distance(self) -> float:
        width_px = max(self.ax.get_window_extent().width, 1.0)
        x0, x1 = self.ax.get_xlim()
        return abs(x1 - x0) / width_px * PICK_PX

    # -- cutting -----------------------------------------------------------------
    def cut_and_save(self) -> None:
        if self._busy or not self.pieces or self.params_error:
            return
        params = self.params()
        self._launch(Task(cut_and_export, self.plan.copy(), self.pieces,
                          self.joins, params, self.fits, self.base_mm(),
                          self.result, Path(self.save_dir)),
                     self._on_cut, "Cutting")

    def _on_cut(self, outcome) -> None:
        self._finish_task()
        self.apply_cut(outcome)

    def apply_cut(self, outcome) -> None:
        result, paths = outcome
        self.result = result
        self.result_current = True
        self.view_combo.clear()
        self.view_combo.addItem(VIEW_BORDER)
        for i in range(len(result.plates)):
            self.view_combo.addItem(f"Key plate {i + 1}")
        note = (f"Wrote {len(result.keyed)} keyed piece(s), "
                f"{len(result.plates)} key plate(s) ({result.key_count} keys "
                f"+ {result.params.spares if result.key_count else 0} spares) "
                f"into {Path(self.save_dir).name}.")
        if result.failed:
            name, message = next(iter(result.failed.items()))
            note += f" {len(result.failed)} piece(s) failed ({name}: {message})."
            QMessageBox.warning(self, "Some pieces failed",
                                "\n".join(f"{n}: {m}" for n, m
                                          in result.failed.items()))
        self._sync()
        self._set_status(note)

    # -- 3D ----------------------------------------------------------------------
    def show_3d(self) -> None:
        """Draw what the 3D selector asks for from the last cut."""
        for visual in self._solid_meshes:
            visual.parent = None
        self._solid_meshes.clear()
        if self.result is None:
            self.solid_title.setText("Cut to see the result in 3D.")
            self.solid_canvas.update()
            return
        view = self.view_combo.currentText()
        if view == VIEW_BORDER:
            join = self.current_join()
            if join is None:
                self.solid_title.setText("Select a border to see it in 3D.")
                self.solid_canvas.update()
                return
            try:
                parts = join_assembly(self.result, self.pieces, join)
            except ValueError as exc:
                self.solid_title.setText(str(exc))
                self.solid_canvas.update()
                return
            meshes = [(m, A_RGBA if i == 0 else B_RGBA if i == 1 else KEY_RGBA)
                      for i, (_, m) in enumerate(parts)]
            title = (f"{join.label} as last cut — {len(parts) - 2} key(s) in "
                     "their slots, seen from below")
            if not self.result_current:
                title += " (the keys have changed since)"
            elevation = -60
        else:
            index = int(view.rsplit(" ", 1)[1]) - 1
            plate = self.result.plates[index]
            meshes = [(plate, KEY_RGBA)]
            title = (f"Key plate {index + 1}: "
                     f"{len(plate.split(only_watertight=False))} key(s)")
            elevation = 28
        for mesh, colour in meshes:
            self._solid_meshes.append(scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces), color=colour,
                shading=None if colour is KEY_RGBA else "flat",
                parent=self.solid_view.scene))
        self.solid_title.setText(title)
        self.solid_view.camera.elevation = elevation
        self.solid_view.camera.set_range()
        self.solid_canvas.update()

    # -- threading ---------------------------------------------------------------
    def _launch(self, task: Task, on_done, what: str) -> None:
        self._set_busy(True)
        self._set_status(f"{what} …")
        self._thread = QThread(self)
        self._task = task
        task.moveToThread(self._thread)
        self._thread.started.connect(task.run)
        task.done.connect(on_done)
        task.failed.connect(self._on_failed)
        task.progress.connect(self._on_progress)
        self._thread.start()

    def _on_progress(self, done: int, total: int, what: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        if what:
            self.status.setText(f"{what} ({done + 1} of {total}) …")

    def _on_failed(self, message: str) -> None:
        self._finish_task()
        QMessageBox.critical(self, "Failed", message)
        self._set_status(f"Failed: {message}")

    def _finish_task(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._task = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0)
        for spin in list(self.key_spins.values()) + [self.spares_spin]:
            spin.setEnabled(not busy)
        self._sync()

    # -- list ------------------------------------------------------------------
    def _item_text(self, join) -> str:
        checks = self.checks.get(join.key, [])
        ok = sum(c.ok for c in checks)
        bits = [f"{ok} key(s)" if checks else "no keys"]
        if len(checks) > ok:
            bits.append(f"{len(checks) - ok} refused")
        if join.stale:
            bits.append("STL out of date")
        if self.plan.seam_moved(join):
            bits.append("border moved")
        if join.status in ("not cleaned", "changed"):
            bits.append(join.status)
        return f"{join.label}   " + " · ".join(bits)

    def _refresh_list(self) -> None:
        self.border_list.blockSignals(True)
        self.border_list.clear()
        for join in self.joins:
            item = QListWidgetItem(self._item_text(join))
            item.setData(Qt.UserRole, join.key)
            item.setForeground(QColor(STATE_COLOURS[self.state_of(join)]))
            self.border_list.addItem(item)
        self.border_list.blockSignals(False)
        self._mark_current_in_list()
        shown = self.problems[:MAX_PROBLEMS_SHOWN]
        text = "\n".join(shown)
        if len(self.problems) > len(shown):
            text += f"\n… and {len(self.problems) - len(shown)} more"
        self.problems_label.setText(text)
        self.problems_label.setVisible(bool(self.problems))

    def _mark_current_in_list(self) -> None:
        self.border_list.blockSignals(True)
        self.border_list.clearSelection()
        for row in range(self.border_list.count()):
            item = self.border_list.item(row)
            if item.data(Qt.UserRole) == self.selected:
                item.setSelected(True)
                self.border_list.scrollToItem(item)
        self.border_list.blockSignals(False)

    # -- map ---------------------------------------------------------------------
    def _redraw_full(self, preserve_view: bool = False) -> None:
        """Re-frame on the pieces unless the view is to be kept."""
        if not preserve_view:
            self.ax.set_xlim(0, 1)
            self.ax.set_ylim(0, 1)
            self._framed = False
        self._redraw(preserve_view=preserve_view)

    def _redraw(self, preserve_view: bool = False) -> None:
        keep = preserve_view and getattr(self, "_framed", False)
        view = (self.ax.get_xlim(), self.ax.get_ylim()) if keep else None
        self.ax.clear()
        self._drag_artist = None
        self.ax.set_aspect("equal", adjustable="datalim")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        if not self.pieces:
            self.ax.set_title("Load provinces and their STLs", fontsize=11)
            self._framed = False
            self.canvas.draw_idle()
            return
        self.ax.set_title("Pick a border, then click its seam to add a key, "
                          "a key to remove it, or drag a key", fontsize=10)
        for piece in self.pieces.values():
            rings = [list(poly.exterior.coords)
                     for poly in _polygons(piece.geom)]
            self.ax.add_collection(PolyCollection(
                rings, facecolor=PROVINCE_COLOUR, edgecolor=EDGE_COLOUR,
                linewidth=0.4, zorder=1))
            at = piece.geom.representative_point()
            self.ax.annotate(piece.label, (at.x, at.y), fontsize=7,
                             color=LABEL_COLOUR, ha="center", va="center",
                             zorder=5)
        for join in self.joins:
            colour = STATE_COLOURS[self.state_of(join)]
            width = SEAM_WIDTH_SELECTED if join.key == self.selected \
                else SEAM_WIDTH
            for part in join.seam.geoms:
                xs, ys = part.xy
                self.ax.plot(xs, ys, color=colour, linewidth=width,
                             solid_capstyle="round", zorder=3)
        for key, checks in self.checks.items():
            for ok in (True, False):
                rings = [list(c.slot.exterior.coords) for c in checks
                         if c.ok == ok]
                if rings:
                    colour = SLOT_OK if ok else SLOT_REFUSED
                    self.ax.add_collection(PolyCollection(
                        rings, facecolor=colour, edgecolor=colour,
                        alpha=0.8 if key == self.selected else 0.45,
                        linewidth=0.6, zorder=4))
        if view is not None:
            self.ax.set_xlim(view[0])
            self.ax.set_ylim(view[1])
        else:
            minx, miny, maxx, maxy = unary_union(
                [p.geom.envelope for p in self.pieces.values()]).bounds
            pad = 0.03 * max(maxx - minx, maxy - miny)
            self.ax.set_xlim(minx - pad, maxx + pad)
            self.ax.set_ylim(miny - pad, maxy + pad)
            self._framed = True
        self.canvas.draw_idle()

    def zoom_to_border(self) -> None:
        """Frame the selected border, grown to the panel's shape."""
        join = self.current_join()
        if join is None:
            return
        params = self.params()
        reach = params.reach_mm * params.mpm * 3
        minx, miny, maxx, maxy = join.seam.bounds
        minx, miny, maxx, maxy = (minx - reach, miny - reach, maxx + reach,
                                  maxy + reach)
        bbox = self.ax.get_window_extent()
        target = max(bbox.width, 1.0) / max(bbox.height, 1.0)
        width, height = maxx - minx, maxy - miny
        if width / height < target:
            width = height * target
        else:
            height = width / target
        grow = 1.0 + 2.0 * ZOOM_PAD
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        self.ax.set_xlim(cx - width * grow / 2, cx + width * grow / 2)
        self.ax.set_ylim(cy - height * grow / 2, cy + height * grow / 2)
        self.canvas.draw_idle()

    # -- status ------------------------------------------------------------------
    def _sync(self) -> None:
        idle = not self._busy
        join = self.current_join()
        self.geojson_label.setText(str(self.geojson_dir or "(choose a folder)"))
        self.stl_label.setText(str(self.stl_dir or "(choose a folder)"))
        self.save_label.setText(str(self.save_dir))
        for widget in (self.btn_geojson, self.btn_stl, self.btn_save_dir):
            widget.setEnabled(idle)
        self.btn_load.setEnabled(idle and self.geojson_dir is not None
                                 and self.stl_dir is not None)
        for widget in (self.btn_auto, self.btn_zoom):
            widget.setEnabled(idle and join is not None)
        self.btn_clear.setEnabled(idle and join is not None
                                  and bool(self.plan.points(join.key)))
        self.btn_auto_all.setEnabled(idle and bool(self.joins))
        self.btn_undo.setEnabled(idle and bool(self._undo))
        self.btn_redo.setEnabled(idle and bool(self._redo))
        self.btn_cut.setEnabled(idle and bool(self.pieces)
                                and self.params_error is None)
        self.btn_show_3d.setEnabled(idle and self.result is not None)
        self.selection_label.setText(self._selection_text())
        params = self.params()
        if self.params_error:
            self.key_label.setText(self.params_error)
            self.key_label.setStyleSheet(f"color:{STATE_COLOURS['stale']};")
        else:
            self.key_label.setText(
                f"Each key {params.mouth_mm:g} × {2 * params.reach_mm:g} × "
                f"{params.key_height_mm:g} mm; slots {params.depth_mm:g} mm "
                f"deep into a {self.base_mm():g} mm base.")
            self.key_label.setStyleSheet("color:#555;")
        self._update_title()

    def _selection_text(self) -> str:
        join = self.current_join()
        if join is None:
            return "Pick a border in the list or on the map."
        checks = self.checks.get(join.key, [])
        refused = [c for c in checks if not c.ok]
        text = (f"{join.label}: {len(checks) - len(refused)} key(s)"
                f"{f', {len(refused)} refused' if refused else ''}.")
        if refused:
            reasons = sorted({c.reason for c in refused})
            text += " Refused: " + "; ".join(reasons) + "."
        if join.stale:
            text += (f" Its STLs sit {join.agreement_mm:.2f} mm off this border:"
                     " rebuild them from the bordered provinces.")
        if self.plan.seam_moved(join):
            text += " The border has moved since its keys were placed."
        return text

    def _set_status(self, prefix: str = "") -> None:
        if not self.pieces:
            self.status.setText(f"{prefix} No pieces loaded.".strip())
            return
        refused = sum(not c.ok for cs in self.checks.values() for c in cs)
        tail = (f"{len(self.pieces)} piece(s), {len(self.joins)} border(s): "
                f"{self.key_count()} key(s)"
                f"{f', {refused} refused' if refused else ''}.")
        self.status.setText(f"{prefix} {tail}".strip())

    def _update_title(self) -> None:
        if not self.pieces:
            self.setWindowTitle("Join Editor — (nothing loaded)")
            return
        self.setWindowTitle(f"Join Editor — {self.key_count()} key(s) on "
                            f"{len(self.joins)} border(s)")

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")          # before the window builds its scene
    window = JoinEditor()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
