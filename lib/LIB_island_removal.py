"""
LIB_island_removal.py

Drops islands smaller than a given area from a set of land masses.

:func:`remove_small_islands` keeps every island whose area is at least a
threshold given in **square kilometres** and discards the rest — the tool for
clearing away specks and slivers that would only clutter a coarse model.

Areas are measured from the geometry itself, so coordinates are expected in a
projected metre CRS (the project uses EPSG:3347); the km² threshold is then a
real ground area.  :func:`area_km2` and :func:`is_below_area` expose the same
measurement/threshold logic the filter uses, so callers (and the accompanying
UI) can classify islands consistently.
"""

import logging

log = logging.getLogger(__name__)

#: Square metres per square kilometre.  Geometry area comes out in the CRS's
#: linear unit squared — metres² in EPSG:3347 — so divide by this to read km².
_M2_PER_KM2 = 1_000_000.0


def area_km2(geom) -> float:
    """
    Area of a geometry in square kilometres, assuming a projected metre CRS.

    Interior rings (lakes) are excluded, exactly as Shapely's ``.area`` already
    subtracts them, so an island's area reflects its solid land.  None/empty
    geometries measure 0.
    """
    if geom is None or geom.is_empty:
        return 0.0
    return geom.area / _M2_PER_KM2


def is_below_area(geom, min_area_km2: float) -> bool:
    """True if ``geom`` is strictly smaller than ``min_area_km2`` square km."""
    return area_km2(geom) < min_area_km2


def remove_small_islands(islands, min_area_km2: float) -> list:
    """
    Keep only the islands at least ``min_area_km2`` square kilometres in area.

    Parameters
    ----------
    islands       : iterable of Shapely geometries in a projected metre CRS
                    (the project uses EPSG:3347).  Each element is treated as
                    one island; None/empty entries are ignored.  A geometry's
                    area already excludes its lakes, so the threshold is applied
                    to solid land.
    min_area_km2  : minimum area to survive, in square kilometres.  0 or
                    negative keeps everything (nothing is small enough to drop).

    Returns
    -------
    A new list of the surviving geometries, in their original order.  Note that
    a MultiPolygon element is judged by its *combined* area — explode inputs to
    one Polygon per element first if each part should be weighed on its own.
    """
    kept = [g for g in islands
            if g is not None and not g.is_empty
            and not is_below_area(g, min_area_km2)]
    log.debug("remove_small_islands: kept %d island(s) at >= %g km^2",
              len(kept), min_area_km2)
    return kept
