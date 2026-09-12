"""
app.canvas

QGraphicsView map canvas showing the imported regions in print millimetres.

Scene units are print-mm, so what you see is literally the printed size, and the
mm labels are read straight off the geometry.  Northing increases upwards while
Qt's Y axis increases downwards, so Y is negated on the way in.

Layers
------
By Step 3 the scene carries a terrain backdrop, role-coloured region fills, a
filled corridor, dashed connectors, the bed-split grid, and preview overlays.
That is too much to read at national scale, so every optional layer is
independently switchable (:meth:`set_layer_visible`) and the vocabulary is
published as :data:`LEGEND_ENTRIES` for the side panel's legend — one source of
truth, so a swatch can never disagree with what is drawn.
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QImage, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QTransform)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPathItem,
                               QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView)

from core.geometry import polygon_parts
from core.project import TARGET

TARGET_FILL      = QColor("#AED6F1")
TARGET_EDGE      = QColor("#1B4F72")
SURROUNDING_FILL = QColor("#DDE1E2")
SURROUNDING_EDGE = QColor("#7F8C8D")
BACKGROUND       = QColor("#D6EAF8")
LABEL_COLOUR     = QColor("#1A1A1A")
BORDER_FILL      = QColor("#FF5C00")   # the carved corridor
DOVETAIL_EDGE    = QColor("#8E2D00")   # connector outlines (dashed)
HIGHLIGHT_FILL   = QColor("#FFD54F")   # part being located from the list
HIGHLIGHT_EDGE   = QColor("#E65100")
BED_EDGE         = QColor("#2E86C1")   # print-bed piece outlines
CUT_EDGE         = QColor("#C0392B")   # where the split actually cuts
GHOST_FILL       = QColor("#E74C3C")   # landmasses a filter would remove
GHOST_EDGE       = QColor("#922B21")
PREVIEW_EDGE     = QColor("#117A65")   # simplified outline, not yet applied
ATTENTION_EDGE   = QColor("#C0392B")   # region a readiness check flagged

#: Layer names accepted by :meth:`MapCanvas.set_layer_visible`.
LAYER_TERRAIN    = "terrain"
LAYER_CORRIDORS  = "corridors"
LAYER_CONNECTORS = "connectors"
LAYER_LABELS     = "labels"
LAYER_BED_SPLIT  = "bed_split"

#: (label, fill, edge, dashed) — drives both the legend swatches and this file.
LEGEND_ENTRIES = [
    ("Target region (printed)", TARGET_FILL, TARGET_EDGE, False),
    ("Surrounding (context only)", SURROUNDING_FILL, SURROUNDING_EDGE, False),
    ("Border corridor", BORDER_FILL, BORDER_FILL, False),
    ("Dovetail connector", None, DOVETAIL_EDGE, True),
    ("Print-bed piece", None, BED_EDGE, True),
    ("Bed cut line", None, CUT_EDGE, False),
    ("Selected landmass", HIGHLIGHT_FILL, HIGHLIGHT_EDGE, False),
    ("Will be removed", GHOST_FILL, GHOST_EDGE, True),
    ("Simplified preview", None, PREVIEW_EDGE, True),
    ("Needs attention", None, ATTENTION_EDGE, False),
]

#: A focused part is zoomed until it is at least this many pixels across, so
#: holding a tiny island actually shows you something.
MIN_FOCUS_PX = 90.0


def geometry_to_path(geom, metres_per_mm: float) -> QPainterPath:
    """Shapely geometry (EPSG:3347 metres) -> QPainterPath in print-mm."""
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)          # interior rings punch holes
    for poly in polygon_parts(geom):
        _add_ring(path, poly.exterior.coords, metres_per_mm)
        for interior in poly.interiors:
            _add_ring(path, interior.coords, metres_per_mm)
    return path


def _add_ring(path: QPainterPath, coords, mpm: float) -> None:
    pts = [QPointF(x / mpm, -y / mpm) for x, y in coords]   # flip Y for Qt
    if len(pts) >= 3:
        path.addPolygon(QPolygonF(pts))
        path.closeSubpath()


def _segment_points(segment):
    """Accept a shapely LineString or a ((x1,y1),(x2,y2)) tuple alike."""
    coords = getattr(segment, "coords", None)
    return list(coords) if coords is not None else list(segment)


class MapCanvas(QGraphicsView):
    """Renders ProjectState regions; one path item + one label per region."""

    #: Emitted with the region name when a region is clicked on the map.
    region_clicked = Signal(str)
    #: Same click, plus the projected position (EPSG:3347 metres) that was hit
    #: and whether Ctrl was held, so the caller can work out *which part* was
    #: clicked and whether to add it to the current selection.
    region_point_clicked = Signal(str, float, float, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(BACKGROUND))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)       # hover drives the label policy

        self.region_items: dict[str, QGraphicsPathItem] = {}
        self.label_items: dict[str, QGraphicsSimpleTextItem] = {}
        self.border_items: list[QGraphicsPathItem] = []
        self.dovetail_items: list[QGraphicsPathItem] = []
        self.bed_items: list = []
        self.ghost_items: list[QGraphicsPathItem] = []
        self.preview_items: list[QGraphicsPathItem] = []

        self._border_geom = None
        self._lobes: list = []
        self._bed_pieces: list = []
        self._bed_cuts: list = []
        self._ghost_geoms: list = []
        self._preview_geoms: list = []
        self._attention: set = set()

        self.terrain_item: QGraphicsPixmapItem | None = None
        self._terrain_pixmap: QPixmap | None = None
        self._terrain_bounds = None
        self._terrain_opacity = 1.0
        self._has_fitted = False     # framed once; afterwards the camera is kept

        # Highlight is stored, not just drawn, so it survives a re-render.
        self._highlight_geoms: list = []
        self.highlight_items: list[QGraphicsPathItem] = []
        self._mpm: float = 1.0
        self._scale: float = 1.0
        self._state = None           # last rendered state, for layer toggles

        #: Dimension labels are noisy at national scale, so by default only the
        #: focused (clicked or hovered) region shows its millimetre size.  The
        #: LAYER_LABELS toggle turns them on for everything.
        self._focus_region: str | None = None
        self.layers = {
            LAYER_TERRAIN: True,
            LAYER_CORRIDORS: True,
            LAYER_CONNECTORS: True,
            LAYER_LABELS: False,
            LAYER_BED_SPLIT: True,
        }

    # -----------------------------------------------------------------------
    # Layers
    # -----------------------------------------------------------------------
    def set_layer_visible(self, layer: str, visible: bool) -> bool:
        """Toggle one layer. Returns False for an unknown layer name."""
        if layer not in self.layers:
            return False
        if self.layers[layer] == visible:
            return True
        self.layers[layer] = visible
        self._rerender()
        return True

    def is_layer_visible(self, layer: str) -> bool:
        return self.layers.get(layer, False)

    def set_terrain_opacity(self, opacity: float) -> None:
        """0 = invisible, 1 = fully opaque. Applied without a full re-render."""
        self._terrain_opacity = max(0.0, min(1.0, opacity))
        if self.terrain_item is not None:
            self.terrain_item.setOpacity(self._terrain_opacity)

    @property
    def terrain_opacity(self) -> float:
        return self._terrain_opacity

    def _rerender(self) -> None:
        if self._state is not None:
            self.render_project(self._state)

    # -----------------------------------------------------------------------
    # Highlight (follows the Step 2 list selection)
    # -----------------------------------------------------------------------
    def set_highlight(self, geoms) -> None:
        """Highlight a set of geometries, replacing whatever was highlighted."""
        self._highlight_geoms = [g for g in (geoms or [])
                                 if g is not None and not g.is_empty]
        self._redraw_highlight()

    def clear_highlight(self) -> None:
        self.set_highlight([])

    @property
    def has_highlight(self) -> bool:
        return bool(self.highlight_items)

    def _redraw_highlight(self) -> None:
        for item in self.highlight_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self.highlight_items.clear()

        pen = QPen(HIGHLIGHT_EDGE, 0)
        pen.setCosmetic(True)
        pen.setWidth(2)
        for geom in self._highlight_geoms:
            item = QGraphicsPathItem(geometry_to_path(geom, self._mpm))
            item.setBrush(QBrush(HIGHLIGHT_FILL))
            item.setPen(pen)
            item.setZValue(20)
            self._scene.addItem(item)
            self.highlight_items.append(item)

    # -----------------------------------------------------------------------
    # Step 2 previews (non-committing — see app.main_window)
    # -----------------------------------------------------------------------
    def set_ghost_geoms(self, geoms) -> None:
        """
        Show landmasses that an operation *would* remove, without removing them.

        Drawn dimmed and red-outlined on top of the region so the user can see
        exactly what a threshold is about to cost before they apply it.
        """
        self._ghost_geoms = [g for g in (geoms or [])
                             if g is not None and not g.is_empty]
        self._redraw_ghosts()

    def clear_ghosts(self) -> None:
        self.set_ghost_geoms([])

    @property
    def has_ghosts(self) -> bool:
        return bool(self.ghost_items)

    def _redraw_ghosts(self) -> None:
        for item in self.ghost_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self.ghost_items.clear()

        fill = QColor(GHOST_FILL)
        fill.setAlpha(110)
        pen = QPen(GHOST_EDGE, 0)
        pen.setCosmetic(True)
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        for geom in self._ghost_geoms:
            item = QGraphicsPathItem(geometry_to_path(geom, self._mpm))
            item.setBrush(QBrush(fill))
            item.setPen(pen)
            item.setZValue(22)
            item.setToolTip("Will be removed when you apply")
            self._scene.addItem(item)
            self.ghost_items.append(item)

    def set_preview_outline(self, geoms) -> None:
        """Overlay a candidate outline (e.g. simplified) against the current one."""
        self._preview_geoms = [g for g in (geoms or [])
                               if g is not None and not g.is_empty]
        self._redraw_preview()

    def clear_preview_outline(self) -> None:
        self.set_preview_outline([])

    @property
    def has_preview_outline(self) -> bool:
        return bool(self.preview_items)

    def _redraw_preview(self) -> None:
        for item in self.preview_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self.preview_items.clear()

        pen = QPen(PREVIEW_EDGE, 0)
        pen.setCosmetic(True)
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        for geom in self._preview_geoms:
            item = QGraphicsPathItem(geometry_to_path(geom, self._mpm))
            item.setBrush(QBrush(Qt.NoBrush))
            item.setPen(pen)
            item.setZValue(23)
            item.setToolTip("Simplified outline — not applied yet")
            self._scene.addItem(item)
            self.preview_items.append(item)

    # -----------------------------------------------------------------------
    # Attention (readiness failures, watertight failures)
    # -----------------------------------------------------------------------
    def set_attention(self, names) -> None:
        """Outline the named regions as needing attention; [] clears."""
        self._attention = {n for n in (names or []) if n}
        self._apply_attention()

    def clear_attention(self) -> None:
        self.set_attention([])

    @property
    def attention(self) -> set:
        return set(self._attention)

    def _apply_attention(self) -> None:
        for name, item in self.region_items.items():
            if name in self._attention:
                pen = QPen(ATTENTION_EDGE, 0)
                pen.setCosmetic(True)
                pen.setWidth(3)
                item.setPen(pen)
                item.setZValue(4)
            else:
                is_target = item.data(1) == TARGET
                item.setPen(QPen(TARGET_EDGE if is_target else SURROUNDING_EDGE, 0))
                item.setZValue(1 if is_target else 0)

    # -----------------------------------------------------------------------
    def center_on_geometry(self, geom_m, metres_per_mm: float = None) -> None:
        """
        Bring one geometry into view.

        Zooms in when the part would otherwise be a speck — centring on
        something you still cannot see is not much help.
        """
        if geom_m is None or geom_m.is_empty:
            return
        mpm = metres_per_mm or self._mpm
        minx, miny, maxx, maxy = geom_m.bounds
        rect = QRectF(minx / mpm, -maxy / mpm,
                      (maxx - minx) / mpm, (maxy - miny) / mpm)

        on_screen = self.transform().mapRect(rect)
        longest = max(on_screen.width(), on_screen.height())
        if 0 < longest < MIN_FOCUS_PX:
            factor = MIN_FOCUS_PX / longest
            self.scale(factor, factor)
        self.centerOn(rect.center())

    def center_on_region(self, name: str) -> bool:
        """Bring a named region into view (used to show a flagged region)."""
        item = self.region_items.get(name)
        if item is None:
            return False
        self.centerOn(item.boundingRect().center())
        return True

    # -----------------------------------------------------------------------
    # Terrain backdrop
    # -----------------------------------------------------------------------
    def set_terrain_overlay(self, rgba=None, bounds_m=None) -> None:
        """
        Set the terrain image drawn behind the provinces.

        ``rgba`` is an (H, W, 4) uint8 array covering ``bounds_m`` in projected
        metres.  Pass nothing to remove it.
        """
        if rgba is None or bounds_m is None:
            self._terrain_pixmap = None
            self._terrain_bounds = None
            return

        height, width, _ = rgba.shape
        image = QImage(rgba.tobytes(), width, height, width * 4,
                       QImage.Format_RGBA8888)
        # copy(): QImage does not take ownership of the numpy buffer
        self._terrain_pixmap = QPixmap.fromImage(image.copy())
        self._terrain_bounds = bounds_m

    @property
    def has_terrain(self) -> bool:
        return self._terrain_pixmap is not None

    @property
    def terrain_drawn(self) -> bool:
        """Whether the terrain is actually on screen (loaded *and* layer on)."""
        return self.has_terrain and self.layers[LAYER_TERRAIN]

    def _add_terrain(self, mpm: float) -> None:
        self.terrain_item = None
        if not self.terrain_drawn or self._terrain_bounds is None:
            return

        minx, miny, maxx, maxy = self._terrain_bounds
        item = QGraphicsPixmapItem(self._terrain_pixmap)
        item.setTransformationMode(Qt.SmoothTransformation)
        # Scale the image to the scene rect it covers, then place its top-left.
        # Scene Y is flipped, so the image top edge sits at -maxy.
        item.setTransform(QTransform.fromScale(
            ((maxx - minx) / mpm) / self._terrain_pixmap.width(),
            ((maxy - miny) / mpm) / self._terrain_pixmap.height()))
        item.setPos(minx / mpm, -maxy / mpm)
        item.setZValue(-10)                     # behind everything
        item.setOpacity(self._terrain_opacity)
        self._scene.addItem(item)
        self.terrain_item = item

    # -----------------------------------------------------------------------
    def set_border_overlay(self, border_geom=None, lobes=None) -> None:
        """Stash the Step 3 result; drawn on the next render."""
        self._border_geom = border_geom
        self._lobes = list(lobes or [])

    def clear_border_overlay(self) -> None:
        self.set_border_overlay(None, [])

    def set_bed_overlay(self, pieces=None, cuts=None) -> None:
        """
        Stash the print-bed split: piece footprints and the lines cutting them.

        Both come from ``core.borders`` / ``core.meshing``, so the grid drawn
        here is the grid the export will actually use — including the corridor
        cut positions that were nudged sideways to miss a connector.
        """
        self._bed_pieces = list(pieces or [])
        self._bed_cuts = list(cuts or [])

    def clear_bed_overlay(self) -> None:
        self.set_bed_overlay([], [])

    @property
    def has_bed_overlay(self) -> bool:
        return bool(self.bed_items)

    def render_project(self, state) -> None:
        """
        Redraw every region, plus any overlays.

        The camera is preserved across redraws — an edit in Step 2 should not
        yank the view back to the whole map.  The view is only fitted the first
        time content appears, or when the user asks via :meth:`fit_to_contents`.
        """
        self._state = state
        restore_view = self._has_fitted
        if restore_view:
            prior_centre = self.mapToScene(self.viewport().rect().center())
            prior_transform = QTransform(self.transform())

        self._scene.clear()          # deletes every item, highlight included
        self.region_items.clear()
        self.label_items.clear()
        self.border_items.clear()
        self.dovetail_items.clear()
        self.bed_items.clear()
        self.highlight_items.clear()
        self.ghost_items.clear()
        self.preview_items.clear()

        mpm = state.metres_per_mm
        self._mpm = mpm
        self._scale = state.scale
        self._add_terrain(mpm)
        for region in state.regions:
            self._add_region(region, mpm, state.scale)

        self._add_border_overlay(mpm)
        self._add_bed_overlay(mpm, state.scale)
        self._redraw_highlight()     # stored overlays survive a re-render
        self._redraw_ghosts()
        self._redraw_preview()
        self._apply_attention()

        rect = self._scene.itemsBoundingRect()
        if not rect.isEmpty():
            self._scene.setSceneRect(rect.adjusted(-10, -10, 10, 10))

        if restore_view:
            self.setTransform(prior_transform)
            self.centerOn(prior_centre)
        elif not rect.isEmpty():
            self.fit_to_contents()

    def _add_border_overlay(self, mpm: float) -> None:
        if (self.layers[LAYER_CORRIDORS] and self._border_geom is not None
                and not self._border_geom.is_empty):
            item = QGraphicsPathItem(geometry_to_path(self._border_geom, mpm))
            item.setBrush(QBrush(BORDER_FILL))
            item.setPen(QPen(BORDER_FILL, 0))
            item.setZValue(5)
            item.setToolTip("Border corridor")
            self._scene.addItem(item)
            self.border_items.append(item)

        if not self.layers[LAYER_CONNECTORS]:
            return
        # Dovetail connectors: dashed outlines so they read as joinery, not land
        pen = QPen(DOVETAIL_EDGE, 0)
        pen.setStyle(Qt.DashLine)
        pen.setCosmetic(True)
        for lobe in self._lobes:
            item = QGraphicsPathItem(geometry_to_path(lobe, mpm))
            item.setBrush(QBrush(Qt.NoBrush))
            item.setPen(pen)
            item.setZValue(6)
            item.setToolTip("Dovetail connector")
            self._scene.addItem(item)
            self.dovetail_items.append(item)

    def _add_bed_overlay(self, mpm: float, scale: float) -> None:
        """Draw the print-bed pieces and the cut lines that separate them."""
        if not self.layers[LAYER_BED_SPLIT]:
            return

        piece_pen = QPen(BED_EDGE, 0)
        piece_pen.setCosmetic(True)
        piece_pen.setWidth(2)
        piece_pen.setStyle(Qt.DashLine)

        for index, piece in enumerate(self._bed_pieces, start=1):
            if piece is None or piece.is_empty:
                continue
            item = QGraphicsPathItem(geometry_to_path(piece, mpm))
            item.setBrush(QBrush(Qt.NoBrush))
            item.setPen(piece_pen)
            item.setZValue(8)
            minx, miny, maxx, maxy = piece.bounds
            w_mm, h_mm = (maxx - minx) / mpm, (maxy - miny) / mpm
            item.setToolTip(f"Piece {index} · {w_mm:.0f} × {h_mm:.0f} mm")
            self._scene.addItem(item)
            self.bed_items.append(item)

            label = QGraphicsSimpleTextItem(
                f"{index}\n{w_mm:.0f} × {h_mm:.0f} mm")
            label.setBrush(QBrush(BED_EDGE))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            label.setPos(((minx + maxx) / 2) / mpm, -((miny + maxy) / 2) / mpm)
            label.setZValue(12)
            self._scene.addItem(label)
            self.bed_items.append(label)

        cut_pen = QPen(CUT_EDGE, 0)
        cut_pen.setCosmetic(True)
        cut_pen.setWidth(2)
        for cut in self._bed_cuts:
            points = _segment_points(cut)
            if len(points) < 2:
                continue
            path = QPainterPath(QPointF(points[0][0] / mpm, -points[0][1] / mpm))
            for x, y in points[1:]:
                path.lineTo(x / mpm, -y / mpm)
            item = QGraphicsPathItem(path)
            item.setPen(cut_pen)
            item.setZValue(9)
            item.setToolTip("Bed cut line")
            self._scene.addItem(item)
            self.bed_items.append(item)

    def _add_region(self, region, mpm: float, scale: float) -> None:
        is_target = region.role == TARGET
        item = QGraphicsPathItem(geometry_to_path(region.geometry, mpm))
        fill = QColor(TARGET_FILL if is_target else SURROUNDING_FILL)
        if self.terrain_drawn:
            fill.setAlpha(120)          # let the terrain read through
        item.setBrush(QBrush(fill))
        item.setPen(QPen(TARGET_EDGE if is_target else SURROUNDING_EDGE, 0))
        item.setZValue(1 if is_target else 0)
        item.setToolTip(self._tooltip(region, scale))
        item.setData(0, region.name)
        item.setData(1, region.role)     # so attention styling can restore it
        self._scene.addItem(item)
        self.region_items[region.name] = item

        label = QGraphicsSimpleTextItem(self._label_text(region, scale))
        label.setBrush(QBrush(LABEL_COLOUR))
        # Keep labels legible at any zoom
        label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        centre = item.boundingRect().center()
        label.setPos(centre)
        label.setZValue(10)
        self._scene.addItem(label)
        self.label_items[region.name] = label

    def _label_text(self, region, scale: float) -> str:
        """
        Region name always; millimetre dimensions only where they earn the room.

        At national scale a dimension on every province is unreadable clutter,
        so they follow the focus (click or hover) unless the labels layer is
        switched on for everything.
        """
        if not (self.layers[LAYER_LABELS] or region.name == self._focus_region):
            return region.name
        w_mm, h_mm = region.size_mm(scale)
        return f"{region.name}\n{w_mm:.0f} × {h_mm:.0f} mm"

    def set_focus_region(self, name: str | None) -> bool:
        """Point the dimension labels at one region. Returns True if changed."""
        if name == self._focus_region:
            return False
        self._focus_region = name
        self._refresh_labels()
        return True

    @property
    def focus_region(self) -> str | None:
        return self._focus_region

    def _refresh_labels(self) -> None:
        """Retext the labels in place — far cheaper than a full re-render."""
        if self._state is None:
            return
        for region in self._state.regions:
            label = self.label_items.get(region.name)
            if label is not None:
                label.setText(self._label_text(region, self._scale))

    @staticmethod
    def _tooltip(region, scale: float) -> str:
        w_mm, h_mm = region.size_mm(scale)
        w_km, h_km = region.size_km()
        return (f"{region.name} ({region.role})\n"
                f"{w_mm:.1f} × {h_mm:.1f} mm\n"
                f"{w_km:,.0f} × {h_km:,.0f} km")

    # -----------------------------------------------------------------------
    def fit_to_contents(self) -> None:
        """Frame everything. Also the 'Recenter' action."""
        rect = self._scene.itemsBoundingRect()
        if not rect.isEmpty():
            self._scene.setSceneRect(rect.adjusted(-10, -10, 10, 10))
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
            self._has_fitted = True

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mouseMoveEvent(self, event):
        """Hovering a region gives it the dimension label."""
        super().mouseMoveEvent(event)
        if not self.layers[LAYER_LABELS]:
            self.set_focus_region(self.region_at(event.position().toPoint()))

    def mousePressEvent(self, event):
        """Left-click a region to select it; anything else pans as usual."""
        if event.button() == Qt.LeftButton:
            view_pos = event.position().toPoint()
            name = self.region_at(view_pos)
            if name:
                self.set_focus_region(name)
                self.region_clicked.emit(name)
                # Hand back the projected point so the caller can identify the
                # exact island that was hit (scene Y is flipped).
                scene_pos = self.mapToScene(view_pos)
                additive = bool(event.modifiers() & Qt.ControlModifier)
                self.region_point_clicked.emit(
                    name, scene_pos.x() * self._mpm, -scene_pos.y() * self._mpm,
                    additive)
        super().mousePressEvent(event)

    def region_at(self, view_pos) -> str | None:
        """Region name under a view-space point, or None."""
        for item in self.items(view_pos):
            name = item.data(0)
            if name:
                return name
        return None

    @property
    def scene_obj(self) -> QGraphicsScene:
        return self._scene
