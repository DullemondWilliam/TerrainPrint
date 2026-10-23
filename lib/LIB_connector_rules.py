#!/usr/bin/env python3
"""
LIB_connector_rules.py

The rules a dovetail connector is held to, wherever it comes from -- placed
by Define, by hand, or by a reshape -- and the geometry of moving one.

A connector (a *lobe*, from ``LIB_border_connectors``) must reach one printed
province, and only one (FR-3.7); its pocket must stay inside that province;
it must keep enough root in the corridor to hang on to the bar and a neck the
printer can hold; it must meet the corridor in one place only; and it must
not crowd another connector's pocket.  ``_fault`` names the first rule a
lobe breaks, ``FAULTS`` / ``STANDING_FAULTS`` / ``FAULT_COUNTS`` say it in
words for a refused placement, a connector already standing, and Define's
report.

Everything here is handed a *definition* -- an object with ``border``,
``targets``, ``lobes``, ``settings`` (a ``BorderSettings``) and ``land()``,
as ``LIB_border_plan.BorderDefinition`` is -- and reads nothing else, so it
runs off the UI thread and tests call it directly.

Split out of ``UI_terrain_editor.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from shapely.geometry import Point

from lib.LIB_border_connectors import (ConnectorParams, _polys, lobe_at,
                                       lobe_pose)

if TYPE_CHECKING:                      # the plan imports these rules
    from lib.LIB_border_plan import BorderDefinition, BorderSettings

#: A lobe counts as reaching into a region past this fraction of its own area,
#: so a lobe grazing a neighbour's corner at float precision is not an owner.
_OWNER_FRAC = 1e-3


#: How much of a pocket, as a fraction of its lobe's area, may lie outside its
#: province before it counts as running out: only the slivers where the
#: carved province edge and the corridor edge disagree by rounding.
_ESCAPE_FRAC = 1e-3


#: Why a connector cannot stand: fault code -> what a refused placement says
#: (``place_connector`` without ``allow_faults``).
FAULTS = {
    "no_province": ("That edge faces no province to print -- a surrounding "
                    "region has nothing to key into."),
    "two_provinces": ("It would reach into two provinces, with no one pocket "
                      "to key into."),
    "escapes": ("Its pocket would run out of the province -- past the coast "
                "or into a neighbour."),
    "crowded": "It would run into another connector.",
    "detached": ("It would come loose from the bar: too little of its root "
                 "is left in the corridor."),
    "recrosses": ("It would meet the corridor in two places -- its far end "
                  "runs back into the bar past a bend or a junction."),
    "on_split": "It would sit on a split, and be cut in two.",
    "thin": "Its neck would be too thin to print.",
}


#: The same, for a connector already placed: what the status line says of a
#: red one.
STANDING_FAULTS = {
    "no_province": "it faces no province to print",
    "two_provinces": "it reaches into two provinces",
    "escapes": "its pocket runs out of the province",
    "crowded": "it runs into another connector",
    "detached": "too little of its root is in the corridor",
    "recrosses": "it meets the corridor in two places",
    "on_split": "it sits on a split, and would be cut in two",
    "thin": "its neck is too thin to print",
}


#: The same, as the Borders report counts generated connectors it dropped.
FAULT_COUNTS = {
    "no_province": "faced no province",
    "two_provinces": "reached into two provinces",
    "escapes": "ran out of their province",
    "crowded": "ran into another connector",
    "detached": "came loose from the bar",
    "recrosses": "met the corridor in two places",
    "crosses_line": "crossed the border line between two provinces",
    "on_split": "sat on a split",
    "thin": "were too thin to print",
}


#: How much of a connector's root must stay in the corridor, as a fraction of
#: what an unturned one of its size has there, for the bar to hold it.
_ROOT_HOLD_FRAC = 0.5


def _pieces(geom, lobe) -> int:
    """How many separate pieces of ``geom`` count, against ``lobe``'s size:
    a sliver where two outlines disagree by rounding is not a crossing."""
    return sum(1 for poly in _polys(geom)
               if poly.area > _OWNER_FRAC * lobe.area)


def _connector_fault(lobe, definition, others, p, land) -> str:
    """Why ``lobe`` cannot stand beside ``others``, or "" when it can.
    ``land`` is the union of the targets, passed in so a caller checking
    many lobes builds it once."""
    code = _fault(lobe, definition, others, p, land)
    return FAULTS[code] if code else ""


def _fault(lobe, definition, others, p, land) -> str:
    """
    The code (a key of ``FAULTS``) of the first rule ``lobe`` breaks beside
    ``others``, or "" when it breaks none.

    * It must reach a province to print, and only one (FR-3.7).
    * Its pocket -- the lobe grown by the clearance, outside the corridor --
      must lie inside that one province: a pocket that crosses the coast or a
      frontier opens in the side of the piece.
    * It must not touch another connector, and its pocket must stay a
      clearance clear of every other's, or two pockets run into one.
    * Turned or shrunk by hand, it must keep enough root in the corridor to
      hang on to the bar, and a neck the printer can hold.
    * It must meet the corridor in one place only -- its root.  Near a bend
      or a junction the far end can run back into the bar, which fuses the
      connector to the bar there and leaves its pocket with a side open.
    """
    code = _own_fault(lobe, definition, p, land)
    if code:
        return code
    reach = lobe.difference(definition.border)
    gap = p.clearance_mm * p.mpm
    for other in others:
        if _crowds(lobe, reach, other, other.difference(definition.border)
                   if _near(lobe, other, gap) else None, gap):
            return "crowded"
    return ""


def _own_fault(lobe, definition, p, land) -> str:
    """The first rule ``lobe`` breaks on its own -- every rule but
    crowding, which needs the others -- or ""."""
    d = definition
    c = lobe.exterior.coords
    neck_m = Point(c[0]).distance(Point(c[3]))
    floor_m = min(2 * d.settings.nozzle_mm, p.neck_mm) * p.mpm
    if neck_m < floor_m * (1 - 1e-6):
        return "thin"
    root_m2 = p.overlap_mm * p.mpm * neck_m          # an unturned root's area
    in_bar = lobe.intersection(d.border)
    if in_bar.area < _ROOT_HOLD_FRAC * root_m2:
        return "detached"
    if _pieces(in_bar, lobe) > 1 \
            or _pieces(lobe.difference(d.border), lobe) > 1:
        return "recrosses"
    if lobe.intersection(land).area < p.min_overlap_frac * lobe.area:
        return "no_province"
    targets = list(d.targets.values())
    if _owners(lobe, d.border, targets) != 1:
        return "two_provinces"
    gap = p.clearance_mm * p.mpm
    pocket = lobe.buffer(gap).difference(d.border)
    owner = max(targets, key=lambda geom: pocket.intersection(geom).area)
    if pocket.difference(owner).area > _ESCAPE_FRAC * lobe.area:
        return "escapes"
    return ""


def _near(lobe, other, gap) -> bool:
    """Whether ``other``'s box comes within ``gap`` of ``lobe``'s: the cheap
    test that spares the geometry for the many that are nowhere near."""
    x0, y0, x1, y1 = lobe.bounds
    ox0, oy0, ox1, oy1 = other.bounds
    return not (ox1 < x0 - gap or ox0 > x1 + gap
                or oy1 < y0 - gap or oy0 > y1 + gap)


def _crowds(lobe, reach, other, other_reach, gap) -> bool:
    """Whether two connectors touch, or their pockets come within ``gap``.
    ``other_reach`` is None when the two are nowhere near."""
    if other_reach is None:
        return False
    return lobe.intersects(other) or reach.distance(other_reach) < gap


def _owners(lobe, border, targets) -> int:
    """How many of ``targets`` the lobe's far end reaches into."""
    reach = lobe.difference(border)
    return sum(1 for geom in targets
               if reach.intersects(geom)
               and reach.intersection(geom).area > _OWNER_FRAC * lobe.area)


