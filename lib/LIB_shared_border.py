#!/usr/bin/env python3
"""
LIB_shared_border.py

Simplify the border two provinces share, for printing, without opening a gap.

Two neighbouring pieces meet along one seam.  Whatever is done to that seam must
be done to **both** sides identically, or the printed pieces leave a crack or
overlap where they meet.  This module conditions the seam once and rebuilds both
provinces against it, so the pair stays an exact partition by construction.

    from lib.LIB_shared_border import simplify_shared_border, SeamParams

    res = simplify_shared_border(
        "Data/JSon-Provinces-Orig/CAN_Ontario.geojson",
        "Data/JSon-Provinces-Orig/CAN_Québec.geojson",
        params=SeamParams(tolerance_mm=0.2, min_segment_mm=0.3))
    print(res.vertices_before, "->", res.vertices_after,
          res.short_segments_after, "sub-nozzle segments left")

What "printable" means here, in numbers
---------------------------------------
At 1:8,000,000 the Ontario/Québec seam is 132 print-mm long and carries 235
vertices.  Measured on that seam, **54.5% of its segments are shorter than
0.4 mm** and 21% are shorter than 0.2 mm -- detail finer than the 0.4 mm nozzle
can lay down.  It is not merely wasted: sub-nozzle zigzag becomes ragged extrusion, and
the needle-thin wedges it creates snap off a thin map tile.

How much of that is the *short-segment* problem varies a lot by seam, which is
why the passes are separate knobs.  Measured at the shipped defaults:

=========================  =============  ==============  ===============
Seam                       Vertices       Sub-nozzle      Drift
=========================  =============  ==============  ===============
British Columbia/Alberta   768 -> 102     732 -> 0        0.19 mm
Ontario/Quebec             235 -> 40      127 -> 0        0.19 mm
Manitoba/Ontario            51 -> 4         1 -> 0        0.12 mm
Alberta/Saskatchewan        35 -> 2         2 -> 0        0.01 mm
New Brunswick/Nova Scotia   19 -> 3        18 -> 0        0.17 mm
=========================  =============  ==============  ===============

The sub-nozzle column is measured against the shipped 0.4 mm nozzle, so it
counts more than it did at 0.3 mm -- 732 of the Rockies seam's 768 segments
rather than 679.  The vertex and drift columns are unchanged: the conditioning
removes the same detail, and the nozzle only decides how much of it was
unprintable in the first place.

The Rockies seam is almost entirely sub-nozzle chatter; the Manitoba/Ontario one
has just one short segment in fifty and is thinned by the tolerance pass alone.
Alberta/Saskatchewan is a surveyed meridian, and correctly collapses to the two
points a straight line needs.

So the conditioning is four passes over the seam, each aimed at one defect:

1. **simplify** (Douglas-Peucker at ``tolerance_mm``) -- drops vertices that say
   nothing at print scale.
2. **drop short segments** (``min_segment_mm``, defaulting to ``nozzle_mm``) --
   collapses runs the nozzle cannot resolve, which DP alone leaves behind when
   they zigzag.  DP measures *perpendicular* deviation, so a tight zigzag inside
   the tolerance corridor survives it with every vertex intact.
3. **despike** (``min_angle_deg``) -- removes needle vertices, the ones that
   print as a fragile spur and break off in handling.
4. **smooth** (``smooth_iterations`` of Chaikin) -- optional; rounds what is left
   so no corner is sharper than the extrusion can follow.

How the pair is kept exact
--------------------------
Rebuilding by simplifying each province separately is what *causes* cracks: two
Douglas-Peucker runs over the same line, reached from opposite winding
directions, do not agree.  ``LIB_border_simplification`` solves that for a whole
country by going through TopoJSON.  This module solves it for a pair, and more
directly:

* the seam is conditioned **once**, as a line;
* its **endpoints are pinned**, so it still meets the coastline exactly where it
  did -- an unpinned endpoint opens a gap at the triple junction;
* both provinces are then re-cut out of their own union
  (``polygonize`` over ``union.boundary + seam``), so the results *partition*
  that union rather than being edited toward each other.

Gap and overlap area are measured on the way out and the result is rejected if
either is non-zero, because that is the failure this module exists to prevent.

Reporting is separated from filtering on purpose.  The counts in
:class:`SeamResult` are measured against the **printer limits**
(``nozzle_mm``, ``spike_angle_deg``); the passes are driven by the **filter
strengths** (``min_segment_mm``, ``min_angle_deg``), which default to those
limits.  Turning a filter down therefore reports a seam that is still
unprintable, rather than one that merely stopped being measured.
"""

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, MultiLineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union

