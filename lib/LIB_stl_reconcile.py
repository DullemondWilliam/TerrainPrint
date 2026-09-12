#!/usr/bin/env python3
"""
LIB_stl_reconcile.py

Recover the metres->print-mm transform of an externally-produced STL.

Province and terrain pieces are built by separate GeoJSON->STL steps (the
dovetail fitter's printed provinces, the DEMto3D terrain slabs) and land on disk
in an *unknown* local print-mm frame, sometimes with a flipped Y axis.  The map
scale is fixed (``ConnectorParams.scale``), so the metres->print-mm mapping is
known up to a translation and an optional Y-flip.  :func:`reconcile` lifts a 2-D
*footprint* off the mesh and matches it against the scaled GeoJSON footprint by
area IoU, returning the best-scoring affine and rejecting anything below
``min_fit_score`` rather than silently mis-placing geometry.

Which footprint to match is caller-supplied (``footprint_fn``): the dovetail
fitter reconciles against the flat **base** footprint (:func:`base_footprint`),
while a rectangular DEMto3D slab has no province-shaped base and instead
reconciles against its raised-terrain silhouette.
"""

from dataclasses import dataclass

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from shapely.ops import unary_union

#: Fraction of base height a triangle may sit above the floor and still count
#: as part of the flat base footprint used for reconciliation.
_BASE_REL_TOL = 1e-3


@dataclass
class ReconcileReport:
    """How an STL was aligned to its GeoJSON (for inspection / tests)."""
    name: str
    score: float              #: best footprint-overlap IoU (0..1)
    origin_m: tuple           #: recovered metric origin the STL was built at
    flipped_y: bool           #: STL Y axis inverted relative to the GeoJSON
    base_z_mm: float          #: z of the flat base
    n_pockets: int = 0        #: dovetail pockets carved into this piece (fitter)


def base_footprint(mesh, rel_tol=_BASE_REL_TOL):
    """Union of the STL's base-plane triangles projected to XY (print-mm)."""
    v = np.asarray(mesh.vertices, dtype=float)
    f = np.asarray(mesh.faces)
    if len(f) == 0:
        return None
    z = v[:, 2]
    zmin, zmax = float(z.min()), float(z.max())
    tol = max((zmax - zmin) * rel_tol, 1e-6)
    base = np.all((z[f] - zmin) <= tol, axis=1)   # every vertex of the face at floor
    polys = []
    for tri in v[f[base]][:, :, :2]:
        p = Polygon(tri)
        if p.is_valid and p.area > 0.0:
            polys.append(p)
    if not polys:
        return None
    fp = unary_union(polys)
    return fp if fp.is_valid else fp.buffer(0)


def reconcile(name, mesh, geom_m, params, min_fit_score, footprint_fn=None):
    """
    Recover the metres->print-mm transform for one STL.

    Scale is known, so only a translation and an optional Y-flip are free; both
    candidates are scored (over centroid/bbox anchors) by footprint overlap and
    the best is returned as a shapely affine matrix ``[a, b, d, e, xoff, yoff]``
    mapping metres to the STL's own print-mm frame.

    ``footprint_fn(mesh)`` yields the 2-D shapely footprint to match against;
    defaults to :func:`base_footprint`.
    """
    footprint_fn = footprint_fn or base_footprint
    mpm = params.mpm
    fp = footprint_fn(mesh)
    if fp is None or fp.is_empty:
        raise ValueError(f"{name}: STL has no usable footprint to reconcile.")
    base_z = float(mesh.bounds[0][2])

    best = None                                   # (score, matrix, flipped)
    for flipped in (False, True):
        e = (-1.0 / mpm) if flipped else (1.0 / mpm)
        scaled = affine_transform(geom_m, [1.0 / mpm, 0.0, 0.0, e, 0.0, 0.0])
        for anchor in ("centroid", "bbox"):
            tx, ty = _offset(fp, scaled, anchor)
            matrix = [1.0 / mpm, 0.0, 0.0, e, tx, ty]
            score = _iou(fp, affine_transform(geom_m, matrix))
            if best is None or score > best[0]:
                best = (score, matrix, flipped)

    score, matrix, flipped = best
    if score < min_fit_score:
        raise ValueError(
            f"{name}: could not reconcile STL to GeoJSON "
            f"(best footprint overlap {score:.2f} < {min_fit_score:.2f}); "
            "check the scale and orientation.")

    # Invert the recovered translation back into a metric origin (informational).
    x0 = -matrix[4] * mpm
    y0 = (matrix[5] * mpm) if flipped else (-matrix[5] * mpm)
    report = ReconcileReport(name=name, score=float(score),
                             origin_m=(float(x0), float(y0)),
                             flipped_y=flipped, base_z_mm=base_z)
    return report, matrix


def _offset(target, moving, anchor):
    """Translation that snaps ``moving`` onto ``target`` by centroid or bbox centre."""
    if anchor == "centroid":
        tc, mc = target.centroid, moving.centroid
        return tc.x - mc.x, tc.y - mc.y
    tx0, ty0, tx1, ty1 = target.bounds
    mx0, my0, mx1, my1 = moving.bounds
    return ((tx0 + tx1 - mx0 - mx1) / 2.0, (ty0 + ty1 - my0 - my1) / 2.0)


def _iou(a, b):
    """Area intersection-over-union of two footprints, robust to invalid input."""
    try:
        inter = a.intersection(b).area
        union = a.union(b).area
    except Exception:
        a, b = a.buffer(0), b.buffer(0)
        inter = a.intersection(b).area
        union = a.union(b).area
    return inter / union if union > 0.0 else 0.0
