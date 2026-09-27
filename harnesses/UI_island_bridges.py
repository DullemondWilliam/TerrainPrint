"""
UI_island_bridges.py

A standalone visual harness for ``LIB_island_bridges``.

Workflow
--------
1. **Import GeoJSON …** — load one or more province files; every polygon part
                          becomes an island (worked in EPSG:3347 metres, so the
                          printed millimetres below are real ground distances).
2. **The bridge**       — width, embedment, the longest span allowed and the
                          slenderness limit.  All printed millimetres: at
                          1:8,000,000 a 1 mm bridge is 8 km of causeway.
3. **The search**       — coastline sampling pitch, what a grazing or badly
                          anchored landing costs in mm of span, the anchorage
                          floor, and the bracing rule.
4. **Plan Bridges**     — score the landings, take the minimum spanning tree and
                          union the slabs on (off the UI thread, with a progress
                          bar: 9 s on Newfoundland, 20 s on British Columbia).
5. **Zoom to Bridges**  — a 1 mm bridge is invisible at province extent.
6. **Save GeoJSON …**   — write the bridged geometry back out, CRS preserved.

The **left** panel is the plan: islands in blue, each primary bridge in red and
each brace in orange, dashed where a bridge is slender, labelled with its span in
millimetres.  The **right** panel is the same geometry once the slabs are
unioned on.  The report under them is the part worth reading — how many pairs
were in reach, how many had no landing good enough to use, and how many separate
landmasses are left.

Run with:  python harnesses/UI_island_bridges.py

A throwaway inspection tool, deliberately kept independent of the main ``app``
window, and styled after ``UI_island_merge.py`` and ``UI_shared_border.py``.
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_island_bridges.py), so put
# the project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_island_bridges import (EPSG_METRIC, BridgeParams, apply_bridges,
                                    plan_bridges)
from lib.LIB_island_merging import _polygons

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"

ISLAND_COLOUR = "#AED6F1"     # blue   — the islands as imported
RESULT_COLOUR = "#ABEBC6"     # green  — the bridged result
BRIDGE_COLOUR = "#E6787A"     # red    — a primary bridge (added land)
BRACE_COLOUR = "#F5B041"      # orange — a second bridge against hinging
EDGE_COLOUR = "#1B4F72"       # dark blue outline

#: Label bridges with their span only while few enough to read.
MAX_BRIDGE_LABELS = 60
#: Padding around the bridges when framing them, as a fraction of their extent.
BRIDGE_ZOOM_PAD = 0.15


class PlanWorker(QObject):
    """Runs the planner off the UI thread (20 s on British Columbia)."""
    done = Signal(object, object)    # BridgePlan, bridged geometry
    failed = Signal(str)

    def __init__(self, islands, params, keep_lagoons):
        super().__init__()
        self._islands = islands
        self._params = params
        self._keep_lagoons = keep_lagoons

    def run(self) -> None:
        try:
            # Geometries, not a path: the harness has already read and
            # reprojected them, and re-reading here would let the picture and
            # the plan disagree about what was bridged.
            plan = plan_bridges(self._islands, self._params)
            geom = apply_bridges(self._islands, plan,
                                 keep_lagoons=self._keep_lagoons)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(plan, geom)


class IslandBridgesTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Island Bridges — Visual Tester")
        self.resize(1250, 740)

        # -- state ----------------------------------------------------------
        #: One row per polygon part: the islands the plan is built over.
        self.islands: gpd.GeoDataFrame | None = None
        self.plan = None                 #: last BridgePlan, for drawing
        self.result = None               #: bridged geometry, ready to save
        self.source_paths: list[Path] = []
        self.source_crs = None           #: what the files carried, for saving back
        self._thread: QThread | None = None
        self._worker: PlanWorker | None = None

        central = QWidget()
        root = QVBoxLayout(central)

        # -- toolbar: inputs and actions ------------------------------------
        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)
        self.btn_run = QPushButton("Plan Bridges")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)
        self.btn_zoom = QPushButton("Zoom to Bridges")
        self.btn_zoom.clicked.connect(self.zoom_to_bridges)
        self.btn_zoom.setEnabled(False)
        self.btn_save = QPushButton("Save GeoJSON …")
        self.btn_save.clicked.connect(self.save_geojson)
        self.btn_save.setEnabled(False)
        for widget in (self.btn_import, self.btn_run, self.btn_zoom,
                       self.btn_save):
            toolbar.addWidget(widget)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        defaults = BridgeParams()

        # -- second row: the bridge itself ----------------------------------
        bridge_bar = QHBoxLayout()
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
        self.width_spin = self._spin(
            0.0, 50.0, defaults.width_mm, 3,
            "Bridge width — the specification, not a consequence.\nEvery bridge "
            "comes out exactly this wide. 1 mm is 3.3 nozzle\nwidths, about the "
            "thinnest run worth printing.")
        self.embed_spin = self._spin(
            0.0, 50.0, defaults.embed_mm, 3,
            "How far each end is pushed past the coastline into the land.\nThis "
            "is what makes the union a lap joint instead of a kiss;\none bridge "
            "width gives a square of overlap at each end.")
        self.span_spin = self._spin(
            0.0, 200.0, defaults.max_span_mm, 2,
            "Refuse to span further than this.\nAlso the cap that turns the "
            "spanning tree into a forest, so it\ndecides which islands are left "
            "separate.")
        self.aspect_spin = self._spin(
            0.0, 200.0, defaults.max_aspect, 1,
            "Slenderness limit, as span/width.\nPast it a bridge is a "
            "matchstick. Reported, not doubled: the fix\nis a wider bridge.")
        self.aspect_spin.setSuffix("x")
        self.drop_slender_check = QCheckBox("Drop slender")
        self.drop_slender_check.setChecked(defaults.drop_slender)
        self.drop_slender_check.setToolTip(
            "Refuse slender bridges instead of flagging them.\nOff by default: "
            "dropping one disconnects whatever it was holding on.")
        for label, widget in (("Scale:", self.scale_spin),
                              ("Width:", self.width_spin),
                              ("Embed:", self.embed_spin),
                              ("Max span:", self.span_spin),
                              ("Max aspect:", self.aspect_spin)):
            bridge_bar.addWidget(QLabel(label))
            bridge_bar.addWidget(widget)
        bridge_bar.addWidget(self.drop_slender_check)
        bridge_bar.addStretch(1)
        root.addLayout(bridge_bar)

        # -- third row: how the landings are searched and scored -------------
        search_bar = QHBoxLayout()
        self.sample_spin = self._spin(
            0.001, 50.0, defaults.sample_mm, 3,
            "Coastline sampling pitch for candidate landings.\nFiner finds "
            "better landings — and features smaller than the\npitch, such as a "
            "needle spit — at a linear cost in time.")
        self.angle_spin = self._spin(
            0.0, 50.0, defaults.angle_penalty_mm, 2,
            "What a fully grazing landing costs, in millimetres of span.\nA "
            "bridge meeting the coast at a shallow angle welds along a\nknife "
            "edge; at 2 mm it must save 2 mm of crossing to be taken.")
        self.anchor_pen_spin = self._spin(
            0.0, 50.0, defaults.anchor_penalty_mm, 2,
            "What no bite on the shore costs, in millimetres of span.\nScored "
            "in the same currency as the span, so the trade is legible.")
        self.anchor_spin = self._spin(
            0.0, 1.0, defaults.min_anchor, 2,
            "Anchorage floor: reject a landing biting less than this fraction\n"
            "of the width x embedment a straight shore would give.\nThis is "
            "what stops a 1 mm bridge welding to the tip of a spit.")
        self.anchor_spin.setSingleStep(0.05)
        self.per_pair_spin = QSpinBox()
        self.per_pair_spin.setRange(1, 4)
        self.per_pair_spin.setValue(int(defaults.max_per_pair))
        self.per_pair_spin.setToolTip(
            "Most bridges one pair may get.\n1 disables bracing entirely.")
        self.hinge_spin = self._spin(
            0.0, 100.0, defaults.hinge_ratio, 1,
            "Brace a pair only when two bridges could be placed this many\n"
            "crossings apart — that is when a single one acts as a hinge\nthe "
            "pair can pivot about.")
        self.hinge_spin.setSuffix("x")
        self.lagoon_check = QCheckBox("Keep lagoons")
        self.lagoon_check.setChecked(True)
        self.lagoon_check.setToolTip(
            "Keep water enclosed between the two bridges of a braced pair.\n"
            "A strait closed by a single bridge is filled either way.\n"
            "Re-applies the plan without re-planning it.")
        self.lagoon_check.stateChanged.connect(self._reapply)
        for label, widget in (("Sample:", self.sample_spin),
                              ("Angle pen.:", self.angle_spin),
                              ("Anchor pen.:", self.anchor_pen_spin),
                              ("Min anchor:", self.anchor_spin),
                              ("Max/pair:", self.per_pair_spin),
                              ("Hinge:", self.hinge_spin)):
            search_bar.addWidget(QLabel(label))
            search_bar.addWidget(widget)
        search_bar.addWidget(self.lagoon_check)
        search_bar.addStretch(1)
        root.addLayout(search_bar)

        for spin in (self.width_spin, self.embed_spin, self.span_spin,
                     self.sample_spin, self.angle_spin, self.anchor_pen_spin):
            spin.setSingleStep(0.05)
            spin.setSuffix(" mm")
        # Every parameter above invalidates the plan: the picture on the right
        # must never be the answer to a question that is no longer on screen.
        for widget in (self.scale_spin, self.width_spin, self.embed_spin,
                       self.span_spin, self.aspect_spin, self.sample_spin,
                       self.angle_spin, self.anchor_pen_spin, self.anchor_spin,
                       self.hinge_spin):
            widget.valueChanged.connect(self._invalidate)
        self.per_pair_spin.valueChanged.connect(self._invalidate)
        self.drop_slender_check.stateChanged.connect(self._invalidate)

        # -- map: plan | result ---------------------------------------------
        self.figure = Figure(figsize=(11, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        # Share the view so pan/zoom on either panel keeps both aligned.
        self.ax_after = self.figure.add_subplot(
            1, 2, 2, sharex=self.ax_before, sharey=self.ax_before)
        self._reset_axes("Import a GeoJSON to begin", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
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

    # -- parameters ---------------------------------------------------------
    def params(self) -> BridgeParams:
        """The parameter block exactly as the controls now read it."""
        return BridgeParams(
            scale=self.scale_spin.value(),
            width_mm=self.width_spin.value(),
            embed_mm=self.embed_spin.value(),
            max_span_mm=self.span_spin.value(),
            sample_mm=self.sample_spin.value(),
            angle_penalty_mm=self.angle_spin.value(),
            anchor_penalty_mm=self.anchor_pen_spin.value(),
            min_anchor=self.anchor_spin.value(),
            max_aspect=self.aspect_spin.value(),
            drop_slender=self.drop_slender_check.isChecked(),
            max_per_pair=self.per_pair_spin.value(),
            hinge_ratio=self.hinge_spin.value())

    def _invalidate(self, *_) -> None:
        """A changed parameter makes the last plan stale; drop it."""
        if self.plan is None and self.result is None:
            return
        self.plan = None
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_zoom.setEnabled(False)
        self._draw_plan(preserve_view=True)
        self._style_axis(self.ax_after, "Bridged  (not yet planned)")
        self.canvas.draw_idle()
        self.status.setText("Parameters changed — plan again.")

    def _reapply(self, *_) -> None:
        """Keep/fill the lagoons without re-planning: only the union changes."""
        if self.plan is None or self.islands is None:
            return
        try:
            self.result = apply_bridges(
                list(self.islands.geometry), self.plan,
                keep_lagoons=self.lagoon_check.isChecked())
        except Exception as exc:
            QMessageBox.critical(self, "Apply failed", str(exc))
            return
        self._draw_result()
        self.status.setText(self._report())

    # -- axes helpers -------------------------------------------------------
    def _reset_axes(self, left_title: str, right_title: str) -> None:
        for ax, title in ((self.ax_before, left_title),
                          (self.ax_after, right_title)):
            self._style_axis(ax, title)
        self.canvas.draw_idle()

    @staticmethod
    def _style_axis(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        """Zoom the panel under the cursor toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        # Keep the point under the cursor fixed while the span grows/shrinks.
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()      # shared axes carry the change to both panels

    def zoom_to_bridges(self) -> None:
        """Frame the bridges: 1 mm of causeway is nothing at province extent."""
        if self.plan is None or not self.plan.bridges:
            return
        boxes = [b.polygon.bounds for b in self.plan.bridges]
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        pad = max(x1 - x0, y1 - y0, 1.0) * BRIDGE_ZOOM_PAD
        self.ax_before.set_xlim(x0 - pad, x1 + pad)
        self.ax_before.set_ylim(y0 - pad, y1 + pad)
        self.canvas.draw_idle()

    # -- drawing ------------------------------------------------------------
    def _draw_plan(self, preserve_view: bool = False) -> None:
        """Islands on the left, with the planned bridges over them."""
        if preserve_view:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        title = "Islands" if self.plan is None else (
            f"Islands + {len(self.plan.bridges)} bridge(s)")
        self._style_axis(self.ax_before, title)
        if self.islands is not None and not self.islands.empty:
            self.islands.plot(ax=self.ax_before, facecolor=ISLAND_COLOUR,
                              edgecolor=EDGE_COLOUR, linewidth=0.5)
        if self.plan is not None and self.plan.bridges:
            crs = self.islands.crs if self.islands is not None else None
            for bridge in self.plan.bridges:
                colour = BRACE_COLOUR if bridge.rank else BRIDGE_COLOUR
                gpd.GeoSeries([bridge.polygon], crs=crs).plot(
                    ax=self.ax_before, facecolor=colour, edgecolor=EDGE_COLOUR,
                    linewidth=1.4 if bridge.slender else 0.8,
                    linestyle="--" if bridge.slender else "-", zorder=3)
            if len(self.plan.bridges) <= MAX_BRIDGE_LABELS:
                for bridge in self.plan.bridges:
                    point = bridge.line.interpolate(0.5, normalized=True)
                    self.ax_before.annotate(
                        f"{bridge.span_mm:.2f}", (point.x, point.y),
                        fontsize=7, color="#7B241C", zorder=4,
                        ha="center", va="bottom")
        if preserve_view:
            self.ax_before.set_xlim(xlim)   # shared axes keep both aligned
            self.ax_before.set_ylim(ylim)
        self.canvas.draw_idle()

    def _draw_result(self) -> None:
        parts = 0 if self.result is None else len(_polygons(self.result))
        self._style_axis(self.ax_after, f"Bridged — {parts} landmass(es)")
        if self.result is not None and not self.result.is_empty:
            crs = self.islands.crs if self.islands is not None else None
            gpd.GeoSeries([self.result], crs=crs).plot(
                ax=self.ax_after, facecolor=RESULT_COLOUR,
                edgecolor=EDGE_COLOUR, linewidth=0.6)
        self.canvas.draw_idle()

    # -- the report ---------------------------------------------------------
    def _report(self) -> str:
        """The numbers worth reading, in one line."""
        plan = self.plan
        if plan is None:
            return "No plan yet."
        braces = len(plan.braces)
        slender = len(plan.slender)
        bits = [f"{len(plan.bridges)} bridge(s)"]
        if braces:
            bits.append(f"{braces} brace(s)")
        if slender:
            worst = max(b.aspect for b in plan.slender)
            bits.append(f"{slender} slender (worst {worst:.0f}x — widen them)")
        bits.append(f"{plan.components_before} → {plan.components_after} "
                    f"landmass(es)")
        bits.append(f"{plan.land_added_mm2:.1f} mm² of land added")
        if plan.pairs_without_landing:
            bits.append(f"{plan.pairs_without_landing} of {plan.pairs_in_reach} "
                        "pairs in reach had no landing good enough")
        if plan.unreachable:
            bits.append(f"{len(plan.unreachable)} island(s) still separate")
        return "; ".join(bits) + ". Inspect on the right, then save."

    # -- actions ------------------------------------------------------------
    def import_geojson(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import GeoJSON (one or more)", str(Path.cwd()),
            GEOJSON_FILTER)
        if not paths:
            return

        frames, failures = [], []
        for path in paths:
            try:
                frame = gpd.read_file(path)
            except Exception as exc:
                failures.append(f"{Path(path).name}: {exc}")
                continue
            if not frame.empty:
                frames.append(frame)

        if failures:
            QMessageBox.warning(
                self, "Some files could not be read", "\n".join(failures))
        if not frames:
            QMessageBox.warning(self, "Nothing to show",
                                "No readable features in the selected file(s).")
            return

        # Reproject into the first file's CRS so everything shares one frame.
        base_crs = frames[0].crs
        if base_crs is not None:
            frames = [f.to_crs(base_crs) if f.crs is not None else f
                      for f in frames]
        gdf = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True), crs=base_crs)

        # Work in metres so a printed millimetre is a real ground distance;
        # results are reprojected back to the source CRS on save.
        self.source_crs = base_crs
        if base_crs is not None:
            gdf = gdf.to_crs(EPSG_METRIC)
        else:
            QMessageBox.warning(
                self, "No CRS on input",
                "The file has no coordinate system, so the millimetre "
                "parameters cannot be turned into ground distances — they will "
                "act in the raw data units instead.")

        # One polygon per row: these are the islands the tree spans.
        self.islands = gdf.explode(index_parts=False, ignore_index=True)
        self.plan = None
        self.result = None
        self.source_paths = [Path(p) for p in paths]

        self._draw_plan()
        self._style_axis(self.ax_after, "Bridged  (not yet planned)")
        self.canvas.draw_idle()

        self.btn_run.setEnabled(True)
        self.btn_zoom.setEnabled(False)
        self.btn_save.setEnabled(False)
        loaded = (self.source_paths[0].name if len(self.source_paths) == 1
                  else f"{len(self.source_paths)} files")
        self.status.setText(
            f"Loaded {len(self.islands)} island(s) from {loaded}. "
            "Set the width, then Plan Bridges.")

    def run(self) -> None:
        if self.islands is None or self.islands.empty:
            return
        if self._thread is not None:       # never launch twice
            return
        params = self.params()
        if params.width_mm <= 0 or params.max_span_mm <= 0:
            QMessageBox.warning(
                self, "Nothing to plan",
                "A bridge needs a width and a span to be allowed: set both "
                "above zero.")
            return

        self._set_busy(True)
        self.status.setText(
            f"Planning bridges for {len(self.islands)} island(s) at "
            f"{params.width_mm:g} mm wide …")
        self._thread = QThread(self)
        self._worker = PlanWorker(list(self.islands.geometry), params,
                                  self.lagoon_check.isChecked())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, plan, geom) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.plan = plan
        self.result = geom
        self._draw_plan(preserve_view=True)
        self._draw_result()
        self.btn_save.setEnabled(geom is not None)
        self.btn_zoom.setEnabled(bool(plan.bridges))
        if not plan.bridges:
            self.status.setText(
                "No bridges: nothing was both within Max span and able to hold "
                "a bridge of this width. Try a longer span, a narrower bridge, "
                "or a lower anchorage floor.")
            return
        self.status.setText(self._report())

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Planning failed", message)
        self.status.setText("Planning failed — see the message for why.")

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        for widget in (self.btn_import, self.btn_run, self.btn_save,
                       self.btn_zoom, self.scale_spin, self.width_spin,
                       self.embed_spin, self.span_spin, self.aspect_spin,
                       self.sample_spin, self.angle_spin, self.anchor_pen_spin,
                       self.anchor_spin, self.per_pair_spin, self.hinge_spin,
                       self.drop_slender_check, self.lagoon_check):
            widget.setEnabled(not busy)

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._worker = self._thread = None

    def save_geojson(self) -> None:
        if self.result is None or self.result.is_empty:
            return
        default = ""
        if self.source_paths:
            first = self.source_paths[0]
            default = str(first.with_name(first.stem + "_bridged.geojson"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save bridged GeoJSON", default, GEOJSON_FILTER)
        if not path:
            return
        out = gpd.GeoDataFrame(geometry=[self.result], crs=self.islands.crs)
        # Write back in the CRS the file was imported in, not the metric working
        # CRS, so the output matches the source.
        if self.source_crs is not None:
            out = out.to_crs(self.source_crs)
        try:
            out.to_file(path, driver="GeoJSON")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(_polygons(self.result))} landmass(es) "
                            f"to {Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    window = IslandBridgesTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
