"""
island_erasing.py

Lifts land away along a freehand brush stroke, so coastline can be cut by hand.

:func:`erase_stroke` sweeps the same brush :func:`lib.LIB_island_bridging.brush_stroke`
lays down and subtracts it from the document instead of unioning it on — the
exact inverse of :func:`lib.LIB_island_bridging.draw_bridge`.

It is also the freehand counterpart to :mod:`lib.LIB_island_removal`, which can
only drop islands **whole** (the ones picked, or the ones under an area
threshold).  The eraser takes a bite out of whatever it is dragged across
instead: trimming a spit, rubbing out a causeway that should not be there, or
cutting one landmass into two.

Interior rings
--------------
Unlike drawing, erasing **keeps every hole it opens**.  A stroke that closes a
ring inside a landmass is a pocket the user deliberately rubbed out, and filling
it back in would undo the edit — where a *bridge* encloses open sea only as a
side effect of joining two shores, which is why that case is filled.  ``Remove
Lakes`` is there for a pocket that turns out not to be wanted.

Coordinates are expected in a projected metre CRS (the project uses EPSG:3347),
so the brush width is a real ground distance.
"""

import logging

from lib.LIB_island_bridging import brush_stroke

log = logging.getLogger(__name__)


def erase_stroke(doc, points, width_km: float):
    """
    Subtract a brush stroke from the document.

    Parameters
    ----------
    doc       : the document geometry (Polygon/MultiPolygon in a metre CRS), or
                None/empty — there is then nothing to erase.
    points    : the pointer path, as for :func:`brush_stroke`.
    width_km  : brush **width** (not radius) in kilometres.

    Returns
    -------
    The document with the swept region removed, or **the very same object** when
    the stroke takes nothing away — so a caller can detect a no-op by identity,
    exactly as with :func:`draw_bridge`.  ``doc`` is likewise returned unchanged
    if the stroke is empty, or on any geometry failure: erasing never destroys
    the document by accident.

    A stroke that covers the whole document returns an **empty** geometry.  That
    is a real answer rather than a failure — the caller decides whether to accept
    an emptied document (the editor refuses, and says so).
    """
    stroke = brush_stroke(points, width_km)
    if stroke is None or doc is None or doc.is_empty:
        return doc

    # Scribbling over open sea takes nothing away, and the difference would not
    # say so: it rebuilds the rings, so the result compares unequal byte for
    # byte while being the same land.  Answering by identity keeps that stroke
    # out of the undo history, and skips the difference entirely.
    if not doc.intersects(stroke):
        return doc

    try:
        remaining = doc.difference(stroke)
    except Exception as exc:
        log.warning("erase_stroke failed: %s", exc)
        return doc

    # An empty result is honest (the stroke covered everything) and is passed
    # back as such; only a missing one is a failure worth falling back from.
    return doc if remaining is None else remaining
