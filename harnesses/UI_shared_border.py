"""
UI_shared_border.py

A standalone visual harness for ``LIB_shared_border.simplify_shared_border``.

Workflow
--------
1. **Import Province A …** — one side of the border.
2. **Import Province B …** — its neighbour.  As soon as both are in, the seam
                             they share is found and the status line reports
                             what is wrong with it: how many vertices, how many
                             segments below a nozzle width, how many spikes.
3. **Tolerance / Min seg / Min angle / Smooth / Nozzle / Max drift** — the
                             conditioning parameters, in printed millimetres.
                             Defaults come from ``project_config.json``.
4. **Simplify Seam**       — run the lib off the UI thread with a progress bar.
5. **Zoom to Seam**        — the seam is a thin thing on two large provinces;
                             this frames it so the change is actually visible.
6. **Save Both …**         — write the rebuilt pair into a chosen folder.

The **left** panel is the pair as imported with the original seam in red; the
**right** is the rebuilt pair with the conditioned seam in green and the
original ghosted behind it, so the drift is visible rather than merely reported.
The two panels move together -- pan or zoom either and the other follows, which
is what makes a before/after on a border worth looking at -- and they keep a
**constant size** whatever shape the seam is, rather than collapsing into two
tall slivers for a north-south border like BC/Alberta and two letterboxes for an
east-west one.  See the note above the axes for how those two wants are
reconciled.

Run with:  python harnesses/UI_shared_border.py

Like the other UI_* harnesses this stays independent of the main ``app`` window.

Why this one is threaded
------------------------
Most pairs are quick -- Ontario/Quebec is 0.4 s, British Columbia/Alberta 0.6 s.
But Northwest Territories/Nunavut takes **7.7 s**: both are island archipelagos,
and the union and polygonize behind the rebuild are doing real work over
thousands of rings.  A seven-second frozen window is not acceptable, so the run
goes on a ``QThread`` with an indeterminate progress bar, per
``BUILDING_UI_HARNESSES`` §11.C.

What the harness is for
-----------------------
The lib reports gap and overlap, and refuses a result that has either.  So the
question this harness answers is not "did it work" but **"is the simplification
one I want"** -- which is a judgement about a picture:

*Did it keep the border honest?*  The ghosted original shows where the new seam
departs from it, and by how much; ``max_deviation_mm`` is the number for the
same thing.

*Did it actually fix the printability?*  Sub-nozzle and spike counts are shown
before and after, measured against the printer rather than against the filter.

*Did it take too much?*  A seam simplified past recognition still passes every
check in the lib.  Only looking at it tells you that.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.ops import unary_union

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_shared_border.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_shared_border import (DEFAULT_SCALE, EPSG_METRIC, SeamParams,
                                   _count_short, _count_spikes, _shared_seam,
                                   simplify_shared_border)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"

KEEP_COLOUR = "#AED6F1"          # blue   — province A
SURROUNDING_COLOUR = "#E5E7E9"   # grey   — province B
MARK_COLOUR = "#E6787A"          # red    — the seam as it came in
RESULT_COLOUR = "#27AE60"        # green  — the conditioned seam
EDGE_COLOUR = "#1B4F72"          # dark blue outline

#: Padding around the seam when framing it, as a fraction of its extent.
SEAM_ZOOM_PAD = 0.08
#: Draw vertex markers only while few enough to tell apart.
MAX_VERTEX_MARKERS = 4000


class SimplifyWorker(QObject):
    """Runs the lib off the UI thread (7.7 s on the worst pair)."""
    done = Signal(object)   # SeamResult
    failed = Signal(str)

    def __init__(self, geom_a, geom_b, params):
        super().__init__()
        self._a = geom_a
        self._b = geom_b
        self._params = params

    def run(self) -> None:
        try:
            # Geometries, not paths: the harness has already read and
            # reprojected them, and re-reading on a worker thread would make
            # the picture and the run disagree about what was simplified.
            result = simplify_shared_border(self._a, self._b, self._params)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class SharedBorderTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shared Border — Visual Tester")
        self.resize(1250, 720)

        # -- state ----------------------------------------------------------
        self.geom_a = None               #: province A, dissolved, in the work CRS
        self.geom_b = None
        self.path_a: Path | None = None
        self.path_b: Path | None = None
        self.source_crs = None           #: what the inputs carried, for saving back
        self.seam = None                 #: the seam as imported
        self.result = None               #: last SeamResult, ready to save
        self._thread: QThread | None = None
        self._worker: SimplifyWorker | None = None

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar: inputs and actions ------------------------------------
        toolbar = QHBoxLayout()
        self.btn_a = QPushButton("Import Province A …")
        self.btn_a.clicked.connect(lambda: self.import_province("a"))
        self.btn_b = QPushButton("Import Province B …")
        self.btn_b.clicked.connect(lambda: self.import_province("b"))
        self.btn_run = QPushButton("Simplify Seam")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)
        self.btn_zoom = QPushButton("Zoom to Seam")
        self.btn_zoom.clicked.connect(self.zoom_to_seam)
        self.btn_zoom.setEnabled(False)
        self.btn_save = QPushButton("Save Both …")
        self.btn_save.clicked.connect(self.save_both)
        self.btn_save.setEnabled(False)
        for widget in (self.btn_a, self.btn_b, self.btn_run, self.btn_zoom,
                       self.btn_save):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        # -- second row: the conditioning parameters ------------------------
        params_bar = QHBoxLayout()
        defaults = SeamParams()
        self.tol_spin = self._spin(
            0.0, 20.0, defaults.tolerance_mm, 2,
            "Douglas-Peucker tolerance.\nDetail finer than this says nothing at "
            "print scale.")
        self.seg_spin = self._spin(
            0.0, 20.0, defaults.min_segment_mm, 2,
            "Collapse seam runs shorter than this.\nDouglas-Peucker measures "
            "perpendicular deviation, so it leaves\ntight zigzag intact; this "
            "is what removes it. 0 skips the pass.")
        self.angle_spin = self._spin(
            0.0, 180.0, defaults.min_angle_deg, 1,
            "Remove corners sharper than this.\nA needle prints as a fragile "
            "spur on one province and a slot\non the other. 0 skips the pass.")
        self.angle_spin.setSuffix(" °")
        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(0, 6)
        self.smooth_spin.setValue(int(defaults.smooth_iterations))
        self.smooth_spin.setToolTip(
            "Chaikin smoothing passes over what survives.\n0 keeps the seam "
            "angular; each pass rounds it further.")
        self.nozzle_spin = self._spin(
            0.01, 20.0, defaults.nozzle_mm, 2,
            "The printer's resolution.\nUsed for the *report* only: segments "
            "shorter than this are\ncounted as unprintable, whatever the "
            "filters are set to.")
        self.drift_spin = self._spin(
            0.0, 50.0, defaults.max_deviation_mm, 2,
            "Refuse a simplification that drags the border further than this\n"
            "from where it really runs.")
        for spin in (self.tol_spin, self.seg_spin, self.nozzle_spin,
                     self.drift_spin):
            spin.setSingleStep(0.05)
            spin.setSuffix(" mm")
        for widget in (self.tol_spin, self.seg_spin, self.angle_spin,
                       self.nozzle_spin, self.drift_spin):
            widget.valueChanged.connect(self._invalidate)
        self.smooth_spin.valueChanged.connect(self._invalidate)

        for label, widget in (("Tolerance:", self.tol_spin),
                              ("Min seg:", self.seg_spin),
                              ("Min angle:", self.angle_spin),
                              ("Smooth:", self.smooth_spin),
                              ("Nozzle:", self.nozzle_spin),
                              ("Max drift:", self.drift_spin)):
            params_bar.addWidget(QLabel(label))
            params_bar.addWidget(widget)
        params_bar.addStretch(1)
        root.addLayout(params_bar)

        # -- map: before | after --------------------------------------------
        # The panels keep a fixed size whatever shape the seam is. That rules
        # out matplotlib's default `adjustable="box"`, which resizes the axes
        # box to match the data -- a north-south border like BC/Alberta gives
        # two tall slivers, an east-west one gives two letterboxes, and the
        # window rearranges itself every time a different pair is imported.
        #
        # `adjustable="datalim"` fixes the box instead, but it cannot be used on
        # shared axes, and left to itself it *crops* to satisfy the aspect --
        # asking for a 20-unit-tall view returns 1.2 units and the seam leaves
        # the screen. So the axes are unshared, linked by hand (_link_views),
        # and every view is fitted to the panel's own shape first (_fit_view),
        # which leaves matplotlib nothing to crop.
        self.figure = Figure(figsize=(11, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import two neighbouring provinces", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        root.addWidget(self.mpl_toolbar)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No provinces loaded.")
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
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])

    def _panel_shape(self) -> float:
        """Width / height of one panel's slot, in inches.

        The slot, not the axes: with a fixed box they are the same thing, and
        asking the subplot spec avoids a circular call through ``apply_aspect``.
        """
        fig_w, fig_h = self.figure.get_size_inches()
        try:
            pos = self.ax_before.get_subplotspec().get_position(self.figure)
            width, height = pos.width * fig_w, pos.height * fig_h
        except Exception:                     # no layout yet; a sane fallback
            width, height = fig_w / 2.0, fig_h
        return width / height if height else 1.0

    def _fit_view(self, bounds) -> None:
        """
        Frame ``bounds`` in a view shaped like the panel, then link both panels.

        Expanding the view to the panel's own aspect ratio is what keeps the
        panel a constant size *and* everything visible: matplotlib is handed
        limits that already satisfy "equal", so it has nothing left to crop.
        The view only ever grows around the content -- never in, so whatever was
        asked for stays on screen.
        """
        minx, miny, maxx, maxy = bounds
        width = max(maxx - minx, 1e-9)
        height = max(maxy - miny, 1e-9)
        target = self._panel_shape()
        if width / height < target:
            width = height * target           # too tall for the panel: widen
        else:
            height = width / target           # too wide: heighten
        # The breathing room has to be proportional, not a constant added to
        # both axes: adding the same padding to a wide half-width and a narrow
        # half-height changes the ratio, and matplotlib then adjusts the view
        # after all -- cropping it, and cropping the two panels differently.
        grow = 1.0 + 2.0 * SEAM_ZOOM_PAD
        cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        half_w, half_h = width * grow / 2.0, height * grow / 2.0
        self.ax_before.set_xlim(cx - half_w, cx + half_w)
        self.ax_before.set_ylim(cy - half_h, cy + half_h)
        self._link_views()
        self.canvas.draw_idle()

    def _link_views(self) -> None:
        """Match the after panel to the before panel.

        Replaces the ``sharex``/``sharey`` the other 2-D harnesses use, which a
        fixed axes box cannot have: both panels are the same provinces in the
        same CRS, so they should still move together.
        """
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
        # Both axes scale by the same factor, so a view that already fits the
        # panel still fits it afterwards and nothing needs re-fitting.
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        if ax is self.ax_after:
            self.ax_before.set_xlim(ax.get_xlim())
            self.ax_before.set_ylim(ax.get_ylim())
        self._link_views()
        self.canvas.draw_idle()

    # -- import -------------------------------------------------------------
    def import_province(self, side: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Import province {side.upper()}", str(Path.cwd()),
            GEOJSON_FILTER)
        if not path:
            return
        try:
            frame = gpd.read_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not read the province", str(exc))
            return
        if frame.empty:
            QMessageBox.warning(self, "Nothing to work with",
                                "That file has no features.")
            return

        if self.source_crs is None:
            self.source_crs = frame.crs
        if frame.crs is not None and str(frame.crs) != str(EPSG_METRIC):
            frame = frame.to_crs(EPSG_METRIC)
        setattr(self, f"geom_{side}", unary_union(frame.geometry.values))
        setattr(self, f"path_{side}", Path(path))

        self._find_seam()
        self._invalidate()
        self._draw_before()
        if self.seam is not None:
            self.zoom_to_seam()

    def _find_seam(self) -> None:
        """The seam as imported, or None until both provinces are in."""
        self.seam = None
        if self.geom_a is None or self.geom_b is None:
            return
        try:
            self.seam = _shared_seam(self.geom_a, self.geom_b)
        except Exception:
            self.seam = None

    # -- parameters and the live report -------------------------------------
    def _params(self) -> SeamParams:
        return SeamParams(
            scale=DEFAULT_SCALE,
            tolerance_mm=self.tol_spin.value(),
            nozzle_mm=self.nozzle_spin.value(),
            min_segment_mm=self.seg_spin.value(),
            min_angle_deg=self.angle_spin.value(),
            smooth_iterations=self.smooth_spin.value(),
            max_deviation_mm=self.drift_spin.value())

    def seam_stats(self) -> dict:
        """
        What is wrong with the seam *as imported*, before anything is run.

        Measured with the lib's own counters against the printer limits on the
        toolbar, so the "before" numbers here and the ones the result reports
        are the same measurement, not two that happen to agree.
        """
        if self.seam is None:
            return {}
        params = self._params()
        nozzle = params.nozzle_mm * params.mpm
        vertices = short = spikes = 0
        for part in self.seam.geoms:
            coords = list(part.coords)
            vertices += len(coords)
            short += _count_short(coords, nozzle)
            spikes += _count_spikes(coords, params.spike_angle_deg)
        return {"parts": len(self.seam.geoms), "vertices": vertices,
                "short": short, "spikes": spikes,
                "length_mm": self.seam.length / params.mpm}

    def _status_text(self) -> str:
        if self.geom_a is None or self.geom_b is None:
            return "Import two neighbouring provinces."
        if self.seam is None:
            return ("These two do not share a border — nothing to simplify. "
                    "Check they are neighbours.")
        stats = self.seam_stats()
        return (f"Seam {stats['length_mm']:.0f} mm in {stats['parts']} part(s): "
                f"{stats['vertices']} vertices, {stats['short']} below the "
                f"nozzle, {stats['spikes']} spike(s). Click 'Simplify Seam'.")

    def _invalidate(self) -> None:
        """Any input or parameter change makes the last result stale."""
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_run.setEnabled(self.seam is not None)
        self.btn_zoom.setEnabled(self.seam is not None)
        self._clear_ax(self.ax_after, "Not yet simplified")
        self.canvas.draw_idle()
        self.status.setText(self._status_text())

    # -- run ----------------------------------------------------------------
    def run(self) -> None:
        # Guard against launching a second run while one is in flight.
        if self._thread is not None or self.seam is None:
            return
        self._set_busy(True)
        self.status.setText("Simplifying the seam …")

        self._thread = QThread(self)
        self._worker = SimplifyWorker(self.geom_a, self.geom_b, self._params())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        self._draw_after()
        self.btn_save.setEnabled(True)
        self.status.setText(
            f"Seam {result.seam_length_mm:.0f} mm: "
            f"{result.vertices_before} → {result.vertices_after} vertices "
            f"({result.vertex_reduction * 100:.0f}% fewer), "
            f"below-nozzle {result.short_segments_before} → "
            f"{result.short_segments_after}, "
            f"spikes {result.spikes_before} → {result.spikes_after}; "
            f"drift {result.max_deviation_mm:.2f} mm, "
            f"{result.area_moved_mm2:.2f} mm² changed hands, "
            f"gap {result.gap_area_mm2:.3g} mm². Inspect, then save.")

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Simplify failed", message)
        self.status.setText(f"Simplify failed: {message}")

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
        for widget in (self.btn_a, self.btn_b, self.btn_run, self.btn_save,
                       self.tol_spin, self.seg_spin, self.angle_spin,
                       self.smooth_spin, self.nozzle_spin, self.drift_spin):
            widget.setEnabled(not busy)
        if not busy:      # re-apply the preconditions the blanket enable ignored
            self.btn_run.setEnabled(self.seam is not None)
            self.btn_save.setEnabled(self.result is not None)

    # -- drawing ------------------------------------------------------------
    def _draw_before(self) -> None:
        title = "As imported"
        if self.seam is not None:
            stats = self.seam_stats()
            title = f"As imported — seam {stats['vertices']} vertices"
        self._clear_ax(self.ax_before, title)
        self._plot_province(self.ax_before, self.geom_b, SURROUNDING_COLOUR)
        self._plot_province(self.ax_before, self.geom_a, KEEP_COLOUR)
        if self.seam is not None:
            self._plot_seam(self.ax_before, self.seam, MARK_COLOUR, markers=True)
        self.canvas.draw_idle()

    def _draw_after(self) -> None:
        if self.result is None:
            self._clear_ax(self.ax_after, "Not yet simplified")
            self.canvas.draw_idle()
            return
        self._clear_ax(
            self.ax_after,
            f"Simplified — seam {self.result.vertices_after} vertices "
            f"({self.result.vertex_reduction * 100:.0f}% fewer)")
        self._plot_province(self.ax_after, self.result.b, SURROUNDING_COLOUR)
        self._plot_province(self.ax_after, self.result.a, KEEP_COLOUR)
        # The original, ghosted behind: drift you can see, not just read.
        self._plot_seam(self.ax_after, self.result.seam_original, MARK_COLOUR,
                        width=0.6, alpha=0.45)
        self._plot_seam(self.ax_after, self.result.seam, RESULT_COLOUR,
                        markers=True)
        # Clearing an axis resets its limits, so put the linked view back.
        self._link_views()
        self.canvas.draw_idle()

    @staticmethod
    def _plot_province(ax, geom, colour) -> None:
        """Fill a province, rings only — no geopandas, which reaches for pyplot."""
        if geom is None or geom.is_empty:
            return
        polygons = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for poly in polygons:
            xs, ys = poly.exterior.xy
            ax.fill(xs, ys, facecolor=colour, edgecolor=EDGE_COLOUR,
                    linewidth=0.4, zorder=1)
            for ring in poly.interiors:          # lakes stay holes, visibly
                xs, ys = ring.xy
                ax.fill(xs, ys, facecolor="white", edgecolor=EDGE_COLOUR,
                        linewidth=0.3, zorder=2)

    @staticmethod
    def _plot_seam(ax, seam, colour, width=1.6, alpha=1.0, markers=False) -> None:
        if seam is None or seam.is_empty:
            return
        parts = seam.geoms if hasattr(seam, "geoms") else [seam]
        total = sum(len(p.coords) for p in parts)
        for part in parts:
            xs, ys = part.xy
            ax.plot(xs, ys, color=colour, linewidth=width, alpha=alpha, zorder=3)
            # Vertices are the thing being counted, so show them — but only
            # while there are few enough to tell apart.
            if markers and total <= MAX_VERTEX_MARKERS:
                ax.plot(xs, ys, linestyle="none", marker="o", markersize=2.2,
                        color=colour, alpha=alpha, zorder=4)

    def zoom_to_seam(self) -> None:
        """
        Frame the seam.

        A border is a thin thing crossing two large provinces, so the default
        full-extent view is exactly the view in which none of this is visible.
        """
        if self.seam is None:
            return
        self._fit_view(self.seam.bounds)

    # -- save ---------------------------------------------------------------
    def save_both(self) -> None:
        if self.result is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save both provinces into …", str(Path.cwd()))
        if not folder:
            return
        folder = Path(folder)
        try:
            written = [self._write(self.result.a, self.path_a, folder),
                       self._write(self.result.b, self.path_b, folder)]
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {written[0].name} and {written[1].name}. "
            "Their shared border is identical on both.")

    def _write(self, geom, source_path, folder) -> Path:
        """One province, in the CRS it arrived in, named after its source."""
        stem = source_path.stem if source_path is not None else "province"
        out = folder / f"{stem}_simplified.geojson"
        frame = gpd.GeoDataFrame(geometry=[geom], crs=EPSG_METRIC)
        if self.source_crs is not None:
            frame = frame.to_crs(self.source_crs)
        frame.to_file(out, driver="GeoJSON")
        return out

    def closeEvent(self, event) -> None:
        self._teardown_thread()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = SharedBorderTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
