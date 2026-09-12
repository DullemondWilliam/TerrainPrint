"""
lake_removal.py

Removes interior rings (holes) from province polygons.

In geographic data, holes typically represent lakes, inland seas, or other
water bodies enclosed within a province.  Removing them produces solid
polygon slabs with no internal voids — desirable for 3D printing where
lakes are not needed.
"""

import logging
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

log = logging.getLogger(__name__)


def remove_lakes(geom):
    """
    Strip all interior rings (holes) from a polygon or multipolygon.

    Parameters
    ----------
    geom : Shapely Polygon, MultiPolygon, or None

    Returns
    -------
    A valid geometry with the same exterior shape but no holes, or the original
    geom unchanged if it has no holes or is None/empty.

    Islands inside lakes
    --------------------
    Real provinces (e.g. Northwest Territories, with islands inside Great Bear
    and Great Slave Lake) carry those in-lake islands as separate polygons in
    the multipolygon.  Naively filling a lake by keeping only its exterior ring
    then leaves the filled slab overlapping those island polygons — a "nested
    shells" error that makes the whole multipolygon invalid and unusable
    downstream.  So once any lake is filled the parts are dissolved together,
    which absorbs the now-redundant in-lake islands and guarantees a valid,
    void-free result.
    """
    if geom is None or geom.is_empty:
        return geom

    if geom.geom_type == "Polygon":
        if not list(geom.interiors):
            return geom   # no holes — return as-is
        return Polygon(geom.exterior.coords)   # one shell can't nest with itself

    if geom.geom_type == "MultiPolygon":
        if not any(part.interiors for part in geom.geoms):
            return geom   # no lakes anywhere — return as-is

        filled = [Polygon(part.exterior.coords) if part.interiors else part
                  for part in geom.geoms]
        # Dissolve to clear nested shells left where a filled lake now covers an
        # island that used to sit inside it; also repairs any resulting overlaps.
        merged = unary_union(filled)
        if merged.geom_type == "Polygon":
            return MultiPolygon([merged])   # keep the type stable for callers
        return merged

    return geom   # other geometry types passed through unchanged
