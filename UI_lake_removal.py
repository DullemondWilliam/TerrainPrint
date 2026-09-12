"""
UI_lake_removal_tester.py

A tiny standalone visual harness for ``LIB_lake_removal.remove_lakes``.

Workflow
--------
1. **Import GeoJSON** — load one or more features from a ``.geojson``/``.json``.
2. **Remove Lakes** — strip interior rings from every feature's geometry.
3. **Inspect** — original (left) and cleaned (right) are drawn side by side;
   holes read as white gaps on the left and vanish on the right.
4. **Save GeoJSON …** — write the cleaned features back out, CRS preserved.

Run with:  python UI_lake_removal_tester.py

This is a throwaway inspection tool, deliberately kept independent of the main
``app`` window so it can be pointed at any GeoJSON without setting up a project.
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from lib.LIB_lake_removal import remove_lakes

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
ORIGINAL_COLOUR = "#AED6F1"
CLEANED_COLOUR = "#ABEBC6"
EDGE_COLOUR = "#1B4F72"


def _hole_count(geom) -> int:
    """Total interior rings across every polygon part of a geometry."""
    if geom is None or geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(len(p.interiors) for p in geom.geoms)
    return 0


class LakeRemovalTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lake Removal — Visual Tester")
        self.resize(1100, 640)

        self.original: gpd.GeoDataFrame | None = None
        self.cleaned: gpd.GeoDataFrame | None = None
        self.source_paths: list[Path] = []

        central = QWidget()
        root = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        self.btn_import = QPushButton("Import GeoJSON …")
        self.btn_import.clicked.connect(self.import_geojson)
        self.btn_remove = QPushButton("Remove Lakes")
        self.btn_remove.clicked.connect(self.remove_lakes)
        self.btn_remove.setEnabled(False)
        self.btn_save = QPushButton("Save GeoJSON …")
        self.btn_save.clicked.connect(self.save_geojson)
        self.btn_save.setEnabled(False)
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.figure = Figure(figsize=(10, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        # Share the view so pan/zoom on either panel keeps before/after aligned.
        self.ax_after = self.figure.add_subplot(
            1, 2, 2, sharex=self.ax_before, sharey=self.ax_before)
        self._reset_axes("Import a GeoJSON to begin", "")

        # Matplotlib's own toolbar: pan (hand), zoom-to-rectangle, and Home to
        # reset the view. Scroll-wheel zoom is added on top for quick inspection.
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
            ax.clear()
            ax.set_title(title, fontsize=11)
            ax.set_aspect("equal")
            ax.set_xticks([])
            ax.set_yticks([])
        self.canvas.draw_idle()

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

    def _plot(self, ax, gdf, title, colour) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        if gdf is not None and not gdf.empty:
            # geopandas renders interior rings as holes, so lakes show as gaps.
            gdf.plot(ax=ax, facecolor=colour, edgecolor=EDGE_COLOUR,
                     linewidth=0.6)

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

        # Reproject every file into the first file's CRS so features from
        # different sources overlay in one consistent coordinate frame.
        base_crs = frames[0].crs
        if base_crs is not None:
            frames = [f.to_crs(base_crs) if f.crs is not None else f
                      for f in frames]
        gdf = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True), crs=base_crs)

        self.source_paths = [Path(p) for p in paths]
        self.original = gdf
        self.cleaned = None
        holes = int(gdf.geometry.map(_hole_count).sum())

        self._plot(self.ax_before, gdf, "Original", ORIGINAL_COLOUR)
        self._reset_after("Cleaned (not yet run)")
        self.canvas.draw_idle()

        self.btn_remove.setEnabled(True)
        self.btn_save.setEnabled(False)
        loaded = (self.source_paths[0].name if len(self.source_paths) == 1
                  else f"{len(self.source_paths)} files")
        self.status.setText(
            f"Loaded {len(gdf)} feature(s) from {loaded} — "
            f"{holes} lake(s)/hole(s) found. Click ‘Remove Lakes’.")

    def _reset_after(self, title: str) -> None:
        self.ax_after.clear()
        self.ax_after.set_title(title, fontsize=11)
        self.ax_after.set_aspect("equal")
        self.ax_after.set_xticks([])
        self.ax_after.set_yticks([])

    def remove_lakes(self) -> None:
        if self.original is None:
            return
        cleaned = self.original.copy()
        cleaned["geometry"] = cleaned.geometry.map(remove_lakes)
        self.cleaned = cleaned

        before = int(self.original.geometry.map(_hole_count).sum())
        after = int(cleaned.geometry.map(_hole_count).sum())

        self._plot(self.ax_after, cleaned, "Lakes removed", CLEANED_COLOUR)
        self.canvas.draw_idle()      # axes are shared, so both panels stay aligned

        self.btn_save.setEnabled(True)
        self.status.setText(
            f"Removed {before - after} lake(s): {before} → {after} hole(s) "
            f"remaining. Inspect on the right, then save.")

    def save_geojson(self) -> None:
        if self.cleaned is None:
            return
        default = ""
        if self.source_paths:
            first = self.source_paths[0]
            default = str(first.with_name(first.stem + "_no_lakes.geojson"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save cleaned GeoJSON", default, GEOJSON_FILTER)
        if not path:
            return
        try:
            self.cleaned.to_file(path, driver="GeoJSON")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(self.cleaned)} feature(s) to "
                            f"{Path(path).name}.")


def main() -> int:
    app = QApplication(sys.argv)
    window = LakeRemovalTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
