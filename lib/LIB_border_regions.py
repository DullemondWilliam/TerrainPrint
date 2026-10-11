"""
LIB_border_regions.py

Carve a configurable-width border region out of a set of region polygons.

The regions are passed in as two lists of geometries (no file loading happens
here):

  * **core** geometries – regions that share borders *with each other*.  Every
    seam between two cores is carved symmetrically, roughly 50/50, so each core
    gives up half the corridor width.
  * **surrounding** geometries – context regions around the cores.  The seam
    between a core and a surrounding is carved **entirely on the surrounding
    (external) side**, so the core keeps its full extent up to the frontier.
    Two surrounding regions never get a seam between them.

Given those two lists and a border width, this builds:
  1. Trimmed regions – each core / surrounding with the relevant strip removed
     (outer edges that abut nothing are untouched).
  2. The border region – the carved strips dissolved into a single merged
     geometry.

Geometry expectations
---------------------
Geometries must already be in a projected CRS whose units are **metres** (the
border width, given in kilometres, is converted to metres internally).  Shared
core–core borders are recovered directly from the geometry by intersecting the
core boundaries, so for clean, sliver-free strips the cores should share exact,
snapped edges.  Any simplification is the caller's responsibility and should be
done before calling in.
"""

import logging
from dataclasses import dataclass

from shapely.geometry import MultiPolygon
from shapely.ops import unary_union
from shapely.validation import make_valid

log = logging.getLogger(__name__)

_M_PER_KM = 1000.0


@dataclass
class BorderRegions:
    """Result of :func:`build_border_regions`.

    ``core`` / ``surrounding`` preserve the order of the input lists; entries
    are the trimmed geometries (``None`` where an input was empty/None).
    ``border`` is the merged border region, or ``None`` if nothing was carved.
    """
    core: list
    surrounding: list
    border: object


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _polygonal_parts(geom):
    """Return the Polygon parts of a geometry (drops points/lines/empties)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if g.geom_type == "Polygon"]
    return []


def _linear_parts(geom):
    """Return the linear parts of a geometry (drops points/polygons/empties)."""
    if geom is None or geom.is_empty:
        return []
    t = geom.geom_type
    if t in ("LineString", "LinearRing"):
        return [geom]
    if t == "MultiLineString":
        return list(geom.geoms)
    if t == "GeometryCollection":
        parts = []
        for g in geom.geoms:
            parts.extend(_linear_parts(g))
        return parts
    return []


def _as_polygonal(geom):
    """Collapse a geometry to a (Multi)Polygon of just its polygonal parts."""
    parts = _polygonal_parts(geom)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _ensure_valid(geom):
    """Return a valid polygonal geometry, repairing via make_valid if needed."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    return _as_polygonal(geom)


def _union(geoms):
    """Valid polygonal union of a list of (possibly None) geometries, or None."""
    parts = [g for g in geoms if g is not None and not g.is_empty]
    if not parts:
        return None
    return _ensure_valid(unary_union(parts))


# ---------------------------------------------------------------------------
# Shared core borders
# ---------------------------------------------------------------------------
def shared_borderlines(core_geoms):
    """
    Recover the shared borders *between* the core geometries.

    Intersects every pair of core boundaries and dissolves the resulting line
    segments into a single geometry.  Only linear parts are kept, so triple
    points (which show up as bare points) don't leave specks behind.

    Returns a (Multi)LineString of the shared arcs, or ``None`` if the cores
    share no edges.
    """
    geoms = [g for g in core_geoms if g is not None and not g.is_empty]
    lines = []
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            inter = geoms[i].boundary.intersection(geoms[j].boundary)
            lines.extend(_linear_parts(inter))
    if not lines:
        return None
    return unary_union(lines)


# ---------------------------------------------------------------------------
# Border strips
# ---------------------------------------------------------------------------
def build_border_strip(border_lines, width_m: float, clip_geom=None):
    """
    Buffer the shared core centrelines into a strip of total width ``width_m``
    (half each side) and optionally clip it to ``clip_geom`` (the land outline).

    Returns the strip geometry (Polygon/MultiPolygon) or None.
    """
    if border_lines is None or border_lines.is_empty:
        return None
    if border_lines.geom_type in ("LineString", "LinearRing", "MultiLineString"):
        lines = border_lines
    else:
        parts = _linear_parts(border_lines)
        if not parts:
            return None
        lines = unary_union(parts)
    strip = lines.buffer(width_m / 2.0, join_style=1, cap_style=1)  # round
    if clip_geom is not None:
        strip = strip.intersection(clip_geom)
    return _ensure_valid(strip)


def build_surrounding_cut(core_union, surrounding_union, width_m: float):
    """
    Region to remove from the surrounding geometries so the core–surrounding
    border corridor lies **entirely on the surrounding (external) side**.

    Grows the whole core union outward by ``width_m`` and intersects it with the
    surrounding regions.  Subtracting this from the surrounding regions pulls
    them back ``width_m`` from the core edge and also removes any surrounding
    land that overlaps a core (datasets often overlap by a few km along a
    shared frontier).  Cores are never touched, so they keep their full extent.

    Only the stretches of core edge that actually abut a surrounding region
    survive the intersection (outer edges grow into empty space and drop out),
    so this isolates the shared frontier automatically.
    """
    if core_union is None or surrounding_union is None:
        return None
    if core_union.is_empty or surrounding_union.is_empty:
        return None
    grown = core_union.buffer(width_m, join_style=1, cap_style=1)  # round
    return _ensure_valid(grown.intersection(surrounding_union))


