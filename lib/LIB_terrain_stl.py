#!/usr/bin/env python3
"""
LIB_terrain_stl.py

Turn a prepared DEM GeoTIFF into a watertight, printable terrain solid.

This is the last raster step and the first mesh step: elevations in, STL out.
It replaces the DEMto3D QGIS plugin for this project (see ``USING_DEMTO3D.md``),
and unlike the plugin it can cut the **coastline directly** rather than leaving a
rectangular skirt for ``LIB_terrain_trim`` to remove afterwards.

    from lib.LIB_terrain_stl import terrain_to_stl, TerrainParams

    res = terrain_to_stl("Data/Clips/NL_3347.tif",
                         boundary="Data/JSon-Provinces-Orig/CAN_Newfoundland and Labrador.geojson",
                         out_path="print/newfoundland.stl",
                         params=TerrainParams(z_exaggeration=30.0))
    print(res.extent_mm, res.triangles, res.watertight)

Give it a raster that is already clipped, reprojected and coarsened
------------------------------------------------------------------
The sampling grid *is* the raster's grid, so resolution is chosen upstream:

    LIB_raster_reproject  (warp to the print CRS, coarsen to the print pitch)
        -> LIB_geotiff_clip   (cut to the province)
            -> LIB_terrain_stl    (this module)

``UI_terrain_editor.py`` drives the first two.  Keeping resampling out of here
means there is exactly one place that knows how to resample a DEM, and it is the
one that does it nodata-aware.

A grid mesh, not a triangulation
--------------------------------
``REQUIREMENTS_AND_DESIGN.md`` §5.6 specifies Delaunay over masked pixel centres
and records three watertightness bugs it caused: walls built from polygon rings
an unconstrained triangulation did not follow, winding-dependent edge counting,
and pinch faces where a coastline narrows to a one-triangle spit.

This module keeps the DEM's own grid instead -- two triangles per cell, a mirrored
base, and four walls -- so the topology is known analytically and **all three of
those bugs are structurally impossible**.  There is no triangulation to disagree
with the rings, no winding to misread, and no scattered points to pinch.  The
coastline is then cut by a boolean against an extruded prism, which is exact.

The cost is triangles: a grid does not adapt to flat ground, so it spends about
twice what a good triangulation would for the same fidelity.  That is controlled
by the resolution of the raster you hand in, and guarded by ``max_triangles``.

Vertical exaggeration is not optional
-------------------------------------
At 1:8,000,000 one print-mm is 8 km, so Newfoundland's 1578 m of relief is
**0.20 mm** at true scale -- invisible.  ``z_exaggeration`` defaults to
``terrain.exaggeration`` in ``project_config.json`` (10, giving 2.0 mm of relief
there; 30 gives 5.9 mm).  Every piece of one map must share the same exaggeration
*and* the same ``floor_m``, or neighbouring provinces will not meet at the seam
-- which is why the defaults live in the project config rather than in a call.

The result knows its own frame
------------------------------
``LIB_stl_reconcile`` exists because DEMto3D writes STLs in a frame nobody
recorded, so the keying step has to recover it by footprint IoU with a
Y-flip search.  This module *builds* the mesh, so it knows the frame exactly and
returns it, writing a ``.frame.json`` sidecar next to the STL.  Downstream code
should read that instead of guessing.

**The sidecar records only what is true of that one piece.**  A piece's origin is
its own; the exaggeration, floor, scale and CRS are properties of the *map*, and
every piece of a map must share them or the pieces do not meet at the seam.
Writing them next to each piece would make thirteen copies of a value that must
never differ -- and thirteen chances for one to.  So they live in
``project_config.json``'s ``terrain`` section, read through
:func:`shared_settings`, and the sidecar carries a ``settings_id`` fingerprint of
them instead of the values.  A piece built under different settings is then
detectable (:func:`frame_matches_config`) rather than silently mixed into a map
it no longer fits.
"""

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import trimesh
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from lib.LIB_border_split import bed_turn
from lib.LIB_project_config import DEFAULTS as _DEFAULTS
from lib.LIB_project_config import build_params, load_files, settings_files
from lib.LIB_project_config import get as _get
from lib.LIB_project_config import shipped as _shipped
from lib.LIB_terrain_trim import _clip

log = logging.getLogger(__name__)

EPSG_METRIC = _shipped("crs.working")
DEFAULT_SCALE = _shipped("print.scale")

