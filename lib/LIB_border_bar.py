#!/usr/bin/env python3
"""
LIB_border_bar.py

The border bar, in the pieces the user plans: splits across the corridor,
and the bar extruded piece by piece.

``split_at`` puts a split straight across the corridor where it is clicked
-- outer edge to outer edge, over any hole -- and refuses one through a
connector or one that would leave a piece too small to build.  The pieces
themselves come from ``LIB_border_plan.BorderDefinition.pieces()``;
``build_bar`` extrudes each with the connectors it carries, all in one
print-mm frame so they meet where they were cut, and counts the ones too big
for the bed.  Nothing is split for you.

Split out of ``UI_terrain_editor.py``.
"""

from dataclasses import dataclass, field, replace

import numpy as np
import trimesh
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from lib.LIB_border_connectors import (MIN_PART_AREA_MM2, _edge_frame,
                                       _polys, _safe_union,
                                       build_border_pieces)
from lib.LIB_border_plan import EPSG_WORKING, BorderDefinition
from lib.LIB_raster_reproject import (ReprojectParams, bounds_for,
                                      reproject_geotiff)
from lib.LIB_terrain_stl import TerrainParams, surface_sampler

#: A split is a straight cut across the corridor, found by walking in from the
#: edge nearest the click.  One that has not come out the other side within
#: this many corridor widths is running *along* the corridor, not across it.
SPLIT_REACH_WIDTHS = 3.0


def split_at(definition, x: float, y: float):
    """
    The split a click at (x, y) puts across the corridor, and why not when it
    cannot go there: ``(line, "")`` or ``(None, reason)``.

    Straight across: through the corridor edge nearest the click, square to
    it, from the bar's outer edge to its outer edge on the other side -- over
    any hole the bar has there (where it rings an island of province), since
    a cut that stops at a hole leaves the ring whole.  Refused where it does
    not come out within ``SPLIT_REACH_WIDTHS`` corridor widths -- a click at
    a junction, or running along a bend -- and where it would pass within a
    clearance of a connector, which it would cut in two, and where it would
    leave a piece under the bar's sliver floor (``MIN_PART_AREA_MM2``), which
    would not be built.  Splits may otherwise sit as close as they like: one
    on each arm is how a junction is taken apart.  The line runs a hair past
    both edges, so it divides the corridor cleanly.
    """
    d = definition
    if d.border is None:
        return None, "There is no corridor to split."
    p = d.settings.connector_params()
    point = Point(x, y)
    part = min(_polys(d.border), key=point.distance)
    ring = min([part.exterior, *part.interiors], key=point.distance)
    frame = _edge_frame(ring, ring.project(point), d.border, p.mpm)
    if frame is None:
        return None, "No clean cut across the corridor here."
    at, _, (nx, ny) = frame
    eps = 0.01 * p.mpm
    reach = SPLIT_REACH_WIDTHS * d.settings.width_mm * p.mpm
    # Both ways from the edge: into the bar, and -- from a hole's edge --
    # back across the hole to the bar on its far side.  Holes filled, so
    # the stretch through the click runs outer edge to outer edge.
    probe = LineString([(at.x + nx * reach, at.y + ny * reach),
                        (at.x - nx * reach, at.y - ny * reach)])
    filled = Polygon(part.exterior)
    inside = [g for g in _lines(probe.intersection(filled))
              if g.distance(at) < 2 * eps]
    outer = part.exterior
    if not inside or inside[0].length >= 2 * reach - 4 * eps or any(
            outer.distance(Point(c)) > 2 * eps
            for c in (inside[0].coords[0], inside[0].coords[-1])):
        return None, ("No clean cut across the corridor here: try a "
                      "straighter stretch, away from a junction.")
    (x0, y0), (x1, y1) = inside[0].coords[0], inside[0].coords[-1]
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    cut = LineString([(x0 - ux * eps, y0 - uy * eps),
                      (x1 + ux * eps, y1 + uy * eps)])
    gap = p.clearance_mm * p.mpm
    if any(cut.distance(lobe) < gap for lobe in d.lobes):
        return None, "It would cut through a connector."
    # Splits may sit as close as they like -- one on each arm of a junction
    # is the usual way to take a junction apart -- but not so close that the
    # piece between falls under the sliver floor the bar is built to, where
    # it would not be built at all and that stretch of bar would be lost.
    # Only the pieces this cut makes -- the two either side of it.  A map's
    # corridor can hold specks under the floor already (where it rings a
    # tiny island), and those are no reason to refuse a split elsewhere.
    floor = MIN_PART_AREA_MM2 * p.mpm ** 2
    trial = replace(d, splits=d.splits + [cut])
    made = [piece for piece in trial.pieces() if piece.distance(cut) < eps]
    if any(piece.area < floor for piece in made):
        return None, (f"It would leave a piece under {MIN_PART_AREA_MM2:g} "
                      "mm², too small to build -- move it a little further "
                      "from the next split or the end of the bar.")
    return cut, ""