from lib.LIB_project_config import build_params, settings_files
from lib.LIB_project_config import shipped as _shipped

log = logging.getLogger(__name__)

EPSG_METRIC = _shipped("crs.working")
DEFAULT_SCALE = _shipped("print.scale")


@dataclass
class SeamParams:
    """How the shared seam is conditioned.  All lengths are **print-mm**."""
    scale: float = DEFAULT_SCALE                #: map scale denominator
    tolerance_mm: float = _shipped("seam.tolerance_mm")       #: Douglas-Peucker tolerance
    #: Printer limits.  These describe the machine and drive the *report*, so a
    #: seam is measured against what can be printed rather than against how hard
    #: the filters below were told to push.
    nozzle_mm: float = _shipped("seam.nozzle_mm")             #: shortest printable run
    spike_angle_deg: float = _shipped("seam.spike_angle_deg")  #: sharpest printable corner
    #: Filter strengths.  Default to the limits above; 0 skips the pass.
    min_segment_mm: float = _shipped("seam.min_segment_mm")   #: collapse shorter runs
    min_angle_deg: float = _shipped("seam.min_angle_deg")     #: despike below this angle
    smooth_iterations: int = _shipped("seam.smooth_iterations")  #: Chaikin passes
    max_deviation_mm: float = _shipped("seam.max_deviation_mm")  #: refuse a bigger drift
    work_crs: str = EPSG_METRIC                 #: metric CRS the work is done in

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0



#: The config key behind each field of :class:`SeamParams`, for
#: :func:`load_seam_params`.
_SEAMPARAMS_KEYS = {
    "tolerance_mm": "seam.tolerance_mm",
    "nozzle_mm": "seam.nozzle_mm",
    "spike_angle_deg": "seam.spike_angle_deg",
    "min_segment_mm": "seam.min_segment_mm",
    "min_angle_deg": "seam.min_angle_deg",
    "smooth_iterations": "seam.smooth_iterations",
    "max_deviation_mm": "seam.max_deviation_mm",
    "scale": "print.scale",
    "work_crs": "crs.working",
}


def load_seam_params(*config_files, **overrides):
    """
    A :class:`SeamParams` filled from config files.

    ``config_files`` are read in order, later ones winning -- a window passes
    ``LIB_project_config.settings_files()``, the project file then this desk's
    local one.  A setting no file mentions keeps its built-in default, and
    ``overrides`` win over everything.  :class:`SeamParams` built directly never
    reads a file: its defaults are the shipped seam settings, the same on
    every desk.
    """
    return build_params(SeamParams, _SEAMPARAMS_KEYS, *config_files, **overrides)

@dataclass
class SeamResult:
    """Result of :func:`simplify_shared_border` (for inspection / tests)."""
    a: object = None                    #: province A, rebuilt (work CRS)
    b: object = None                    #: province B, rebuilt
    seam: object = None                 #: the conditioned seam
    seam_original: object = None
    crs: object = None
    source_crs: object = None           #: what the inputs carried, for saving back
    #: Printability, before and after — the numbers this module exists to move.
    vertices_before: int = 0
    vertices_after: int = 0
    short_segments_before: int = 0
    short_segments_after: int = 0
    spikes_before: int = 0
    spikes_after: int = 0
    seam_length_mm: float = 0.0
    #: How far the seam moved, and how much land changed hands.
    max_deviation_mm: float = 0.0
    area_moved_mm2: float = 0.0
    #: The invariant: both must be zero, or the printed pieces do not meet.
    gap_area_mm2: float = 0.0
    overlap_area_mm2: float = 0.0
    parts: int = 0                      #: seam pieces after merging

    @property
    def vertex_reduction(self) -> float:
        """Fraction of seam vertices removed (0..1)."""
        if not self.vertices_before:
            return 0.0
        return 1.0 - self.vertices_after / self.vertices_before


