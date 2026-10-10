"""
border_simplification.py

Topology-aware simplification of province boundaries with two separate tolerances:
  - coastal_tol  : arcs belonging to only one province (coastlines, international borders)
  - internal_tol : arcs shared between two provinces (inter-provincial borders)

Uses TopoJSON to ensure shared borders are simplified identically → zero gaps.

Seam clean-up
-------------
Douglas-Peucker measures *perpendicular* deviation, so a tight zigzag inside the
tolerance corridor survives it with every vertex intact -- the sub-nozzle chatter
and needle spikes ``LIB_shared_border`` was written to remove.  Passing
``min_segment_m`` / ``min_angle_deg`` runs that lib's two printability passes over
every **internal** arc after its DP pass.  Doing it per arc rather than per pair
of provinces is what keeps it exact across a whole map: an arc is conditioned
once and both sides reference it, and its endpoints are the junctions where three
regions meet, which both passes pin.  Both default to 0 (off).
"""

import logging

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, LineString
import topojson as tp

# The same two passes LIB_shared_border runs over a pair's seam, applied here
# per arc so one definition of "printable seam" serves both.
from lib.LIB_shared_border import _despike, _drop_short_segments

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Arc-level helpers
# ---------------------------------------------------------------------------

def _find_shared_arcs(topo_dict: dict) -> set:
    """
    Return the set of arc indices referenced by 2+ distinct geometries.
    These are the inter-provincial (internal) borders.
    """
    arc_users: dict[int, set] = {}
    for geom_idx, geom in enumerate(topo_dict["objects"]["data"]["geometries"]):
        rings_list = geom["arcs"]
        if geom["type"] == "MultiPolygon":
            rings_list = [ring for poly in rings_list for ring in poly]
        for ring in rings_list:
            for ref in ring:
                idx = ref if ref >= 0 else -(ref + 1)
                arc_users.setdefault(idx, set()).add(geom_idx)
    return {idx for idx, users in arc_users.items() if len(users) >= 2}


def _simplify_arc(arc_coords: list, tolerance_m: float) -> list:
    """Apply Douglas-Peucker to a single arc, preserving endpoints."""
    if len(arc_coords) <= 2 or tolerance_m <= 0:
        return arc_coords
    simplified = LineString(arc_coords).simplify(tolerance_m, preserve_topology=False)
    pts = list(simplified.coords)
    return pts if len(pts) >= 2 else arc_coords[:2]


def _condition_arc(coords: list, min_segment_m: float,
                   min_angle_deg: float) -> list:
    """
    Collapse sub-``min_segment_m`` runs and drop needle vertices from one arc.

    Endpoints are pinned by both passes, so the arc still meets its neighbours
    at the same junctions.  A closed arc (a whole ring held by one arc, e.g. an
    island two regions share) needs four points to stay a ring; if the passes
    would leave fewer, the arc is returned as it came rather than have its
    polygon silently dropped on reconstruction.
    """
    out = _drop_short_segments(list(coords), min_segment_m)
    out = _despike(out, min_angle_deg)
    closed = len(coords) >= 2 and tuple(coords[0]) == tuple(coords[-1])
    if closed and len(out) < 4:
        return coords
    return out


#: The corner at a vertex, in degrees, below which the arc counts as doubling
#: back on itself (0 is straight back, 180 straight on).
#:
#: Not 90.  A border can genuinely turn by more than a right angle, and
#: dropping the vertex it turns at cuts the corner off: the NWT/Nunavut line
#: on Victoria Island turns 92 degrees at its south end, and with that vertex
#: gone the shortcut crossed the NWT's own coast, the repaired outline no
#: longer shared the seam's north-east leg, and that leg got no corridor.
REVERSAL_CORNER_DEG = 30.0


def _remove_reversals(coords: list) -> list:
    """
    Remove interior vertices where an arc doubles back on itself.

    A reversal is a vertex whose corner is sharper than
    ``REVERSAL_CORNER_DEG`` -- the line runs out and comes back nearly the way
    it went, which Douglas-Peucker with ``preserve_topology=False`` can leave
    behind.  A real corner, however sharp a turn, is kept.  Iterates until
    stable so that cascading reversals are fully eliminated.

    Endpoints are always preserved.
    """
    if len(coords) <= 2:
        return coords

    # cos of the turn (180 - corner): a dot product below this is a reversal.
    limit = -np.cos(np.radians(REVERSAL_CORNER_DEG))
    prev_len = -1
    while len(coords) != prev_len:
        prev_len = len(coords)
        pts = np.array(coords, dtype=np.float64)
        keep = [0]
        for i in range(1, len(pts) - 1):
            A = pts[keep[-1]]
            B = pts[i]
            C = pts[i + 1]
            v1 = B - A
            v2 = C - B
            norm = np.linalg.norm(v1) * np.linalg.norm(v2)
            if norm == 0 or np.dot(v1, v2) >= limit * norm:
                keep.append(i)
        keep.append(len(pts) - 1)
        coords = [coords[k] for k in keep]

    return coords


