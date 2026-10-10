#!/usr/bin/env python3
"""
LIB_border_connectors.py

Dovetail back-spline connectors joining the border region to the provinces.

The border strip is printed ``thickness_mm`` tall (default 2 mm).  Its bottom
``dovetail_mm`` (default 1 mm) carries dovetail *lobes* that reach sideways into
the adjacent provinces; each province gets a matching ``dovetail_mm``-deep
dovetail pocket carved into its underside (back).  Dropped together from the
back, the lobes lock the provinces against sliding apart in the map plane (a
butterfly / bowtie key), while the top layer stays as the visible border.

All connector geometry is generated once in EPSG:3347 metres, so the border tabs
and the province pockets share an identical dovetail profile even though every
printed piece lives in its own local print-mm frame.  Alignment between the
border piece and a province therefore depends only on both tools using the same
``borders.geojson`` and the same :class:`ConnectorParams` (crucially the same
``scale``).
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import manifold3d as m3d
import shapely
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, LineString, Point
from shapely.affinity import affine_transform
from shapely.ops import unary_union
from shapely.validation import make_valid
import trimesh

from lib.LIB_project_config import build_params, settings_files
from lib.LIB_project_config import shipped as _shipped

EPSG_SRC      = _shipped("crs.source")
EPSG_METRIC   = _shipped("crs.working")
DEFAULT_SCALE = _shipped("print.scale")          # 1 print-mm : scale/1e6 km

# Border parts smaller than this triangulate into non-manifold slivers and are
# too small to print anyway (matches the island filter in LIB_terrain_stl).
MIN_PART_AREA_MM2 = 0.5

# The dovetail layer and the corridor layer are overlapped by this much so the
# boolean union sees intersecting solids rather than coplanar touching faces.
_LAYER_OVERLAP_MM = 0.01


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
@dataclass
class ConnectorParams:
    scale:        float = DEFAULT_SCALE   # map scale denominator (1 : scale)
    thickness_mm: float = _shipped("print.border_thickness_mm")   # total border height
    dovetail_mm:  float = _shipped("print.dovetail_mm")  # dovetail layer height / pocket depth (Z)
    spacing_mm:   float = 14.0            # gap between keys along the seam
    neck_mm:      float = 1.6             # dovetail width at the corridor edge (throat)
    mouth_mm:     float = 3.2             # dovetail width at the far end (wide -> locks)
    reach_mm:     float = 3.0             # how far a lobe reaches into the province
    overlap_mm:   float = 0.8             # how far the lobe root sinks into the corridor
    clearance_mm: float = 0.2             # slide gap added to the province pocket
    min_overlap_frac: float = 0.15        # drop lobes barely touching a province

    @property
    def mpm(self) -> float:               # metres per print-mm
        return self.scale / 1000.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

#: The config key behind each field of :class:`ConnectorParams`, for
#: :func:`load_connector_params`.
_CONNECTORPARAMS_KEYS = {
    "thickness_mm": "print.border_thickness_mm",
    "dovetail_mm": "print.dovetail_mm",
    "scale": "print.scale",
}


def load_connector_params(*config_files, **overrides):
    """
    A :class:`ConnectorParams` filled from config files.

    ``config_files`` are read in order, later ones winning -- a window passes
    ``LIB_project_config.settings_files()``, the project file then this desk's
    local one.  A setting no file mentions keeps its built-in default, and
    ``overrides`` win over everything.  :class:`ConnectorParams` built directly never
    reads a file: its defaults are the shipped connector settings, the same on
    every desk.
    """
    return build_params(ConnectorParams, _CONNECTORPARAMS_KEYS, *config_files, **overrides)

def _polys(geom):
    """Polygon parts of a geometry (drops non-polygonal bits / empties)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if g.geom_type == "Polygon"]
    return []


def _rings(geom):
    """Every boundary ring (exterior + interiors) of all parts, as LineStrings."""
    rings = []
    for poly in _polys(geom):
        rings.append(LineString(poly.exterior.coords))
        for r in poly.interiors:
            rings.append(LineString(r.coords))
    return rings


