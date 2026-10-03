"""
island_bridging.py

Lays down land along a freehand brush stroke, so islands can be joined by hand.

:func:`draw_bridge` buffers the stroke's polyline by half the brush width and
unions the result onto the document.  A stroke crossing two islands therefore
welds them into one landmass, at exactly the path and width drawn — and nothing
more: water the new land happens to enclose is left as water.

This is the deliberate counterpart to
:func:`lib.LIB_island_merging.merge_islands`, which is conservative by design —
it closes the whole group and then keeps only the necks spanning two or more
islands, so it can never add land that was not asked for.  The brush is the
manual escape hatch for the cases that heuristic cannot serve: a gap too wide
for any workable buffer, or a buffer that bridges the wrong pair before it
reaches the intended one.  Here the stroke is honoured **literally** — whatever
is drawn becomes land, and only that.

Coordinates are expected in a projected metre CRS (the project uses EPSG:3347),
so the brush width is a real ground distance.
"""

import logging

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from lib.LIB_island_merging import _polygons

log = logging.getLogger(__name__)

#: Metres per kilometre — the brush is specified in km but buffers in the
#: geometry's metre units (the project works in EPSG:3347 metres).
_METRES_PER_KM = 1000.0

#: Stroke simplification tolerance, as a fraction of the brush radius.  A drag
#: samples the pointer at the mouse's rate, so a slow hand lays down thousands
#: of near-coincident points; below this spacing they cannot move the buffered
#: outline by a visible amount, and dropping them keeps the buffer cheap and the
#: saved geometry clean.
_SIMPLIFY_FRACTION = 0.01

#: Hairline-ring tolerance, as a fraction of the brush radius.  Unioning the
#: stroke's round outline onto a coastline it grazes can leave a four-point
#: crumb of a ring behind — on real provinces, 30 m² against an 18 km brush —
#: and such a ring draws as a border scratch inside solid land.  A ring thinner
#: than this cannot hold water at any print scale, so it is numerical litter
#: rather than something the stroke enclosed.  It is the same relative hair that
#: :func:`lib.LIB_island_merging.merge_islands` welds its necks with.
_HAIRLINE_FRACTION = 0.001


def _distinct(points) -> list:
    """The stroke's points with consecutive duplicates dropped."""
    out: list = []
    for point in points:
        xy = (float(point[0]), float(point[1]))
        if not out or xy != out[-1]:
            out.append(xy)
    return out


def brush_stroke(points, width_km: float):
    """
    The land a brush stroke lays down: its path buffered by half the width.

    Round caps and joins make the buffered polyline exactly the region swept by
    a circular brush of ``width_km`` carried along ``points`` — so what the
    stroke covers on screen is what becomes land.

    Parameters
    ----------
    points    : iterable of ``(x, y)`` in a projected metre CRS — the pointer
                path, in the order drawn.  Consecutive duplicates are ignored;
                a single distinct point is a dab (a disc of the brush size).
    width_km  : brush **width** (not radius) in kilometres.  0 or negative
                draws nothing.

    Returns
    -------
    A Polygon/MultiPolygon of the swept land, or None if the stroke is empty or
    the width is not positive.
    """
    if width_km is None or width_km <= 0:
        return None
    path_points = _distinct(points or [])
    if not path_points:
        return None

    radius_m = width_km * _METRES_PER_KM / 2.0
    if len(path_points) == 1:
        path = Point(path_points[0])
    else:
        path = LineString(path_points).simplify(radius_m * _SIMPLIFY_FRACTION)

    stroke = path.buffer(radius_m, cap_style="round", join_style="round")
    return None if stroke.is_empty else stroke


def _without_hairlines(geom, radius_m: float):
    """
    The geometry with hairline interior rings dropped, real water kept.

    A ring that cannot hold a disc of :data:`_HAIRLINE_FRACTION` of the brush
    radius is a crumb left by the union, not a pocket of water; see that
    constant.  Everything wider survives, lakes and newly enclosed straits
    alike, so the stroke only ever adds the land it drew.
    """
    hair = max(radius_m * _HAIRLINE_FRACTION, 1e-6)
    rebuilt, dropped = [], False
    for part in _polygons(geom):
        holes = [ring for ring in part.interiors
                 if not Polygon(ring).buffer(-hair).is_empty]
        dropped = dropped or len(holes) != len(part.interiors)
        rebuilt.append(Polygon(part.exterior, holes))
    if not dropped or not rebuilt:
        return geom          # nothing to rebuild; keep the union's own object
    return rebuilt[0] if len(rebuilt) == 1 else MultiPolygon(rebuilt)


def draw_bridge(doc, points, width_km: float):
    """
    Union a brush stroke onto the document, keeping the water it encloses.

    Interior rings
    --------------
    Every hole the stroke closes is **kept**, exactly as
    :func:`lib.LIB_island_erasing.erase_stroke` keeps the holes it opens: a
    strait sealed at both ends, or water a stroke was drawn in a ring around,
    becomes a lake rather than land, and the document's own lakes come through
    untouched.  The brush adds the land it was dragged over and nothing else;
    ``Remove Lakes`` is there for a pocket that turns out not to be wanted.
    Only hairline rings the union itself leaves behind are dropped — see
    :data:`_HAIRLINE_FRACTION`.

    Parameters
    ----------
    doc       : the document geometry (Polygon/MultiPolygon in a metre CRS), or
                None/empty to draw on a blank document.
    points    : the pointer path, as for :func:`brush_stroke`.
    width_km  : brush width in kilometres.

    Returns
    -------
    The document with the stroke added, or **the very same object** when the
    stroke adds nothing — so a caller can detect a no-op by identity.  ``doc``
    is likewise returned unchanged if the stroke is empty, or on any geometry
    failure: drawing never destroys the document.
    """
    stroke = brush_stroke(points, width_km)
    if stroke is None:
        return doc
    if doc is None or doc.is_empty:
        return stroke

    # Scribbling over land the document already has adds nothing, and the union
    # would not say so: it rebuilds the rings, so the result compares unequal
    # byte for byte while being the same shape.  Answering by identity keeps
    # that stroke out of the undo history, and skips the union entirely — the
    # covers test costs about 15 ms on the heaviest province against 200 ms.
    if doc.covers(stroke):
        return doc

    try:
        merged = unary_union([doc, stroke])
        merged = _without_hairlines(merged, width_km * _METRES_PER_KM / 2.0)
    except Exception as exc:
        log.warning("draw_bridge failed: %s", exc)
        return doc

    if merged is None or merged.is_empty:
        return doc
    return merged
