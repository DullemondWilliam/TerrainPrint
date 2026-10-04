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
   until cleaned and green after.  Click a province on the map to select it, or
   click a border in a list to select both of its provinces.
3. **Edit Border →** -- once the two selected provinces share a seam (or
   double-click a border in either list).

**Edit Border** (the second page) is the Shared Border editor itself: the same
parameters, the same before/after panels, the same Simplify Seam.  Two
differences:

* **Apply and Mark Cleaned** replaces *Save Both*.  It writes both provinces
  into the save folder under their own file names, in the CRS each arrived in,
  records the border as cleaned, and returns to the list.
* **← Borders** goes back without saving.

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

Reuses ``lib.LIB_shared_border``, ``lib.LIB_border_ledger`` and the seam editor
from ``harnesses.UI_shared_border``.
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

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QFileDialog, QGroupBox,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

from harnesses.UI_shared_border import SharedBorderTester
from lib.LIB_border_ledger import LEDGER_NAME, BorderLedger, find_borders
from lib.LIB_project_config import get as cfg

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


class SeamPage(SharedBorderTester):
    """
    The second screen: the Shared Border editor, fed a pair rather than files.

    Subclassed rather than copied so it *is* the same editor -- every control,
    the threaded run, the linked panels -- and stays so as the harness changes.
    Only the ends are different: the pair arrives from the list, and Save Both
    becomes Apply, which hands the result back instead of asking for a folder.
    """
    back_requested = Signal()
    applied = Signal(object)              # SeamResult

    def __init__(self):
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
        toolbar = self.btn_a.parentWidget().layout().itemAt(0).layout()
        toolbar.insertWidget(0, self.btn_back)
        toolbar.insertWidget(1, self.heading)

        self.province_a = None
        self.province_b = None

    def load_pair(self, province_a: Province, province_b: Province,
                  status: str = "not cleaned") -> None:
        """Put a pair on the editor, as Import A and Import B would have."""
        self.province_a, self.province_b = province_a, province_b
        self.geom_a, self.geom_b = province_a.geometry, province_b.geometry
        self.path_a, self.path_b = province_a.path, province_b.path
        self.source_crs = province_a.source_crs or province_b.source_crs
        self.heading.setText(
            f"{province_a.label} ⇄ {province_b.label} — "
            f"{STATUS_TEXT.get(status, status)}")
        self._find_seam()
        self._invalidate()
        self._draw_before()
        if self.seam is not None:
            self.zoom_to_seam()

    def save_both(self) -> None:
        """Apply: hand the result back; the window decides where it goes."""
        if self.result is None:
            return
        self.applied.emit(self.result)

    def _set_busy(self, busy: bool) -> None:
        super()._set_busy(busy)
        # Leaving mid-run would orphan the worker's result.
        self.btn_back.setEnabled(not busy)


class BorderEditor(QMainWindow):

    def __init__(self):
        super().__init__()
        self.resize(1300, 780)

        # -- state ----------------------------------------------------------
        self.provinces: dict = {}           #: name -> Province
        self.borders: list = []             #: every shared seam, by key
        self.selected: list = []            #: up to two province names, in order
        self.save_dir = DEFAULT_SAVE_DIR
        self.ledger = BorderLedger.load(self.save_dir)
        self._press = None                  #: pointer position at button press

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
        for widget in (self.btn_folder, self.btn_files, self.btn_clear):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Save to:"))
        toolbar.addWidget(self.save_label)
        toolbar.addWidget(self.btn_save_dir)
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
        body.addLayout(map_box, stretch=1)

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
        side.addWidget(self.selection_label)
        self.btn_edit = QPushButton("Edit Border →")
        self.btn_edit.setToolTip(
            "Open the border the two selected provinces share.")
        self.btn_edit.clicked.connect(self.open_editor)
        side.addWidget(self.btn_edit)
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
        if status == "cleaned":
            entry = self.ledger.entry(border)
            return (f"{names}   {entry.get('vertices_before', '?')} → "
                    f"{border.vertices} vertices")
        if status == "changed":
            return f"{names}   {border.vertices} vertices · changed since cleaned"
        return f"{names}   {border.vertices} vertices"

    def _refresh_lists(self) -> None:
        for widget, borders in ((self.list_todo, self.not_cleaned()),
                                (self.list_done, self.cleaned())):
            widget.blockSignals(True)
            widget.clear()
            for border in borders:
                item = QListWidgetItem(self._item_text(border))
                item.setData(Qt.UserRole, border.key)
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
                f"{STATUS_TEXT[self.status_of(border)]}.")

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
