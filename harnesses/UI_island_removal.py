"""
UI_island_removal.py

A tiny standalone visual harness for ``LIB_island_removal.remove_small_islands``.

Workflow
--------
1. **Import GeoJSON** — load features; every polygon part becomes an island and
   its area (km²) is measured.
2. **Set the minimum area** — islands smaller than this are marked for removal;
   the left panel highlights them live as you change the value.
3. **Remove Small Islands** — drop every marked island; the survivors are drawn
   on the right.
4. **Save GeoJSON …** — write the surviving islands back out, CRS preserved.

Run with:  python harnesses/UI_island_removal.py

A throwaway inspection tool, deliberately kept independent of the main ``app``
window, and styled after ``UI_island_merge.py``.
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_island_removal.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_island_removal import area_km2, is_below_area

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
#: Project print CRS (Statistics Canada Lambert, metres). Islands are worked in
#: metres so the km² threshold means a real ground area, then reprojected back
#: to the source CRS on save.
EPSG_METRIC = "EPSG:3347"

#: Where the file dialogs start, resolved next to this script: the lake-removed
#: inputs feed island removal, whose output lands in the islands folder.
_ROOT = Path(__file__).resolve().parent
DEFAULT_LOAD_DIR = _ROOT / "Data" / "Province-Lakes"
DEFAULT_SAVE_DIR = _ROOT / "Data" / "Provinces-Islands"


def _dir_or_cwd(path: Path) -> str:
    """Directory as a string if it exists, else fall back to the cwd."""
    return str(path if path.is_dir() else Path.cwd())
ISLAND_COLOUR = "#AED6F1"     # islands that are kept
REMOVE_COLOUR = "#E6787A"     # islands marked for / after removal
KEPT_COLOUR = "#ABEBC6"       # the surviving result
EDGE_COLOUR = "#1B4F72"
REMOVE_EDGE = "#943126"


class IslandRemovalTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Island Removal — Visual Tester")
        self.resize(1100, 640)

        #: One row per polygon part (the islands), with an ``area_km2`` column.
        self.islands: gpd.GeoDataFrame | None = None
        #: Result of the last removal (the surviving islands), ready to save.
        self.result: gpd.GeoDataFrame | None = None
        self.source_paths: list[Path] = []
        #: CRS the files were loaded in; results are saved back into it.
        self.source_crs = None

        #: Set while one panel is being matched to the other, so the
        #: limit callbacks ignore our own writes coming back.
        self._linking = False

        central = QWidget()
        root = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)
        self.btn_remove = QPushButton("Remove Small Islands")
        self.btn_remove.clicked.connect(self.remove_small)
        self.btn_remove.setEnabled(False)
        self.btn_save = QPushButton("Save GeoJSON …")
        self.btn_save.clicked.connect(self.save_geojson)
        self.btn_save.setEnabled(False)
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(self.btn_save)

        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("Min area (km²)"))
        self.area_spin = QDoubleSpinBox()
        self.area_spin.setDecimals(3)
        self.area_spin.setRange(0.0, 1_000_000.0)
        self.area_spin.setSingleStep(1.0)
        self.area_spin.setValue(1.0)
        self.area_spin.setToolTip(
            "Islands smaller than this area (real ground area in square "
            "kilometres) are removed. Islands at or above it are kept.")
        self.area_spin.valueChanged.connect(self._preview)
        toolbar.addWidget(self.area_spin)

        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.figure = Figure(figsize=(10, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Deliberately NOT sharex/sharey: the panels keep the size the window
        # gives them (see the aspect note below), and matplotlib refuses a fixed
        # axes box on shared axes. They are linked by hand (_link_views).
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import a GeoJSON to begin", "")

        self.toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        root.addWidget(self.toolbar)
        root.addWidget(self.canvas, stretch=1)

        self.status = QLabel("No file loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.setCentralWidget(central)

    # -- axes helpers -------------------------------------------------------
    def _reset_axes(self, left_title: str, right_title: str) -> None:
        for ax, title in ((self.ax_before, left_title),
                          (self.ax_after, right_title)):
            self._style_axis(ax, title)
        self.canvas.draw_idle()

    def _style_axis(self, ax, title: str) -> None:
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
        """Zoom the panel under the cursor toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        # Keep the point under the cursor fixed while the span grows/shrinks.
        self._set_view(ax,
                       (xd - (xd - x0) * scale, xd + (x1 - xd) * scale),
                       (yd - (yd - y0) * scale, yd + (y1 - yd) * scale))
        self.canvas.draw_idle()

    # -- classification -----------------------------------------------------
    def _small_mask(self):
        """Boolean Series: True where an island is below the area threshold."""
        threshold = self.area_spin.value()
        return self.islands.geometry.map(lambda g: is_below_area(g, threshold))

    # -- drawing ------------------------------------------------------------
    def _draw_original(self, preserve_view: bool = False) -> None:
        # Redrawing to reflect a new threshold must not fling the map back to the
        # full extent, so hold the current pan/zoom across the clear + replot.
        if preserve_view:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        self._style_axis(self.ax_before,
                         "Islands  (red = below the minimum area)")
        if self.islands is not None and not self.islands.empty:
            small = self._small_mask()
            self.islands[~small].plot(
                ax=self.ax_before, facecolor=ISLAND_COLOUR,
                edgecolor=EDGE_COLOUR, linewidth=0.6)
            if small.any():
                self.islands[small].plot(
                    ax=self.ax_before, facecolor=REMOVE_COLOUR,
                    edgecolor=REMOVE_EDGE, linewidth=0.9)
        if preserve_view:
            self._set_view(self.ax_before, xlim, ylim)
        else:
            self._link_views()
        self.canvas.draw_idle()

    def _draw_result(self) -> None:
        self._style_axis(self.ax_after, "Kept")
        if self.result is not None and not self.result.empty:
            self.result.plot(ax=self.ax_after, facecolor=KEPT_COLOUR,
                             edgecolor=EDGE_COLOUR, linewidth=0.6)
        # The after panel just autoscaled to its own data; before owns the frame.
        self._link_views()
        self.canvas.draw_idle()

    # -- actions ------------------------------------------------------------
    def import_geojson(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import GeoJSON (one or more)", _dir_or_cwd(DEFAULT_LOAD_DIR),
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

        # Work in metres (EPSG:3347) so the km² threshold is a real ground area;
        # results are reprojected back to the source CRS on save. Without a
        # source CRS we cannot do this, so areas stay in the raw data units.
        self.source_crs = base_crs
        if base_crs is not None:
            gdf = gdf.to_crs(EPSG_METRIC)
        else:
            QMessageBox.warning(
                self, "No CRS on input",
                "The file has no coordinate system, so the threshold cannot be "
                "interpreted as square kilometres — it will act in the raw "
                "data units instead.")

        # Explode to one polygon per row: those are the islands we filter.
        islands = gdf.explode(index_parts=False, ignore_index=True)
        islands["area_km2"] = islands.geometry.map(area_km2)
        self.islands = islands
        self.result = None
        self.source_paths = [Path(p) for p in paths]

        self._draw_original()
        self._style_axis(self.ax_after, "Kept (not yet run)")
        self._link_views()      # clearing reset it to matplotlib's 0..1 view
        self.canvas.draw_idle()

        self.btn_remove.setEnabled(True)
        self.btn_save.setEnabled(False)
        loaded = (self.source_paths[0].name if len(self.source_paths) == 1
                  else f"{len(self.source_paths)} files")
        self._report_marked(f"Loaded {len(self.islands)} island(s) from {loaded}.")

    def _preview(self) -> None:
        """Threshold changed: re-highlight which islands would be removed."""
        if self.islands is None:
            return
        # A new threshold invalidates the previous result.
        self.result = None
        self.btn_save.setEnabled(False)
        self._style_axis(self.ax_after, "Kept (not yet run)")
        self._draw_original(preserve_view=True)
        self._report_marked("Threshold changed.")

    def _report_marked(self, prefix: str) -> None:
        small = int(self._small_mask().sum())
        total = len(self.islands)
        self.status.setText(
            f"{prefix} {small} of {total} island(s) below "
            f"{self.area_spin.value():g} km² marked for removal. "
            "Click ‘Remove Small Islands’.")

    def remove_small(self) -> None:
        if self.islands is None:
            return
        threshold = self.area_spin.value()
        keep_mask = ~self._small_mask()
        removed = int((~keep_mask).sum())
        # Slice the GeoDataFrame (not the lib's list) so attributes and CRS ride
        # along to the saved output; the survivors match remove_small_islands,
        # which classifies with the same is_below_area predicate.
        self.result = self.islands[keep_mask].reset_index(drop=True)

        self._draw_result()
        self.btn_save.setEnabled(not self.result.empty)
        if self.result.empty:
            self.status.setText(
                f"Every island is below {threshold:g} km² — nothing left to "
                "keep. Lower the minimum area.")
        else:
            self.status.setText(
                f"Removed {removed} island(s) below {threshold:g} km²; "
                f"{len(self.result)} kept. Inspect on the right, then save.")

    def save_geojson(self) -> None:
        if self.result is None or self.result.empty:
            return
        # Open the save dialog in the islands output folder (create it so the
        # dialog can navigate into it), suggesting the source's own filename.
        DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        stem = self.source_paths[0].stem if self.source_paths else "islands"
        default = str(DEFAULT_SAVE_DIR / f"{stem}.geojson")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save pruned GeoJSON", default, GEOJSON_FILTER)
        if not path:
            return
        # Write back in the CRS the file was imported in, not the metric working
        # CRS, so the output matches the source. Drop the helper area column.
        out = self.result.drop(columns="area_km2", errors="ignore")
        if self.source_crs is not None:
            out = out.to_crs(self.source_crs)
        try:
            out.to_file(path, driver="GeoJSON")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(out)} island(s) to "
                            f"{Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    window = IslandRemovalTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
