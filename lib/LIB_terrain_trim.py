#!/usr/bin/env python3
"""
LIB_terrain_trim.py

Clip a DEMto3D terrain slab down to the province coastline.

DEMto3D can only print within a **rectangular** extent: even when the DEM is
clipped to a province polygon first, nodata cells outside the coastline are
rendered as a flat slab at floor height, so the export is a rectangular slab with
the province raised on top (see ``docs/USING_DEMTO3D.md``).  This module removes
that skirt as a post-process: it loads the slab, **reconciles** its unknown local
print-mm frame against the province GeoJSON, then intersects the mesh with the
province polygon so the cut follows the true coastline regardless of elevation.

    from lib.LIB_terrain_trim import trim_terrain
    res = trim_terrain("print/pei_slab.stl",
                       "borders/…/name_Prince_Edward_Island.geojson",
                       out_path="print/pei_trimmed.stl")

The result is a watertight, coastline-accurate province solid with its base
retained under the terrain.  Because the trimmed STL now has a province-shaped
flat base, it also unblocks the downstream butterfly keys
(:mod:`lib.LIB_butterfly_keys`), whose reconciliation matches that base against
the same GeoJSON.  Recommended workflow: DEMto3D export -> ``trim_terrain`` ->
``LIB_butterfly_keys``.

Reconciliation
--------------
The map scale is fixed (``ConnectorParams.scale``), so the metres->print-mm
mapping is known up to a translation and an optional Y-flip.  A rectangular slab
has no province-shaped *base* to match, so reconciliation scores against the
**raised-terrain silhouette** (:func:`_terrain_footprint`) instead — that region
is province-shaped even before the cut.  The exact coastline cut then uses the
GeoJSON polygon, so genuine 0 m coastal land (which reads as floor height) is
still kept.

Note: the slab's base thickness must exceed the dovetail pocket depth if the
piece is later keyed to a border (``base_height_mm`` > ``dovetail_mm``); trimming
does not change the base thickness.
"""

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import manifold3d as m3d
import geopandas as gpd
import trimesh
from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from shapely.ops import unary_union

from lib.LIB_border_connectors import (ConnectorParams, DEFAULT_SCALE,
                                       EPSG_METRIC, _extrude_man,
                                       _man_to_trimesh, _safe_union,
                                       load_connector_params)
from lib.LIB_project_config import settings_files
from lib.LIB_stl_reconcile import reconcile

log = logging.getLogger(__name__)

#: Terrain-silhouette detection: a top face counts as "raised" only if every
#: vertex stands at least this fraction of the terrain relief above the floor.
_TERRAIN_REL_TOL = 0.02
#: The cutter prism overshoots the slab by this much top and bottom so the
#: boolean sees a clean through-cut rather than coplanar faces.
_PAD_MM = 1.0


@dataclass
class TrimResult:
    """Result of :func:`trim_terrain` (for inspection / tests)."""
    mesh: object = None                 #: trimmed, watertight Trimesh
    score: float = 0.0                  #: reconciliation footprint IoU (0..1)
    flipped_y: bool = False             #: slab Y axis inverted vs. the GeoJSON
    origin_m: tuple = (0.0, 0.0)        #: recovered metric origin
    kept_fraction: float = 0.0          #: trimmed volume / slab volume


