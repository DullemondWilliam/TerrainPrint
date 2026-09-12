"""
border_simplification.py

Topology-aware simplification of province boundaries with two separate tolerances:
  - coastal_tol  : arcs belonging to only one province (coastlines, international borders)
  - internal_tol : arcs shared between two provinces (inter-provincial borders)

Uses TopoJSON to ensure shared borders are simplified identically → zero gaps.
"""

import logging

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, LineString
import topojson as tp

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


def _remove_reversals(coords: list) -> list:
    """
    Remove interior vertices that create local reversals in an arc.

    A reversal occurs when dot(incoming_vec, outgoing_vec) < 0 — the line
    briefly travels backwards relative to its local direction.  Iterates until
    stable so that cascading reversals are fully eliminated.

    Endpoints are always preserved.
    """
    if len(coords) <= 2:
        return coords

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
            if np.dot(v1, v2) >= 0:
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
) -> tuple[gpd.GeoDataFrame, list, list]:
    """
    Build a TopoJSON topology and simplify with two separate tolerances.

    Parameters
    ----------
    gdf            : GeoDataFrame in a projected metre CRS (e.g. EPSG:3347)
    coast_tol_m    : simplification tolerance in metres for coastal/international arcs
    internal_tol_m : simplification tolerance in metres for inter-provincial shared arcs

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