def _reconstruct_ring(arc_refs: list, all_arcs: list) -> list:
    """
    Reconstruct a closed ring from TopoJSON arc references.
    Positive index → arc forward.  Negative index → arc reversed (index = -(ref+1)).
    Junction points between consecutive arcs are deduplicated.
    """
    coords: list = []
    for ref in arc_refs:
        arc = all_arcs[ref] if ref >= 0 else list(reversed(all_arcs[-(ref + 1)]))
        if coords:
            coords.extend(arc[1:])
        else:
            coords.extend(arc)
    if len(coords) >= 2:
        if coords[0][0] != coords[-1][0] or coords[0][1] != coords[-1][1]:
            coords.append(coords[0])
    return coords


# ---------------------------------------------------------------------------
# Topology dict → GeoDataFrame
# ---------------------------------------------------------------------------

def _topology_dict_to_gdf(topo_dict: dict, crs) -> gpd.GeoDataFrame:
    """Reconstruct a GeoDataFrame from a (possibly modified) topology dict."""
    all_arcs = topo_dict["arcs"]
    records: list[dict] = []

    for geom_data in topo_dict["objects"]["data"]["geometries"]:
        props     = dict(geom_data.get("properties", {}))
        geom_type = geom_data.get("type", "")
        arc_refs  = geom_data.get("arcs", [])

        def _safe_polygon(rings: list) -> Polygon | None:
            if not rings:
                return None
            if len(rings[0]) < 4:
                return None
            holes = [r for r in rings[1:] if len(r) >= 4]
            try:
                return Polygon(rings[0], holes)
            except Exception:
                return None

        try:
            if geom_type == "Polygon":
                rings = [_reconstruct_ring(r, all_arcs) for r in arc_refs]
                geom  = _safe_polygon(rings)

            elif geom_type == "MultiPolygon":
                polys = []
                for poly_arcs in arc_refs:
                    rings = [_reconstruct_ring(r, all_arcs) for r in poly_arcs]
                    p = _safe_polygon(rings)
                    if p is not None:
                        polys.append(p)
                geom = MultiPolygon(polys) if polys else None

            else:
                geom = None

        except Exception as exc:
            log.warning("Ring reconstruction error: %s", exc)
            geom = None

        props["geometry"] = geom
        records.append(props)

    return gpd.GeoDataFrame(pd.DataFrame(records), geometry="geometry", crs=crs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def topo_simplify_two_tolerances(
    gdf: gpd.GeoDataFrame,
    coast_tol_m: float,
    internal_tol_m: float,
    *,
    min_segment_m: float = 0.0,
    min_angle_deg: float = 0.0,
) -> tuple[gpd.GeoDataFrame, list, list]:
    """
    Build a TopoJSON topology and simplify with two separate tolerances.

    Parameters
    ----------
    gdf            : GeoDataFrame in a projected metre CRS (e.g. EPSG:3347)
    coast_tol_m    : simplification tolerance in metres for coastal/international arcs
    internal_tol_m : simplification tolerance in metres for inter-provincial shared arcs
    min_segment_m  : seam clean-up -- collapse internal-arc runs shorter than this
                     (metres; 0 skips the pass)
    min_angle_deg  : seam clean-up -- drop internal-arc corners sharper than this
                     (degrees; 0 skips the pass)

    Returns
    -------
    gdf_result     : simplified GeoDataFrame (same CRS as input)
    coastal_segs   : list of (N, 2) arrays for visualising coastal arcs
    internal_segs  : list of (N, 2) arrays for visualising internal arcs
    """
    log.info(
        "Building topology (coast=%.0f m  internal=%.0f m) ...",
        coast_tol_m, internal_tol_m,
    )
    topo     = tp.Topology(gdf, prequantize=False)
    raw_dict = topo.to_dict()

    shared = _find_shared_arcs(raw_dict)
    log.info(
        "  %d arcs total: %d internal, %d coastal",
        len(raw_dict["arcs"]), len(shared), len(raw_dict["arcs"]) - len(shared),
    )

    new_arcs      = []
    coastal_segs  = []
    internal_segs = []

    for i, arc in enumerate(raw_dict["arcs"]):
        is_internal = i in shared
        tol         = internal_tol_m if is_internal else coast_tol_m
        simplified  = _simplify_arc(arc, tol)
        if is_internal:
            simplified = _remove_reversals(simplified)
            if min_segment_m > 0 or min_angle_deg > 0:
                simplified = _condition_arc(simplified, min_segment_m,
                                            min_angle_deg)
        new_arcs.append(simplified)
        seg = np.array(simplified)
        if is_internal:
            internal_segs.append(seg)
        else:
            coastal_segs.append(seg)

    modified         = dict(raw_dict)
    modified["arcs"] = new_arcs
    gdf_result       = _topology_dict_to_gdf(modified, gdf.crs)

    log.info("Simplification complete.")
    return gdf_result, coastal_segs, internal_segs