def bridge_gaps(core_union, surrounding_union, gap_m: float):
    """
    The no-man's-land between the cores and the surrounding regions that is
    narrower than ``gap_m`` -- to be treated as surrounding land, so the
    corridor reaches the core across it.

    Two datasets that never shared an edge (Quebec and the US states) meet
    with slivers between them -- a kilometre here, a river there -- and a
    corridor carved only out of surrounding land starts at the surrounding
    edge, leaving the gap between it and the core: nothing for a connector to
    key into.

    The gap is found by *closing* the two together -- grown by half
    ``gap_m``, shrunk back by as much -- which fills what lies between them
    when it is narrower than ``gap_m`` and adds nothing round an outside
    corner, so the bar does not flare where a frontier meets the sea.  Only
    pieces touching both sides count: a narrow bay of the core alone is
    not a gap between two regions.  Done only where the two come within
    ``gap_m`` of each other, which keeps it quick on a whole province.

    Returns the bridge (Polygon/MultiPolygon) or None.
    """
    if gap_m <= 0 or core_union is None or surrounding_union is None:
        return None
    near = core_union.buffer(gap_m).intersection(
        surrounding_union.buffer(gap_m))
    if near.is_empty:
        return None
    zone = near.buffer(gap_m)
    core = core_union.intersection(zone)
    other = surrounding_union.intersection(zone)
    half = gap_m / 2.0
    closed = unary_union([core, other]).buffer(half, join_style=1)         .buffer(-half, join_style=1)
    gap = _ensure_valid(closed.intersection(near).difference(core)
                        .difference(other))
    if gap is None:
        return None
    touch = 1e-6 * max(gap_m, 1.0) + 1e-3
    keep = [part for part in _polygonal_parts(gap)
            if part.distance(core) <= touch and part.distance(other) <= touch]
    return _union(keep)


def carve(geoms, strip_geom):
    """
    Subtract ``strip_geom`` from every geometry in ``geoms``, preserving order.

    Geometries the strip does not touch come through unchanged.  Returns a new
    list; ``None``/empty inputs are passed through as-is.
    """
    if strip_geom is None or strip_geom.is_empty:
        return list(geoms)

    out = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            out.append(geom)
            continue
        trimmed = _ensure_valid(geom.difference(strip_geom))
        out.append(trimmed if trimmed is not None else geom)
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def build_border_regions(core_geoms, surrounding_geoms=None,
                         border_width_km: float = 1.0,
                         bridge_gap_km: float = 0.0) -> BorderRegions:
    """
    Carve border regions between the core geometries and along the core edge of
    the surrounding geometries.

    Parameters
    ----------
    core_geoms : list of shapely geometries
        Regions that share borders with each other; each shared seam is carved
        symmetrically (~50/50, half the width from each side).
    surrounding_geoms : list of shapely geometries, optional
        Context regions around the cores.  Each core–surrounding seam is carved
        entirely on the surrounding (external) side; two surrounding regions
        never get a seam between them.
    border_width_km : float
        Total border corridor width in kilometres.
    bridge_gap_km : float
        Gaps between a core and a surrounding region up to this wide are
        bridged (:func:`bridge_gaps`): the corridor runs across them to the
        core's edge, rather than stopping at the surrounding's.  0 bridges
        nothing.

    Geometries must be in a projected, metre-based CRS.

    Returns
    -------
    BorderRegions
        ``core`` / ``surrounding`` are the trimmed geometries (input order) and
        ``border`` is the merged border region (or None).
    """
    core_geoms = list(core_geoms)
    surrounding_geoms = list(surrounding_geoms or [])
    width_m = border_width_km * _M_PER_KM

    log.info(
        "Carving borders (cores=%d  surrounding=%d  width=%.3f km) ...",
        len(core_geoms), len(surrounding_geoms), border_width_km,
    )

    core_union = _union(core_geoms)
    surrounding_union = _union(surrounding_geoms)

    # Core ↔ core: symmetric strip along the shared arcs.  Clip it to the
    # combined core + surrounding land so a seam that runs down to a surrounding
    # frontier meets the surrounding-side corridor at the triple points (rather
    # than stopping short in any dataset-overlap band).
    land = _union([core_union, surrounding_union])
    border_lines = shared_borderlines(core_geoms)
    core_strip = build_border_strip(border_lines, width_m, land)

    # Core ↔ surrounding: region taken out of the surrounding regions (corridor
    # + any dataset overlap) -- and out of the gaps between the two datasets,
    # bridged as if they were surrounding land, so the corridor starts at the
    # core's edge wherever the surrounding comes near it.
    bridge = bridge_gaps(core_union, surrounding_union,
                         bridge_gap_km * _M_PER_KM)
    surrounding_cut = build_surrounding_cut(
        core_union, _union([surrounding_union, bridge]), width_m)

    # Cores are carved by the core–core strip only, so they keep their full
    # extent along any core–surrounding frontier.
    trimmed_core = carve(core_geoms, core_strip)
    # Surrounding regions are carved by the surrounding-side region only;
    # nothing is carved between two surrounding regions.
    trimmed_surrounding = carve(surrounding_geoms, surrounding_cut)

    # Visible border region: core–core strip + the surrounding-side corridor
    # (excluding any part that lies inside a core, which stays core).
    surrounding_corridor = (
        _ensure_valid(surrounding_cut.difference(core_union))
        if surrounding_cut is not None and core_union is not None
        else surrounding_cut
    )
    border = _union([core_strip, surrounding_corridor])

    log.info("Carve complete.")
    return BorderRegions(core=trimmed_core,
                         surrounding=trimmed_surrounding,
                         border=border)
