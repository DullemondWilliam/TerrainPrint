"""
app.widgets

Small custom widgets for the shell.
"""

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QListWidget, QPushButton, QVBoxLayout,
                               QWidget)


class HoldListWidget(QListWidget):
    """
    A list that reports press-and-hold on a row.

    Plain clicking still selects as normal; holding a row for
    :attr:`hold_ms` fires :attr:`item_held` so the caller can preview that
    entry (here: centre the map on an island).  :attr:`hold_released` fires
    afterwards so the preview can be undone.
    """

    item_held = Signal(int)       # row index
    hold_released = Signal()

    def __init__(self, parent=None, hold_ms: int = 350):
        super().__init__(parent)
        self.hold_ms = hold_ms

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire_hold)

        self._pending_row = -1
        self._holding = False

    # -----------------------------------------------------------------------
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        item = self.itemAt(event.position().toPoint())
        self._pending_row = self.row(item) if item is not None else -1
        if self._pending_row >= 0:
            self._timer.start(self.hold_ms)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._end_hold()

    def leaveEvent(self, event):
        # Dragging off the widget should not leave a preview stuck on
        super().leaveEvent(event)
        self._end_hold()

    # -----------------------------------------------------------------------
    def _fire_hold(self) -> None:
        if self._pending_row >= 0:
            self._holding = True
            self.item_held.emit(self._pending_row)

    def _end_hold(self) -> None:
        self._timer.stop()
        self._pending_row = -1
        if self._holding:
            self._holding = False
            self.hold_released.emit()


class RestoreToast(QFrame):
    """
    A one-shot "that was a mistake" bar shown just after a destructive Back.

    Deliberately narrow in scope: it covers the misclick, not backward editing.
    It is dismissed by :meth:`hide_toast` as soon as the user does anything
    else, because once they have started working in the phase they have gone
    back to, restoring the discarded output would drop stale state back into a
    project that has already moved on.
    """

    restore_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background:#FDF3D8; border:1px solid #D4AC0D;"
            " border-radius:4px; }")

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 6, 5)
        row.setSpacing(8)

        self.message = QLabel()
        self.message.setStyleSheet("color:#7D6608; font-size:11px; border:none;")
        self.message.setWordWrap(True)
        row.addWidget(self.message, stretch=1)

        self.restore_button = QPushButton("Undo going back")
        self.restore_button.setStyleSheet("font-size:11px;")
        self.restore_button.clicked.connect(self.restore_requested)
        row.addWidget(self.restore_button)

        dismiss = QPushButton("✕")
        dismiss.setFixedWidth(24)
        dismiss.setFlat(True)
        dismiss.clicked.connect(self.hide_toast)
        row.addWidget(dismiss)

        self.hide()

    def show_toast(self, text: str) -> None:
        self.message.setText(text)
        self.show()

    def hide_toast(self) -> None:
        self.hide()

    @property
    def is_showing(self) -> bool:
        # isHidden(), not isVisible(): the toast is "showing" once it has been
        # shown, whether or not an ancestor happens to be on screen yet.
        return not self.isHidden()


class LegendWidget(QFrame):
    """
    A persistent key for the map's visual vocabulary.

    Entries are ``(label, fill, edge, dashed)``; ``fill`` may be None for an
    outline-only entry (dovetails, cut lines).  Built from data the canvas
    owns so the swatches cannot drift from what is actually drawn.
    """

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        title = QLabel("Legend")
        title.setStyleSheet("font-weight:600; font-size:11px;")
        layout.addWidget(title)
        for entry in entries:
            layout.addWidget(_LegendRow(*entry))

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), 160))
        return hint


class _LegendRow(QWidget):
    """One swatch + label."""

    def __init__(self, label: str, fill, edge, dashed: bool = False, parent=None):
        super().__init__(parent)
        self._fill, self._edge, self._dashed = fill, edge, dashed
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addSpacing(22)                 # room for the swatch drawn in paintEvent
        text = QLabel(label)
        text.setStyleSheet("font-size:10px; color:#444;")
        row.addWidget(text, stretch=1)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        box = QRectF(1, (self.height() - 10) / 2.0, 18, 10)

        pen = QPen(QColor(self._edge), 1.4)
        if self._dashed:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(self._fill)) if self._fill else Qt.NoBrush)
        painter.drawRect(box)


class AssemblyDiagramView(QGraphicsView):
    """
    Top-down map of the exported pieces, drawn from ``core.meshing`` plain data.

    Reads :class:`core.meshing.AssemblyPiece` records — the same records that
    produced the SVG written next to the STLs — so what the user sees here and
    what they get in the folder are the same diagram.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor("#FFFFFF")))
        self.setMinimumHeight(240)

    def set_pieces(self, pieces, connectors=()) -> None:
        self._scene.clear()
        if not pieces:
            return

        label_font = QFont()
        label_font.setBold(True)
        label_font.setPointSizeF(max(6.0, min(p.width_mm for p in pieces) / 5.0))

        for piece in pieces:
            # Scene Y grows downwards, so flip the piece's bottom edge over.
            rect = QRectF(piece.x_mm, -(piece.y_mm + piece.height_mm),
                          piece.width_mm, piece.height_mm)
            item = self._scene.addRect(
                rect, QPen(QColor("#1B4F72"), 0), QBrush(QColor("#EAF2F8")))
            item.setToolTip(f"{piece.label} · {piece.name}\n"
                            f"{piece.width_mm:.0f} × {piece.height_mm:.0f} mm")

            text = self._scene.addSimpleText(piece.label, label_font)
            text.setBrush(QBrush(QColor("#1B4F72")))
            bounds = text.boundingRect()
            text.setPos(rect.center().x() - bounds.width() / 2,
                        rect.center().y() - bounds.height() / 2)

        pen = QPen(QColor("#8E2D00"), 0)
        brush = QBrush(QColor("#8E2D00"))
        for x_mm, y_mm in connectors:
            self._scene.addEllipse(x_mm - 0.8, -y_mm - 0.8, 1.6, 1.6, pen, brush)

        self._scene.setSceneRect(self._scene.itemsBoundingRect())
        self.fit()

    def fit(self) -> None:
        rect = self._scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.fitInView(rect.adjusted(-5, -5, 5, 5), Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()
