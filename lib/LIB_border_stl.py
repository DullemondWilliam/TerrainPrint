#!/usr/bin/env python3
"""
LIB_border_stl.py

Extrude border-region geometry into a flat, watertight STL bar of a given height.

Given the border polygons (for example ``LIB_border_regions.BorderRegions.border``)
and a height in millimetres, every border part is raised straight up from ``z=0``
to ``z=height_mm`` and returned as a single watertight ``trimesh`` ready to export
as an STL:

    from lib.LIB_border_stl import build_border_stl
    mesh = build_border_stl(regions.border, height_mm=2.0, out_path="borders.stl")

Projection and units
---------------------
The model is built in ``projection`` (default EPSG:3347, the project's metric
build CRS).  If ``geometry_crs`` is given and differs from ``projection``, the
footprint is reprojected into ``projection`` first.  Then, if ``scale`` is given,
the projected metres are converted to print-mm (1 print-mm = ``scale`` mm on the
ground), matching the convention in ``LIB_border_connectors``.  When
``geometry_crs`` is ``None`` the geometry is assumed to already be in
``projection``; coordinates are otherwise left where they are, so a border built
here stays aligned with province pieces built in the same frame.
"""

import logging
from pathlib import Path

from pyproj import CRS, Transformer
from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon
from shapely.ops import transform as shp_transform

from lib.LIB_border_connectors import MIN_PART_AREA_MM2, extrude_solid

log = logging.getLogger(__name__)

#: CRS the border model is built in by default (Statistics Canada Lambert, metres).
DEFAULT_PROJECTION = "EPSG:3347"


def build_border_stl(border_geom, height_mm, geometry_crs=None,
                     projection=DEFAULT_PROJECTION, scale=None, out_path=None):
    """
    Extrude border geometry into a watertight solid of height ``height_mm``.

    Parameters
    ----------
    border_geom : shapely (Multi)Polygon
        The border regions to raise (e.g. ``BorderRegions.border``).  Polygonal
        parts only; anything else is ignored.
    height_mm : float
        Extrusion height in millimetres.  The solid runs from ``z=0`` (flat base)
        to ``z=height_mm``.
    geometry_crs : optional CRS-like
        CRS of ``border_geom``.  If given and different from ``projection``, the
        geometry is reprojected into ``projection`` before extruding.  If ``None``
        the geometry is assumed to already be in ``projection``.
    projection : CRS-like
        CRS the model is built in (default EPSG:3347, metres).
    scale : float, optional
        Map-scale denominator.  When given, the (projected) footprint is treated
        as metres and converted to print-mm first (1 print-mm = ``scale`` mm
        ground).  Leave ``None`` when the geometry is already in print-mm.
    out_path : str | pathlib.Path, optional
        If given, the mesh is also exported here (e.g. ``borders.stl``).

    Returns
    -------
    trimesh.Trimesh
        The watertight border solid.  ``mesh.export(path)`` writes an STL.

    Raises
    ------
    ValueError
        If ``height_mm`` is not positive, the geometry is empty, or no part is
        large enough to extrude.
    """
    if height_mm <= 0:
        raise ValueError("height_mm must be positive.")
    if border_geom is None or getattr(border_geom, "is_empty", True):
        raise ValueError("border_geom is empty; nothing to extrude.")

    geom = border_geom
    if geometry_crs is not None and projection is not None:
        if CRS.from_user_input(geometry_crs) != CRS.from_user_input(projection):
            geom = _reproject_geometry(geom, geometry_crs, projection)
    if scale is not None:
        mpm = scale / 1000.0                       # metres per print-mm
        geom = affine_transform(geom, [1.0 / mpm, 0, 0, 1.0 / mpm, 0, 0])

    # Drop sub-printable slivers (they triangulate into degenerate parts), then
    # extrude every surviving part together in one watertight manifold pass.
    parts = [p for p in _polys(geom) if p.area >= MIN_PART_AREA_MM2]
    if not parts:
        raise ValueError(
            "No border part is at least "
            f"{MIN_PART_AREA_MM2} mm^2; nothing large enough to extrude "
            "(is the geometry in the expected mm units?).")
    footprint = parts[0] if len(parts) == 1 else MultiPolygon(parts)

    mesh = extrude_solid(footprint, 0.0, float(height_mm))
    if mesh is None:
        raise ValueError("Extrusion produced no geometry.")

    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        mesh.export(str(out_path))

    log.info("Extruded %d border part(s) to %.3f mm (%d verts, watertight=%s)",
             len(parts), height_mm, len(mesh.vertices), mesh.is_watertight)
    return mesh


def _reproject_geometry(geom, src_crs, dst_crs):
    """Reproject a shapely geometry from ``src_crs`` to ``dst_crs``."""
    transformer = Transformer.from_crs(
        CRS.from_user_input(src_crs), CRS.from_user_input(dst_crs),
        always_xy=True)
    return shp_transform(
        lambda xs, ys, zs=None: transformer.transform(xs, ys), geom)


def _polys(geom):
    """Polygon parts of a geometry (drops non-polygonal bits / empties)."""
    if geom is None or geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "Polygon":
        return [geom]
    if kind == "MultiPolygon":
        return list(geom.geoms)
    if kind == "GeometryCollection":
        return [g for g in geom.geoms if g.geom_type == "Polygon"]
    return []
