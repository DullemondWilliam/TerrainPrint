"""
island_merging.py

Merges a list of islands with connecting necks of land.

:func:`merge_islands` closes the whole group by a buffer distance to discover
where bridges would form, keeps only the added land spanning two or more
islands (the necks), and unions them onto the originals — so every coastline
survives exactly.  A neck forms between islands lying closer than
``2 * buffer_km``.  Coordinates are expected in a projected metre CRS (the
project uses EPSG:3347).
"""

import logging

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

log = logging.getLogger(__name__)

#: Metres per kilometre — :func:`merge_islands` takes km but buffers in the
#: geometry's metre units (the project works in EPSG:3347 metres).
_METRES_PER_KM = 1000.0


def _polygons(geom) -> list:
    """The Polygon parts of a geometry (Polygon / MultiPolygon / collection)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


def _fill_new_holes(geom, keep_region):
    """
    Drop interior rings the merge introduced, keep the ones that were lakes.

    Bridging two islands can enclose the strait between them, turning open sea
    into a hole that reads as a border inside the merged landmass.  Any ring
    whose inside is not part of ``keep_region`` (the union of the originals'
    real lakes) is such a pocket and is filled; genuine lakes are preserved.
    """
    rebuilt = []
    for part in _polygons(geom):
        holes = [ring for ring in part.interiors
                 if keep_region is not None
                 and Polygon(ring).representative_point().within(keep_region)]
        rebuilt.append(Polygon(part.exterior, holes))
    if not rebuilt:
        return geom
    return rebuilt[0] if len(rebuilt) == 1 else MultiPolygon(rebuilt)


def merge_islands(islands, buffer_km: float):
    """
    Merge a list of islands by adding only the necks of land that connect them.

    Morphologically closes the *whole group* by ``buffer_km`` to discover where
    bridges would form, keeps just the added land that spans **two or more**
    distinct islands (the connecting necks), and unions those necks onto the
    *original* islands::

        d = buffer_km * 1000                        # km -> metres
        closed = unary_union(islands).buffer(d).buffer(-d)
        necks  = [p for p in (closed - union(islands)) if p touches >= 2 islands]
        merged = unary_union([*islands, *necks])

    Because the originals are unioned back untouched, every coastline survives
    **exactly** — and, unlike a plain closing, each island's own bays are left
    open (added land touching only one island is its own filled bay, and is
    discarded).  A neck forms between any two islands lying closer than
    ``2 * buffer_km``, so there is no intermediate "floating sliver" regime.

    This is a best-effort merge: islands too far from the rest simply stay
    separate, so the result may still be a MultiPolygon of several groups.

    Interior rings
    --------------
    Bridging can trap the strait between islands into an enclosed hole; such
    merge-created pockets are filled so the result reads as solid landmasses.
    The islands' own lakes (their original interior rings) are preserved.

    Parameters
    ----------
    islands   : iterable of Shapely Polygon/MultiPolygon in a projected metre
                CRS (the project uses EPSG:3347).  None/empty entries are
                ignored.
    buffer_km : buffer distance in kilometres.  0 or negative → a plain union
                with no bridging.

    Returns
    -------
    The merged geometry, or None if the list holds nothing to merge.  A single
    island is returned unchanged.  On failure, falls back to the plain union so
    no island is ever lost.
    """
    present = [g for g in islands if g is not None and not g.is_empty]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    if buffer_km <= 0:
        return unary_union(present)   # no room to bridge — union as-is

    buffer_m = buffer_km * _METRES_PER_KM
    # Overlap each neck into the islands by a hair so the union has real area to
    # dissolve.  A neck taken straight from ``difference`` only *touches* the
    # coastlines (zero-area), and unioning zero-area contacts leaves a hairline
    # slit — the ring runs up the seam and back — that draws as a border reaching
    # into the interior.  A sub-metre weld removes it without visibly growing the
    # shape.
    weld_m = max(buffer_m * 1e-3, 1e-6)
    try:
        originals = unary_union(present)
        # Close the group to find the bridging land, then keep only pieces that
        # span two or more islands — that discards each island's own filled bays,
        # so only the connecting necks are added and the coastlines stay intact.
        closed = originals.buffer(buffer_m).buffer(-buffer_m)
        necks = [p.buffer(weld_m)
                 for p in _polygons(closed.difference(originals))
                 if sum(p.intersects(isl) for isl in present) >= 2]
        merged = unary_union([*present, *necks])

        # Bridging can enclose straits between islands into holes; fill those,
        # but keep the islands' real lakes (their original interior rings).
        lakes = [Polygon(r) for g in present
                 for part in _polygons(g) for r in part.interiors]
        keep_region = unary_union(lakes) if lakes else None
        merged = _fill_new_holes(merged, keep_region)
    except Exception as exc:
        log.warning("merge_islands failed: %s", exc)
        return unary_union(present)

    if merged is None or merged.is_empty:
        return unary_union(present)   # never destroy the islands on edge cases

    return merged
