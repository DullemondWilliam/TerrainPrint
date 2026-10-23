"""
UI_border_split.py

A standalone visual harness for ``LIB_border_split.split_to_bed``.

Workflow
--------
1. **Import STL …**   — load a border solid (e.g. a border bar saved by
                        ``UI_terrain_editor``).
2. **Bed / Margin**   — set the usable print-bed size (mm) and the keep-clear
                        margin taken off each edge.
3. **Split**          — subdivide the bar into bed-fitting pieces (off the UI
                        thread, with an indeterminate progress bar) and preview
                        each piece in a different colour.
4. **Save Pieces …**  — write every piece as a numbered ``.stl`` into a folder.

The whole input bar is previewed on the left; the bed-fitting pieces, one colour
each, on the right.  Both panels are GPU-accelerated ``vispy`` scenes (drag to
rotate, scroll to zoom) — matplotlib's ``mplot3d`` is a CPU painter's-algorithm
renderer that cannot keep up when a dense mesh is rotated.

Run with:  python harnesses/UI_border_split.py

Like the other UI_* harnesses this stays independent of the editors so it can
be pointed at any STL without setting up a project.  It works purely on the
mesh, so — like the lib — it has no knowledge of dovetail connectors and a cut
may cross one; the terrain editor plans its splits around them instead.
"""

import sys
from itertools import cycle
from pathlib import Path

import numpy as np
import trimesh
import vispy
from vispy import scene

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_border_split.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_border_split import load_bed_params, split_to_bed
from lib.LIB_project_config import settings_files

STL_FILTER = "STL (*.stl);;All files (*)"

INPUT_COLOUR = "#AED6F1"    # blue — the whole imported bar
BG_COLOUR = "#FDFEFE"       # near-white scene background
# Distinct fills cycled across the split pieces so each cut reads clearly.
PIECE_COLOURS = ["#ABEBC6", "#F9E79F", "#F5B7B1", "#D7BDE2",
                 "#A9DFBF", "#FAD7A0", "#AED6F1", "#F5CBA7"]