def _lines(geom) -> list:
    """The LineStrings in ``geom``."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in _lines(part)]
    return []


@dataclass
class BarResult:
    """The border bar, split for the bed (result of :func:`build_bar`)."""
    mesh: object = None
    pieces: list = field(default_factory=list)
    origin_m: tuple = (0.0, 0.0)   #: metres point at the bar's print-mm (0, 0)
    mpm: float = 0.0
    lobes: int = 0
    lobes_cut: int = 0             #: connectors a split went through
    draped: bool = False           #: top follows the terrain, not flat
    too_big: int = 0               #: pieces that will not fit the bed
    removed: int = 0               #: pieces the user took out, not built
    #: Per solid in ``pieces``: whether it is part of a piece too big for
    #: the bed -- the 3D panel draws those red.
    piece_too_big: list = field(default_factory=list)


def build_bar(definition: BorderDefinition, bed_mm=None,
              margin_mm=None, surface=None) -> BarResult:
    """
    The border bar (corridor + lobe layer), in the pieces the splits plan.

    Nothing is split for you: each piece is a stretch of corridor between the
    splits drawn on the Borders tab (:meth:`BorderDefinition.pieces`), with
    the connectors it carries, extruded on its own.  All of them share one
    local frame whose metres origin is known -- so they lie where they will
    join, and the 3D panel can lay a terrain solid beside them.  A piece too
    big for the bed is built all the same and counted (``too_big``); one
    the user took out (``BorderDefinition.removed``) is not built at all, and
    neither are its connectors.  The frame is the whole bar's either way, so
    what is built still lies where it will join.  For ``too_big``,
    ``bed_mm`` and ``margin_mm`` are what it is measured against.

    ``surface`` -- ``(xs_m, ys_m) -> print-mm``, from :func:`bar_surface` --
    drapes the bar's top over the terrain so it stands as tall as the solids
    either side of it; without one the top is flat at the bar's thickness.
    """
    if definition.border is None:
        raise ValueError("No corridor to print: the targets share no border "
                         "and touch no surrounding region.")
    params = definition.settings.connector_params()
    lobes = definition.lobes
    # Print-mm (0, 0) at the bottom-left of the corridor and lobes together,
    # for every piece: they stay where they lie relative to each other.
    bottom = _safe_union([definition.border] + list(lobes)) if lobes \
        else definition.border
    origin = tuple(bottom.bounds[:2])
    big = set(definition.too_big(bed_mm, margin_mm))
    pieces, flags = [], []
    for i, (piece, mine, out) in enumerate(zip(
            definition.pieces(), definition.piece_lobes(),
            definition.removed_flags())):
        if out:
            continue                     # taken out of the bar by the user
        solids = build_border_pieces(piece, mine, params, surface,
                                     origin_m=origin)
        pieces += solids
        flags += [i in big] * len(solids)
    if not pieces:
        if any(definition.removed_flags()):
            raise ValueError("Every piece of the bar has been removed.")
        raise ValueError("The corridor is too thin to extrude; widen it.")
    mesh = pieces[0] if len(pieces) == 1 else trimesh.util.concatenate(pieces)
    kept = definition.kept_lobes()
    result = BarResult(mesh=mesh, pieces=pieces, origin_m=origin,
                       mpm=params.mpm, lobes=len(kept),
                       removed=sum(definition.removed_flags()),
                       draped=surface is not None, too_big=len(big),
                       piece_too_big=flags)
    result.lobes_cut = _lobes_cut(pieces, kept, origin, params.mpm)
    return result


def bar_surface(raster, corridor, warp: ReprojectParams,
                terrain: TerrainParams, warp_path):
    """
    The terrain's height over the corridor, for :func:`build_bar` to drape on.

    The DEM is warped to the working CRS -- where the corridor is -- over the
    corridor's extent, at the resolution and resampling the crop uses, then
    read through the solid's own elevation -> mm rule.  So the bar's top meets
    each solid at the height that solid has at the seam, whichever province's
    crop it came from.
    """
    pad = 2 * max(warp.dst_res_m, 1.0)
    params = replace(warp, bounds=bounds_for(corridor, EPSG_WORKING, pad))
    reproject_geotiff(raster, EPSG_WORKING, warp_path, params)
    return surface_sampler(warp_path, terrain)


def build_draped_bar(definition: BorderDefinition, raster, warp, terrain,
                     warp_path) -> BarResult:
    """:func:`build_bar` with its top draped over ``raster``."""
    return build_bar(definition, surface=bar_surface(
        raster, definition.border, warp, terrain, warp_path))


def _footprint(mesh):
    """A solid's plan footprint, from its downward-facing triangles."""
    down = mesh.face_normals[:, 2] < -0.5
    tris = [Polygon(t[:, :2]) for t in mesh.triangles[down]]
    return unary_union([t for t in tris if t.area > 0])


def _lobes_cut(pieces, lobes, origin, mpm) -> int:
    """How many lobes ended up in two or more bed pieces (FR-3.11)."""
    if len(pieces) < 2 or not lobes:
        return 0
    prints = [_footprint(piece) for piece in pieces]
    matrix = [1 / mpm, 0, 0, 1 / mpm, -origin[0] / mpm, -origin[1] / mpm]
    cut = 0
    for lobe in lobes:
        lobe_mm = affine_transform(lobe, matrix)
        holders = sum(1 for fp in prints
                      if lobe_mm.intersection(fp).area > 1e-3 * lobe_mm.area)
        cut += holders > 1
    return cut


def bar_vertex_colours(bar: BarResult, fit_colour, big_colour):
    """
    Per-vertex RGBA for a built bar: ``fit_colour`` on each piece that fits
    the bed, ``big_colour`` on each that does not -- or None when every
    piece fits (draw it in one colour).

    The bar's mesh is its pieces concatenated in order, so their vertex
    counts, in order, say which vertices are whose; a mesh that is not that
    concatenation gets None rather than colours that lie.
    """
    if bar is None or not any(bar.piece_too_big) \
            or len(bar.piece_too_big) != len(bar.pieces):
        return None
    if len(bar.pieces) == 1:
        counts = [len(bar.mesh.vertices)]
    else:
        counts = [len(piece.vertices) for piece in bar.pieces]
    if sum(counts) != len(bar.mesh.vertices):
        return None
    return np.vstack([np.tile(big_colour if big else fit_colour, (n, 1))
                      for n, big in zip(counts, bar.piece_too_big)]
                     ).astype(np.float32)