def simplify_shared_border(geojson_a, geojson_b, params=None,
                           out_a=None, out_b=None):
    """
    Condition the seam two provinces share, and rebuild both against it.

    Parameters
    ----------
    geojson_a, geojson_b : str | Path | shapely geometry | GeoDataFrame
        The two neighbouring provinces.  Paths and frames are read and dissolved;
        a bare geometry is assumed to already be in ``params.work_crs``.
    params : SeamParams, optional
    out_a, out_b : str | Path, optional
        Write the rebuilt provinces here as GeoJSON, in the CRS they arrived in.

    Returns
    -------
    SeamResult

    Raises
    ------
    ValueError
        If either input is empty, the two do not share a border, the conditioned
        seam drifts further than ``max_deviation_mm``, or the rebuilt pair fails
        the gap/overlap check.
    """
    params = params or SeamParams()
    geom_a, crs_a = _load(geojson_a, params.work_crs)
    geom_b, crs_b = _load(geojson_b, params.work_crs)
    for name, geom in (("A", geom_a), ("B", geom_b)):
        if geom is None or geom.is_empty:
            raise ValueError(f"province {name} has no geometry to work with.")

    seam = _shared_seam(geom_a, geom_b)
    if seam is None:
        raise ValueError(
            "these two provinces do not share a border, so there is no seam to "
            "simplify. Check they are neighbours and in the same CRS.")

    result = SeamResult(crs=params.work_crs, source_crs=crs_a or crs_b,
                        seam_original=seam)
    conditioned = _condition(seam, params, result)
    result.seam = conditioned

    deviation = conditioned.hausdorff_distance(seam) / params.mpm
    result.max_deviation_mm = float(deviation)
    if deviation > params.max_deviation_mm:
        raise ValueError(
            f"the conditioned seam strays {deviation:.2f} mm from the original, "
            f"over max_deviation_mm ({params.max_deviation_mm:.2f}). Lower "
            "tolerance_mm or raise the cap if that much drift is acceptable.")

    result.a, result.b = _rebuild(geom_a, geom_b, conditioned)
    _measure(geom_a, geom_b, result, params)
    _validate(result)

    if out_a is not None:
        _write(result.a, out_a, result)
    if out_b is not None:
        _write(result.b, out_b, result)

    log.info("Seam %.0f mm: %d -> %d vertices, %d -> %d sub-nozzle segments, "
             "%d -> %d spikes; moved %.3f mm2, max drift %.2f mm.",
             result.seam_length_mm, result.vertices_before, result.vertices_after,
             result.short_segments_before, result.short_segments_after,
             result.spikes_before, result.spikes_after,
             result.area_moved_mm2, result.max_deviation_mm)
    return result


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def _shared_seam(geom_a, geom_b):
    """
    The line the two provinces share, stitched into as few pieces as possible.

    Real data returns this fragmented -- 233 pieces for Ontario/Québec, and as a
    GeometryCollection mixing lines with the isolated points where boundaries
    merely touch.  Points are dropped and ``linemerge`` stitches the rest, which
    takes that 233 down to 2.
    """
    shared = geom_a.boundary.intersection(geom_b.boundary)
    if shared.is_empty:
        return None
    parts = shared.geoms if hasattr(shared, "geoms") else [shared]
    lines = [g for g in parts
             if g.geom_type in ("LineString", "LinearRing") and g.length > 0]
    if not lines:
        return None                    # they touch at points only, not a border
    merged = linemerge(lines)
    return merged if merged.geom_type == "MultiLineString" \
        else MultiLineString([merged])


def _condition(seam, params, result):
    """Run the four printability passes over every piece of the seam."""
    mpm = params.mpm
    tol = params.tolerance_mm * mpm
    min_seg = params.min_segment_mm * mpm
    # Reported against the *printer*, not against the filter. Measuring with
    # min_segment_mm would make "0 sub-nozzle segments" the automatic answer
    # whenever the filter is turned off -- the report would agree with whatever
    # was asked of it instead of describing the seam.
    nozzle = params.nozzle_mm * mpm
    spike_limit = params.spike_angle_deg
    out = []
    result.parts = len(seam.geoms)
    for part in seam.geoms:
        coords = list(part.coords)
        result.vertices_before += len(coords)
        result.short_segments_before += _count_short(coords, nozzle)
        result.spikes_before += _count_spikes(coords, spike_limit)
        result.seam_length_mm += part.length / mpm

        if tol > 0 and len(coords) > 2:
            coords = list(LineString(coords).simplify(
                tol, preserve_topology=False).coords)
        coords = _drop_short_segments(coords, min_seg)
        coords = _despike(coords, params.min_angle_deg)
        for _ in range(max(0, int(params.smooth_iterations))):
            coords = _chaikin(coords)

        result.vertices_after += len(coords)
        result.short_segments_after += _count_short(coords, nozzle)
        result.spikes_after += _count_spikes(coords, spike_limit)
        out.append(LineString(coords))
    return MultiLineString(out)


