#!/usr/bin/env python3
"""
LIB_border_split.py

Auto-subdivide a border STL into watertight pieces that each fit a print bed.

The border bar produced by :func:`LIB_border_stl.build_border_stl` can be far
longer than any print bed.  This engine recursively bisects it along its longer
over-bed axis until every piece's footprint fits the given bed, keeping each
piece watertight:

    from lib.LIB_border_split import split_to_bed
    pieces = split_to_bed("borders.stl", bed_mm=256, margin_mm=10,
                          out_dir="bed_pieces")

Method
------
Cuts are made with ``manifold3d`` half-space splits (the project's boolean
backend), not trimesh sectioning, so each cut face is capped into a watertight
solid rather than left open.  The mesh is first split into connected bodies so
already-separate bars are never cut against each other; each body is then
bisected at the midpoint of whichever axis (X or Y) most overflows the bed.
Bisection terminates because every cut strictly shrinks the axis it cuts.

Bed fit is an XY-plate test: the border bar is thin in Z, so fitting the bed is
about the footprint, not the height.

Connectors
----------
An STL carries only triangles, so this mesh-only splitter cannot see dovetail
connectors and may cut through one (cf. FR-3.11).  The current
``build_border_stl`` bar has no connectors, so that is moot here; a ``keepout``
hook is reserved on :func:`split_to_bed` for a future connector-aware pass.
"""

import logging
from pathlib import Path

import numpy as np
import manifold3d as m3d
import trimesh

log = logging.getLogger(__name__)

#: Default usable print-bed edge in millimetres (matches the project default).
DEFAULT_BED_MM = 256.0
#: Default keep-clear border subtracted from each bed edge, per side.
DEFAULT_MARGIN_MM = 10.0
#: Border parts smaller than this are sub-printable slivers (matches the other libs).
MIN_PART_AREA_MM2 = 0.5

_EPS = 1e-6


def split_to_bed(mesh, bed_mm=DEFAULT_BED_MM, margin_mm=0.0,
                 out_dir=None, out_stem="border"):
    """
    Subdivide a border mesh into watertight pieces that each fit the print bed.

    Parameters
    ----------
    mesh : trimesh.Trimesh | str | pathlib.Path
        The border solid, or a path to an STL to load.
    bed_mm : float | tuple[float, float]
        Usable bed size in millimetres.  A scalar means a square bed; a
        ``(width, depth)`` pair sets the two axes independently.
    margin_mm : float
        Keep-clear border subtracted from *each* side of the bed, so the usable
        area is ``bed - 2 * margin`` per axis.
    out_dir : str | pathlib.Path, optional
        If given, each piece is exported as ``<out_stem>_001.stl``,
        ``<out_stem>_002.stl`` … inside this directory.
    out_stem : str
        Filename stem for exported pieces.

    Returns
    -------
    list[trimesh.Trimesh]
        The bed-fitting pieces, each watertight with positive volume.  A mesh
        that already fits comes back as a single-element list.

    Raises
    ------
    ValueError
        If the bed (after margin) is not positive, the mesh is empty, or a piece
        fails the watertight / positive-volume gate.
    """
    bed_w, bed_d = _bed_dims(bed_mm)
    fit_w = bed_w - 2.0 * margin_mm
    fit_d = bed_d - 2.0 * margin_mm
    if fit_w <= 0.0 or fit_d <= 0.0:
        raise ValueError(
            f"Usable bed is non-positive after margin: {fit_w:.3f} x {fit_d:.3f} mm.")

    tri = _load(mesh)
    if tri.is_empty or len(tri.faces) == 0:
        raise ValueError("mesh is empty; nothing to split.")

    # Sliver floor scaled by the bar height, so a grazing cut's crumbs are dropped.
    zmin, zmax = tri.bounds[0][2], tri.bounds[1][2]
    min_vol = MIN_PART_AREA_MM2 * max(zmax - zmin, _EPS)

    man = _to_manifold(tri)
    parts: list[m3d.Manifold] = []
    for body in man.decompose():                 # never cut separate bars together
        _bisect(body, fit_w, fit_d, min_vol, parts)

    pieces = []
    for part in parts:
        piece = _to_trimesh(part)
        if piece is None:
            continue
        if not piece.is_watertight or piece.volume <= 0.0:
            raise ValueError(
                "A split piece failed the watertight / positive-volume gate "
                f"(watertight={piece.is_watertight}, volume={piece.volume:.4f}).")
        pieces.append(piece)

    if not pieces:
        raise ValueError("Splitting produced no printable pieces.")

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, piece in enumerate(pieces, start=1):
            piece.export(str(out_dir / f"{out_stem}_{i:03d}.stl"))

    log.info("Split into %d bed-fitting piece(s) for a %.0f x %.0f mm bed "
             "(usable %.0f x %.0f mm).",
             len(pieces), bed_w, bed_d, fit_w, fit_d)
    return pieces


