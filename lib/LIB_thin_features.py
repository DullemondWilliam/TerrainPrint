#!/usr/bin/env python3
"""
LIB_thin_features.py

Find the land that is too narrow to print, before it is printed.

A map tile comes off the bed as a solid of some thickness, and land narrower
than about a millimetre on paper does not survive being one: a neck snaps when
the piece is lifted, a spit breaks off in the post, and an islet smaller than
the nozzle can lay simply is not there.  This module answers one question about
a province -- **where is the land narrower than W?** -- and answers it in the
millimetres the printer works in::

    from lib.LIB_thin_features import ThinParams, thin_features

    report = thin_features(province, ThinParams(width_mm=1.0))
    print(report.patches, "thin patches,",
          f"{report.thin_area_mm2:.1f} mm2", "of",
          f"{report.land_area_mm2:.1f} mm2", "below 1 mm")

All lengths are **print-mm** and convert through ``print.scale`` at the moment
of use, as in :class:`lib.LIB_shared_border.SeamParams`.  Geometry is expected
in a projected metre CRS (the project uses EPSG:3347).

How the width is measured
-------------------------
By morphological **opening**: erode the land by half the width, dilate it back
by the same amount, and whatever does not come back was narrower than the width.
A disc of diameter W cannot be placed inside a neck narrower than W, so the
erosion deletes that neck and the dilation cannot restore it::

    r      = width_mm / 2                       # in metres, through the scale
    opened = land.buffer(-r).buffer(+r)
    thin   = land.difference(opened)

This is exact rather than approximate, and the measurement means something: a
0.4 mm neck 2 mm long is reported as 0.8 mm² of thin land, which is the neck.
Tested at a 1 mm setting, a 0.95 mm neck is flagged and a 1.05 mm neck is not.

**Mitre joins, not round.**  The buffers use ``join_style="mitre"`` for a
reason that is easy to get wrong.  With the default round joins, opening also
rounds off every convex corner, so the difference reports a sliver at each one
-- a plain 4 mm square, which prints perfectly, comes back with four "thin"
patches and 0.216 mm² of them.  That is not a width problem; the land at a
corner is as wide as its two edges make it.  Mitre joins restore a corner
exactly, so the square reports nothing, and on a real coastline the noise drops
by a fifth to a half (Nova Scotia: 403 patches down to 338; Prince Edward
Island: 103 down to 49).  A corner sharper than the mitre limit is still
bevelled and still shows up, which is correct -- a needle tip is genuinely
unprintable.

Necks and whole islands are different problems
----------------------------------------------
``patches`` counts thin regions, but the two kinds want opposite fixes.  A neck
inside a bigger landmass wants **widening** -- ``LIB_island_bridges`` or a
wider merge buffer.  An island wholly below the width has nothing printable in
it at all and wants **removing** -- ``LIB_island_removal``.  So
:attr:`ThinReport.whole_parts` counts the parts that vanish completely under
the erosion, separately from the necks.

Saying so when it cannot tell
-----------------------------
Real GeoJSON is sometimes invalid -- a self-intersecting ring will defeat the
difference outright -- and a detector that answered "no thin land" there would
be worse than useless, because the whole point is to be believed.  So the
geometry is repaired with ``make_valid`` and retried (``repaired`` records
that), and if even that fails the report comes back with ``measured`` False
rather than clean.  "I could not tell" and "there is nothing wrong" are not the
same answer.

Cost
----
Two buffers and a difference over the whole coastline: 0.02 s for Nova Scotia,
0.07 s for British Columbia, 0.51 s for Nunavut's 223 islands.  Cheap enough to
run on a button press, not cheap enough to run inside a draw loop -- callers
that redraw should keep the report, keyed on the geometry and the width.
"""

import logging
from dataclasses import dataclass, field

import shapely

from lib.LIB_island_merging import _polygons
from lib.LIB_project_config import get as _cfg

log = logging.getLogger(__name__)

EPSG_METRIC = _cfg("crs.working")
DEFAULT_SCALE = _cfg("print.scale")

#: Segments per quarter circle in the buffers.  Only the curved stretches of
#: the offset use it -- the corners are mitred -- but a coarse disc would
#: mis-measure a width on a curving shore, and 16 costs nothing next to the
#: difference that follows.
QUAD_SEGS = 16

#: How far a mitred corner may reach out, as a multiple of the offset.  Shapely's
#: own default.  A right angle needs 1.41 and is restored exactly; anything
#: needing more than this is bevelled instead, which is why a needle tip still
#: reports as thin.
MITRE_LIMIT = 5.0

#: How many of the thin patches to hand back as geometry.  Nunavut has 7 250 of
#: them and a caller wants to be taken to the worst, not given the list.
WORST_KEPT = 20