def place_connector(definition: BorderDefinition, x: float, y: float,
                    ignore: int | None = None, angle_deg: float = 0.0,
                    scale: float = 1.0, allow_faults: bool = False):
    """
    The connector a click at (x, y) puts on the corridor edge nearest it, and
    why not when it cannot go there: ``(lobe, "")`` or ``(None, reason)``.

    A hand-placed lobe is held to the rules a generated one is -- it reaches a
    province to print, and only one (FR-3.7) -- and to one more: its far end
    stays a clearance clear of every other lobe's, or two pockets run into
    one.  ``ignore`` is the index of the lobe being moved, which may land
    where it already was.  ``angle_deg`` and ``scale`` turn and size it, as
    in :func:`lobe_at`.

    ``allow_faults`` hands the connector back even when it breaks a rule,
    as ``(lobe, reason)``: placed by hand, it is kept and drawn red until an
    edit makes it valid.  Only a connector that cannot be built at all is
    ``(None, reason)``.
    """
    d = definition
    if d.border is None:
        return None, "There is no corridor to put a connector on."
    p = d.settings.connector_params()
    lobe = lobe_at(d.border, x, y, p, angle_deg, scale)
    if lobe is None:
        return None, "No connector fits there."
    others = [other for i, other in enumerate(d.lobes) if i != ignore]
    why = _connector_fault(lobe, d, others, p, d.land())
    if allow_faults:
        return lobe, why
    return (None, why) if why else (lobe, "")