def _to_mm(geom_m, mpm, x0, y0):
    """EPSG:3347 metres -> print-mm with the local origin at (x0, y0)."""
    return affine_transform(geom_m, [1.0/mpm, 0, 0, 1.0/mpm, -x0/mpm, -y0/mpm])


def _extrude_man(poly_mm, z0, z1):
    """
    Extrude a (Multi)Polygon between z0 and z1 -> manifold3d.Manifold.

    Uses manifold3d's CrossSection rather than trimesh's earcut: the border
    corridor is a long, very thin, highly detailed ribbon and earcut produces
    inverted or degenerate triangulations on it.
    """
    height = z1 - z0
    if height <= 0:
        return None

    contours = []
    for poly in _polys(poly_mm):
        if poly.area <= 0:
            continue
        for pp in _polys(poly.buffer(0)):          # repair self-touching rings
            contours.append(list(pp.exterior.coords)[:-1])
            for r in pp.interiors:
                contours.append(list(r.coords)[:-1])
    if not contours:
        return None

    man = m3d.CrossSection(contours, m3d.FillRule.EvenOdd).extrude(height)
    if z0:
        man = man.translate([0.0, 0.0, z0])
    return man


def _man_to_trimesh(man):
    """manifold3d.Manifold -> Trimesh (None if empty)."""
    if man is None:
        return None
    mm = man.to_mesh()
    faces = np.asarray(mm.tri_verts)
    if len(faces) == 0:
        return None
    verts = np.asarray(mm.vert_properties)[:, :3]
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def _extrude(poly_mm, z0, z1):
    """Extrude a (Multi)Polygon between z0 and z1 -> watertight Trimesh."""
    return _man_to_trimesh(_extrude_man(poly_mm, z0, z1))


#: Public alias – the manifold-backed extruder, reused by ``core.meshing``.
extrude_solid = _extrude


def _union(meshes):
    meshes = [m for m in meshes if m is not None]
    if not meshes:
        return None
    if len(meshes) == 1:
        return meshes[0]
    return trimesh.boolean.union(meshes)


