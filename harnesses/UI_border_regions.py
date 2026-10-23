"""
UI_border_regions.py

A standalone visual harness for ``LIB_border_regions.build_border_regions``.

Workflow
--------
1. **Import Core …**        — load the regions that border *each other*.  Their
                              shared seams are carved symmetrically (~50/50).
2. **Import Surrounding …** — load the context regions around the cores.  The
                              core–surrounding seam is carved entirely on the
                              surrounding (external) side; cores keep their full
                              extent, and two surrounding regions never get a
                              seam between them.
3. **Border width (km)**    — set the total corridor width.
4. **Carve**                — run the lib (off the UI thread, with an
                              indeterminate progress bar) and draw the result.
5. **Save Result …**        — write the trimmed regions and the border region
                              back out as GeoJSON.

The input regions are drawn on the left ("before") and the carved result — the
trimmed regions with the border region highlighted in red — on the right
("after"), sharing a view so pan/zoom keeps the two panels aligned.

Run with:  python harnesses/UI_border_regions.py

Like the other UI_* harnesses this stays independent of the editors so it can
be pointed at any GeoJSON without setting up a project.  Imported
features are reprojected into the print CRS (EPSG:3347, metres) because the lib
works in metres and takes the width in kilometres; the carved result is written
back out in the CRS the core files came from.
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
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_border_regions.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_border_regions import build_border_regions

EPSG_SRC = "EPSG:4326"      # WGS84 – source GeoJSON
EPSG_PRINT = "EPSG:3347"    # Statistics Canada Lambert, metres (lib works in metres)

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
CORE_COLOUR = "#AED6F1"        # blue-ish
SURROUNDING_COLOUR = "#E5E7E9"  # light grey
BORDER_COLOUR = "#E74C3C"       # red strip
EDGE_COLOUR = "#1B4F72"


def _read_projected(paths: list[str]) -> tuple[gpd.GeoDataFrame | None, object]:
    """
    Read one or more GeoJSON files and reproject them into the print CRS.

    Returns ``(gdf_in_print_crs, source_crs)``.  ``source_crs`` is the CRS the
    first readable file carried (or WGS84 when a file has none), so the carved
    result can be written back out in the frame it came from.
    """
    frames, failures, source_crs = [], [], None
    for path in paths:
        try:
            frame = gpd.read_file(path)
        except Exception as exc:
            failures.append(f"{Path(path).name}: {exc}")
            continue
        if frame.empty:
            continue
        if source_crs is None:
            source_crs = frame.crs if frame.crs is not None else EPSG_SRC
        # Assume WGS84 when a file carries no CRS, then bring everything into
        # the metre-based print CRS so the lib's kilometre width is meaningful.
        if frame.crs is None:
            frame = frame.set_crs(EPSG_SRC)
        frames.append(frame.to_crs(EPSG_PRINT))

    if failures:
        raise RuntimeError("\n".join(failures))
    if not frames:
        return None, None
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=EPSG_PRINT), source_crs


class CarveWorker(QObject):
    """Runs ``build_border_regions`` off the UI thread (it can be slow)."""
    done = Signal(object)   # BorderRegions
    failed = Signal(str)

    def __init__(self, core_geoms, surrounding_geoms, width_km: float):
        super().__init__()
        self._core = core_geoms
        self._surrounding = surrounding_geoms
        self._width_km = width_km

    def run(self) -> None:
        try:
            result = build_border_regions(
                self._core, self._surrounding, self._width_km)
        except Exception as exc:      # surface any geometry failure to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class BorderRegionsTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Border Regions — Visual Tester")
        self.resize(1200, 720)

        # Raw inputs (print CRS) and the carved result.
        self.core: gpd.GeoDataFrame | None = None
        self.surrounding: gpd.GeoDataFrame | None = None
        self.trimmed_core: gpd.GeoSeries | None = None
        self.trimmed_surrounding: gpd.GeoSeries | None = None
        self.border: gpd.GeoSeries | None = None
        self.applied = False
        #: Files (from the core import) and CRS the result is saved back into.
        self.source_paths: list[Path] = []
        self.source_crs = None
        self._thread: QThread | None = None
        self._worker: CarveWorker | None = None

        #: Set while one panel is being matched to the other, so the
        #: limit callbacks ignore our own writes coming back.
        self._linking = False

        central = QWidget()
        root = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.btn_core = QPushButton("Import Core …")
        self.btn_core.clicked.connect(self.import_core)
        self.btn_surrounding = QPushButton("Import Surrounding …")
        self.btn_surrounding.clicked.connect(self.import_surrounding)

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setDecimals(2)
        self.width_spin.setRange(0.01, 500.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(2.0)
        self.width_spin.setSuffix(" km")
        self.width_spin.setToolTip("Total border corridor width")

        self.btn_carve = QPushButton("Carve")
        self.btn_carve.clicked.connect(self.carve)
        self.btn_carve.setEnabled(False)
        self.btn_save = QPushButton("Save Result …")
        self.btn_save.clicked.connect(self.save_result)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(self.btn_core)
        toolbar.addWidget(self.btn_surrounding)
        toolbar.addWidget(QLabel("Width:"))
        toolbar.addWidget(self.width_spin)
        toolbar.addWidget(self.btn_carve)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.figure = Figure(figsize=(11, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Deliberately NOT sharex/sharey: the panels keep the size the window
        # gives them (see the aspect note below), and matplotlib refuses a fixed
        # axes box on shared axes. They are linked by hand (_link_views).
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import core (and optionally surrounding) regions", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        root.addWidget(self.mpl_toolbar)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No regions loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    # -- axes helpers -------------------------------------------------------
    def _reset_axes(self, before_title: str, after_title: str) -> None:
        for ax, title in ((self.ax_before, before_title),
                          (self.ax_after, after_title)):
            self._clear_ax(ax, title)
        self.canvas.draw_idle()

    def _clear_ax(self, ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        # Fixed panel, not a fixed view: with the default ``adjustable="box"``
        # matplotlib satisfies "equal" by shrinking the axes box to the data's
        # own shape, so a north-south province collapses the panel to a sliver
        # and the window rearranges itself every time a differently shaped file
        # is imported. ``datalim`` holds the box the window gave it and widens
        # the *limits* instead.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        self._watch_limits(ax)

    def _watch_limits(self, ax) -> None:
        """(Re)connect the callbacks that keep the two panels together.

        ``ax.clear()`` throws the axes' callback registry away, so this has to
        run again after every clear -- which is what makes _style_axis its
        home. Without it matplotlib's own toolbar (pan, zoom-to-rectangle,
        Home) would move one panel and leave the other behind; sharing used to
        make that impossible, and unsharing for the fixed box gave it up.
        """
        ax.callbacks.connect("xlim_changed", self._link_views)
        ax.callbacks.connect("ylim_changed", self._link_views)

    def _set_view(self, ax, xlim, ylim) -> None:
        """Move a panel to a new view, both limits at once.

        The limit callbacks have to be held off in between: fired on the x
        change alone, the link settles the aspect against a y that has not
        moved yet, and ``apply_aspect`` pulls x straight back -- which cancels
        a wheel zoom completely rather than subtly.
        """
        self._linking = True
        try:
            ax.set_xlim(xlim)
            ax.set_ylim(ylim)
        finally:
            self._linking = False
        self._link_views(ax)

    def _link_views(self, changed=None) -> None:
        """Match the after panel to the before panel.

        Replaces the ``sharex``/``sharey`` a fixed axes box cannot have:
        matplotlib refuses ``adjustable="datalim"`` on shared axes at draw
        time. Both panels hold the same geometry in the same CRS, and the
        result never reaches outside the input, so before owns the frame.
        """
        if self._linking:
            return              # our own write coming back as a callback
        self._linking = True
        try:
            # Whichever panel moved is the one to follow, so a drag on either
            # carries. Settle it first: ``datalim`` widens the limits during
            # the draw, so reading them beforehand hands the other panel a
            # data-shaped view it then has to override -- matplotlib logs
            # "Ignoring fixed y limits to fulfill fixed data aspect" and picks
            # the frame itself. apply_aspect does that widening now, so what is
            # copied already fits.
            source = changed if changed is self.ax_after else self.ax_before
            target = (self.ax_after if source is self.ax_before
                      else self.ax_before)
            source.apply_aspect()
            target.set_xlim(source.get_xlim())
            target.set_ylim(source.get_ylim())
        finally:
            self._linking = False

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        self._set_view(ax,
                       (xd - (xd - x0) * scale, xd + (x1 - xd) * scale),
                       (yd - (yd - y0) * scale, yd + (y1 - yd) * scale))
        self.canvas.draw_idle()

    # -- imports ------------------------------------------------------------
    def import_core(self) -> None:
        self._import_into("core", "Import Core regions (one or more)")

    def import_surrounding(self) -> None:
        self._import_into("surrounding", "Import Surrounding regions (one or more)")

    def _import_into(self, role: str, caption: str) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, caption, str(Path.cwd()), GEOJSON_FILTER)
        if not paths:
            return
        try:
            gdf, src_crs = _read_projected(paths)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Some files could not be read", str(exc))
            return
        if gdf is None:
            QMessageBox.warning(self, "Nothing to show",
                                "No readable features in the selected file(s).")
            return

        setattr(self, role, gdf)
        # The core is what gets printed, so its frame seeds the save CRS and the
        # default output filename; fall back to the surrounding's CRS if only it
        # has been imported so far.
        if role == "core":
            self.source_crs = src_crs
            self.source_paths = [Path(p) for p in paths]
        elif self.source_crs is None:
            self.source_crs = src_crs
        self.applied = False
        self.btn_carve.setEnabled(self.core is not None)
        self.btn_save.setEnabled(False)
        self._draw_before()
        self._clear_ax(self.ax_after, "Not yet carved")
        self._link_views()      # clearing reset it to matplotlib's 0..1 view
        self.canvas.draw_idle()

        n_core = 0 if self.core is None else len(self.core)
        n_surr = 0 if self.surrounding is None else len(self.surrounding)
        self.status.setText(
            f"Loaded {n_core} core + {n_surr} surrounding region(s). "
            "Set the width and click ‘Carve’.")

    # -- carve --------------------------------------------------------------
    def carve(self) -> None:
        # Guard against launching a second carve while one is running.
        if self.core is None or self._thread is not None:
            return
        core_geoms = list(self.core.geometry)
        surrounding_geoms = ([] if self.surrounding is None
                             else list(self.surrounding.geometry))

        self._set_busy(True)
        self.status.setText(
            f"Carving with a {self.width_spin.value():.2f} km border …")

        # The carve is a single blocking geometry call, so run it off the UI
        # thread and drive an indeterminate progress bar while it works.
        self._thread = QThread(self)
        self._worker = CarveWorker(
            core_geoms, surrounding_geoms, self.width_spin.value())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_carved)
        self._worker.failed.connect(self._on_carve_failed)
        self._thread.start()

    def _on_carved(self, result) -> None:
        self.trimmed_core = gpd.GeoSeries(result.core, crs=EPSG_PRINT)
        self.trimmed_surrounding = gpd.GeoSeries(
            result.surrounding, crs=EPSG_PRINT)
        self.border = (None if result.border is None
                       else gpd.GeoSeries([result.border], crs=EPSG_PRINT))
        self.applied = True

        self._teardown_thread()
        self._set_busy(False)
        self._draw_after()
        self.btn_save.setEnabled(True)
        self.status.setText(
            f"Carved with a {self.width_spin.value():.2f} km border. "
            "Border region shown in red. Save the result or adjust the width.")

    def _on_carve_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Carve failed", message)
        self.status.setText(f"Carve failed: {message}")

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
        for w in (self.btn_core, self.btn_surrounding, self.btn_carve,
                  self.btn_save, self.width_spin):
            w.setEnabled(not busy)

    # -- drawing ------------------------------------------------------------
    def _draw_before(self) -> None:
        self._clear_ax(self.ax_before, "Input regions")
        if self.surrounding is not None and not self.surrounding.empty:
            self.surrounding.plot(ax=self.ax_before, facecolor=SURROUNDING_COLOUR,
                                  edgecolor=EDGE_COLOUR, linewidth=0.5)
        if self.core is not None and not self.core.empty:
            self.core.plot(ax=self.ax_before, facecolor=CORE_COLOUR,
                           edgecolor=EDGE_COLOUR, linewidth=0.6)
        self._link_views()
        self.canvas.draw_idle()

    def _draw_after(self) -> None:
        self._clear_ax(self.ax_after, "Trimmed regions + border (red)")
        if (self.trimmed_surrounding is not None
                and not self.trimmed_surrounding.dropna().empty):
            self.trimmed_surrounding.plot(ax=self.ax_after,
                                          facecolor=SURROUNDING_COLOUR,
                                          edgecolor=EDGE_COLOUR, linewidth=0.5)
        if (self.trimmed_core is not None
                and not self.trimmed_core.dropna().empty):
            self.trimmed_core.plot(ax=self.ax_after, facecolor=CORE_COLOUR,
                                   edgecolor=EDGE_COLOUR, linewidth=0.6)
        if self.border is not None and not self.border.dropna().empty:
            self.border.plot(ax=self.ax_after, facecolor=BORDER_COLOUR,
                             edgecolor="none", alpha=0.85)
        # The after panel just autoscaled to its own data; before owns the frame.
        self._link_views()
        self.canvas.draw_idle()

    # -- save ---------------------------------------------------------------
    def save_result(self) -> None:
        if not self.applied:
            return
        # Default the name from the core source file, like the other harnesses.
        default = str(Path.cwd() / "trimmed.geojson")
        if self.source_paths:
            first = self.source_paths[0]
            default = str(first.with_name(first.stem + "_trimmed.geojson"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save trimmed regions", default, GEOJSON_FILTER)
        if not path:
            return

        stem = Path(path)
        try:
            self._write_trimmed(stem)
            border_path = stem.with_name(stem.stem + "_border" + stem.suffix)
            self._write_border(border_path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        self.status.setText(
            f"Saved trimmed regions to {stem.name} and the border region to "
            f"{stem.stem}_border{stem.suffix}.")

    def _to_source_crs(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        """Reproject an output GeoDataFrame back into the imported (source) CRS."""
        if self.source_crs is not None:
            return gdf.to_crs(self.source_crs)
        return gdf

    def _write_trimmed(self, path: Path) -> None:
        """Write trimmed core + surrounding to one GeoJSON tagged by kind."""
        rows = []
        for series, kind in ((self.trimmed_core, "core"),
                             (self.trimmed_surrounding, "surrounding")):
            if series is None:
                continue
            for geom in series:
                if geom is not None and not geom.is_empty:
                    rows.append({"kind": kind, "geometry": geom})
        if not rows:
            return
        gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=EPSG_PRINT)
        self._to_source_crs(gdf).to_file(path, driver="GeoJSON")

    def _write_border(self, path: Path) -> None:
        if self.border is None or self.border.dropna().empty:
            return
        gdf = gpd.GeoDataFrame(geometry=self.border, crs=EPSG_PRINT)
        self._to_source_crs(gdf).to_file(path, driver="GeoJSON")


def main() -> int:
    app = QApplication(sys.argv)
    window = BorderRegionsTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
