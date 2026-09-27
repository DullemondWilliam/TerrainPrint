"""
UI_island_merge.py

A tiny standalone visual harness for ``LIB_island_merging.merge_islands``.

Workflow
--------
1. **Import GeoJSON** — load features; every polygon part becomes a selectable
   island.
2. **Select islands** — click two or more islands on the left panel.
   Click a selected island again to deselect it.
3. **Merge Selected** — bridge the selected islands with connecting necks of
   land at the chosen buffer distance; the result is drawn on the right.
4. **Save GeoJSON …** — write the merged set back out, CRS preserved.

Run with:  python harnesses/UI_island_merge.py

A throwaway inspection tool, deliberately kept independent of the main ``app``
window, and styled after ``UI_lake_removal.py``.
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from shapely.geometry import Point

from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

# Runnable by path from anywhere (python harnesses/UI_island_merge.py), so put the
# project root on sys.path before importing lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.LIB_island_merging import _polygons, merge_islands

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
#: Project print CRS (Statistics Canada Lambert, metres). Islands are worked in
#: metres so the km buffer means a real ground distance, then reprojected back
#: to the source CRS on save.
EPSG_METRIC = "EPSG:3347"
ISLAND_COLOUR = "#AED6F1"     # unselected islands
SELECT_COLOUR = "#F5B041"     # the (up to two) picked islands
MERGED_COLOUR = "#ABEBC6"     # the merged result
EDGE_COLOUR = "#1B4F72"
SELECT_EDGE = "#B9770E"


class IslandMergeTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Island Merge — Visual Tester")
        self.resize(1100, 640)

        #: One row per polygon part (the selectable islands).
        self.islands: gpd.GeoDataFrame | None = None
        #: Indices into ``self.islands`` currently picked (at most two).
        self.selected: list[int] = []
        #: Result of the last merge, ready to save.
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
        self.btn_merge = QPushButton("Merge Selected")
        self.btn_merge.clicked.connect(self.merge_selected)
        self.btn_merge.setEnabled(False)
        self.btn_save = QPushButton("Save GeoJSON …")
        self.btn_save.clicked.connect(self.save_geojson)
        self.btn_save.setEnabled(False)
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_merge)
        toolbar.addWidget(self.btn_save)

        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("Buffer (km)"))
        self.buffer_spin = QDoubleSpinBox()
        self.buffer_spin.setDecimals(2)
        self.buffer_spin.setRange(0.0, 500.0)
        self.buffer_spin.setSingleStep(0.25)
        self.buffer_spin.setValue(2.0)
        self.buffer_spin.setToolTip(
            "Morphological-closing distance in kilometres (real ground "
            "distance). Islands closer than twice this value are bridged; "
            "larger values smooth the coastline more.")
        toolbar.addWidget(self.buffer_spin)

        toolbar.addStretch(1)
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(self.select_all)
        self.btn_select_all.setEnabled(False)
        toolbar.addWidget(self.btn_select_all)
        self.btn_clear = QPushButton("Clear Selection")
        self.btn_clear.clicked.connect(self.clear_selection)
        self.btn_clear.setEnabled(False)
        toolbar.addWidget(self.btn_clear)
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
        self.canvas.mpl_connect("button_press_event", self._on_click)
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

    # -- selection ----------------------------------------------------------
    def _on_click(self, event) -> None:
        """Left-panel clicks pick/unpick an island (unless panning/zooming)."""
        if self.toolbar.mode:            # a pan/zoom tool owns the mouse
            return
        if event.inaxes is not self.ax_before or self.islands is None:
            return
        if event.xdata is None or event.ydata is None:
            return

        idx = self._island_at(event.xdata, event.ydata)
        if idx is None:
            return

        if idx in self.selected:
            self.selected.remove(idx)            # click again to deselect
        else:
            self.selected.append(idx)            # any number of islands may join

        # Changing the selection invalidates any previous merge.
        self.result = None
        self.btn_save.setEnabled(False)
        self._style_axis(self.ax_after, "Merged (not yet run)")
        self._draw_original(preserve_view=True)
        self._sync_buttons()

    def _island_at(self, x: float, y: float) -> int | None:
        """Index of the island containing (x, y), or None."""
        point = Point(x, y)
        hits = self.islands.index[self.islands.geometry.contains(point)]
        return int(hits[0]) if len(hits) else None

    def select_all(self) -> None:
        """Pick every land mass in one click, ready to merge them all."""
        if self.islands is None or self.islands.empty:
            return
        self.selected = list(self.islands.index)
        self.result = None
        self.btn_save.setEnabled(False)
        self._style_axis(self.ax_after, "Merged (not yet run)")
        self._draw_original(preserve_view=True)
        self._sync_buttons()
        self.status.setText(
            f"Selected all {len(self.selected)} land masses. "
            "Adjust the buffer, then Merge Selected.")

    def clear_selection(self) -> None:
        self.selected = []
        self.result = None
        self.btn_save.setEnabled(False)
        self._style_axis(self.ax_after, "Merged (not yet run)")
        self._draw_original(preserve_view=True)
        self._sync_buttons()
        self.status.setText("Selection cleared. Click two or more islands to merge.")

    def _sync_buttons(self) -> None:
        self.btn_merge.setEnabled(len(self.selected) >= 2)
        self.btn_clear.setEnabled(bool(self.selected))
        has_islands = self.islands is not None and not self.islands.empty
        self.btn_select_all.setEnabled(has_islands)

    # -- drawing ------------------------------------------------------------
    def _draw_original(self, preserve_view: bool = False) -> None:
        # Redrawing to reflect a selection must not fling the map back to the
        # full extent, so hold the current pan/zoom across the clear + replot.
        if preserve_view:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        self._style_axis(self.ax_before, "Islands  (click two or more to select)")
        if self.islands is not None and not self.islands.empty:
            self.islands.plot(ax=self.ax_before, facecolor=ISLAND_COLOUR,
                              edgecolor=EDGE_COLOUR, linewidth=0.6)
            if self.selected:
                self.islands.loc[self.selected].plot(
                    ax=self.ax_before, facecolor=SELECT_COLOUR,
                    edgecolor=SELECT_EDGE, linewidth=1.0)
        if preserve_view:
            self._set_view(self.ax_before, xlim, ylim)
        else:
            self._link_views()
        self.canvas.draw_idle()

    def _draw_result(self) -> None:
        self._style_axis(self.ax_after, "Merged")
        if self.result is not None and not self.result.empty:
            # Last row is the merged island; earlier rows are the untouched ones.
            self.result.iloc[:-1].plot(
                ax=self.ax_after, facecolor=ISLAND_COLOUR,
                edgecolor=EDGE_COLOUR, linewidth=0.6)
            self.result.iloc[[-1]].plot(
                ax=self.ax_after, facecolor=MERGED_COLOUR,
                edgecolor=EDGE_COLOUR, linewidth=0.9)
        # The after panel just autoscaled to its own data; before owns the frame.
        self._link_views()
        self.canvas.draw_idle()

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

        # Work in metres (EPSG:3347) so the km buffer is a real ground distance;
        # results are reprojected back to the source CRS on save. Without a
        # source CRS we cannot do this, so distances stay in the raw data units.
        self.source_crs = base_crs
        if base_crs is not None:
            gdf = gdf.to_crs(EPSG_METRIC)
        else:
            QMessageBox.warning(
                self, "No CRS on input",
                "The file has no coordinate system, so the buffer cannot be "
                "interpreted as kilometres — it will act in the raw data "
                "units instead.")

        # Explode to one polygon per row: those are the islands you can pick.
        self.islands = gdf.explode(index_parts=False, ignore_index=True)
        self.selected = []
        self.result = None
        self.source_paths = [Path(p) for p in paths]

        self._draw_original()
        self._style_axis(self.ax_after, "Merged (not yet run)")
        self._link_views()      # clearing reset it to matplotlib's 0..1 view
        self.canvas.draw_idle()

        self.btn_save.setEnabled(False)
        self._sync_buttons()
        loaded = (self.source_paths[0].name if len(self.source_paths) == 1
                  else f"{len(self.source_paths)} files")
        self.status.setText(
            f"Loaded {len(self.islands)} island(s) from {loaded}. "
            f"Click two or more islands to select them.")

    def merge_selected(self) -> None:
        if self.islands is None or len(self.selected) < 2:
            return
        chosen = [self.islands.geometry.iloc[k] for k in self.selected]
        buffer_km = self.buffer_spin.value()

        try:
            merged = merge_islands(chosen, buffer_km)
        except Exception as exc:
            QMessageBox.critical(self, "Merge failed", str(exc))
            return

        # Keep the untouched islands, then append the merged one last (its
        # attributes inherited from the first selection).
        keep = self.islands.drop(index=self.selected)
        merged_row = self.islands.iloc[[self.selected[0]]].copy()
        merged_row = merged_row.set_geometry([merged], crs=self.islands.crs)
        self.result = gpd.GeoDataFrame(
            pd.concat([keep, merged_row], ignore_index=True),
            crs=self.islands.crs)

        self._draw_result()
        self.btn_save.setEnabled(True)
        groups = len(_polygons(merged))
        self.status.setText(
            f"Merged {len(chosen)} islands at buffer {buffer_km:g} km — "
            + ("all bridged into one landmass." if groups == 1 else
               f"formed {groups} group(s); some are too far apart to bridge — "
               "increase the buffer.")
            + " Inspect on the right, then save.")

    def save_geojson(self) -> None:
        if self.result is None:
            return
        default = ""
        if self.source_paths:
            first = self.source_paths[0]
            default = str(first.with_name(first.stem + "_merged.geojson"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save merged GeoJSON", default, GEOJSON_FILTER)
        if not path:
            return
        # Write back in the CRS the file was imported in, not the metric working
        # CRS, so the output matches the source.
        out = (self.result.to_crs(self.source_crs)
               if self.source_crs is not None else self.result)
        try:
            out.to_file(path, driver="GeoJSON")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(out)} feature(s) to "
                            f"{Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    window = IslandMergeTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