def trim_terrain(dem_stl, province_geojson, params=None, build_crs=EPSG_METRIC,
                 buffer_mm=0.0, min_fit_score=0.90, out_path=None):
    """
    Clip a DEMto3D slab STL to a province coastline.

    Parameters
    ----------
    dem_stl : str | pathlib.Path | trimesh.Trimesh
        The DEMto3D terrain slab (a path or an in-memory mesh).
    province_geojson : str | pathlib.Path
        Province boundary (WGS84) the DEM was clipped by; the cut follows it.
    params : ConnectorParams, optional
        Only ``scale`` is used — the map scale the slab was printed at.
        Defaults to ``ConnectorParams()`` (1:8,000,000).
    build_crs : str
        Projected CRS the DEMto3D model was built in (the province polygon is
        reprojected to this before scaling).  DEMto3D uses EPSG:3347 or the
        DEM-native EPSG:3979.  Defaults to EPSG:3347.
    buffer_mm : float
        Outward buffer on the polygon before cutting, to keep a thin coastal lip.
    min_fit_score : float
        Reject if the best reconciliation overlap is below this.
    out_path : str | pathlib.Path, optional
        If given, export the trimmed STL here.

    Returns
    -------
    TrimResult

    Raises
    ------
    ValueError
        If the GeoJSON is empty, the slab cannot be reconciled, the cutter is
        empty, or the trimmed mesh fails the watertight / positive-volume gate.
    """
    params = params or ConnectorParams()
    mesh = _load_mesh(dem_stl)
    geom_m = _load_union_crs(province_geojson, build_crs)
    if geom_m is None or geom_m.is_empty:
        raise ValueError(
            f"{province_geojson}: no province geometry to clip against.")

    name = Path(str(province_geojson)).stem
    report, matrix = reconcile(name, mesh, geom_m, params, min_fit_score,
                               footprint_fn=_terrain_footprint)
    trimmed = _clip(mesh, geom_m, matrix, buffer_mm)
    _validate(trimmed, name)

    kept = float(trimmed.volume / mesh.volume) if mesh.volume > 0 else 0.0
    result = TrimResult(mesh=trimmed, score=report.score,
                        flipped_y=report.flipped_y, origin_m=report.origin_m,
                        kept_fraction=kept)
    if out_path is not None:
        trimmed.export(str(out_path))
    log.info("Trimmed %s to coastline (fit %.2f, kept %.0f%% volume).",
             name, report.score, kept * 100.0)
    return result


# ---------------------------------------------------------------------------
# Terrain silhouette (reconciliation target for a rectangular slab)
# ---------------------------------------------------------------------------
def _terrain_footprint(mesh, rel_tol=_TERRAIN_REL_TOL):
    """
    Union of the raised-terrain top faces projected to XY (print-mm).

    Top-facing triangles whose every vertex stands above the flat nodata floor
    are the province-shaped part of the slab; the flat skirt and the vertical
    coastal walls are excluded.  Used only to reconcile the slab's frame — the
    actual cut uses the GeoJSON polygon.
    """
    v = np.asarray(mesh.vertices, dtype=float)
    f = np.asarray(mesh.faces)
    if len(f) == 0:
        return None
    up = np.asarray(mesh.face_normals)[:, 2] > 0.3      # top surface only
    if not up.any():
        return None
    zf = v[f][:, :, 2]                                  # (nfaces, 3) vertex z
    top_z = zf[up]
    floor_z, zmax = float(top_z.min()), float(top_z.max())
    tol = max((zmax - floor_z) * rel_tol, 1e-6)
    raised = up & np.all(zf > floor_z + tol, axis=1)
    polys = []
    for tri in v[f[raised]][:, :, :2]:
        p = Polygon(tri)
        if p.is_valid and p.area > 0.0:
            polys.append(p)
    if not polys:
        return None
    fp = unary_union(polys)
    return fp if fp.is_valid else fp.buffer(0)


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------
def _clip(mesh, geom_m, matrix, buffer_mm):
    """Intersect the slab with the province polygon, mapped into its own frame.

    Done in manifold space (``^`` is manifold intersection) rather than through
    ``trimesh.boolean``: the codebase's rule is to stay in manifold space, and it
    is robust where the trimesh backend returns an empty result on the coplanar
    walls a coastline cut can produce.
    """
    poly_mm = affine_transform(geom_m, matrix)
    if buffer_mm:
        poly_mm = poly_mm.buffer(buffer_mm)
    z0 = float(mesh.bounds[0][2]) - _PAD_MM
    z1 = float(mesh.bounds[1][2]) + _PAD_MM
    cutter = _extrude_man(poly_mm, z0, z1)              # EvenOdd fill: winding-agnostic
    if cutter is None:
        raise ValueError("province polygon produced an empty cutter prism.")
    trimmed = _man_to_trimesh(_to_manifold(mesh) ^ cutter)
    if trimmed is None:
        raise ValueError("coastline cut removed the whole slab; check the fit.")
    return trimmed