def reshape_connectors(definition: BorderDefinition,
                       settings: BorderSettings):
    """
    ``definition``'s connectors in the shape ``settings`` asks for, each where
    it stood: ``(lobes, dropped)``.

    A lobe is rebuilt at its anchor on the corridor edge, turned and sized as
    it was (:func:`lobe_pose`), so a connector put, turned or resized by hand
    stays as it was left.  One the new shape no longer lets stand -- grown into
    a neighbour, or now reaching into two provinces -- is kept, and drawn red
    until it is fixed; only one that cannot be built at all is dropped and
    counted.  The rest keep their order.
    """
    d = definition
    if d.border is None or not d.lobes:
        return list(d.lobes), 0
    old, new = d.settings.connector_params(), settings.connector_params()
    kept, dropped = [], 0
    for lobe in d.lobes:
        x, y, angle, scale = lobe_pose(lobe, d.border, old)
        reshaped = lobe_at(d.border, x, y, new, angle, scale)
        if reshaped is None:
            dropped += 1
        else:
            kept.append(reshaped)
    return kept, dropped


def _standing_lobes(lobes, definition, p: ConnectorParams):
    """
    The generated ``lobes`` that may stand, in order, and why the rest may
    not: ``(kept, {fault code: count})``.

    Each is held to the rules a hand-placed connector is (:func:`_fault`)
    against the ones already kept, so no two pockets run together and none
    leaves the province it keys into.  First come, first kept: along a seam
    that is the order they were spaced in.

    And to one more, here only: it must not cross the border line between
    two printed provinces.  In the inside corner of a bend -- Victoria
    Island's NWT/Nunavut seam has one -- a connector hung from one leg can
    swing its root over the other leg's line.  By hand the same graze is
    allowed: a connector turned past about 18 degrees puts its root corner
    over the line by design, and the bar it sits in is one piece.
    """
    land = definition.land()
    line = definition.border_line()
    kept, reasons = [], {}
    for lobe in lobes:
        code = _fault(lobe, definition, kept, p, land)
        if not code and line is not None and lobe.intersects(line):
            code = "crosses_line"
        if code:
            reasons[code] = reasons.get(code, 0) + 1
        else:
            kept.append(lobe)
    return kept, reasons


# ---------------------------------------------------------------------------
# Moving a connector by hand
# ---------------------------------------------------------------------------
def connector_pose(definition, index: int) -> tuple:
    """``(x, y, angle_deg, scale)`` of connector ``index``: where it meets
    the corridor, its turn from square, and its size (see ``lobe_pose``)."""
    d = definition
    return lobe_pose(d.lobes[index], d.border, d.settings.connector_params())


def handle_at(definition, index: int) -> tuple:
    """Where the turning handle of connector ``index`` is drawn: the middle
    of its far end."""
    c = definition.lobes[index].exterior.coords
    return (c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2


def angle_toward(definition, index: int, x: float, y: float) -> float:
    """The angle that points connector ``index`` at (x, y), measured from
    square to its edge as ``lobe_pose`` measures it -- what dragging its
    handle to (x, y) turns it to."""
    ax_, ay_, angle, _ = connector_pose(definition, index)
    hx, hy = handle_at(definition, index)
    here = np.degrees(np.arctan2(hy - ay_, hx - ax_))
    there = np.degrees(np.arctan2(y - ay_, x - ax_))
    turn = (there - here + 180.0) % 360.0 - 180.0
    return angle + turn