def _drop_short_segments(coords, min_len):
    """
    Keep vertices at least ``min_len`` apart, always keeping both endpoints.

    Douglas-Peucker measures *perpendicular* distance, so a tight zigzag that
    stays within the tolerance corridor survives it with every vertex intact.
    That is exactly the sub-nozzle chatter worth removing, and it takes a
    spacing rule rather than a deviation rule to see it.
    """
    if len(coords) <= 2 or min_len <= 0:
        return coords
    kept = [coords[0]]
    for point in coords[1:-1]:
        if np.hypot(point[0] - kept[-1][0], point[1] - kept[-1][1]) >= min_len:
            kept.append(point)
    kept.append(coords[-1])            # the endpoint is pinned, never dropped
    # Pinning the endpoint can leave one stub shorter than the rest: the last
    # vertex kept may sit just inside min_len of it. Drop back until it does
    # not, so the guarantee is "no segment under min_len" rather than "none
    # except the last one".
    while len(kept) > 2 and np.hypot(kept[-1][0] - kept[-2][0],
                                     kept[-1][1] - kept[-2][1]) < min_len:
        del kept[-2]
    return kept if len(kept) >= 2 else list(coords[:2])


def _despike(coords, min_angle_deg):
    """
    Drop interior vertices whose corner is sharper than ``min_angle_deg``.

    The angle measured is the one *at* the vertex, between the two edges meeting
    there: near 180 degrees is a straight run, near 0 is a needle that doubles
    back on itself. A needle prints as a fragile spur, and on a seam it prints
    twice -- once as a spur on one province, once as a slot on the other.

    Iterates because removing one spike can expose the next.
    """
    if min_angle_deg <= 0:
        return coords
    limit = np.radians(min_angle_deg)
    working = list(coords)
    for _ in range(len(coords)):
        out = [working[0]]
        for i in range(1, len(working) - 1):
            if _corner_angle(working[i - 1], working[i], working[i + 1]) >= limit:
                out.append(working[i])
        out.append(working[-1])
        if len(out) == len(working):
            return working
        working = out
        if len(working) <= 2:
            break
    return working


def _corner_angle(prev_pt, point, next_pt):
    """Interior angle at ``point``, in radians (0 = needle, pi = straight)."""
    v1 = np.array(prev_pt[:2]) - np.array(point[:2])
    v2 = np.array(next_pt[:2]) - np.array(point[:2])
    n1, n2 = np.hypot(*v1), np.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return np.pi               # a duplicate point is not a spike
    cosine = np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0)
    return float(np.arccos(cosine))


def _chaikin(coords):
    """One corner-cutting pass, with both endpoints pinned."""
    if len(coords) <= 2:
        return coords
    out = [coords[0]]
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        out.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
        out.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
    out.append(coords[-1])
    return out


def _count_short(coords, min_len):
    if len(coords) < 2 or min_len <= 0:
        return 0
    pts = np.asarray(coords, dtype=float)[:, :2]
    return int((np.hypot(*(pts[1:] - pts[:-1]).T) < min_len).sum())


def _count_spikes(coords, min_angle_deg):
    if len(coords) < 3 or min_angle_deg <= 0:
        return 0
    limit = np.radians(min_angle_deg)
    return sum(1 for i in range(1, len(coords) - 1)
               if _corner_angle(coords[i - 1], coords[i], coords[i + 1]) < limit)


# ---------------------------------------------------------------------------
# Rebuilding the pair
# ---------------------------------------------------------------------------

def _rebuild(geom_a, geom_b, seam):
    """
    Re-cut both provinces out of their own union, along the conditioned seam.

    Cutting the union is what makes the pair exact: ``polygonize`` returns faces
    that *partition* it, so there is nowhere for a gap or an overlap to hide.
    Editing each province toward the new seam separately is the approach that
    leaves cracks.

    Each face goes to whichever original province it mostly came from, so land
    the simplification moved across the seam changes hands cleanly.
    """
    union = unary_union([geom_a, geom_b])
    # Keep only faces that are actually land. ``union.boundary`` carries the
    # interior rings too, so polygonize hands back a face for every lake and
    # bay; assigning those to a province would quietly fill them in. Ontario
    # and Manitoba gained 378 mm2 of filled lake before this filter existed.
    faces = [face for face in polygonize(unary_union([union.boundary, seam]))
             if union.contains(face.representative_point())]
    if not faces:
        raise ValueError("splitting the union along the seam produced no faces.")
    keep_a, keep_b = [], []
    for face in faces:
        (keep_a if face.intersection(geom_a).area >= face.intersection(geom_b).area
         else keep_b).append(face)
    return unary_union(keep_a) if keep_a else None, \
        unary_union(keep_b) if keep_b else None