# ---------------------------------------------------------------------------
# Dovetail lobes  (shared between the border piece and the province pockets)
# ---------------------------------------------------------------------------
def generate_lobes(border_geom_m, prov_union_m, p: ConnectorParams):
    """
    Dovetail lobe polygons (EPSG:3347 metres) placed along the border seam.

    Each lobe is a full trapezoid – narrow throat (``neck_mm``) at the corridor
    edge, widening to ``mouth_mm`` ``reach_mm`` into the province – so the wide
    end is captured behind the throat and the province cannot slide away from the
    border in the map plane.  Lobes are returned *unclipped* so the border tab
    and the province pocket have the exact same profile; a lobe is dropped only
    if it fails to reach into ``prov_union_m``.
    """
    spacing_m = p.spacing_mm * p.mpm
    lobes = []
    for ring in _rings(border_geom_m):
        length = ring.length
        if length < spacing_m:
            continue
        count = int(length // spacing_m)
        for k in range(count):
            lobe = _lobe_on_ring(ring, (k + 0.5) * spacing_m, border_geom_m, p)
            if lobe is None:
                continue
            if prov_union_m is not None:
                if lobe.intersection(prov_union_m).area < p.min_overlap_frac * lobe.area:
                    continue
            lobes.append(lobe)
    return lobes


def lobe_at(border_geom_m, x, y, p: ConnectorParams):
    """
    The lobe :func:`generate_lobes` would place at the corridor edge nearest
    (x, y) -- one connector, put by hand.

    The point is projected onto the nearest boundary ring of the corridor, so a
    click either side of the corridor picks the edge (and so the province) on
    that side, and the lobe has the same profile as a generated one.  Returns
    None when there is no corridor or the lobe there would be degenerate.
    """
    point = Point(x, y)
    rings = _rings(border_geom_m)
    if not rings:
        return None
    ring = min(rings, key=point.distance)
    return _lobe_on_ring(ring, ring.project(point), border_geom_m, p)


def _lobe_on_ring(ring, s, border_geom_m, p: ConnectorParams):
    """The dovetail trapezoid ``s`` metres along ``ring``, reaching out of the
    corridor; None where the ring has no direction or the lobe is invalid."""
    mpm       = p.mpm
    neck_m    = p.neck_mm    * mpm
    mouth_m   = p.mouth_mm   * mpm
    reach_m   = p.reach_mm   * mpm
    overlap_m = p.overlap_mm * mpm
    eps       = 0.001 * mpm
    length    = ring.length

    P  = ring.interpolate(s)
    Pa = ring.interpolate(min(s + eps, length))
    Pb = ring.interpolate(max(s - eps, 0.0))
    tx, ty = Pa.x - Pb.x, Pa.y - Pb.y
    tlen = (tx*tx + ty*ty) ** 0.5
    if tlen == 0:
        return None
    tx, ty = tx/tlen, ty/tlen          # unit tangent
    nx, ny = -ty, tx                    # unit normal (perpendicular)

    # Point the normal OUT of the border corridor (into a province).
    if border_geom_m.contains(Point(P.x + nx*eps*10, P.y + ny*eps*10)):
        nx, ny = -nx, -ny

    rc = (P.x - nx*overlap_m, P.y - ny*overlap_m)   # root centre (in corridor)
    fc = (P.x + nx*reach_m,   P.y + ny*reach_m)     # far centre  (in province)
    root1 = (rc[0] + tx*neck_m/2,  rc[1] + ty*neck_m/2)
    root2 = (rc[0] - tx*neck_m/2,  rc[1] - ty*neck_m/2)
    far1  = (fc[0] + tx*mouth_m/2, fc[1] + ty*mouth_m/2)
    far2  = (fc[0] - tx*mouth_m/2, fc[1] - ty*mouth_m/2)

    lobe = Polygon([root1, far1, far2, root2])
    if (not lobe.is_valid) or lobe.is_empty:
        return None
    return lobe


# ---------------------------------------------------------------------------
# Border piece  (2 mm bar + 1 mm dovetail lobes)
# ---------------------------------------------------------------------------
def _drape(man, z_top, top_mm, x0, y0, p: ConnectorParams):
    """
    Lift the top face of a prism (its vertices at ``z_top``) onto ``top_mm``.

    ``top_mm(xs_m, ys_m)`` gives the print-mm height of the ground beside the
    bar, so the bar's top follows the terrain solids either side of it; it never
    drops below ``p.thickness_mm``, which the connector layer needs.
    """
    if man is None or top_mm is None:
        return man

    def lift(verts):
        verts = np.array(verts, dtype="float64")
        top = verts[:, 2] >= z_top - 1e-6
        if top.any():
            heights = top_mm(x0 + verts[top, 0] * p.mpm,
                             y0 + verts[top, 1] * p.mpm)
            verts[top, 2] = np.maximum(p.thickness_mm, heights)
        return verts

    return man.warp_batch(lift)


def _densify(poly_mm, step_mm):
    """Extra outline vertices every ``step_mm``, for a drape to bend at."""
    return shapely.segmentize(poly_mm, step_mm) if step_mm else poly_mm


#: Outline spacing of a draped top, print-mm: fine enough to follow the
#: terrain's relief along the corridor without bloating the mesh.
DRAPE_STEP_MM = 0.5


def build_border_pieces(border_geom_m, lobes_m, p: ConnectorParams,
                        top_mm=None):
    """
    Watertight stepped solids in print-mm – one per connected border part.

    Each part is extruded and unioned on its own: doing it globally produces a
    non-manifold soup, and sub-printable slivers triangulate badly, so parts
    below ``MIN_PART_AREA_MM2`` are dropped.  All pieces share one local origin
    so they stay in correct relative position.

    ``top_mm(xs_m, ys_m) -> print-mm`` drapes the bar's top over the terrain
    (see :func:`LIB_terrain_stl.surface_sampler`) instead of leaving it flat at
    ``thickness_mm``, so it meets the solids either side at their height.
    """
    step = DRAPE_STEP_MM if top_mm is not None else 0.0
    bottom_m = _safe_union([border_geom_m] + list(lobes_m)) if lobes_m else border_geom_m
    minx, miny = bottom_m.bounds[0], bottom_m.bounds[1]
    min_area_m = MIN_PART_AREA_MM2 * (p.mpm ** 2)

    pieces = []
    for part in _polys(bottom_m):
        if part.area < min_area_m:
            continue                                  # sub-printable sliver
        top_part = part.intersection(border_geom_m)   # corridor portion of this part

        # No lobes on this part → plain full-height prism, no union needed.
        if top_part.is_empty or top_part.area >= part.area * 0.999:
            man = _extrude_man(_densify(_to_mm(part, p.mpm, minx, miny), step),
                               0.0, p.thickness_mm)
            man = _drape(man, p.thickness_mm, top_mm, minx, miny, p)
        else:
            # Stay in manifold space for the union – round-tripping through
            # trimesh between operations loses watertightness.  The layers are
            # overlapped slightly so the union sees intersecting solids rather
            # than coplanar touching faces.
            bottom = _extrude_man(_to_mm(part, p.mpm, minx, miny), 0.0, p.dovetail_mm)
            top    = _extrude_man(_densify(_to_mm(top_part, p.mpm, minx, miny), step),
                                  p.dovetail_mm - _LAYER_OVERLAP_MM, p.thickness_mm)
            top    = _drape(top, p.thickness_mm, top_mm, minx, miny, p)
            man = bottom if top is None else (top if bottom is None else bottom + top)

        solid = _man_to_trimesh(man)
        if solid is not None:
            pieces.append(solid)
    return pieces


def build_border_mesh(border_geom_m, lobes_m, p: ConnectorParams, top_mm=None):
    """All border pieces combined into a single mesh (for one STL export)."""
    pieces = build_border_pieces(border_geom_m, lobes_m, p, top_mm)
    if not pieces:
        return None
    return pieces[0] if len(pieces) == 1 else trimesh.util.concatenate(pieces)


# ---------------------------------------------------------------------------
# Province pockets  (carved into the underside during terrain generation)
# ---------------------------------------------------------------------------
def pocket_cutters(lobes_m, x0_m, y0_m, p: ConnectorParams):
    """Cutter prisms (local print-mm, z in [-0.1, dovetail_mm]) for one province."""
    cutters = []
    for lobe in lobes_m:
        lobe_c  = lobe.buffer(p.clearance_mm * p.mpm)    # pocket slightly oversized
        poly_mm = _to_mm(lobe_c, p.mpm, x0_m, y0_m)
        c = _extrude(poly_mm, -0.1, p.dovetail_mm)
        if c is not None:
            cutters.append(c)
    return cutters


def notch_province(mesh, lobes_m, x0_m, y0_m, p: ConnectorParams):
    """
    Carve dovetail pockets into the underside of a province mesh.

    ``x0_m, y0_m`` must be the same local origin (min easting / northing) that the
    province mesh was built with, so the pockets land under the border tabs.

    Done in manifold space, like the coastline cut in ``LIB_terrain_trim``:
    ``trimesh.boolean`` (4.0 has no manifold3d engine) returned a New Brunswick
    terrain solid that was no longer watertight, where the same cut through
    manifold3d stayed closed and removed the same 40 mm3.
    """
    tool = None
    for lobe in lobes_m:
        lobe_c = lobe.buffer(p.clearance_mm * p.mpm)     # pocket slightly oversized
        cutter = _extrude_man(_to_mm(lobe_c, p.mpm, x0_m, y0_m),
                              -0.1, p.dovetail_mm)
        if cutter is not None:
            tool = cutter if tool is None else tool + cutter
    if tool is None:
        return mesh
    notched = _man_to_trimesh(_trimesh_to_man(mesh) - tool)
    if notched is None:
        raise ValueError("the pockets removed the whole piece; check the "
                         "origin the mesh was built with.")
    return notched


def _trimesh_to_man(mesh):
    """Trimesh -> manifold3d.Manifold for boolean ops."""
    return m3d.Manifold(m3d.Mesh(
        vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint32)))