@dataclass
class ThinParams:
    """What counts as too thin.  All lengths are **print-mm**."""
    scale: float = DEFAULT_SCALE                #: map scale denominator
    #: The narrowest land worth printing.  Defaults to the same figure
    #: ``LIB_island_bridges`` builds its bridges at, and for the same reason:
    #: a neck you already have and a bridge you would add need the same width
    #: to survive being handled.
    width_mm: float = _cfg("thin.width_mm")
    work_crs: str = EPSG_METRIC                 #: metric CRS the work is done in

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def width_m(self) -> float:
        return self.width_mm * self.mpm

    @property
    def radius_m(self) -> float:
        """Half the width: the radius of the disc that has to fit."""
        return self.width_m / 2.0


@dataclass
class ThinReport:
    """Where the land is too narrow, and how much of it there is."""
    thin: object = None             #: the thin land itself, in the work CRS
    width_mm: float = 0.0           #: the width it was measured against
    #: False when the geometry defeated the measurement even after repair.  A
    #: report that is not ``measured`` says nothing about the province; it is
    #: not a clean bill of health.
    measured: bool = True
    repaired: bool = False          #: ``make_valid`` was needed first
    patches: int = 0                #: separate thin regions
    thin_area_mm2: float = 0.0
    land_area_mm2: float = 0.0
    parts: int = 0                  #: polygon parts examined
    #: Parts with nothing printable in them at all -- an islet below the width
    #: end to end.  These want removing, where a neck wants widening.
    whole_parts: int = 0
    #: The largest thin patches as geometry, worst first, at most
    #: ``WORST_KEPT`` of them -- for a caller that wants to take someone to the
    #: problem.  A 1 mm patch is invisible at the zoom a whole province is read
    #: at, so a list of areas would not be enough to find it by.
    worst: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Nothing too thin -- and we were able to tell."""
        return self.measured and self.patches == 0

    @property
    def fraction(self) -> float:
        """Share of the land that is too thin (0..1)."""
        if self.land_area_mm2 <= 0:
            return 0.0
        return self.thin_area_mm2 / self.land_area_mm2


def _opened(geom, radius_m: float):
    """The land that a disc of ``2 * radius_m`` across can still reach.

    Mitre joins on both passes, so a corner comes back as the corner it was --
    see the module docstring for what round joins cost here.
    """
    kwargs = dict(quad_segs=QUAD_SEGS, join_style="mitre",
                  mitre_limit=MITRE_LIMIT)
    return geom.buffer(-radius_m, **kwargs).buffer(radius_m, **kwargs)


def thin_features(geom, params: ThinParams = None) -> ThinReport:
    """
    Find the land narrower than ``params.width_mm`` as it will print.

    Parameters
    ----------
    geom   : Shapely Polygon/MultiPolygon in a projected metre CRS (the project
             uses EPSG:3347).  One province, as it will be printed: the piece's
             own outline is what has to survive, so a province is measured on
             its own and not as part of a country.
    params : ThinParams, optional

    Returns
    -------
    A :class:`ThinReport`.  Nothing is modified; this only measures.
    """
    params = params or ThinParams()
    report = ThinReport(width_mm=params.width_mm)
    if geom is None or geom.is_empty:
        return report

    parts = _polygons(geom)
    report.parts = len(parts)
    report.land_area_mm2 = geom.area / params.mpm ** 2
    if params.radius_m <= 0:
        return report               # no width asked for, nothing to flag

    working, thin = geom, None
    for attempt in range(2):
        try:
            opened = _opened(working, params.radius_m)
            thin = working.difference(opened)
            break
        except Exception as exc:
            if attempt:             # repair already tried; we cannot tell
                log.warning("thin_features could not measure: %s", exc)
                report.measured = False
                return report
            # A self-intersecting ring defeats the difference outright. Repair
            # and retry rather than reporting a province clean because it could
            # not be read.
            log.info("thin_features repairing invalid geometry: %s", exc)
            try:
                working = shapely.make_valid(geom)
            except Exception as repair_exc:
                log.warning("thin_features could not repair: %s", repair_exc)
                report.measured = False
                return report
            report.repaired = True

    if thin is None or thin.is_empty:
        return report

    patches = _polygons(thin)
    report.thin = thin
    report.patches = len(patches)
    report.thin_area_mm2 = thin.area / params.mpm ** 2
    report.worst = sorted(patches, key=lambda part: part.area,
                          reverse=True)[:WORST_KEPT]

    # A part the erosion deleted entirely is not a neck to widen but an islet to
    # drop, so it is counted apart. Asked of the opened land, which is already
    # in hand, rather than by eroding every part again.
    prepared = shapely.prepared.prep(opened)
    report.whole_parts = sum(1 for part in parts
                             if not prepared.intersects(part))
    return report