def _to_manifold(mesh):
    """Trimesh -> manifold3d.Manifold for boolean ops."""
    return m3d.Manifold(m3d.Mesh(
        vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint32)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_union_crs(path, crs):
    """Union of a GeoJSON's geometry reprojected into ``crs`` (metres)."""
    gdf = gpd.read_file(path).to_crs(crs)
    return _safe_union(list(gdf.geometry))


def _load_mesh(stl):
    """Return a Trimesh from a Trimesh or a path to an STL."""
    if isinstance(stl, trimesh.Trimesh):
        return stl
    loaded = trimesh.load(str(stl), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"{stl} did not load as a single mesh.")
    return loaded


def _safe_name(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "province"


def _validate(mesh, name):
    """Reject an empty, non-watertight, or negative-volume result."""
    if mesh is None or mesh.is_empty:
        raise ValueError(f"{name}: trimming produced an empty mesh.")
    if not mesh.is_watertight or mesh.volume <= 0.0:
        raise ValueError(
            f"{name}: trimmed mesh failed the watertight / positive-volume gate "
            f"(watertight={mesh.is_watertight}, volume={mesh.volume:.4f}).")


def trim_terrain_from_dir(geojson_dir, stl_dir, params=None, build_crs=EPSG_METRIC,
                          buffer_mm=0.0, min_fit_score=0.90, out_dir=None,
                          stl_pattern="{name}.stl"):
    """
    Convenience: pair ``<geojson_dir>/name_*.geojson`` with STLs in ``stl_dir``.

    ``name_Prince_Edward_Island.geojson`` is matched to
    ``stl_dir / stl_pattern.format(name="Prince_Edward_Island")``.  Provinces
    whose STL is missing are skipped (logged).  Returns ``{name: TrimResult}``.
    """
    geojson_dir = Path(geojson_dir)
    stl_dir = Path(stl_dir)
    out_dir = Path(out_dir) if out_dir is not None else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for gj in sorted(geojson_dir.glob("name_*.geojson")):
        stem = gj.stem[len("name_"):]
        stl = stl_dir / stl_pattern.format(name=stem)
        if not stl.exists():
            log.warning("No STL for %s at %s; skipping.", stem, stl)
            continue
        out = (out_dir / f"{_safe_name(stem)}_trimmed.stl") if out_dir else None
        results[stem] = trim_terrain(str(stl), str(gj), params=params,
                                     build_crs=build_crs, buffer_mm=buffer_mm,
                                     min_fit_score=min_fit_score, out_path=out)
    if not results:
        raise ValueError(
            f"No province GeoJSON/STL pairs found under {geojson_dir} / {stl_dir}.")
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Clip a DEMto3D terrain slab STL to the province coastline.")
    ap.add_argument("dem_stl", help="DEMto3D slab STL")
    ap.add_argument("province_geojson", help="Province boundary GeoJSON (WGS84)")
    ap.add_argument("-o", "--out", default=None,
                    help="Output STL path (default: <dem_stl>_trimmed.stl)")
    ap.add_argument("--scale", type=float,
                    default=load_connector_params(*settings_files()).scale,
                    help="map scale the slab was printed at (1 : scale)")
    ap.add_argument("--build-crs", default=EPSG_METRIC,
                    help="projected CRS the DEMto3D model was built in")
    ap.add_argument("--buffer-mm", type=float, default=0.0,
                    help="outward coastal lip added to the cut")
    ap.add_argument("--min-fit", type=float, default=0.90,
                    help="minimum reconciliation overlap")
    args = ap.parse_args()

    params = ConnectorParams(scale=args.scale)
    out = args.out or str(Path(args.dem_stl).with_name(
        Path(args.dem_stl).stem + "_trimmed.stl"))
    res = trim_terrain(args.dem_stl, args.province_geojson, params=params,
                       build_crs=args.build_crs, buffer_mm=args.buffer_mm,
                       min_fit_score=args.min_fit, out_path=out)
    print(f"Wrote {out}  (fit score {res.score:.2f}, flipped_y={res.flipped_y}, "
          f"kept {res.kept_fraction * 100:.0f}% volume, "
          f"watertight={res.mesh.is_watertight})")


if __name__ == "__main__":
    main()