def _measure(geom_a, geom_b, result, params):
    """Area that changed hands, and the gap/overlap the pair must not have."""
    mm2 = params.mpm ** 2
    union_before = unary_union([geom_a, geom_b])
    union_after = unary_union([result.a, result.b])
    result.area_moved_mm2 = float(
        result.a.symmetric_difference(geom_a).area / mm2)
    result.gap_area_mm2 = float(
        union_before.symmetric_difference(union_after).area / mm2)
    result.overlap_area_mm2 = float(result.a.intersection(result.b).area / mm2)


def _validate(result):
    """The invariant: the pair still tiles the ground it started with."""
    if result.a is None or result.b is None:
        raise ValueError(
            "one province came back empty; the seam probably does not reach "
            "the outer boundary at both ends.")
    # A shared edge has zero width, so these are exact-zero checks in principle;
    # the tolerance is for floating-point dust on million-metre coordinates.
    if result.overlap_area_mm2 > 1e-6:
        raise ValueError(
            f"the rebuilt provinces overlap by {result.overlap_area_mm2:.6f} mm2; "
            "they would not print as a pair.")
    if result.gap_area_mm2 > 1e-6:
        raise ValueError(
            f"the rebuilt pair covers {result.gap_area_mm2:.6f} mm2 less or more "
            "than it started with; a gap has opened at the seam.")


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _load(source, work_crs):
    """One province as a dissolved geometry in ``work_crs``, and its own CRS."""
    if isinstance(source, BaseGeometry):
        return source, None
    frame = source if isinstance(source, gpd.GeoDataFrame) \
        else gpd.read_file(str(source))
    if frame.empty:
        return None, None
    source_crs = frame.crs
    if source_crs is not None and str(source_crs) != str(work_crs):
        frame = frame.to_crs(work_crs)
    return unary_union(frame.geometry.values), source_crs


def _write(geom, path, result):
    """Write one rebuilt province back out, in the CRS it arrived in."""
    frame = gpd.GeoDataFrame(geometry=[geom], crs=result.crs)
    if result.source_crs is not None:
        frame = frame.to_crs(result.source_crs)
    path = Path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(path, driver="GeoJSON")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Simplify the border two provinces share, for printing.")
    ap.add_argument("province_a")
    ap.add_argument("province_b")
    ap.add_argument("-A", "--out-a", help="write the rebuilt province A here")
    ap.add_argument("-B", "--out-b", help="write the rebuilt province B here")
    ap.add_argument("--tolerance", type=float, default=None, metavar="MM",
                    help="Douglas-Peucker tolerance on the seam")
    ap.add_argument("--min-segment", type=float, default=None, metavar="MM",
                    help="collapse seam segments shorter than this")
    ap.add_argument("--min-angle", type=float, default=None, metavar="DEG",
                    help="remove seam corners sharper than this")
    ap.add_argument("--smooth", type=int, default=None, metavar="N",
                    help="Chaikin smoothing passes")
    ap.add_argument("--scale", type=float, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The command line is a front end like any window: the saved settings
    # are its defaults, and a flag overrides one.
    defaults = load_seam_params(*settings_files())
    params = SeamParams(
        scale=_pick(args.scale, defaults.scale),
        tolerance_mm=_pick(args.tolerance, defaults.tolerance_mm),
        min_segment_mm=_pick(args.min_segment, defaults.min_segment_mm),
        min_angle_deg=_pick(args.min_angle, defaults.min_angle_deg),
        smooth_iterations=_pick(args.smooth, defaults.smooth_iterations))
    res = simplify_shared_border(args.province_a, args.province_b, params,
                                 args.out_a, args.out_b)
    print(f"seam {res.seam_length_mm:.0f} mm in {res.parts} part(s): "
          f"{res.vertices_before} -> {res.vertices_after} vertices "
          f"({res.vertex_reduction * 100:.0f}% fewer), "
          f"sub-nozzle {res.short_segments_before} -> {res.short_segments_after}, "
          f"spikes {res.spikes_before} -> {res.spikes_after}, "
          f"drift {res.max_deviation_mm:.2f} mm, "
          f"gap {res.gap_area_mm2:.6f} mm2")
    return 0


def _pick(value, fallback):
    return fallback if value is None else value


if __name__ == "__main__":
    raise SystemExit(main())
