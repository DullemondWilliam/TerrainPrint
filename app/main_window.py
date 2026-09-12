"""
app.main_window

The single-window shell: a step navigator across the top, a persistent side
panel on the right, and the map canvas filling the middle.

Navigation rule
---------------
Forward movement is a heavy commit; backward movement is destructive.  Moving
back to a phase discards that phase's outputs **and every later phase's**, so
each phase always re-enters clean.  Two consequences shape this file:

* There is no stale downstream state, so there is no staleness detection, no
  cross-step scale checking, and no "rebuild required" gate anywhere here.
  Reversing already destroys downstream work, so those states cannot arise.
* Every Back discards real work — sometimes a twenty-second meshing run — so it
  is gated behind a confirmation that names the actual cost, and each phase
  front-loads enough preview that users rarely need to reverse at all.

Undo (Step 2) and Back are deliberately different animals: Undo is granular,
safe, and lives in the Step 2 panel; Back is wholesale, destructive, and lives
in the top navigation bar.  They never share a neighbourhood.

All geometry work is delegated to ``core`` so this file stays UI-only and
testable via pytest-qt.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QUndoStack
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSlider, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from shapely.geometry import Point

from lib.LIB_border_connectors import build_border_pieces

from core import borders, cleanup, measure, meshing, terrain_preview
from core.borders import BorderSettings
from core.meshing import MeshSettings
from core.project import ProjectState, SURROUNDING, TARGET

from .canvas import (LAYER_BED_SPLIT, LAYER_CONNECTORS, LAYER_CORRIDORS,
                     LAYER_LABELS, LAYER_TERRAIN, LEGEND_ENTRIES, MapCanvas)
from .commands import (CloseGapsCommand, FilterIslandsCommand,
                       MergeIslandsCommand, RemoveIslandsCommand,
                       RemoveLakesCommand, SimplifyCommand)
from .widgets import (AssemblyDiagramView, HoldListWidget, LegendWidget,
                      RestoreToast)

STEPS = ["1 · Import", "2 · Cleanup", "3 · Borders", "4 · Mesh & Export"]
STEP_IMPORT, STEP_CLEANUP, STEP_BORDERS, STEP_EXPORT = range(4)

PROVINCES_DIR = Path("Provinces-Islands")
GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"

#: Rough seconds a terrain meshing run costs per target region, used only to
#: put a number on what a Back is about to throw away.  Measured on the
#: reference project (~23 s for five regions); flat slabs are near-instant.
SECONDS_PER_TERRAIN_REGION = 4.5
#: How long a preview slider must sit still before the dry run is recomputed.
PREVIEW_DEBOUNCE_MS = 250


@dataclass
class PhaseState:
    """
    A snapshot of the working model as a phase was entered.

    Restoring one is how a backward move discards a phase's outputs: put the
    model back to how the phase found it and the phase, by definition, re-enters
    clean.  Geometry objects are immutable in practice, so this is cheap.
    """
    geometries: dict = field(default_factory=dict)
    terrain_path: Path | None = None
    border_result: object = None
    export_summary: str = ""
    assembly: tuple = ()          # (pieces, connectors) from the last export


class MainWindow(QMainWindow):

    def __init__(self, state: ProjectState | None = None):
        super().__init__()
        self.setWindowTitle("3D Terrain")
        self.resize(1440, 860)

        self.state = state or ProjectState()
        self.undo_stack = QUndoStack(self)
        self.border_result = None
        self._terrain_key = None        # (path, rounded bounds) already rendered
        self._assembly = ((), ())       # last export's diagram data

        #: Snapshot of the model as each phase was entered, keyed by step index.
        self._phase_entry: dict[int, PhaseState] = {}
        #: The single one-step restore for an immediately accidental Back.
        self._back_restore: tuple | None = None
        #: True while a long operation owns the UI (see :meth:`_busy`).
        self._busy_depth = 0

        self.canvas = MapCanvas(self)
        self.canvas.region_clicked.connect(self._on_region_clicked)
        self.canvas.region_point_clicked.connect(self._on_map_part_clicked)

        self._build_ui()
        self._build_undo_actions()
        self._refresh()
        self._phase_entry[STEP_IMPORT] = self._capture_phase()

    def _on_region_clicked(self, name: str) -> None:
        """Clicking the map focuses that region in the Step 2 part editor."""
        if self.select_region_for_parts(name):
            self.statusBar().showMessage(f"Selected {name}")

    def _on_map_part_clicked(self, name: str, x_m: float, y_m: float,
                             additive: bool = False) -> bool:
        """
        Clicking an island on the map selects that island in the list.

        Ctrl+click adds to (or toggles out of) the current selection, so several
        landmasses can be picked straight off the map.  Adding only applies
        within one region — the list shows a single region's parts, so clicking
        into a different region always starts a fresh selection.
        """
        current = self.part_region.currentItem()
        same_region = current is not None and current.data(Qt.UserRole) == name

        if not self.select_region_for_parts(name):
            return False
        region = self.state.find(name)
        if region is None:
            return False

        point = Point(x_m, y_m)
        for index, part in enumerate(cleanup.island_parts(region.geometry)):
            if not part.contains(point):
                continue
            mode = (QItemSelectionModel.Toggle if additive and same_region
                    else QItemSelectionModel.ClearAndSelect)
            self.part_list.setCurrentRow(index, mode)   # also drives the highlight
            label = "mainland" if index == 0 else f"island {index}"
            selected = len(self.part_list.selectedItems())
            suffix = f"  ({selected} selected)" if selected > 1 else ""
            self.statusBar().showMessage(
                f"{name} · {label} · {part.area / 1e6:,.0f} km²{suffix}")
            return True
        return False

    # =======================================================================
    # Layout
    # =======================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        outer.addWidget(self._build_stepper())

        self.restore_toast = RestoreToast()
        self.restore_toast.restore_requested.connect(self.restore_last_back)
        outer.addWidget(self.restore_toast)

        body = QHBoxLayout()
        body.setSpacing(8)
        # Left rail first: it owns the selection widgets that the step pages and
        # the preview timers both reach for, so it has to exist before them.
        body.addWidget(self._build_left_rail())
        body.addWidget(self.canvas, stretch=1)
        body.addWidget(self._build_side_panel())
        outer.addLayout(body)

        self.setCentralWidget(central)
        self._build_status_bar()
        # Both need widgets built above, and go_to_step() already touches the
        # preview timers, so they have to be wired first.
        self._build_preview_timers()
        self.go_to_step(STEP_IMPORT)
        self.statusBar().showMessage("Ready")

    def _build_status_bar(self) -> None:
        """Bottom bar: messages on the left, long-operation progress on the right."""
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

        self.recenter_action = QAction("Recenter", self)
        self.recenter_action.setShortcut(QKeySequence("Ctrl+0"))
        self.recenter_action.triggered.connect(self.recenter_map)
        self.addAction(self.recenter_action)

    def recenter_map(self) -> None:
        self.canvas.fit_to_contents()
        self.statusBar().showMessage("View recentred")

    def _build_stepper(self) -> QWidget:
        """
        Back on the left, the phase list in the middle, Confirm on the right.

        Back is styled as the destructive commitment it is — a red-tinted button
        at the far edge of the window, nowhere near the Step 2 Undo button in
        the side panel.  Reaching for one cannot land you on the other.
        """
        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        self.back_button = QPushButton("←  Back  (discards this phase)")
        self.back_button.setStyleSheet(
            "QPushButton { color:#7B241C; background:#FADBD8;"
            " border:1px solid #C0392B; border-radius:4px; padding:5px 12px;"
            " font-weight:600; }"
            "QPushButton:disabled { color:#B2BABB; background:#F4F6F6;"
            " border-color:#D5DBDB; }")
        self.back_button.setToolTip(
            "Return to the previous phase.\n"
            "This discards that phase's work and everything after it — you will "
            "be asked to confirm.\nTo take back a single Step 2 edit, use Undo "
            "in the side panel instead.")
        self.back_button.clicked.connect(self.go_back)
        row.addWidget(self.back_button)

        row.addStretch(1)
        self.step_labels: list[QLabel] = []
        for index, title in enumerate(STEPS):
            label = QLabel(title)
            label.setAlignment(Qt.AlignCenter)
            self.step_labels.append(label)
            row.addWidget(label)
            if index < len(STEPS) - 1:
                chevron = QLabel("›")
                chevron.setStyleSheet("color:#B0B0B0; font-size:14px;")
                row.addWidget(chevron)
        row.addStretch(1)

        self.next_button = QPushButton("Confirm  →")
        self.next_button.setStyleSheet(
            "QPushButton { font-weight:600; padding:5px 12px; }")
        self.next_button.clicked.connect(self.go_next)
        row.addWidget(self.next_button)
        return bar

    def _build_side_panel(self) -> QWidget:
        """
        The right panel: what this phase asks of you, and what you have picked.

        The step pages sit above a persistent Selection group.  The selection
        outlives any one phase — you want to see what is picked while reading
        the border report — so the group itself never changes with the step,
        only whether its edits are live (see :meth:`_sync_selection_controls`).
        """
        panel = QWidget()
        panel.setFixedWidth(310)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_import_page())
        self.pages.addWidget(self._build_cleanup_page())
        self.pages.addWidget(self._build_borders_page())
        self.pages.addWidget(self._build_export_page())
        layout.addWidget(self.pages, stretch=1)
        layout.addWidget(self._build_selection_group(), stretch=1)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self.summary_label)
        return panel

    # -----------------------------------------------------------------------
    # Left rail — how the map is drawn
    # -----------------------------------------------------------------------
    def _build_left_rail(self) -> QWidget:
        """
        The persistent view column: layers, terrain opacity, recentre, legend.

        Everything here changes how the map is *drawn* without changing the
        model, which is why it sits apart from the phase panel and never
        switches with the step.
        """
        panel = QWidget()
        panel.setFixedWidth(240)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(self._build_view_controls())
        layout.addWidget(LegendWidget(LEGEND_ENTRIES))
        layout.addStretch(1)
        return panel

    def _build_selection_group(self) -> QWidget:
        """Province chooser, its landmasses, and the Step 2 edits on them."""
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)

        layout.addWidget(_heading("Selection"))
        layout.addWidget(_hint(
            "Tick the provinces that Step 2's cleanup applies to. Click one to "
            "list its landmasses, then select parts to remove or merge.\n"
            "Click and hold a part to centre it on the map."))

        self.part_region = QListWidget()
        self.part_region.setMaximumHeight(80)
        self.part_region.currentItemChanged.connect(
            lambda *_: self._refresh_part_list())
        layout.addWidget(self.part_region)

        self.part_list = HoldListWidget()
        self.part_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Selecting parts highlights them; holding one also centres the map.
        self.part_list.itemSelectionChanged.connect(self.highlight_selected_parts)
        self.part_list.item_held.connect(self.focus_part)
        layout.addWidget(self.part_list, stretch=1)

        bridge_form = QFormLayout()
        self.bridge_width = _mm_spin(cleanup.DEFAULT_BRIDGE_MM, maximum=10.0)
        self.bridge_width.setToolTip(
            "Width of the land bridge joining merged islands that do not touch")
        bridge_form.addRow("Merge bridge (mm)", self.bridge_width)
        layout.addLayout(bridge_form)

        row = QHBoxLayout()
        self.btn_remove_parts = QPushButton("Remove")
        self.btn_remove_parts.clicked.connect(self.apply_remove_parts)
        self.btn_merge_parts = QPushButton("Merge")
        self.btn_merge_parts.clicked.connect(self.apply_merge_parts)
        row.addWidget(self.btn_remove_parts)
        row.addWidget(self.btn_merge_parts)
        layout.addLayout(row)

        self.selection_note = _hint("")
        layout.addWidget(self.selection_note)

        #: Controls that mutate geometry, and so only belong to Step 2.
        self._selection_edits = (self.bridge_width, self.btn_remove_parts,
                                 self.btn_merge_parts)
        return box

    def _sync_selection_controls(self) -> None:
        """
        Editing the selection is a Step 2 power; inspecting it is always on.

        Allowing a Remove or Merge from Step 3 would silently invalidate the
        corridor built against that geometry — precisely the stale downstream
        state the navigation rule exists to make impossible.  So the list stays
        readable everywhere and the edits switch off outside Step 2.
        """
        editable = self.current_step == STEP_CLEANUP
        for widget in self._selection_edits:
            widget.setEnabled(editable)
        self.selection_note.setText(
            "" if editable else
            "Editing landmasses is a Step 2 action — go back to change them.")

    # -----------------------------------------------------------------------
    # View controls (§6)
    # -----------------------------------------------------------------------
    def _build_view_controls(self) -> QWidget:
        """Layer toggles and terrain opacity — the clutter management."""
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        title = QLabel("Layers")
        title.setStyleSheet("font-weight:600; font-size:11px;")
        layout.addWidget(title)

        self.layer_boxes: dict[str, QCheckBox] = {}
        for layer, label in ((LAYER_TERRAIN, "Terrain"),
                             (LAYER_CORRIDORS, "Corridors"),
                             (LAYER_CONNECTORS, "Connectors"),
                             (LAYER_BED_SPLIT, "Bed split"),
                             (LAYER_LABELS, "Dimension labels (all regions)")):
            check = QCheckBox(label)
            check.setStyleSheet("font-size:11px;")
            check.setChecked(self.canvas.is_layer_visible(layer))
            check.toggled.connect(
                lambda on, name=layer: self.set_layer(name, on))
            layout.addWidget(check)
            self.layer_boxes[layer] = check
        self.layer_boxes[LAYER_LABELS].setToolTip(
            "Off: only the region you click or hover shows its mm size.\n"
            "On: every region shows its size at once.")

        row = QHBoxLayout()
        row.setSpacing(6)
        opacity_label = QLabel("Terrain opacity")
        opacity_label.setStyleSheet("font-size:11px;")
        row.addWidget(opacity_label)
        self.terrain_opacity = QSlider(Qt.Horizontal)
        self.terrain_opacity.setRange(0, 100)
        self.terrain_opacity.setValue(100)
        self.terrain_opacity.valueChanged.connect(
            lambda v: self.canvas.set_terrain_opacity(v / 100.0))
        row.addWidget(self.terrain_opacity, stretch=1)
        layout.addLayout(row)

        # Recentring is a view action like the rest of this box, so it lives
        # with them rather than off in the status bar.
        self.recenter_button = QPushButton("Recenter view")
        self.recenter_button.setToolTip(
            "Fit the whole map in view (Ctrl+0).\n"
            "The camera is otherwise left alone as you edit.")
        self.recenter_button.clicked.connect(self.recenter_map)
        layout.addWidget(self.recenter_button)
        return box

    def set_layer(self, layer: str, visible: bool) -> bool:
        """Toggle a canvas layer and keep the checkbox in step."""
        changed = self.canvas.set_layer_visible(layer, visible)
        box = self.layer_boxes.get(layer)
        if box is not None and box.isChecked() != visible:
            box.setChecked(visible)
        return changed

    # -----------------------------------------------------------------------
    # Step 1 – Import
    # -----------------------------------------------------------------------
    def _build_import_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_heading("Import"))
        layout.addWidget(_hint("Target regions are printed. Surrounding "
                               "regions only shape the borders."))

        self.btn_import_target = QPushButton("Import Target Provinces …")
        self.btn_import_target.clicked.connect(lambda: self._choose_files(TARGET))
        layout.addWidget(self.btn_import_target)

        self.btn_import_surrounding = QPushButton("Import Surrounding Provinces …")
        self.btn_import_surrounding.clicked.connect(
            lambda: self._choose_files(SURROUNDING))
        layout.addWidget(self.btn_import_surrounding)

        self.btn_import_terrain = QPushButton("Import Terrain Raster …")
        self.btn_import_terrain.clicked.connect(self._choose_terrain)
        layout.addWidget(self.btn_import_terrain)

        form = QFormLayout()
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(100_000, 100_000_000)
        self.scale_spin.setSingleStep(500_000)
        self.scale_spin.setGroupSeparatorShown(True)
        self.scale_spin.setValue(int(self.state.scale))
        self.scale_spin.valueChanged.connect(self._on_scale_changed)
        form.addRow("Scale  1 :", self.scale_spin)
        layout.addLayout(form)

        layout.addWidget(_heading("Regions"))
        self.region_list = QListWidget()
        layout.addWidget(self.region_list, stretch=1)

        self.btn_remove = QPushButton("Remove Selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        layout.addWidget(self.btn_remove)
        return page

    # -----------------------------------------------------------------------
    # Step 2 – Cleanup
    # -----------------------------------------------------------------------
    def _build_cleanup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_heading("Simplify"))
        layout.addWidget(_hint("All regions are simplified in one shared "
                               "topology, so shared borders stay matched."))
        form = QFormLayout()
        self.coast_tol = _km_spin(2.4)
        self.internal_tol = _km_spin(4.0)
        form.addRow("Coastal (km)", self.coast_tol)
        form.addRow("Internal (km)", self.internal_tol)
        layout.addLayout(form)

        self.simplify_preview_label = _hint("")
        layout.addWidget(self.simplify_preview_label)

        btn_simplify = QPushButton("Apply Simplification")
        btn_simplify.clicked.connect(self.apply_simplify)
        layout.addWidget(btn_simplify)

        layout.addWidget(_heading("Islands"))
        form2 = QFormLayout()
        self.island_min_area = _km_spin(50.0, maximum=100_000.0)
        self.gap_close = _km_spin(0.0, maximum=500.0)
        form2.addRow("Min area (km²)", self.island_min_area)
        form2.addRow("Close gaps (km)", self.gap_close)
        layout.addLayout(form2)

        self.filter_preview_label = _hint("")
        layout.addWidget(self.filter_preview_label)

        btn_filter = QPushButton("Filter Small Islands")
        btn_filter.clicked.connect(self.apply_island_filter)
        layout.addWidget(btn_filter)

        btn_gaps = QPushButton("Close Island Gaps")
        btn_gaps.clicked.connect(self.apply_close_gaps)
        layout.addWidget(btn_gaps)

        btn_lakes = QPushButton("Remove Lakes")
        btn_lakes.clicked.connect(self.apply_remove_lakes)
        layout.addWidget(btn_lakes)

        layout.addWidget(_hint(
            "These act on the provinces ticked in the Selection panel, "
            "on the left of the map."))
        layout.addStretch(1)

        # Undo/redo: quiet, granular, non-destructive — the opposite of Back,
        # and kept down here in the panel so the two can never be confused.
        undo_row = QHBoxLayout()
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Take back the last Step 2 edit (Ctrl+Z). "
                                 "Safe and granular — nothing else is lost.")
        self.btn_undo.clicked.connect(self.undo_stack.undo)
        self.btn_redo = QPushButton("Redo")
        self.btn_redo.clicked.connect(self.undo_stack.redo)
        undo_row.addWidget(self.btn_undo)
        undo_row.addWidget(self.btn_redo)
        layout.addLayout(undo_row)
        return page

    # -----------------------------------------------------------------------
    # Step 2 previews (§3) — dry runs, never committed, never undoable
    # -----------------------------------------------------------------------
    def _build_preview_timers(self) -> None:
        """
        Debounced preview recomputation.

        Both previews call the real ``core.cleanup`` functions rather than
        approximating them in the UI, so what is ghosted is exactly what
        applying would do.  Simplification in particular is not free, hence the
        debounce: recompute once the slider settles, not on every tick.
        """
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self.update_filter_preview)
        self.island_min_area.valueChanged.connect(
            lambda *_: self._filter_timer.start())

        self._simplify_timer = QTimer(self)
        self._simplify_timer.setSingleShot(True)
        self._simplify_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._simplify_timer.timeout.connect(self.update_simplify_preview)
        for spin in (self.coast_tol, self.internal_tol):
            spin.valueChanged.connect(lambda *_: self._simplify_timer.start())

    def _preview_scope(self) -> list:
        """Regions the Step 2 previews apply to (the ticked ones)."""
        checked = self.checked_regions()
        return [r for r in self.state.regions
                if not checked or r.name in checked]

    def update_filter_preview(self) -> int:
        """
        Ghost the landmasses the area threshold would remove.

        Returns the count, which is also reported next to the slider so the
        cost of the threshold is legible before anything is deleted.
        """
        if self.current_step != STEP_CLEANUP:
            return 0
        min_area_m2 = self.island_min_area.value() * 1e6

        doomed = []
        for region in self._preview_scope():
            kept = cleanup.filter_small_islands(region.geometry, min_area_m2)
            if kept is region.geometry:
                continue                       # nothing dropped in this region
            survivors = {p.wkb for p in cleanup.island_parts(kept)}
            doomed.extend(p for p in cleanup.island_parts(region.geometry)
                          if p.wkb not in survivors)

        self.canvas.set_ghost_geoms(doomed)
        self.filter_preview_label.setText(
            f"Will remove {len(doomed)} landmass(es)" if doomed
            else "Nothing would be removed at this threshold")
        return len(doomed)

    def update_simplify_preview(self) -> int:
        """Overlay the simplified outline against the current one."""
        if self.current_step != STEP_CLEANUP:
            return 0
        scope = self._preview_scope()
        if not scope:
            self.canvas.clear_preview_outline()
            return 0

        geoms = {r.name: r.geometry for r in scope}
        try:
            simplified = cleanup.simplify_regions(
                geoms, self.coast_tol.value() * 1000,
                self.internal_tol.value() * 1000)
        except Exception as exc:
            self.simplify_preview_label.setText(f"Preview unavailable: {exc}")
            self.canvas.clear_preview_outline()
            return 0

        changed = [geom for name, geom in simplified.items()
                   if geom is not None and geom.wkb != geoms[name].wkb]
        self.canvas.set_preview_outline(changed)
        self.simplify_preview_label.setText(
            f"Preview: {len(changed)} region(s) would change outline" if changed
            else "Preview: no visible change at these tolerances")
        return len(changed)

    def clear_step2_previews(self) -> None:
        """Drop both previews (on leaving Step 2, or after applying)."""
        self._filter_timer.stop()
        self._simplify_timer.stop()
        self.canvas.clear_ghosts()
        self.canvas.clear_preview_outline()
        self.filter_preview_label.setText("")
        self.simplify_preview_label.setText("")

    # -----------------------------------------------------------------------
    # Step 3 – Borders
    # -----------------------------------------------------------------------
    def _build_borders_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_heading("Border corridor"))
        layout.addWidget(_hint("Targets split a shared seam evenly. A seam "
                               "against a surrounding region is carved "
                               "entirely on that region's side."))

        form = QFormLayout()
        self.border_width = _mm_spin(2.0, maximum=20.0)
        self.border_coast_tol = _km_spin(2.4)
        self.border_internal_tol = _km_spin(4.0)
        form.addRow("Width (mm)", self.border_width)
        form.addRow("Coastal (km)", self.border_coast_tol)
        form.addRow("Internal (km)", self.border_internal_tol)
        layout.addLayout(form)

        layout.addWidget(_heading("Print bed"))
        form2 = QFormLayout()
        self.bed_size = _mm_spin(256.0, maximum=1000.0)
        self.bed_margin = _mm_spin(10.0, maximum=50.0)
        form2.addRow("Bed (mm)", self.bed_size)
        form2.addRow("Margin (mm)", self.bed_margin)
        layout.addLayout(form2)

        # The bed split is what Step 4 will actually mesh, so it updates live —
        # seeing the piece count here is what stops users confirming into a
        # meshing run they are going to reject.
        for spin in (self.bed_size, self.bed_margin):
            spin.valueChanged.connect(lambda *_: self.update_bed_preview())

        self.bed_report = QLabel("")
        self.bed_report.setWordWrap(True)
        self.bed_report.setStyleSheet("font-size:11px; color:#2E86C1;")
        layout.addWidget(self.bed_report)

        btn_build = QPushButton("Build Borders + Connectors")
        btn_build.clicked.connect(self.build_borders)
        layout.addWidget(btn_build)

        btn_clear = QPushButton("Clear Border Preview")
        btn_clear.clicked.connect(self.clear_borders)
        layout.addWidget(btn_clear)

        self.border_report = QLabel("Not built yet.")
        self.border_report.setWordWrap(True)
        self.border_report.setStyleSheet("font-size:11px;")
        layout.addWidget(self.border_report)
        layout.addStretch(1)
        return page

    def border_settings(self) -> BorderSettings:
        return BorderSettings(
            width_mm=self.border_width.value(),
            coast_tol_km=self.border_coast_tol.value(),
            internal_tol_km=self.border_internal_tol.value(),
            bed_mm=self.bed_size.value(),
            margin_mm=self.bed_margin.value(),
        )

    # -----------------------------------------------------------------------
    # Bed-split preview (§2.1)
    # -----------------------------------------------------------------------
    def bed_preview_geometry(self) -> tuple[list, list]:
        """
        (piece footprints, cut lines) for the whole model at the current bed.

        Everything here comes straight out of ``core.meshing.bed_footprints`` /
        ``core.borders.split_to_bed_detailed`` — the same functions the export
        splits with — so the preview cannot promise a split the export will not
        deliver, including corridor cuts nudged sideways to miss a connector.
        """
        bed_mm, margin_mm = self.bed_size.value(), self.bed_margin.value()
        pieces, cuts = [], []

        for region in self.state.targets:
            pieces.extend(meshing.bed_footprints(
                region.geometry, self.state.scale, bed_mm, margin_mm))
            cuts.extend(meshing.bed_cut_lines(
                region.geometry, self.state.scale, bed_mm, margin_mm))

        if self.border_result is not None and self.border_result.strip is not None:
            split = borders.split_to_bed_detailed(
                self.border_result.strip, bed_mm, self.state.scale,
                lobes=self.border_result.lobes, margin_mm=margin_mm)
            pieces.extend(split.pieces)
            cuts.extend(split.cuts)
        return pieces, cuts

    def update_bed_preview(self) -> int:
        """Redraw the bed grid and report piece count + sizes. Returns count."""
        if not self.state.regions:
            self.canvas.clear_bed_overlay()
            self.bed_report.setText("")
            self.canvas.render_project(self.state)
            return 0

        pieces, cuts = self.bed_preview_geometry()
        self.canvas.set_bed_overlay(pieces, cuts)
        self.canvas.render_project(self.state)

        if pieces:
            sizes = [measure.size_mm(p, self.state.scale) for p in pieces]
            biggest = max(sizes, key=lambda s: max(s))
            self.bed_report.setText(
                f"Bed split: {len(pieces)} piece(s), largest "
                f"{biggest[0]:.0f} × {biggest[1]:.0f} mm "
                f"(bed {self.bed_size.value():.0f} mm, "
                f"margin {self.bed_margin.value():.0f} mm)")
        else:
            self.bed_report.setText("")
        return len(pieces)

    def build_borders(self) -> bool:
        """Carve the corridor, place dovetails, split to bed size."""
        if not self.state.regions:
            self.statusBar().showMessage("Import regions first")
            return False

        settings = self.border_settings()
        with self._busy("Building borders and connectors …"):
            try:
                self.border_result = borders.build(self.state, settings)
            except Exception as exc:
                self.border_report.setText(f"Border build failed: {exc}")
                self.statusBar().showMessage(f"Border build failed: {exc}")
                return False

        result = self.border_result
        self.canvas.set_border_overlay(result.strip, result.lobes)
        self.update_bed_preview()
        self._refresh()

        oversized = result.oversized(settings.bed_mm, self.state.scale,
                                     settings.margin_mm)
        orphans = result.pieces_without_connectors()
        largest = ""
        if result.pieces:
            big = max(result.pieces, key=lambda p: p.area)
            w, h = measure.size_mm(big, self.state.scale)
            largest = f"largest {w:.0f} × {h:.0f} mm"

        lines = [f"{result.piece_count} piece(s) · {len(result.lobes)} dovetails",
                 largest,
                 "All pieces fit the bed." if not oversized
                 else f"{len(oversized)} piece(s) still oversized!"]
        if orphans:
            lines.append(f"{len(orphans)} corridor piece(s) have no connector — "
                         f"they would float free in the assembly.")
        self.border_report.setText("\n".join(line for line in lines if line))
        self.statusBar().showMessage(
            f"Borders built: {result.piece_count} pieces, "
            f"{len(result.lobes)} dovetails")
        return True

    def clear_borders(self) -> None:
        self.border_result = None
        self.canvas.clear_border_overlay()
        self.border_report.setText("Not built yet.")
        self.update_bed_preview()
        self._refresh()

    # -----------------------------------------------------------------------
    # Step 4 – Mesh & Export
    # -----------------------------------------------------------------------
    def _build_export_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(_heading("Mesh"))
        layout.addWidget(_hint("Targets are meshed from the cleaned geometry. "
                               "Without a terrain raster they export as flat "
                               "slabs."))
        form = QFormLayout()
        self.terrain_factor = _mm_spin(10.0, maximum=100.0)
        self.base_thickness = _mm_spin(2.0, maximum=20.0)
        form.addRow("Terrain ×", self.terrain_factor)
        form.addRow("Base (mm)", self.base_thickness)
        layout.addLayout(form)

        self.split_meshes = QCheckBox("Split solids to print bed")
        self.split_meshes.setChecked(True)
        layout.addWidget(self.split_meshes)

        self.include_borders = QCheckBox("Include border pieces")
        self.include_borders.setChecked(True)
        layout.addWidget(self.include_borders)

        self.emboss_ids = QCheckBox("Engrave piece IDs on undersides")
        self.emboss_ids.setChecked(True)
        self.emboss_ids.setToolTip(
            "Cuts each piece's ID into its underside as a row of pips, matching "
            "the assembly diagram written alongside the STLs.")
        layout.addWidget(self.emboss_ids)

        btn_export = QPushButton("Export STL …")
        btn_export.clicked.connect(self._choose_export_dir)
        layout.addWidget(btn_export)

        self.export_report = QLabel("Nothing exported yet.")
        self.export_report.setWordWrap(True)
        self.export_report.setStyleSheet("font-size:11px;")
        self.export_report.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.export_report)
        layout.addStretch(1)
        return page

    def mesh_settings(self) -> MeshSettings:
        return MeshSettings(
            terrain_factor=self.terrain_factor.value(),
            base_mm=self.base_thickness.value(),
            bed_mm=self.bed_size.value(),
            margin_mm=self.bed_margin.value(),
            split_to_bed=self.split_meshes.isChecked(),
        )

    def build_meshes(self, progress=None) -> tuple[dict, list]:
        """
        Mesh every target region (and the border pieces) into named solids.

        Returns ``(solids, assembly_pieces)``: the named meshes, plus one
        :class:`core.meshing.AssemblyPiece` per solid positioned in the whole
        model's shared print-mm frame so the assembly diagram can place them.
        Piece numbers are allocated here and engraved into the solids, so the
        engraved marking and the diagram always agree.
        """
        settings = self.mesh_settings()
        connector = self.border_settings().connector_params(self.state.scale)
        lobes = self.border_result.lobes if self.border_result else []
        mpm = self.state.metres_per_mm

        origin = self.state.bounds()
        origin_x, origin_y = (origin[0], origin[1]) if origin else (0.0, 0.0)

        solids: dict = {}
        placements: list = []
        targets = list(self.state.targets)

        for index, region in enumerate(targets, start=1):
            if progress:
                progress(index, len(targets), region.name)

            if self.state.terrain_path:
                mesh = meshing.build_terrain_mesh(
                    region.geometry, self.state.terrain_path, self.state.scale,
                    settings, lobes=lobes, connector=connector,
                    progress=lambda m, n=region.name: self.statusBar().showMessage(
                        f"{n}: {m}"))
            else:
                mesh = meshing.build_flat_mesh(
                    region.geometry, self.state.scale, settings.base_mm,
                    lobes=lobes, connector=connector)

            pieces = (meshing.split_mesh_to_bed(mesh, settings.bed_mm,
                                                settings.margin_mm)
                      if settings.split_to_bed else [mesh])
            # The mesh lives in a frame whose origin is its own geometry's
            # lower-left corner, so shift by that to reach the shared frame.
            geom_x, geom_y, _, _ = region.geometry.bounds
            offset = ((geom_x - origin_x) / mpm, (geom_y - origin_y) / mpm)
            for piece_index, piece in enumerate(pieces, start=1):
                suffix = f"_{piece_index}" if len(pieces) > 1 else ""
                name = f"{region.name}{suffix}"
                solids[name] = piece
                placements.append((name, piece, offset))

        if self.include_borders.isChecked() and self.border_result:
            for name, piece, offset in self._border_solids(connector, origin, mpm):
                solids[name] = piece
                placements.append((name, piece, offset))

        assembly = self._finalise_pieces(solids, placements)
        return solids, assembly

    def _finalise_pieces(self, solids: dict, placements: list) -> list:
        """Number, engrave, and place every solid in the shared print-mm frame."""
        assembly = []
        for number, (name, piece, (dx, dy)) in enumerate(placements, start=1):
            if self.emboss_ids.isChecked():
                engraved = meshing.engrave_piece_id(piece, number)
                solids[name] = engraved
                piece = engraved
            (minx, miny, _), (maxx, maxy, _) = piece.bounds
            assembly.append(meshing.AssemblyPiece(
                number=number, name=name,
                x_mm=minx + dx, y_mm=miny + dy,
                width_mm=maxx - minx, height_mm=maxy - miny))
        return assembly

    def _border_solids(self, connector, origin, mpm: float) -> list:
        """Stepped border solids, one per bed-sized corridor piece."""
        origin_x, origin_y = (origin[0], origin[1]) if origin else (0.0, 0.0)
        out = []
        for index, footprint in enumerate(self.border_result.pieces, start=1):
            touching = [l for l in self.border_result.lobes
                        if l.intersects(footprint)]
            # build_border_pieces works from the footprint's own lower-left.
            foot_x, foot_y, _, _ = footprint.bounds
            offset = ((foot_x - origin_x) / mpm, (foot_y - origin_y) / mpm)
            for sub, piece in enumerate(
                    build_border_pieces(footprint, touching, connector)):
                suffix = f"_{sub + 1}" if sub else ""
                out.append((f"border_{index}{suffix}", piece, offset))
        return out

    def connector_positions(self) -> list:
        """Dovetail centres in the shared print-mm frame, for the diagram."""
        if not self.border_result or not self.border_result.lobes:
            return []
        origin = self.state.bounds()
        origin_x, origin_y = (origin[0], origin[1]) if origin else (0.0, 0.0)
        mpm = self.state.metres_per_mm
        points = []
        for lobe in self.border_result.lobes:
            centre = lobe.centroid
            points.append(((centre.x - origin_x) / mpm,
                           (centre.y - origin_y) / mpm))
        return points

    def export_all(self, out_dir) -> bool:
        """Mesh, validate, and write every piece plus the assembly diagram."""
        if not self.state.targets:
            self.statusBar().showMessage("Import at least one target region")
            return False

        # Export-time guard: a corridor piece with no connector floats free in
        # the assembly, so it is refused here as well as flagged at the
        # turnstile (the user may have changed parameters since).
        orphans = self._orphan_pieces()
        if orphans:
            message = (f"{len(orphans)} corridor piece(s) have no seated "
                       f"connector: {', '.join(str(i + 1) for i in orphans)}.\n"
                       "They would not key into any province. Go back to Step 3 "
                       "and rebuild with a wider corridor or closer connector "
                       "spacing.")
            self.export_report.setText(f"Export refused:\n{message}")
            QMessageBox.warning(self, "Export refused", message)
            return False

        total = max(1, len(self.state.targets))
        try:
            with self._busy("Meshing …", maximum=total) as tick:
                solids, assembly = self.build_meshes(
                    progress=lambda i, n, name: tick(
                        i, f"Meshing {name} — {i} of {n} regions"))
                if not solids:
                    raise meshing.MeshingError("Nothing to export")
                written = meshing.export_meshes(solids, out_dir)
        except meshing.ExportFailure as failure:
            self._report_export_failure(failure)
            return False
        except Exception as exc:
            self.export_report.setText(f"Export failed:\n{exc}")
            self.statusBar().showMessage(f"Export failed: {exc}")
            QMessageBox.critical(self, "Export failed", str(exc))
            return False

        self.canvas.clear_attention()
        connectors = self.connector_positions()
        diagram = None
        try:
            diagram = meshing.write_assembly_diagram(
                assembly, connectors, Path(out_dir) / "assembly.svg",
                title=f"Assembly · {len(assembly)} pieces")
        except Exception as exc:                  # a diagram is not worth losing
            self.statusBar().showMessage(f"Assembly diagram not written: {exc}")

        self._assembly = (assembly, connectors)
        names = "\n".join(f"  • {p.name}" for p in written[:12])
        more = f"\n  … and {len(written) - 12} more" if len(written) > 12 else ""
        self.export_report.setText(
            f"Exported {len(written)} piece(s), all watertight, to:\n"
            f"{Path(out_dir)}\n{names}{more}")
        self.statusBar().showMessage(f"Exported {len(written)} piece(s)")
        self.show_export_window(out_dir, written, assembly, connectors, diagram)
        return True

    def _orphan_pieces(self) -> list:
        """Corridor pieces carrying no seated connector (empty if no borders)."""
        if not (self.include_borders.isChecked() and self.border_result):
            return []
        return self.border_result.pieces_without_connectors()

    def _report_export_failure(self, failure) -> None:
        """
        Name the offending piece, its failure mode, and where it is (§8).

        "Export failed" with no locus is a dead end now that fixing it costs a
        destructive walk backwards, so every failing piece is named with a
        distilled diagnostic and its region is highlighted on the map.
        """
        lines = [f"• {f.name}: {f.mode} — {f.detail}" for f in failure.failures]
        regions = {f.region for f in failure.failures}
        self.canvas.set_attention(regions)
        first = next(iter(regions), None)
        if first:
            self.canvas.center_on_region(first)

        body = "\n".join(lines)
        self.export_report.setText(
            f"Export failed — nothing was written.\n{body}")
        self.statusBar().showMessage(
            f"Export failed: {len(failure.failures)} piece(s) unprintable")
        QMessageBox.critical(
            self, "Export failed",
            f"{len(failure.failures)} piece(s) cannot be printed, so nothing "
            f"was written:\n\n{body}\n\n"
            f"The affected region(s) are highlighted on the map.")

    def show_export_window(self, out_dir, written, assembly, connectors,
                           diagram_path=None) -> QDialog:
        """The post-export summary: assembly diagram, file list, output folder."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Export complete")
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)

        layout.addWidget(_heading(f"{len(written)} piece(s) written"))
        folder = QLabel(str(Path(out_dir)))
        folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        folder.setStyleSheet("font-size:11px; color:#333;")
        layout.addWidget(folder)

        layout.addWidget(_hint(
            "Each piece's ID is engraved into its underside as a row of pips. "
            "Match them to the diagram below to reassemble the model."))

        self.diagram_view = AssemblyDiagramView()
        self.diagram_view.set_pieces(assembly, connectors)
        layout.addWidget(self.diagram_view, stretch=1)

        if diagram_path is not None:
            layout.addWidget(_hint(f"Diagram also written to {diagram_path.name}"))

        files = QListWidget()
        files.setMaximumHeight(150)
        by_name = {p.name: p for p in assembly}
        for path in written:
            piece = by_name.get(path.stem)
            files.addItem(f"{piece.label}  ·  {path.name}" if piece
                          else path.name)
        layout.addWidget(files)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.show()
        return dialog

    def _choose_export_dir(self) -> None:
        out = QFileDialog.getExistingDirectory(
            self, "Choose output folder", str(Path.cwd()))
        if out:
            self.export_all(out)

    def _build_undo_actions(self) -> None:
        self.action_undo = self.undo_stack.createUndoAction(self, "Undo")
        self.action_undo.setShortcut(QKeySequence.Undo)
        self.action_redo = self.undo_stack.createRedoAction(self, "Redo")
        self.action_redo.setShortcut(QKeySequence.Redo)
        self.addAction(self.action_undo)
        self.addAction(self.action_redo)

        # Connect the stack's signals straight to the buttons.  Routing through
        # a lambda that captures the stack keeps firing during teardown, after
        # the underlying C++ object is gone.
        self.undo_stack.canUndoChanged.connect(self.btn_undo.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.btn_redo.setEnabled)
        self.btn_undo.setEnabled(self.undo_stack.canUndo())
        self.btn_redo.setEnabled(self.undo_stack.canRedo())

    # =======================================================================
    # Long operations (§4)
    # =======================================================================
    @contextmanager
    def _busy(self, message: str, maximum: int = 0):
        """
        Own the UI for an operation that may exceed a second.

        Interaction policy (R4.3), stated explicitly because it must hold
        whether or not this work ever moves off the GUI thread:

        * **The UI is locked, not queued.** Back, Confirm, and every action that
          mutates the model are disabled for the duration.  Step 2 edits are
          *not* queued for later application — a queued edit would land on
          geometry the operation had already consumed.
        * **No cancellation is offered.** These operations produce one indivisible
          result; a half-finished meshing run has nothing worth keeping, and a
          cancel button that discards everything is indistinguishable from the
          destructive Back this design is trying to keep users away from.
        * Disabling Back is the point: a silent multi-second freeze reads as a
          crash, and a user who reaches for Back mid-computation would destroy
          the very work they are waiting on.

        ``maximum > 0`` gives a determinate bar; yield value is a ``tick``
        callable taking ``(value, message)``.
        """
        self._busy_depth += 1
        self._set_controls_enabled(False)
        self.progress.setRange(0, maximum)      # (0, 0) renders as busy/indeterminate
        self.progress.setValue(0)
        self.progress.setFormat(message if maximum else "%p%")
        self.progress.show()
        self.statusBar().showMessage(message)

        def tick(value: int, text: str = "") -> None:
            if maximum:
                self.progress.setValue(value)
            if text:
                self.progress.setFormat(text)
                self.statusBar().showMessage(text)
            # Keep the window repainting so the operation cannot read as a hang.
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()

        try:
            tick(0, message)
            yield tick
        finally:
            self._busy_depth -= 1
            if self._busy_depth <= 0:
                self._busy_depth = 0
                self.progress.hide()
                self._set_controls_enabled(True)

    @property
    def is_busy(self) -> bool:
        return self._busy_depth > 0

    def _set_controls_enabled(self, enabled: bool) -> None:
        """Lock navigation and the destructive actions during a long run."""
        for widget in (self.back_button, self.next_button, self.pages,
                       self.restore_toast, *self._selection_edits):
            widget.setEnabled(enabled)
        if enabled:
            self._sync_stepper()      # restores the real Back/Confirm states

    # =======================================================================
    # Actions  (kept free of dialogs so tests can drive them directly)
    # =======================================================================
    def update_terrain_preview(self, force: bool = False) -> bool:
        """
        Render the terrain backdrop for the current extent.

        Cached on (raster, extent): panning the list around should not re-warp a
        7 GB raster.  Returns True if an image is now in place.
        """
        if not self.state.terrain_path or not self.state.regions:
            self.canvas.set_terrain_overlay(None)
            self._terrain_key = None
            return False

        bounds = self.state.bounds()
        if bounds is None:
            return False
        pad = 0.05 * max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        padded = (bounds[0] - pad, bounds[1] - pad,
                  bounds[2] + pad, bounds[3] + pad)

        key = (str(self.state.terrain_path), tuple(round(v, -3) for v in padded))
        if key == self._terrain_key and not force:
            return self.canvas.has_terrain

        # 3–4 s on the reference raster: long enough to read as a freeze.
        with self._busy("Rendering terrain backdrop …"):
            try:
                preview = terrain_preview.build_preview(
                    self.state.terrain_path, padded)
            except Exception as exc:
                self.statusBar().showMessage(f"Terrain preview failed: {exc}")
                return False

        if preview is None:
            self.canvas.set_terrain_overlay(None)
            self.statusBar().showMessage("Terrain raster does not cover this area")
            self._terrain_key = key
            return False

        rgba, covered = preview
        self.canvas.set_terrain_overlay(rgba, covered)
        self._terrain_key = key
        return True

    def import_regions(self, paths, role: str) -> int:
        """Import region files as `role`. Returns how many were added."""
        if not paths:
            return 0
        try:
            added = self.state.add_region_files(paths, role=role)
        except Exception as exc:
            self.statusBar().showMessage(f"Import failed: {exc}")
            QMessageBox.critical(self, "Import failed", str(exc))
            return 0
        self.update_terrain_preview()
        self._refresh()
        self._invalidate_restore()
        self.statusBar().showMessage(
            f"Imported {len(added)} {role} region(s)")
        return len(added)

    def set_terrain(self, path) -> bool:
        try:
            self.state.set_terrain(path)
        except Exception as exc:
            QMessageBox.critical(self, "Terrain failed", str(exc))
            return False
        self.update_terrain_preview()
        self._refresh()
        self._invalidate_restore()
        self.statusBar().showMessage(f"Terrain: {Path(path).name}")
        return True

    # -----------------------------------------------------------------------
    # Step 2 actions — each pushes exactly one undoable command
    # -----------------------------------------------------------------------
    def _push(self, command) -> bool:
        """Push a command only if it actually changed something."""
        if not command.changed:
            self.statusBar().showMessage("No change")
            return False
        self.undo_stack.push(command)
        self._invalidate_restore()
        self.statusBar().showMessage(command.text())
        return True

    def _scope(self) -> set | None:
        """Ticked provinces, or None if the selector has nothing to offer."""
        if self.part_region.count() == 0:
            return None
        checked = self.checked_regions()
        if not checked:
            self.statusBar().showMessage(
                "No provinces ticked — tick at least one to apply cleanup")
        return checked

    def apply_simplify(self) -> bool:
        scope = self._scope()
        if scope is not None and not scope:
            return False
        with self._busy("Simplifying …"):
            applied = self._push(SimplifyCommand(
                self.state, self.coast_tol.value() * 1000,
                self.internal_tol.value() * 1000, self._refresh, only=scope))
        if applied:
            self.clear_step2_previews()
        return applied

    def apply_island_filter(self) -> bool:
        scope = self._scope()
        if scope is not None and not scope:
            return False
        applied = self._push(FilterIslandsCommand(
            self.state, self.island_min_area.value() * 1e6,
            self._refresh, only=scope))
        if applied:
            self.clear_step2_previews()
        return applied

    def apply_close_gaps(self) -> bool:
        scope = self._scope()
        if scope is not None and not scope:
            return False
        return self._push(CloseGapsCommand(
            self.state, self.gap_close.value() * 1000, self._refresh, only=scope))

    def apply_remove_lakes(self) -> bool:
        scope = self._scope()
        if scope is not None and not scope:
            return False
        return self._push(RemoveLakesCommand(self.state, self._refresh, only=scope))

    def apply_remove_parts(self) -> bool:
        name, indices = self.selected_parts()
        if not name or not indices:
            self.statusBar().showMessage("Select one or more parts first")
            return False
        return self._push(RemoveIslandsCommand(
            self.state, name, indices, self._refresh))

    def apply_merge_parts(self) -> bool:
        name, indices = self.selected_parts()
        if not name or len(indices) < 2:
            self.statusBar().showMessage("Select at least two parts to merge")
            return False
        bridge_m = measure.mm_to_metres(self.bridge_width.value(), self.state.scale)
        return self._push(MergeIslandsCommand(
            self.state, name, indices, bridge_m, self._refresh))

    def checked_regions(self) -> set:
        """Names of the provinces ticked in the Step 2 selector."""
        return {self.part_region.item(row).data(Qt.UserRole)
                for row in range(self.part_region.count())
                if self.part_region.item(row).checkState() == Qt.Checked}

    def set_region_checked(self, name: str, checked: bool = True) -> bool:
        for row in range(self.part_region.count()):
            item = self.part_region.item(row)
            if item.data(Qt.UserRole) == name:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                return True
        return False

    def _current_parts(self) -> list:
        """Polygon parts of the region chosen in the Step 2 editor."""
        item = self.part_region.currentItem()
        if item is None:
            return []
        region = self.state.find(item.data(Qt.UserRole))
        return cleanup.island_parts(region.geometry) if region else []

    def highlight_selected_parts(self) -> int:
        """Highlight every part selected in the list. Returns how many."""
        parts = self._current_parts()
        rows = [i.data(Qt.UserRole) for i in self.part_list.selectedItems()]
        chosen = [parts[r] for r in rows if r is not None and 0 <= r < len(parts)]
        self.canvas.set_highlight(chosen)
        return len(chosen)

    def focus_part(self, row: int) -> bool:
        """Centre the map on one part (the press-and-hold gesture)."""
        parts = self._current_parts()
        if not 0 <= row < len(parts):
            return False

        part = parts[row]
        self.canvas.set_highlight([part])
        self.canvas.center_on_geometry(part, self.state.metres_per_mm)

        region_item = self.part_region.currentItem()
        name = region_item.data(Qt.UserRole) if region_item else "?"
        label = "mainland" if row == 0 else f"island {row}"
        self.statusBar().showMessage(
            f"{name} · {label} · {part.area / 1e6:,.0f} km²")
        return True

    def selected_parts(self) -> tuple[str | None, list[int]]:
        """(region name, selected part indices) from the Step 2 lists."""
        item = self.part_region.currentItem()
        if item is None:
            return None, []
        name = item.data(Qt.UserRole)
        indices = [i.data(Qt.UserRole) for i in self.part_list.selectedItems()]
        return name, sorted(i for i in indices if i is not None)

    def select_region_for_parts(self, name: str) -> bool:
        """Focus a region in the part editor (used by canvas clicks and tests)."""
        for row in range(self.part_region.count()):
            item = self.part_region.item(row)
            if item.data(Qt.UserRole) == name:
                self.part_region.setCurrentRow(row)
                return True
        return False

    # =======================================================================
    # Phase navigation (§1)
    # =======================================================================
    def _capture_phase(self) -> PhaseState:
        """Snapshot the working model as it stands right now."""
        return PhaseState(
            geometries={r.name: r.geometry for r in self.state.regions},
            terrain_path=self.state.terrain_path,
            border_result=self.border_result,
            export_summary=self.export_report.text(),
            assembly=self._assembly,
        )

    def _restore_phase(self, snapshot: PhaseState) -> None:
        """Put the working model back to a snapshot, discarding everything since."""
        self.state.regions = [r for r in self.state.regions
                              if r.name in snapshot.geometries]
        for region in self.state.regions:
            region.geometry = snapshot.geometries[region.name]
        self.state.terrain_path = snapshot.terrain_path

        self.border_result = snapshot.border_result
        if self.border_result is not None:
            self.canvas.set_border_overlay(self.border_result.strip,
                                           self.border_result.lobes)
            self.border_report.setText(f"{self.border_result.piece_count} "
                                       f"piece(s) restored.")
        else:
            self.canvas.clear_border_overlay()
            self.border_report.setText("Not built yet.")

        self._assembly = snapshot.assembly
        self.export_report.setText(snapshot.export_summary or
                                   "Nothing exported yet.")
        self.undo_stack.clear()       # Step 2 history belongs to the discarded run
        self.canvas.clear_attention()
        self.clear_step2_previews()

    # -----------------------------------------------------------------------
    def discard_summary(self, target: int) -> list[str]:
        """
        What a Back to ``target`` would actually destroy, in plain sentences.

        Generated from live state — piece counts, edit counts, whether meshing
        has run — never from a fixed string, so the dialog cannot promise a cost
        that does not match the project in front of the user.
        """
        losses = []
        current = self.current_step

        if current > STEP_BORDERS and self.export_report.text().startswith(
                "Exported"):
            losses.append("the export summary and assembly diagram")

        if target <= STEP_BORDERS and self.border_result is not None:
            result = self.border_result
            losses.append(
                f"the border corridor: {result.piece_count} piece(s) and "
                f"{len(result.lobes)} connector(s)")

        if target <= STEP_CLEANUP:
            edits = self.undo_stack.count()
            if edits:
                losses.append(f"{edits} cleanup edit(s) — Step 2 restarts from "
                              f"the imported outlines")

        if target <= STEP_IMPORT:
            count = len(self.state.regions)
            if count:
                terrain = (" and the terrain raster"
                           if self.state.terrain_path else "")
                losses.append(f"{count} imported region(s){terrain}")

        # Meshing is the expensive one, so name the price of rebuilding it.
        if current == STEP_EXPORT and self.state.targets:
            seconds = self._mesh_seconds_estimate()
            if seconds:
                losses.append(f"the generated meshes (~{seconds:.0f}s to rebuild)")
        return losses

    def _mesh_seconds_estimate(self) -> float:
        """Rough rebuild cost of a meshing run, or 0 for the flat-slab path."""
        if not self.state.terrain_path:
            return 0.0
        return len(self.state.targets) * SECONDS_PER_TERRAIN_REGION

    def _confirm_back(self, target: int) -> bool:
        """Ask before destroying; returns whether the user consented."""
        losses = self.discard_summary(target)
        if not losses:
            return True                       # nothing to lose, no ceremony

        body = "\n".join(f"  • {item}" for item in losses)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Go back and discard work?")
        box.setText(f"Going back to {STEPS[target]} discards:")
        box.setInformativeText(
            f"{body}\n\nThis cannot be undone once you continue working. "
            f"To take back a single Step 2 edit instead, use Undo.")
        discard = box.addButton("Go back and discard", QMessageBox.DestructiveRole)
        box.addButton("Stay here", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        return box.clickedButton() is discard

    def go_to_step(self, index: int) -> None:
        """Show a phase, clamped to the valid range, and restyle the header."""
        index = max(0, min(len(STEPS) - 1, index))
        previous = self.pages.currentIndex()
        self.pages.setCurrentIndex(index)

        if index > previous:
            # Entering a phase forward: remember what it started from, so a
            # later Back can put the model exactly here again.
            self._phase_entry[index] = self._capture_phase()

        if index != STEP_CLEANUP:
            self.clear_step2_previews()
        if index == STEP_BORDERS:
            self.update_bed_preview()
        self._sync_stepper()

    def go_next(self) -> bool:
        """
        Confirm the current phase and advance — the heavy commit.

        Readiness is checked *here*, at the turnstile, rather than after the
        user has paid the forward cost: anything that would fail later is
        surfaced now, with the offending region highlighted, so it can be fixed
        without a destructive walk backwards.
        """
        if self.current_step >= len(STEPS) - 1:
            return False
        if not self._confirm_forward(self.current_step + 1):
            return False
        self._invalidate_restore()
        self.go_to_step(self.current_step + 1)
        return True

    def go_back(self) -> bool:
        """Return to the previous phase, discarding it and everything after."""
        current = self.current_step
        if current <= 0:
            return False
        target = current - 1

        if not self._confirm_back(target):
            return False

        # Stash exactly one step of restore for an immediate misclick (R1.4).
        # It restores the phase output that was just discarded and nothing else,
        # so it cannot reintroduce stale downstream state.
        self._back_restore = (current, self._capture_phase())

        snapshot = self._phase_entry.get(target)
        if snapshot is not None:
            self._restore_phase(snapshot)
        self.go_to_step(target)
        self._refresh()

        losses = ", ".join(self.discard_summary(target)) or "no work"
        self.restore_toast.show_toast(
            f"Went back to {STEPS[target]} and discarded {losses}.")
        self.statusBar().showMessage(f"Back to {STEPS[target]} — phase reset")
        return True

    def restore_last_back(self) -> bool:
        """
        Take back an immediately accidental Back (R1.4).

        Available only until the next action: once the user has edited anything
        in the phase they went back to, restoring would drop discarded output
        onto a model that has already moved on, which is exactly the stale
        downstream state this design refuses to allow.
        """
        if self._back_restore is None:
            return False
        step, snapshot = self._back_restore
        self._back_restore = None
        self.restore_toast.hide_toast()

        self._restore_phase(snapshot)
        self.pages.setCurrentIndex(step)
        self._sync_stepper()
        self._refresh()
        if step == STEP_BORDERS:
            self.update_bed_preview()
        self.statusBar().showMessage(f"Restored {STEPS[step]}")
        return True

    def _invalidate_restore(self) -> None:
        """Any real action retires the one-step Back restore."""
        self._back_restore = None
        if self.restore_toast.is_showing:
            self.restore_toast.hide_toast()

    @property
    def can_restore_back(self) -> bool:
        return self._back_restore is not None

    # -----------------------------------------------------------------------
    # Pre-confirm readiness (§2.2)
    # -----------------------------------------------------------------------
    def readiness_checks(self, target: int) -> list[tuple[bool, str, str]]:
        """
        ``(ok, subject, message)`` for everything checked at this turnstile.

        Cheap by design — this runs before the expensive step, so it may only
        inspect geometry that is already in hand.
        """
        checks: list[tuple[bool, str, str]] = []

        if target == STEP_BORDERS:
            targets = self.state.targets
            checks.append((bool(targets), "Regions",
                           f"{len(targets)} target region(s) to print"
                           if targets else "No target regions — nothing to print"))
            for region in targets:
                empty = region.geometry is None or region.geometry.is_empty
                checks.append((not empty, region.name,
                               "outline is empty" if empty else "outline present"))

        if target == STEP_EXPORT:
            built = self.border_result is not None
            checks.append((built, "Borders",
                           "corridor and connectors built" if built
                           else "borders not built — Step 4 will export regions "
                                "only, with nothing joining them"))

            if built:
                settings = self.border_settings()
                oversized = self.border_result.oversized(
                    settings.bed_mm, self.state.scale, settings.margin_mm)
                checks.append((not oversized, "Bed fit",
                               "every corridor piece fits the bed" if not oversized
                               else f"{len(oversized)} corridor piece(s) exceed "
                                    f"the {settings.bed_mm:.0f} mm bed"))

                # R2.5 / R5.4 — the connector-per-piece check.
                orphans = self.border_result.pieces_without_connectors()
                checks.append((not orphans, "Connectors",
                               "every corridor piece has a seated connector"
                               if not orphans
                               else "corridor piece(s) "
                                    + ", ".join(str(i + 1) for i in orphans)
                                    + " have no connector and would float free"))

            # R2.6 — a lightweight per-region "does this look exportable" pass.
            for region in self.state.targets:
                ok, note = self._region_readiness(region)
                checks.append((ok, region.name, note))
        return checks

    def _region_readiness(self, region) -> tuple[bool, str]:
        """A cheap, geometry-only guess at whether a region will mesh."""
        geom = region.geometry
        if geom is None or geom.is_empty:
            return False, "outline is empty — nothing to mesh"
        if geom.area <= 0:
            return False, "outline has no area"
        if not geom.is_valid:
            return False, "outline is self-intersecting — clean it in Step 2"

        parts = cleanup.island_parts(geom)
        mpm = self.state.metres_per_mm
        # A part under a few printed mm² triangulates into slivers that fail the
        # watertight check; catching it here is far cheaper than at export.
        tiny = [p for p in parts if p.area / (mpm ** 2) < 1.0]
        if tiny:
            return False, (f"{len(tiny)} landmass(es) under 1 mm² at this scale — "
                           f"filter them in Step 2 or they will fail to mesh")

        w_mm, h_mm = measure.size_mm(geom, self.state.scale)
        pieces = len(meshing.bed_footprints(
            geom, self.state.scale, self.bed_size.value(),
            self.bed_margin.value()))
        return True, (f"{len(parts)} landmass(es), {w_mm:.0f} × {h_mm:.0f} mm, "
                      f"{pieces} bed piece(s)")

    def _confirm_forward(self, target: int) -> bool:
        """Show the readiness summary at the turnstile; block on failures."""
        checks = self.readiness_checks(target)
        failures = [c for c in checks if not c[0]]
        if not checks:
            return True

        # R2.7 — highlight what is wrong, on the map, before they pay anything.
        region_names = {r.name for r in self.state.regions}
        self.canvas.set_attention(
            {subject for _ok, subject, _ in failures if subject in region_names})

        if not failures:
            self.canvas.clear_attention()
            if target != STEP_EXPORT:
                return True

        lines = "\n".join(f"  {'✓' if ok else '✗'}  {subject}: {note}"
                          for ok, subject, note in checks)
        box = QMessageBox(self)
        box.setWindowTitle(f"Confirm → {STEPS[target]}")
        box.setIcon(QMessageBox.Warning if failures else QMessageBox.Information)
        box.setText(
            f"{len(checks) - len(failures)} of {len(checks)} checks passed."
            if failures else "Everything looks ready.")
        box.setInformativeText(
            f"{lines}\n\n"
            + ("Fix the failures above before continuing — going back later "
               "discards this phase's work."
               if failures else
               "Confirming moves forward. Coming back later discards this "
               "phase's work, so review the map first."))
        proceed = box.addButton(
            "Continue anyway" if failures else "Continue",
            QMessageBox.AcceptRole)
        box.addButton("Stay here", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1] if failures else proceed)
        box.exec()
        return box.clickedButton() is proceed

    @property
    def current_step(self) -> int:
        return self.pages.currentIndex()

    def _sync_stepper(self) -> None:
        """Highlight the current phase; mute the done and upcoming ones."""
        current = self.current_step
        for index, label in enumerate(self.step_labels):
            if index == current:
                style = ("font-weight:700; color:#0B5394; background:#D6EAF8;"
                         "border-radius:4px; padding:4px 12px;")
            elif index < current:
                style = "color:#3C763D; padding:4px 12px;"      # completed
            else:
                style = "color:#9A9A9A; padding:4px 12px;"      # upcoming
            label.setStyleSheet(style)

        if self.is_busy:
            return                    # _busy owns the buttons until it releases
        self._sync_selection_controls()
        self.back_button.setEnabled(current > 0)
        self.next_button.setEnabled(current < len(STEPS) - 1)
        self.next_button.setText(
            "Confirm  →" if current < len(STEPS) - 1 else "Finished")

    def _remove_selected(self) -> None:
        item = self.region_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        if self.state.remove_region(name):
            self._refresh()
            self._invalidate_restore()
            self.statusBar().showMessage(f"Removed {name}")

    def _on_scale_changed(self, value: int) -> None:
        self.state.scale = float(value)
        self._refresh()

    # -----------------------------------------------------------------------
    # Dialog wrappers
    # -----------------------------------------------------------------------
    def _choose_files(self, role: str) -> None:
        start = PROVINCES_DIR if PROVINCES_DIR.exists() else Path.cwd()
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Select {role} province GeoJSON files",
            str(start), GEOJSON_FILTER)
        self.import_regions(paths, role)

    def _choose_terrain(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select terrain GeoTIFF", str(Path.cwd()),
            "GeoTIFF (*.tif *.tiff);;All files (*)")
        if path:
            self.set_terrain(path)

    # =======================================================================
    # Refresh
    # =======================================================================
    def _refresh(self, command=None) -> None:
        """
        Redraw everything after a change.

        ``command`` is the :class:`~app.commands.GeometryCommand` that caused
        this refresh, if any.  It carries an input→output landmass mapping, so
        a selection can follow an edit onto its successor instead of being
        cleared — merging three islands leaves you on the merged landmass.
        """
        selection = self._selection_after(command)
        self.canvas.render_project(self.state)

        self.region_list.clear()
        for region in self.state.regions:
            w, h = region.size_mm(self.state.scale)
            item_text = f"{region.name}  ·  {w:.0f} × {h:.0f} mm"
            self.region_list.addItem(item_text)
            list_item = self.region_list.item(self.region_list.count() - 1)
            list_item.setData(Qt.UserRole, region.name)
            if region.role == SURROUNDING:
                list_item.setForeground(Qt.gray)

        self._refresh_region_chooser()
        self._reapply_selection(selection)
        self.summary_label.setText(self.state.summary())

    def _selection_after(self, command) -> tuple[str | None, list[int]]:
        """
        Where the Step 2 selection should land once this edit is drawn.

        Without a command there is nothing to map through, so the selection is
        dropped (it may point at geometry that no longer exists).  With one, each
        selected index is re-resolved through the command's mapping; indices
        whose landmass genuinely disappeared are dropped, and the rest survive.
        """
        name, indices = self.selected_parts()
        if command is None or name is None or not indices:
            self.canvas.clear_highlight()
            return None, []
        if name not in command.touched_regions:
            return name, indices        # this region was untouched — keep as is

        mapping = command.part_mapping(name)
        resolved = {mapping.get(i) for i in indices}
        return name, sorted(i for i in resolved if i is not None)

    def _reapply_selection(self, selection) -> None:
        """Re-select the reconciled rows, or leave nothing selected."""
        name, indices = selection
        if not name or not indices:
            self.canvas.clear_highlight()
            return
        if not self.select_region_for_parts(name):
            return

        self.part_list.clearSelection()
        for row in range(self.part_list.count()):
            if self.part_list.item(row).data(Qt.UserRole) in indices:
                self.part_list.item(row).setSelected(True)
        self.highlight_selected_parts()

    def _refresh_region_chooser(self) -> None:
        """
        Repopulate the Step 2 province selector.

        Each province carries a tick box (which provinces the bulk cleanup
        steps act on) and its landmass count.  Both the current row and the
        tick states are preserved across refreshes.
        """
        previous = None
        if self.part_region.currentItem() is not None:
            previous = self.part_region.currentItem().data(Qt.UserRole)
        was_checked = self.checked_regions() if self.part_region.count() else None

        self.part_region.blockSignals(True)
        self.part_region.clear()
        for region in self.state.regions:
            count = len(cleanup.island_parts(region.geometry))
            plural = "landmass" if count == 1 else "landmasses"
            self.part_region.addItem(f"{region.name}  ·  {count} {plural}")
            item = self.part_region.item(self.part_region.count() - 1)
            item.setData(Qt.UserRole, region.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # New provinces start ticked, so cleanup applies unless opted out
            ticked = was_checked is None or region.name in was_checked
            item.setCheckState(Qt.Checked if ticked else Qt.Unchecked)
        self.part_region.blockSignals(False)

        if self.part_region.count():
            names = [self.part_region.item(r).data(Qt.UserRole)
                     for r in range(self.part_region.count())]
            self.part_region.setCurrentRow(
                names.index(previous) if previous in names else 0)
        self._refresh_part_list()

    def _refresh_part_list(self) -> None:
        """List the polygon parts of the region selected in Step 2."""
        self.part_list.clear()
        item = self.part_region.currentItem()
        if item is None:
            return
        region = self.state.find(item.data(Qt.UserRole))
        if region is None:
            return

        for index, area in enumerate(cleanup.island_areas_m2(region.geometry)):
            label = "mainland" if index == 0 else f"island {index}"
            self.part_list.addItem(f"{label}  ·  {area / 1e6:,.0f} km²")
            self.part_list.item(self.part_list.count() - 1).setData(
                Qt.UserRole, index)


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("font-weight:600; font-size:13px; margin-top:6px;")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#777; font-size:11px;")
    return label


def _km_spin(value: float, maximum: float = 1000.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0.0, maximum)
    # 2 decimals so the 0.25 step lands on exact values instead of rounding.
    spin.setDecimals(2)
    spin.setSingleStep(0.25)
    spin.setValue(value)
    return spin


def _mm_spin(value: float, maximum: float = 100.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0.1, maximum)
    spin.setDecimals(2)
    spin.setSingleStep(0.5)
    spin.setValue(value)
    return spin