#: Defaults come from ``project_config.json``'s ``terrain`` section, so every
#: piece of a map is meshed the same way without anyone passing arguments.
#: See "Vertical exaggeration is not optional".
DEFAULT_EXAGGERATION = _shipped("terrain.exaggeration")
DEFAULT_BASE_MM = _shipped("terrain.base_mm")
DEFAULT_FLOOR_M = _shipped("terrain.floor_m")
DEFAULT_MAX_TRIANGLES = _shipped("terrain.max_triangles")


@dataclass
class TerrainParams:
    """How the solid is built."""
    scale: float = DEFAULT_SCALE            #: map scale denominator (1 : scale)
    z_exaggeration: float = DEFAULT_EXAGGERATION   #: height multiplier
    base_height_mm: float = DEFAULT_BASE_MM  #: solid base under the terrain
    floor_m: float = DEFAULT_FLOOR_M        #: elevation the base measures from (sea level)
    clamp_below_floor: bool = True          #: below-sea-level land sits at the floor
    buffer_mm: float = 0.0                  #: outward lip on the coastline cut
    max_triangles: int = DEFAULT_MAX_TRIANGLES   #: refuse before meshing beyond this
    expect_crs: str = EPSG_METRIC           #: raster CRS guard; None to skip
    dovetail_mm: float = _shipped("print.dovetail_mm")   #: base must clear this
    bed_mm: float = _shipped("print.bed_mm")
    bed_margin_mm: float = _shipped("print.bed_margin_mm")
    validate: bool = True                   #: watertight + positive volume gate
    band: int = 1

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0



#: The config key behind each field of :class:`TerrainParams`, for
#: :func:`load_terrain_params`.
_TERRAINPARAMS_KEYS = {
    "dovetail_mm": "print.dovetail_mm",
    "bed_mm": "print.bed_mm",
    "bed_margin_mm": "print.bed_margin_mm",
    "scale": "print.scale",
    "z_exaggeration": "terrain.exaggeration",
    "base_height_mm": "terrain.base_mm",
    "floor_m": "terrain.floor_m",
    "max_triangles": "terrain.max_triangles",
    "expect_crs": "crs.working",
}


def load_terrain_params(*config_files, **overrides):
    """
    A :class:`TerrainParams` filled from config files.

    ``config_files`` are read in order, later ones winning -- a window passes
    ``LIB_project_config.settings_files()``, the project file then this desk's
    local one.  A setting no file mentions keeps its built-in default, and
    ``overrides`` win over everything.  :class:`TerrainParams` built directly never
    reads a file: its defaults are the shipped terrain settings, the same on
    every desk.
    """
    return build_params(TerrainParams, _TERRAINPARAMS_KEYS, *config_files, **overrides)

@dataclass
class TerrainResult:
    """Result of :func:`terrain_to_stl` (for inspection / tests)."""
    mesh: object = None                 #: watertight trimesh
    path: str = None                    #: written STL, if ``out_path`` was given
    frame_path: str = None              #: the .frame.json sidecar
    #: The metres -> print-mm frame.  Together these *are* the affine, which is
    #: why nothing downstream needs to reconcile this piece.
    origin_m: tuple = (0.0, 0.0)
    mpm: float = 0.0
    crs: object = None
    floor_m: float = 0.0
    z_exaggeration: float = 0.0
    base_height_mm: float = 0.0         #: solid base under the terrain
    res_m: tuple = (0.0, 0.0)           #: the raster's pitch
    spacing_mm: float = 0.0             #: that pitch in print-mm
    grid: tuple = (0, 0)                #: (cols, rows) of sampled points
    extent_mm: tuple = (0.0, 0.0, 0.0)  #: (w, h, z) as printed
    bed_fits: bool = False
    #: The turn (degrees) it fits the bed at; 0 as it lies, None if never.
    bed_angle_deg: float = None
    triangles: int = 0
    volume_mm3: float = 0.0
    watertight: bool = False
    zmin_m: float = None                #: elevation range of the sampled terrain
    zmax_m: float = None
    cut_to_boundary: bool = False
    #: Fingerprint of the map-wide settings the piece was built under -- see
    #: :func:`settings_id`.  Set by :func:`terrain_to_stl` from the config it
    #: was handed, so the sidecar says what the piece *was* built with.
    settings_id: str = ""

    @property
    def frame(self) -> dict:
        """
        What the sidecar records: this piece's own facts, and nothing shared.

        ``origin_m`` is the only thing here that cannot be read from the project
        config, and the only thing that legitimately differs between pieces --
        it is where this province sits in metres.  Paired with the configured
        scale it *is* the metres->print-mm affine, which is what saves the
        keying step from reconciling.

        ``settings_id`` fingerprints the map-wide settings the piece was built
        with, so a piece built before someone changed the exaggeration can be
        spotted instead of quietly ruining an assembly.
        """
        return {
            "origin_m": list(self.origin_m),
            "settings_id": self.settings_id or settings_id(),
        }