# ---------------------------------------------------------------------------
# Convenience loaders / CLI
# ---------------------------------------------------------------------------
def _repair(geom):
    """Return a valid polygonal geometry (exported rings are sometimes invalid)."""
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    parts = _polys(geom)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _safe_union(geoms):
    """Union geometries, repairing invalid input (GEOS side-location conflicts)."""
    clean = [g for g in (_repair(g) for g in geoms) if g is not None]
    if not clean:
        return None
    try:
        return unary_union(clean)
    except Exception:
        # Zero-width buffer nudges stubborn topology into a unionable state.
        return unary_union([g.buffer(0) for g in clean])


#: The lobes as placed, beside ``borders.geojson`` in a saved borders folder.
CONNECTORS_NAME = "connectors.geojson"


def _polys_of(geoms):
    """Every polygon part of a sequence of geometries, in order."""
    return [poly for geom in geoms for poly in _polys(geom)]


def _load_union_m(path):
    gdf = gpd.read_file(path).to_crs(EPSG_METRIC)
    return _safe_union(list(gdf.geometry))


def build_border_from_dir(border_dir, out_path, p: ConnectorParams):
    """
    Read ``<border_dir>/borders.geojson`` (+ sibling ``name_*.geojson`` provinces)
    and write the border STL with dovetail lobes.  Returns (mesh, n_lobes).

    When the folder holds ``connectors.geojson`` -- the lobes as placed, by hand
    or otherwise, in the terrain editor -- those are used as they are rather
    than generated again, so the bar keys into the pockets that were carved.
    """
    border_dir = Path(border_dir)
    border_geom_m = _load_union_m(border_dir / "borders.geojson")

    placed = border_dir / CONNECTORS_NAME
    if placed.exists():
        lobes = _polys_of(gpd.read_file(placed).to_crs(EPSG_METRIC).geometry)
    else:
        prov_paths = sorted(border_dir.glob("name_*.geojson"))
        prov_union_m = (_safe_union([_load_union_m(pp) for pp in prov_paths])
                        if prov_paths else None)
        lobes = generate_lobes(border_geom_m, prov_union_m, p)
    mesh  = build_border_mesh(border_geom_m, lobes, p)
    if mesh is not None:
        mesh.export(str(out_path))
    return mesh, len(lobes)