# ---------------------------------------------------------------------------
# Recursive bisection
# ---------------------------------------------------------------------------
def _bisect(man, fit_w, fit_d, min_vol, out):
    """Recursively halve ``man`` until each part fits ``fit_w`` x ``fit_d``."""
    if man.is_empty() or man.volume() < min_vol:
        return
    xmin, ymin, _, xmax, ymax, _ = man.bounding_box()
    w, d = xmax - xmin, ymax - ymin

    if w <= fit_w + _EPS and d <= fit_d + _EPS:
        out.append(man)
        return

    # Cut the axis that most overflows its bed limit, at the midpoint of that
    # axis; each half's extent on that axis then strictly shrinks (< w or < d),
    # so the recursion is guaranteed to terminate.
    if w / fit_w >= d / fit_d:
        normal, offset = [1.0, 0.0, 0.0], 0.5 * (xmin + xmax)
    else:
        normal, offset = [0.0, 1.0, 0.0], 0.5 * (ymin + ymax)

    positive, negative = man.split_by_plane(normal, offset)
    _bisect(negative, fit_w, fit_d, min_vol, out)
    _bisect(positive, fit_w, fit_d, min_vol, out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bed_dims(bed_mm):
    """Normalise ``bed_mm`` (scalar or pair) to a positive ``(width, depth)``."""
    if isinstance(bed_mm, (tuple, list)):
        if len(bed_mm) != 2:
            raise ValueError("bed_mm as a sequence must be (width, depth).")
        bed_w, bed_d = float(bed_mm[0]), float(bed_mm[1])
    else:
        bed_w = bed_d = float(bed_mm)
    if bed_w <= 0.0 or bed_d <= 0.0:
        raise ValueError(f"bed_mm must be positive; got {bed_w} x {bed_d}.")
    return bed_w, bed_d


def _load(mesh):
    """Return a Trimesh from a Trimesh or a path to an STL."""
    if isinstance(mesh, trimesh.Trimesh):
        return mesh
    if isinstance(mesh, (str, Path)):
        loaded = trimesh.load(str(mesh), force="mesh")
        if not isinstance(loaded, trimesh.Trimesh):
            raise ValueError(f"{mesh} did not load as a single mesh.")
        return loaded
    raise TypeError("mesh must be a trimesh.Trimesh or a path to an STL.")


def _to_manifold(tri):
    """Trimesh -> manifold3d.Manifold."""
    m = m3d.Mesh(vert_properties=np.asarray(tri.vertices, dtype=np.float32),
                 tri_verts=np.asarray(tri.faces, dtype=np.uint32))
    return m3d.Manifold(m)


def _to_trimesh(man):
    """manifold3d.Manifold -> Trimesh (None if empty)."""
    if man is None or man.is_empty():
        return None
    mm = man.to_mesh()
    faces = np.asarray(mm.tri_verts)
    if len(faces) == 0:
        return None
    verts = np.asarray(mm.vert_properties)[:, :3]
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)