def shared_settings(config=None) -> dict:
    """
    The map-wide terrain settings, from ``config``.

    One reader for the values every piece must agree on, so there is one place
    to change them and no per-piece copies to drift.  ``config`` is a merged
    configuration, as ``LIB_project_config.load_files`` returns it; without
    one, the shipped settings -- this never reads a file of its own accord.
    """
    source = _DEFAULTS if config is None else config
    def get(key):
        return _get(key, config=source)
    return {
        "scale": get("print.scale"),
        "crs": get("crs.working"),
        "exaggeration": get("terrain.exaggeration"),
        "base_mm": get("terrain.base_mm"),
        "floor_m": get("terrain.floor_m"),
        "spacing_mm": get("terrain.spacing_mm"),
    }


def settings_id(config=None) -> str:
    """
    A short fingerprint of :func:`shared_settings`.

    Recorded per piece in place of the settings themselves: it says *which* map a
    piece belongs to without copying the values that define it.
    """
    payload = json.dumps(shared_settings(config), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def frame_matches_config(frame, config=None) -> bool:
    """
    Whether a piece was built with the settings currently configured.

    False means the piece on disk predates a settings change: it will not meet
    its neighbours, and should be rebuilt rather than assembled.
    """
    if not isinstance(frame, dict):
        return False
    return frame.get("settings_id") == settings_id(config)


def read_frame(stl_path):
    """The sidecar beside an STL, or None if it has none."""
    path = Path(str(stl_path)).with_suffix(".frame.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def terrain_to_stl(raster, boundary=None, out_path=None, params=None,
                   config=None):
    """
    Build a terrain solid from a prepared DEM.

    Parameters
    ----------
    raster : str | pathlib.Path
        A DEM already clipped, reprojected and coarsened -- its grid becomes the
        sampling grid.  Must be in ``params.expect_crs`` unless that is None.
    boundary : str | pathlib.Path | shapely geometry | GeoDataFrame, optional
        Cut the solid to this coastline.  Omit for a rectangular slab (what
        DEMto3D produces), which is useful for comparing against old pieces.
    out_path : str | pathlib.Path, optional
        Write the STL here, plus a ``.frame.json`` sidecar beside it.
    params : TerrainParams, optional
    config : dict, optional
        The merged configuration the piece is built under, as
        ``LIB_project_config.load_files`` returns it.  Only its map-wide
        settings are used, fingerprinted into the sidecar's ``settings_id`` so
        a piece can later be told from one built under different settings.
        Defaults to the shipped settings.

    Returns
    -------
    TerrainResult

    Raises
    ------
    ValueError
        If the raster is in the wrong CRS, the base does not clear the dovetail
        pocket, the grid would exceed ``max_triangles``, the boundary removes
        everything, or the result fails the watertight gate.
    """
    params = params or TerrainParams()
    _check_base(params)

    with rasterio.open(str(raster)) as src:
        _check_crs(src, raster, params)
        _check_size(src, params)
        transform, crs, nodata = src.transform, src.crs, src.nodata
        elev = src.read(params.band).astype("float64")

    result = TerrainResult(
        mpm=params.mpm, crs=crs, floor_m=params.floor_m,
        z_exaggeration=params.z_exaggeration,
        base_height_mm=params.base_height_mm,
        res_m=(abs(transform.a), abs(transform.e)),
        spacing_mm=abs(transform.a) / params.mpm,
        grid=(elev.shape[1], elev.shape[0]),
        cut_to_boundary=boundary is not None,
        settings_id=settings_id(config))

    mesh = _build_slab(elev, transform, nodata, params, result)
    if boundary is not None:
        geom_m = _load_union_crs(boundary, crs)
        if geom_m is None or geom_m.is_empty:
            raise ValueError(f"{boundary}: no boundary geometry to cut with.")
        x0, y0 = result.origin_m
        matrix = [1.0 / params.mpm, 0, 0, 1.0 / params.mpm,
                  -x0 / params.mpm, -y0 / params.mpm]
        # Reuses LIB_terrain_trim's cutter: same boolean, in manifold space.  The
        # difference is that this module knows `matrix` outright instead of
        # recovering it by footprint IoU -- it built the mesh.
        mesh = _clip(mesh, geom_m, matrix, params.buffer_mm)

    _finish(mesh, result, params)
    if params.validate:
        _validate(result, raster)
    if out_path is not None:
        _write(mesh, result, out_path)

    log.info("Meshed %s: %d triangles, %.0f x %.0f x %.1f mm, watertight=%s.",
             Path(str(raster)).name, result.triangles, *result.extent_mm,
             result.watertight)
    return result


# ---------------------------------------------------------------------------
# Guards -- each refuses before doing expensive work
# ---------------------------------------------------------------------------

def _check_base(params):
    """The dovetail pocket is cut into the underside; the base must outlast it."""
    if params.base_height_mm <= params.dovetail_mm:
        raise ValueError(
            f"base_height_mm ({params.base_height_mm}) must exceed dovetail_mm "
            f"({params.dovetail_mm}), or the connector pocket cuts through the "
            "bottom of the piece.")


def _check_crs(src, raster, params):
    """
    The raster must be in the CRS the rest of the print pipeline uses.

    Not a formality: the same province is 175.7 x 137.0 print-mm in EPSG:3347 and
    181.1 x 131.1 in EPSG:3979 -- a 5.4 x 5.9 mm disagreement against a 1.6 mm
    dovetail neck.  Meshing the wrong one produces a piece that cannot key in,
    and nothing downstream would notice until it was printed.
    """
    if params.expect_crs is None or src.crs is None:
        return
    from rasterio.crs import CRS
    if src.crs != CRS.from_user_input(params.expect_crs):
        raise ValueError(
            f"{raster} is in {src.crs}, but the print pipeline builds in "
            f"{params.expect_crs}. Reproject it first (LIB_raster_reproject), "
            "or pass expect_crs=None if you really mean to mesh this frame.")


def _check_size(src, params):
    """Refuse an unprintable mesh from the metadata, before reading a pixel."""
    cols, rows = src.width, src.height
    triangles = _triangle_count(cols, rows)
    if triangles > params.max_triangles:
        raise ValueError(
            f"{cols}x{rows} would make {triangles:,} triangles, over "
            f"max_triangles ({params.max_triangles:,}). Coarsen the raster "
            "first (LIB_raster_reproject.downsample_geotiff) or raise the cap.")


def _triangle_count(cols, rows):
    """Top + base + the four walls, exactly."""
    cells = max(cols - 1, 0) * max(rows - 1, 0)
    walls = 2 * 2 * (max(cols - 1, 0) + max(rows - 1, 0))
    return 4 * cells + walls


# ---------------------------------------------------------------------------
# The slab
# ---------------------------------------------------------------------------


def triangle_count(cols, rows):
    """The triangles a ``cols`` x ``rows`` grid meshes to -- what
    ``max_triangles`` is checked against.  Public so the limit can be planned
    for before a raster is cut."""
    return _triangle_count(cols, rows)


def _build_slab(elev, transform, nodata, params, result):
    """
    Two triangles per cell on top, a mirrored base at z=0, and four walls.

    Vertices are the raster's **pixel centres**, so the solid is one pixel
    narrower than the raster's bounding box in each axis -- correct for a
    point-sampled surface.
    """
    rows, cols = elev.shape
    res_x, res_y = abs(transform.a), abs(transform.e)
    # Pixel centres in metres, then a local origin so the piece is built at (0,0)
    # in print-mm -- the project's convention (design doc §4.3).
    xs_m = transform.c + (np.arange(cols) + 0.5) * res_x
    ys_m = transform.f - (np.arange(rows) + 0.5) * res_y
    result.origin_m = (float(xs_m[0]), float(ys_m[-1]))

    mpm = params.mpm
    xs = (xs_m - result.origin_m[0]) / mpm
    ys = (ys_m - result.origin_m[1]) / mpm
    grid_x, grid_y = np.meshgrid(xs, ys)

    valid = np.ones(elev.shape, dtype=bool) if nodata is None else (elev != nodata)
    valid &= ~np.isnan(elev)
    if valid.any():
        result.zmin_m = float(elev[valid].min())
        result.zmax_m = float(elev[valid].max())

    height = _surface_mm(elev, valid, params)
    n = grid_x.size
    top = np.column_stack([grid_x.ravel(), grid_y.ravel(), height.ravel()])
    base = np.column_stack([grid_x.ravel(), grid_y.ravel(), np.zeros(n)])
    vertices = np.vstack([top, base])

    faces = np.vstack(_faces(cols, rows, n))
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def _surface_mm(elev, valid, params):
    """Elevation -> print-mm height above the build plate."""
    z = np.where(valid, elev, params.floor_m)
    if params.clamp_below_floor:
        # Below-sea-level land is real (-17.7 m in Labrador, -13 m in BC); without
        # this it would punch through the base instead of resting on it.
        z = np.maximum(z, params.floor_m)
    height = ((z - params.floor_m) * params.z_exaggeration / params.mpm
              + params.base_height_mm)
    if height.min() < 0.0:
        # Only reachable unclamped. The shell still closes, so the watertight
        # gate passes it -- but the surface has crossed the base plane and the
        # solid self-intersects, which no gate here detects. Say so.
        log.warning(
            "Terrain reaches %.2f mm below the base plane: %.0f m at x%.0f "
            "exaggeration exceeds the %.1f mm base. The solid self-intersects; "
            "clamp_below_floor=True avoids this.",
            -height.min(), z.min(), params.z_exaggeration, params.base_height_mm)
    return height


def surface_sampler(raster, params=None):
    """
    A function ``(xs_m, ys_m) -> print-mm heights`` over ``raster``'s surface.

    The heights are the ones :func:`terrain_to_stl` would give the top of a
    solid meshed from the same raster -- the same pixel-centre grid, linearly
    interpolated between centres, through the same elevation -> mm rule -- so
    a piece built beside the solid (the border bar) can stand as tall as the
    ground it meets.  Points are in the raster's CRS; off the raster, or on
    nodata, the ground is at the floor (the bare base).
    """
    params = params or TerrainParams()
    with rasterio.open(str(raster)) as src:
        transform, nodata = src.transform, src.nodata
        elev = src.read(params.band).astype("float64")
    valid = np.ones(elev.shape, dtype=bool) if nodata is None else (elev != nodata)
    valid &= ~np.isnan(elev)
    height = _surface_mm(elev, valid, params)
    rows, cols = height.shape
    # Pad with the bare base so points past the outermost centres fall to it.
    padded = np.pad(height, 1, constant_values=params.base_height_mm)
    inverse = ~transform

    def sample(xs_m, ys_m):
        col, row = inverse * (np.asarray(xs_m, dtype="float64"),
                              np.asarray(ys_m, dtype="float64"))
        # Pixel centres sit at +0.5; +1 more for the pad.
        c = np.clip(np.asarray(col) + 0.5, 0.0, cols + 1.0)
        r = np.clip(np.asarray(row) + 0.5, 0.0, rows + 1.0)
        c0 = np.minimum(np.floor(c).astype(int), cols)
        r0 = np.minimum(np.floor(r).astype(int), rows)
        fc, fr = c - c0, r - r0
        top = padded[r0, c0] * (1 - fc) + padded[r0, c0 + 1] * fc
        bottom = padded[r0 + 1, c0] * (1 - fc) + padded[r0 + 1, c0 + 1] * fc
        return top * (1 - fr) + bottom * fr

    return sample


def _faces(cols, rows, n):
    """
    Index arrays for the closed shell.

    Winding is chosen so every normal points outward; the orders below are what
    produce a positive volume, which the watertight gate then checks rather than
    trusting.  ``n`` offsets into the base copy of the vertex grid.
    """
    def cell_corners(offset):
        top_left = (np.arange(rows - 1)[:, None] * cols
                    + np.arange(cols - 1)[None, :]).ravel() + offset
        return top_left, top_left + 1, top_left + cols, top_left + cols + 1

    a, b, c, d = cell_corners(0)                       # terrain surface
    faces = [np.column_stack([a, c, b]), np.column_stack([b, c, d])]
    a, b, c, d = cell_corners(n)                       # base, wound the other way
    faces += [np.column_stack([a, b, c]), np.column_stack([b, d, c])]

    def wall(ring):
        i, j = ring[:-1], ring[1:]
        return [np.column_stack([i, j, i + n]), np.column_stack([j, j + n, i + n])]

    # The four edges of the grid, traversed as one consistent loop.
    faces += wall(np.arange(cols))                              # north
    faces += wall(np.arange(cols - 1, rows * cols, cols))        # east
    faces += wall(np.arange(rows * cols - 1, rows * cols - cols - 1, -1))   # south
    faces += wall(np.arange((rows - 1) * cols, -1, -cols))       # west
    return faces


# ---------------------------------------------------------------------------
# Finish, validate, write
# ---------------------------------------------------------------------------

def _finish(mesh, result, params):
    result.mesh = mesh
    result.triangles = int(len(mesh.faces))
    result.volume_mm3 = float(mesh.volume)
    result.watertight = bool(mesh.is_watertight)
    extents = mesh.extents
    if extents is None:          # an empty mesh has no extents; let the gate,
        result.extent_mm = (0.0, 0.0, 0.0)   # not a TypeError, be what reports it
        result.bed_fits = False
        return
    result.extent_mm = (float(extents[0]), float(extents[1]), float(extents[2]))
    usable = params.bed_mm - 2 * params.bed_margin_mm
    # At any turn, as a slicer would place it -- diagonally included -- not
    # only as it lies: a province that will not go on square often will
    # corner to corner.
    result.bed_angle_deg = bed_turn(mesh.vertices, usable)
    result.bed_fits = result.bed_angle_deg is not None


def _validate(result, raster):
    """FR-4.7: nothing leaves here that a printer cannot slice."""
    if result.triangles == 0:
        raise ValueError(f"{raster}: produced an empty mesh.")
    if not result.watertight:
        raise ValueError(
            f"{raster}: mesh is not watertight ({result.triangles} triangles).")
    if result.volume_mm3 <= 0.0:
        raise ValueError(
            f"{raster}: non-positive volume ({result.volume_mm3:.3f} mm3); "
            "the shell is probably inside out.")


def _write(mesh, result, out_path):
    """Write the STL and, beside it, the frame nobody should have to guess."""
    out_path = Path(str(out_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out_path))
    frame_path = out_path.with_suffix(".frame.json")
    frame_path.write_text(json.dumps(result.frame, indent=2), encoding="utf-8")
    result.path = str(out_path)
    result.frame_path = str(frame_path)


def _load_union_crs(boundary, crs):
    """Dissolve the boundary to one geometry in the raster's CRS."""
    if isinstance(boundary, BaseGeometry):
        return boundary
    frame = boundary if isinstance(boundary, gpd.GeoDataFrame) \
        else gpd.read_file(str(boundary))
    if frame.empty:
        return None
    if frame.crs is not None and crs is not None and frame.crs != crs:
        frame = frame.to_crs(crs)
    return unary_union(frame.geometry.values)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build a printable terrain STL from a prepared DEM GeoTIFF.")
    # The saved settings are the defaults here, as in a window.
    saved = load_terrain_params(*settings_files())
    ap.add_argument("raster", help="DEM, already clipped/reprojected/coarsened")
    ap.add_argument("out", help="output STL")
    ap.add_argument("-b", "--boundary",
                    help="cut the solid to this coastline GeoJSON")
    ap.add_argument("--scale", type=float, default=saved.scale,
                    help="map scale denominator")
    ap.add_argument("--exaggeration", type=float, default=saved.z_exaggeration,
                    help="vertical exaggeration (x1 is invisibly flat at 1:8M)")
    ap.add_argument("--base", type=float, default=saved.base_height_mm,
                    metavar="MM", help="solid base under the terrain")
    ap.add_argument("--floor", type=float, default=saved.floor_m, metavar="M",
                    help="elevation the base measures from (default sea level)")
    ap.add_argument("--buffer", type=float, default=0.0, metavar="MM",
                    help="outward lip on the coastline cut")
    ap.add_argument("--max-triangles", type=int,
                    default=saved.max_triangles)
    ap.add_argument("--any-crs", action="store_true",
                    help="skip the print-CRS guard")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = terrain_to_stl(
        args.raster, args.boundary, args.out,
        load_terrain_params(
            *settings_files(), scale=args.scale,
            z_exaggeration=args.exaggeration, base_height_mm=args.base,
            floor_m=args.floor, buffer_mm=args.buffer,
            max_triangles=args.max_triangles,
            expect_crs=None if args.any_crs else EPSG_METRIC),
        config=load_files(*settings_files()))
    print(f"{res.triangles:,} triangles  "
          f"{res.extent_mm[0]:.0f} x {res.extent_mm[1]:.0f} x "
          f"{res.extent_mm[2]:.1f} mm  "
          f"{'fits' if res.bed_fits else 'DOES NOT FIT'} the bed  "
          f"watertight={res.watertight}  -> {res.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
