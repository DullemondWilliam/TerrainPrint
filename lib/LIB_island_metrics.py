"""
LIB_island_metrics.py

Measures a set of land masses: how many there are, and how far apart they lie.

:func:`landmass_count` counts the separate bodies of land in a geometry — one
per polygon part, so a lake never splits a landmass in two and a merge that
bridges two islands drops the count by one.

:func:`gap_km` and :func:`gap_range_km` report the **edge-to-edge** distance
between islands: the water a merge would actually have to bridge, not any
centre-to-centre figure.  Since ``merge_islands`` bridges islands lying closer
than ``2 * buffer_km``, a gap read here halves to the buffer that would close
it.

Distances and counts are taken from the geometry itself, so coordinates are
expected in a projected metre CRS (the project uses EPSG:3347); the kilometre
readings are then real ground distances.

Note that :func:`gap_range_km` compares every pair, so its cost grows with the
square of the island count — 223 islands (the largest province here) is roughly
25 000 pairs and a couple of seconds.  Callers on an interactive path should cap
the number of islands they hand it.
"""

import logging

import math

from shapely.ops import unary_union

from lib.LIB_island_merging import _polygons, merge_islands

log = logging.getLogger(__name__)

#: Metres per kilometre.  Distances come out in the CRS's linear unit — metres
#: in EPSG:3347 — so divide by this to read km.
_METRES_PER_KM = 1000.0

#: Buffers :func:`bridging_buffer_km` tries, as multiples of the gap, smallest
#: first.  Half the gap is the floor — below it the closing cannot even reach
#: across — but it only suffices for islands large relative to their gap, where
#: the facing coasts are effectively parallel walls.  Real coastlines curve
#: away from each other, so the contact the closing makes is a thin lens that
#: the erosion step then removes; measured pairs here needed 0.59x to 1.74x.
#: There is no closed form, so the ladder is tried against the real geometry.
_BRIDGE_FACTORS = (0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)


def landmass_count(geom) -> int:
    """
    Number of separate land masses in a geometry.

    One per polygon part: interior rings (lakes) do not split a landmass, and a
    Polygon counts as one however many lakes it holds.  None/empty geometries
    count 0.
    """
    return len(_polygons(geom))


def gap_km(a, b) -> float:
    """
    Edge-to-edge distance between two islands, in kilometres.

    Measured coastline to coastline, so islands that touch or overlap measure
    0.0.  A None/empty island has no finite gap to anything and measures
    ``inf``; :func:`gap_range_km` drops such entries before pairing.
    """
    if a is None or b is None or a.is_empty or b.is_empty:
        return float("inf")
    return a.distance(b) / _METRES_PER_KM


def gap_range_km(islands):
    """
    Closest and widest edge-to-edge gaps across a group of islands.

    Parameters
    ----------
    islands : iterable of Shapely geometries in a projected metre CRS (the
              project uses EPSG:3347).  None/empty entries are ignored.

    Returns
    -------
    ``(closest, widest)`` in kilometres over every unordered pair, or None if
    fewer than two islands are present (a single island has nothing to measure
    against).  Both figures are 0.0 when every island touches its neighbours.
    """
    present = [g for g in islands if g is not None and not g.is_empty]
    if len(present) < 2:
        return None
    gaps = [gap_km(a, b)
            for i, a in enumerate(present)
            for b in present[i + 1:]]
    log.debug("gap_range_km: %d island(s), %d pair(s)", len(present), len(gaps))
    return min(gaps), max(gaps)


def closest_pair(islands):
    """
    The two islands lying closest together.

    Returns ``(a, b, gap_km)`` for the nearest pair, or None if fewer than two
    islands are present.  Ties go to the first pair found.
    """
    present = [g for g in islands if g is not None and not g.is_empty]
    if len(present) < 2:
        return None
    best = None
    for i, a in enumerate(present):
        for b in present[i + 1:]:
            d = gap_km(a, b)
            if best is None or d < best[2]:
                best = (a, b, d)
    return best


def _closing_joins(a, b, buffer_km: float) -> bool:
    """
    Whether closing this pair by ``buffer_km`` leaves them one landmass.

    The cheap half of the test in :func:`bridging_buffer_km`: it runs the same
    two buffer passes ``merge_islands`` does but skips the neck extraction, so
    it rejects hopeless candidates for a fraction of the cost.
    """
    if buffer_km <= 0:
        return False
    d = buffer_km * _METRES_PER_KM
    joined = unary_union([a, b]).buffer(d).buffer(-d)
    return len(_polygons(joined)) == 1


def _merges(a, b, buffer_km: float) -> bool:
    """Whether ``merge_islands`` really returns these two as one landmass."""
    return len(_polygons(merge_islands([a, b], buffer_km))) == 1


def bridging_buffer_km(a, b, max_km: float = None, step: float = None):
    """
    Smallest tried buffer that verifiably merges two islands into one landmass.

    Every candidate is confirmed by running ``merge_islands`` on the real
    coastlines, because neither shortcut survives contact with the data:

    * **No fixed multiple of the gap works.**  ``2 * buffer > gap`` is only
      necessary — it makes the dilated coasts overlap, but on curved shores that
      overlap is a lens the erosion step wipes out.  Half the gap suffices only
      for islands large relative to it; the provinces here need 0.59x to 1.74x,
      and small islands far apart may need 24x or never merge at all.
    * **The merge is not monotonic in the buffer near its threshold.**  One real
      pair merges at 1.725, 1.727 and 1.729 km but *not* at 1.728 or 1.730 —
      the neck there is a hairline and the ``intersects >= 2`` filter that keeps
      it is knife-edge.  So a candidate is only trustworthy if the exact value
      has been tried, which is what ``step`` is for.

    Parameters
    ----------
    a, b   : Shapely geometries in a projected metre CRS (EPSG:3347 here).
    max_km : optional ceiling; candidates above it are not tried.
    step   : optional quantum to round candidates **up** to, so the value
             returned is one the caller can actually store and use unchanged.
             Without it a caller that rounds afterwards may land back on an
             untested — and per the above, possibly failing — value.

    Returns
    -------
    The buffer in kilometres, or None if no candidate merges them.  Already
    touching islands need no buffer and return 0.0.
    """
    gap = gap_km(a, b)
    if gap == 0.0:
        return 0.0
    if gap == float("inf"):
        return None
    tried = set()
    for factor in _BRIDGE_FACTORS:
        candidate = gap * factor
        if step:
            candidate = math.ceil(candidate / step - 1e-9) * step
        if max_km is not None and candidate > max_km:
            break
        if candidate in tried:          # rounding can collapse two rungs into one
            continue
        tried.add(candidate)
        # Cheap reject first, then confirm with the operation that will actually
        # run — the closing joining is necessary but, at the threshold, not
        # sufficient.
        if _closing_joins(a, b, candidate) and _merges(a, b, candidate):
            log.debug("bridging_buffer_km: %g km gap merges at %g km (%.2fx)",
                      gap, candidate, factor)
            return candidate
    return None