def main():
    ap = argparse.ArgumentParser(
        description="Build the border STL with dovetail back-spline connectors.")
    ap.add_argument("border_dir",
                    help="Folder containing borders.geojson (+ name_*.geojson)")
    ap.add_argument("-o", "--out", default=None,
                    help="Output STL path (default: <border_dir>/border_connectors.stl)")
    saved = load_connector_params(*settings_files())
    ap.add_argument("--scale",     type=float, default=saved.scale)
    ap.add_argument("--thickness", type=float, default=saved.thickness_mm,
                    help="border height mm")
    ap.add_argument("--dovetail",  type=float, default=saved.dovetail_mm,
                    help="dovetail/pocket mm")
    ap.add_argument("--spacing",   type=float, default=14.0)
    ap.add_argument("--neck",      type=float, default=1.6)
    ap.add_argument("--mouth",     type=float, default=3.2)
    ap.add_argument("--reach",     type=float, default=3.0)
    ap.add_argument("--clearance", type=float, default=0.2)
    args = ap.parse_args()

    p = ConnectorParams(
        scale=args.scale, thickness_mm=args.thickness, dovetail_mm=args.dovetail,
        spacing_mm=args.spacing, neck_mm=args.neck, mouth_mm=args.mouth,
        reach_mm=args.reach, clearance_mm=args.clearance,
    )
    out = args.out or str(Path(args.border_dir) / "border_connectors.stl")
    mesh, n = build_border_from_dir(args.border_dir, out, p)
    if mesh is None:
        print("No border geometry produced.")
    else:
        print(f"Wrote {out}  ({n} dovetail lobes, "
              f"{len(mesh.vertices):,} verts, watertight={mesh.is_watertight})")


if __name__ == "__main__":
    main()