def _rgba(hex_colour: str, alpha: float = 1.0) -> tuple:
    """'#RRGGBB' -> (r, g, b, a) floats in 0..1 for vispy."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


class SplitWorker(QObject):
    """Runs ``split_to_bed`` off the UI thread (the boolean cuts can be slow)."""
    done = Signal(object)   # list[trimesh.Trimesh]
    failed = Signal(str)

    def __init__(self, mesh, bed, margin):
        super().__init__()
        self._mesh = mesh
        self._bed = bed
        self._margin = margin

    def run(self) -> None:
        try:
            pieces = split_to_bed(self._mesh, bed_mm=self._bed,
                                  margin_mm=self._margin)
        except Exception as exc:      # surface any geometry failure to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(pieces)


class BorderSplitTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Border Split — Visual Tester")
        self.resize(1200, 720)

        # -- state ----------------------------------------------------------
        self.source = None                 # imported trimesh.Trimesh
        self.pieces = None                 # last split result (list)
        self.source_path: Path | None = None
        self._thread: QThread | None = None
        self._worker: SplitWorker | None = None
        self._before_meshes: list = []     # vispy visuals currently shown
        self._after_meshes: list = []

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar --------------------------------------------------------
        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import STL …")
        self.btn_import.clicked.connect(self.import_stl)

        saved = load_bed_params(*settings_files())
        self.bed_w_spin = self._mm_spin(saved.bed_mm, "Usable bed width")
        self.bed_d_spin = self._mm_spin(saved.bed_mm, "Usable bed depth")
        self.margin_spin = self._mm_spin(saved.margin_mm, "Keep-clear margin per side")
        self.margin_spin.setRange(0.0, 1000.0)
        for spin in (self.bed_w_spin, self.bed_d_spin, self.margin_spin):
            spin.valueChanged.connect(self._invalidate)

        self.btn_split = QPushButton("Split")
        self.btn_split.clicked.connect(self.split)
        self.btn_split.setEnabled(False)
        self.btn_save = QPushButton("Save Pieces …")
        self.btn_save.clicked.connect(self.save_pieces)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(QLabel("Bed W:"))
        toolbar.addWidget(self.bed_w_spin)
        toolbar.addWidget(QLabel("Bed D:"))
        toolbar.addWidget(self.bed_d_spin)
        toolbar.addWidget(QLabel("Margin:"))
        toolbar.addWidget(self.margin_spin)
        toolbar.addWidget(self.btn_split)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- viewports: whole bar | split pieces (two GPU scenes) ----------
        panels = QHBoxLayout()
        self.before_canvas, self.before_view = self._make_scene()
        self.after_canvas, self.after_view = self._make_scene()
        panels.addLayout(self._titled("Input bar", self.before_canvas))
        panels.addLayout(self._titled("Split pieces", self.after_canvas))
        root.addLayout(panels, stretch=1)

        # -- status + progress ---------------------------------------------
        self.status = QLabel("No file loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- construction helpers ----------------------------------------------
    @staticmethod
    def _mm_spin(value: float, tip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(0.01, 10_000.0)
        spin.setSingleStep(1.0)
        spin.setValue(value)
        spin.setSuffix(" mm")
        spin.setToolTip(tip)
        return spin

    @staticmethod
    def _make_scene():
        """A vispy SceneCanvas with a turntable-camera view; returns (canvas, view)."""
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    @staticmethod
    def _titled(title: str, canvas) -> QVBoxLayout:
        col = QVBoxLayout()
        label = QLabel(title)
        label.setStyleSheet("color:#555;")
        col.addWidget(label)
        col.addWidget(canvas.native, stretch=1)
        return col

    # -- mesh drawing -------------------------------------------------------
    @staticmethod
    def _clear(visuals: list) -> None:
        for v in visuals:
            v.parent = None
        visuals.clear()

    def _add_mesh(self, view, mesh, colour, store: list) -> None:
        vis = scene.visuals.Mesh(
            vertices=np.asarray(mesh.vertices, dtype=np.float32),
            faces=np.asarray(mesh.faces),
            color=colour, shading="flat", parent=view.scene)
        store.append(vis)

    def _draw_before(self) -> None:
        self._clear(self._before_meshes)
        if self.source is not None:
            self._add_mesh(self.before_view, self.source,
                           _rgba(INPUT_COLOUR), self._before_meshes)
            self.before_view.camera.set_range()

    def _draw_after(self) -> None:
        self._clear(self._after_meshes)
        if self.pieces:
            for piece, hexc in zip(self.pieces, cycle(PIECE_COLOURS)):
                self._add_mesh(self.after_view, piece, _rgba(hexc),
                               self._after_meshes)
            self.after_view.camera.set_range()

    # -- import -------------------------------------------------------------
    def import_stl(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import border STL", str(Path.cwd()), STL_FILTER)
        if not path:
            return
        try:
            mesh = trimesh.load(path, force="mesh")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
            QMessageBox.warning(self, "Nothing to show",
                                "That file did not load as a single mesh.")
            return

        self.source = mesh
        self.source_path = Path(path)
        self.pieces = None
        self.btn_split.setEnabled(True)
        self.btn_save.setEnabled(False)
        self._draw_before()
        self._clear(self._after_meshes)
        w, d, _ = mesh.extents
        self.status.setText(
            f"Loaded a {w:.1f} x {d:.1f} mm bar "
            f"({len(mesh.faces)} faces). Set the bed and click ‘Split’.")

    # -- parameters ---------------------------------------------------------
    def _invalidate(self, *_) -> None:
        """A bed/margin change invalidates the last split; disable Save."""
        if self.pieces is None:
            return
        self.pieces = None
        self.btn_save.setEnabled(False)
        self._clear(self._after_meshes)
        if self.source is not None:
            self.status.setText("Parameters changed. Click ‘Split’ to rebuild.")

    # -- split --------------------------------------------------------------
    def split(self) -> None:
        # Guard against launching a second split while one is running.
        if self.source is None or self._thread is not None:
            return
        bed = (self.bed_w_spin.value(), self.bed_d_spin.value())

        self._set_busy(True)
        self.status.setText(
            f"Splitting for a {bed[0]:.0f} x {bed[1]:.0f} mm bed …")

        # The boolean cuts are a single blocking geometry call, so run them off
        # the UI thread and drive an indeterminate progress bar meanwhile.
        self._thread = QThread(self)
        self._worker = SplitWorker(self.source, bed, self.margin_spin.value())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_split)
        self._worker.failed.connect(self._on_split_failed)
        self._thread.start()

    def _on_split(self, pieces) -> None:
        self.pieces = pieces
        self._teardown_thread()
        self._set_busy(False)
        self._draw_after()
        self.btn_save.setEnabled(True)
        self.status.setText(
            f"Split into {len(pieces)} bed-fitting piece(s). "
            "Inspect on the right, then save.")

    def _on_split_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Split failed", message)
        self.status.setText(f"Split failed: {message}")

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
        for w in (self.btn_import, self.bed_w_spin, self.bed_d_spin,
                  self.margin_spin, self.btn_split, self.btn_save):
            w.setEnabled(not busy)

    # -- save ---------------------------------------------------------------
    def save_pieces(self) -> None:
        if not self.pieces:
            return
        start = str(self.source_path.parent) if self.source_path else str(Path.cwd())
        folder = QFileDialog.getExistingDirectory(
            self, "Save pieces into folder", start)
        if not folder:
            return
        stem = self.source_path.stem if self.source_path else "border"
        try:
            for i, piece in enumerate(self.pieces, start=1):
                piece.export(str(Path(folder) / f"{stem}_{i:03d}.stl"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {len(self.pieces)} piece(s) to {Path(folder).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")            # bind vispy to the running Qt app
    window = BorderSplitTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
