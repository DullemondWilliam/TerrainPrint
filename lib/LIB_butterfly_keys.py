#!/usr/bin/env python3
"""
LIB_butterfly_keys.py

Join two neighbouring province STLs directly with butterfly (double-dovetail)
keys: a slot in each piece's underside, and a separate key that drops into both.

``LIB_border_connectors`` joins provinces *through* a printed border bar.  This
joins two pieces that meet with no bar between them: each key is a bowtie
centred on the seam -- a dovetail reaching ``reach_mm`` into each province from a
``neck_mm`` waist out to a ``mouth_mm`` end -- so once it is pressed into the two
slots from the back, the wide ends are captured behind the waist and the pieces
cannot pull apart in the map plane.

    from lib.LIB_butterfly_keys import fit_keys
    res = fit_keys("print/NB.stl", "Data/…/CAN_New Brunswick.geojson",
                   "print/NS.stl", "Data/…/CAN_Nova Scotia.geojson",
                   out_dir="keyed")
    # keyed/NB_keyed.stl, keyed/NS_keyed.stl, keyed/butterfly_keys.stl

Frames
------
Each key is placed once, in EPSG:3347 metres, and cut into both pieces from that
one polygon, so the two halves of a slot cannot disagree.  What has to be known
is how each STL's print-mm frame maps to metres:

* an STL built by ``LIB_terrain_stl`` carries a ``.frame.json`` sidecar whose
  ``origin_m`` *is* that mapping (with ``print.scale``), so nothing is guessed;
* any other STL is reconciled by footprint (``LIB_stl_reconcile``) and refused below ``min_fit_score``.

A sidecar frame is still checked against the GeoJSON by the same footprint
overlap, so an STL paired with the wrong province is caught either way.

The seam
--------
Found as the boundary the two GeoJSONs share (``LIB_shared_border``'s seam
finder).  Pieces that were simplified or trimmed separately may not share an
exact edge, so when they do not, the seam falls back to the part of A's boundary
within ``seam_tol_mm`` of B.

What is refused, and what is reported
-------------------------------------
A key whose footprint (with clearance) hangs off the land -- into the sea at a
coast, or past the end of a short seam -- would print as a slot open to the side
and a key half in air.  Those keys are skipped and listed in
:attr:`KeyResult.skipped`, never cut.  Every output mesh must be watertight with
positive volume (FR-4.7), or nothing is returned.

Pockets are cut in manifold3d rather than through ``trimesh.boolean``: trimesh
4.0 has no manifold engine, and its difference returned real terrain solids that
were no longer closed (see ``LIB_border_connectors.notch_province``).
"""

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import trimesh
from shapely.affinity import affine_transform
from shapely.geometry import MultiLineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union

from lib.LIB_border_connectors import (ConnectorParams, _extrude_man,
                                       _man_to_trimesh, _repair,
                                       _trimesh_to_man)
from lib.LIB_project_config import build_params
from lib.LIB_project_config import shipped as _shipped
from lib.LIB_shared_border import _shared_seam
from lib.LIB_stl_reconcile import _iou, base_footprint, reconcile
from lib.LIB_terrain_stl import read_frame

log = logging.getLogger(__name__)

EPSG_METRIC = _shipped("crs.working")
#: The border connectors' dovetail profile: one opinion about what locks.
_CP = ConnectorParams()
#: A pocket cutter starts this far below the base, so the boolean sees an
#: intersecting solid rather than a coplanar face.
_UNDERCUT_MM = 0.1
#: Gap between keys laid out on the bed in the key STL.
_LAYOUT_GAP_MM = 2.0


@dataclass
class KeyParams:
    """Butterfly key geometry.  All lengths are **print-mm**."""
    scale: float = _shipped("print.scale")            #: map scale denominator
    depth_mm: float = _shipped("print.dovetail_mm")   #: slot depth into each piece
    neck_mm: float = _CP.neck_mm                  #: key width at the seam
    mouth_mm: float = _CP.mouth_mm                #: key width at each end
    reach_mm: float = _CP.reach_mm                #: seam to each end
    clearance_mm: float = _CP.clearance_mm        #: slot oversize, per side
    #: The key is printed this much shorter than the slot is deep, so it seats
    #: below the base instead of standing proud of it.
    height_clearance_mm: float = 0.1
    spacing_mm: float = _CP.spacing_mm            #: auto-placement pitch
    #: Keep auto-placed keys this far from a seam's ends (the coast or a triple
    #: junction); defaults to the key's own half-width.
    end_margin_mm: float | None = None
    #: How far apart two boundaries may be and still count as one seam.
    seam_tol_mm: float = 0.1
    #: Fraction of a key that may hang off the land before it is refused.
    max_overhang: float = 0.02
    min_fit_score: float = 0.90                   #: STL/GeoJSON reconciliation

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def key_height_mm(self) -> float:
        return self.depth_mm - self.height_clearance_mm

    @property
    def margin_mm(self) -> float:
        if self.end_margin_mm is not None:
            return self.end_margin_mm
        return self.mouth_mm / 2.0 + self.clearance_mm

    def validate(self) -> None:
        if self.depth_mm <= 0 or self.key_height_mm <= 0:
            raise ValueError("depth_mm must exceed height_clearance_mm.")
        if not 0 < self.neck_mm < self.mouth_mm:
            raise ValueError("a dovetail needs 0 < neck_mm < mouth_mm; "
                             "otherwise nothing is captured behind the waist.")
        if self.reach_mm <= 0 or self.spacing_mm <= 0:
            raise ValueError("reach_mm and spacing_mm must be positive.")


# ---------------------------------------------------------------------------
# Provinces and their frames
# ---------------------------------------------------------------------------

#: The config key behind each field of :class:`KeyParams`, for
#: :func:`load_key_params`.
_KEYPARAMS_KEYS = {
    "scale": "print.scale",
    "depth_mm": "print.dovetail_mm",
}


def load_key_params(*config_files, **overrides):
    """
    A :class:`KeyParams` filled from config files.

    ``config_files`` are read in order, later ones winning -- a window passes
    ``LIB_project_config.settings_files()``, the project file then this desk's
    local one.  A setting no file mentions keeps its built-in default, and
    ``overrides`` win over everything.  :class:`KeyParams` built directly never
    reads a file: its defaults are the shipped butterfly-key settings, the same on
    every desk.
    """
    return build_params(KeyParams, _KEYPARAMS_KEYS, *config_files, **overrides)

@dataclass
class Province:
    """One piece: its footprint in metres, its mesh, and how the two align."""
    name: str
    geom_m: object                   #: footprint, EPSG:3347 metres
    mesh: object                     #: trimesh.Trimesh as loaded
    matrix: list                     #: shapely affine, metres -> mesh print-mm
    base_z_mm: float                 #: z of the flat base
    method: str                      #: "sidecar" or "reconciled"
    score: float                     #: footprint overlap of the fit (0..1)
    source_crs: object = None        #: what the GeoJSON arrived in


def load_province(stl, geojson, params=None, name=None) -> Province:
    """
    Read one piece and work out how its STL sits over its GeoJSON.

    ``stl`` is a path or a Trimesh; ``geojson`` a path, GeoDataFrame or bare
    geometry (assumed EPSG:3347).  A path STL with a ``.frame.json`` sidecar is
    placed from it; anything else is reconciled by footprint.
    """
    params = params or KeyParams()
    geom_m, source_crs = _load_geom(geojson)
    if geom_m is None:
        raise ValueError(f"{geojson}: no polygon to work with.")
    mesh = _load_mesh(stl)
    if name is None:
        name = Path(str(stl)).stem if not isinstance(stl, trimesh.Trimesh) \
            else "province"
    frame = None if isinstance(stl, trimesh.Trimesh) else read_frame(stl)
    if frame and "origin_m" in frame:
        x0, y0 = frame["origin_m"]
        mpm = params.mpm
        matrix = [1.0 / mpm, 0.0, 0.0, 1.0 / mpm, -x0 / mpm, -y0 / mpm]
        score = _footprint_score(mesh, geom_m, matrix)
        if score < params.min_fit_score:
            raise ValueError(
                f"{name}: its frame sidecar does not put it over this GeoJSON "
                f"(footprint overlap {score:.2f} < {params.min_fit_score:.2f}). "
                "Is it paired with the right province, at the right scale?")
        return Province(name, geom_m, mesh, matrix,
                        float(mesh.bounds[0][2]), "sidecar", score, source_crs)
    report, matrix = reconcile(name, mesh, geom_m, _connector(params),
                               params.min_fit_score)
    return Province(name, geom_m, mesh, matrix, report.base_z_mm,
                    "reconciled", report.score, source_crs)


def _footprint_score(mesh, geom_m, matrix) -> float:
    footprint = base_footprint(mesh)
    if footprint is None or footprint.is_empty:
        return 0.0
    return _iou(footprint, affine_transform(geom_m, matrix))


def _connector(params) -> ConnectorParams:
    """The ConnectorParams the shared reconciler reads (it wants ``mpm``)."""
    return ConnectorParams(scale=params.scale)


# ---------------------------------------------------------------------------
# The seam, and where keys go along it
# ---------------------------------------------------------------------------
def find_seam(geom_a, geom_b, params=None):
    """
    The line two footprints share, as a MultiLineString in metres, or None.

    Exact first; if the boundaries do not coincide (pieces simplified or
    trimmed separately), the part of A's boundary within ``seam_tol_mm`` of B.
    """
    params = params or KeyParams()
    seam = _shared_seam(geom_a, geom_b)
    if seam is not None:
        return seam
    near = geom_a.boundary.intersection(geom_b.buffer(params.seam_tol_mm
                                                      * params.mpm))
    lines = [g for g in getattr(near, "geoms", [near])
             if g.geom_type in ("LineString", "LinearRing") and g.length > 0]
    if not lines:
        return None
    merged = linemerge(lines)
    return merged if merged.geom_type == "MultiLineString" \
        else MultiLineString([merged])


@dataclass(frozen=True)
class Placement:
    """Where a key sits: seam part ``part``, ``s_m`` metres along it."""
    part: int
    s_m: float


def auto_place(seam, params=None) -> list:
    """
    Keys every ``spacing_mm`` along each seam part, centred, clear of the ends.

    A part too short for one key with its margins gets one at its middle if
    the key fits at all, and none otherwise.
    """
    params = params or KeyParams()
    mpm = params.mpm
    spacing, margin = params.spacing_mm * mpm, params.margin_mm * mpm
    out = []
    for i, part in enumerate(seam.geoms):
        usable = part.length - 2 * margin
        if usable < 0:
            if part.length >= params.mouth_mm * mpm:
                out.append(Placement(i, part.length / 2.0))
            continue
        count = int(usable // spacing) + 1
        start = margin + (usable - (count - 1) * spacing) / 2.0
        out.extend(Placement(i, start + k * spacing) for k in range(count))
    return out


def snap(seam, x, y) -> Placement:
    """The seam point nearest (x, y), as a Placement."""
    point = Point(x, y)
    best = min(range(len(seam.geoms)),
               key=lambda i: seam.geoms[i].distance(point))
    return Placement(best, float(seam.geoms[best].project(point)))


def key_polygon(seam, placement, params=None, clearance=False):
    """
    The bowtie for one placement, in metres.

    Built from the seam's local tangent at the placement, so the waist lies
    along the seam and each dovetail reaches square into its own province.
    ``clearance=True`` gives the slot: the key grown by ``clearance_mm`` with
    mitred corners, so the dovetail angles stay true.
    """
    params = params or KeyParams()
    mpm = params.mpm
    part = seam.geoms[placement.part]
    s = min(max(placement.s_m, 0.0), part.length)
    eps = min(0.05 * mpm, part.length / 4.0) or 1e-6
    p = part.interpolate(s)
    a = part.interpolate(min(s + eps, part.length))
    b = part.interpolate(max(s - eps, 0.0))
    tx, ty = a.x - b.x, a.y - b.y
    norm = math.hypot(tx, ty) or 1.0
    tx, ty = tx / norm, ty / norm
    nx, ny = -ty, tx
    neck, mouth = params.neck_mm * mpm / 2, params.mouth_mm * mpm / 2
    reach = params.reach_mm * mpm

    def at(along, across):
        return (p.x + tx * along + nx * across, p.y + ty * along + ny * across)

    poly = Polygon([at(neck, 0), at(mouth, reach), at(-mouth, reach),
                    at(-neck, 0), at(-mouth, -reach), at(mouth, -reach)])
    if clearance:
        poly = poly.buffer(params.clearance_mm * mpm, join_style="mitre",
                           mitre_limit=5.0)
    return poly


@dataclass
class KeyCheck:
    """Whether one key can be cut, and why not."""
    placement: Placement
    polygon: object                 #: the key, metres
    slot: object                    #: the key with clearance, metres
    overhang: float = 0.0           #: fraction of the slot off the land
    overlaps: bool = False          #: slot collides with another key's slot
    ok: bool = True
    reason: str = ""


def check_keys(seam, placements, land, params=None) -> list:
    """
    Every placement with its polygon and verdict.

    Refused: a slot hanging off ``land`` (the two footprints' union) by more
    than ``max_overhang``, or one that collides with an earlier key's slot.
    """
    params = params or KeyParams()
    grown = land.buffer(params.seam_tol_mm * params.mpm)
    checks = []
    for placement in placements:
        key = key_polygon(seam, placement, params)
        slot = key_polygon(seam, placement, params, clearance=True)
        check = KeyCheck(placement, key, slot)
        check.overhang = slot.difference(grown).area / slot.area
        if check.overhang > params.max_overhang:
            check.ok = False
            check.reason = f"{check.overhang * 100:.0f}% off the land"
        elif any(c.ok and c.slot.intersects(slot) for c in checks):
            check.ok, check.overlaps = False, True
            check.reason = "overlaps another key"
        checks.append(check)
    return checks


# ---------------------------------------------------------------------------
# Cutting and building
# ---------------------------------------------------------------------------
def cut_slots(province: Province, slots_m, params=None):
    """
    The province mesh with every slot cut into its underside.

    Slots are mapped from metres through the province's own frame, so a
    flipped-Y STL is handled by its matrix rather than special-cased.  Only the
    part of a slot that lies under this piece removes anything, which is what
    lets one bowtie cut its half into each neighbour.
    """
    params = params or KeyParams()
    z0 = province.base_z_mm - _UNDERCUT_MM
    z1 = province.base_z_mm + params.depth_mm
    tool = None
    for slot in slots_m:
        cutter = _extrude_man(affine_transform(slot, province.matrix), z0, z1)
        if cutter is not None:
            tool = cutter if tool is None else tool + cutter
    if tool is None:
        return province.mesh
    cut = _man_to_trimesh(_trimesh_to_man(province.mesh) - tool)
    if cut is None:
        raise ValueError(f"{province.name}: the slots removed the whole piece.")
    return cut


def key_solid(params=None):
    """One key as a watertight solid, centred on its waist, in print-mm."""
    params = params or KeyParams()
    n, m, r = params.neck_mm / 2, params.mouth_mm / 2, params.reach_mm
    # Waist along x, dovetails along y -- the same shape key_polygon builds,
    # at the origin, so the printed key and the cut slot are one profile.
    poly = Polygon([(n, 0), (m, r), (-m, r), (-n, 0), (-m, -r), (m, -r)])
    return _man_to_trimesh(_extrude_man(poly, 0.0, params.key_height_mm))


def key_layout(count, params=None, bed_mm=None, margin_mm=None):
    """``count`` keys laid out in rows on the bed, as one mesh (or None)."""
    if count <= 0:
        return None
    params = params or KeyParams()
    bed = (_shipped("print.bed_mm") if bed_mm is None else bed_mm) \
        - 2 * (_shipped("print.bed_margin_mm") if margin_mm is None else margin_mm)
    solid = key_solid(params)
    pitch_x = params.mouth_mm + _LAYOUT_GAP_MM
    pitch_y = 2 * params.reach_mm + _LAYOUT_GAP_MM
    per_row = max(1, int((bed + _LAYOUT_GAP_MM) // pitch_x))
    copies = []
    for i in range(count):
        row, col = divmod(i, per_row)
        copy = solid.copy()
        copy.apply_translation([col * pitch_x, row * pitch_y, 0.0])
        copies.append(copy)
    return trimesh.util.concatenate(copies)


@dataclass
class KeyResult:
    """Result of :func:`fit_keys` (for inspection / tests)."""
    a: Province = None
    b: Province = None
    keyed_a: object = None          #: province A with its slots
    keyed_b: object = None
    keys_mesh: object = None        #: every key, laid out for the bed
    seam: object = None
    keys: list = field(default_factory=list)      #: KeyCheck, cut
    skipped: list = field(default_factory=list)   #: KeyCheck, refused
    params: KeyParams = None

    @property
    def origin_m(self) -> tuple:
        """Metres point at the assembly's print-mm (0, 0)."""
        x0, y0, _, _ = unary_union([self.a.geom_m, self.b.geom_m]).bounds
        return (x0, y0)


def fit_keys(stl_a, geojson_a, stl_b, geojson_b, params=None,
             placements=None, out_dir=None, names=None) -> KeyResult:
    """
    Slot two neighbouring province STLs and build the keys that join them.

    Parameters
    ----------
    stl_a, stl_b : str | Path | trimesh.Trimesh
    geojson_a, geojson_b : str | Path | GeoDataFrame | shapely geometry
    params : KeyParams, optional
    placements : list[Placement], optional
        Where the keys go; :func:`auto_place` along the seam if omitted.
    out_dir : str | Path, optional
        Writes ``<A>_keyed.stl``, ``<B>_keyed.stl`` and ``butterfly_keys.stl``.
    names : (str, str), optional
        Names for the two pieces; default to the STL stems.

    Raises
    ------
    ValueError
        If either piece cannot be aligned to its GeoJSON, the two share no
        seam, no key survives the checks, or any output fails the watertight
        gate.
    """
    params = params or KeyParams()
    params.validate()
    names = names or (None, None)
    a = load_province(stl_a, geojson_a, params, names[0])
    b = load_province(stl_b, geojson_b, params, names[1])
    return fit_loaded(a, b, params, placements, out_dir)


def fit_loaded(a: Province, b: Province, params=None, placements=None,
               out_dir=None) -> KeyResult:
    """:func:`fit_keys` for provinces already loaded (the harness's path)."""
    params = params or KeyParams()
    params.validate()
    seam = find_seam(a.geom_m, b.geom_m, params)
    if seam is None:
        raise ValueError(
            f"{a.name} and {b.name} share no seam (none within "
            f"{params.seam_tol_mm:.2f} mm). Butterfly keys join neighbours.")
    if placements is None:
        placements = auto_place(seam, params)
    checks = check_keys(seam, placements, unary_union([a.geom_m, b.geom_m]),
                        params)
    result = KeyResult(a=a, b=b, seam=seam, params=params,
                       keys=[c for c in checks if c.ok],
                       skipped=[c for c in checks if not c.ok])
    if not result.keys:
        raise ValueError("No key can be cut: "
                         + ("none placed." if not checks else
                            "; ".join(sorted({c.reason for c in checks}))))
    slots = [c.slot for c in result.keys]
    result.keyed_a = cut_slots(a, slots, params)
    result.keyed_b = cut_slots(b, slots, params)
    result.keys_mesh = key_layout(len(result.keys), params)
    for label, mesh in ((a.name, result.keyed_a), (b.name, result.keyed_b),
                        ("keys", result.keys_mesh)):
        _validate(mesh, label)
    if out_dir is not None:
        export(result, out_dir)
    log.info("Keyed %s to %s with %d butterfly key(s) (%d refused).",
             a.name, b.name, len(result.keys), len(result.skipped))
    return result


def export(result: KeyResult, out_dir) -> list:
    """Write both slotted pieces and the key layout; returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"{result.a.name}_keyed.stl",
             out_dir / f"{result.b.name}_keyed.stl",
             out_dir / "butterfly_keys.stl"]
    for path, mesh in zip(paths, (result.keyed_a, result.keyed_b,
                                  result.keys_mesh)):
        mesh.export(str(path))
    return paths


def assembly(result: KeyResult) -> list:
    """
    ``(label, mesh)`` for both slotted pieces and every key *in place*, in one
    print-mm frame (origin :attr:`KeyResult.origin_m`, base at z = 0).

    The view that shows a slot that missed its key, and what the tests use to
    check that none did.
    """
    mpm = result.params.mpm
    ox, oy = result.origin_m
    out = [(result.a.name, _to_common(result.keyed_a, result.a, ox, oy, mpm)),
           (result.b.name, _to_common(result.keyed_b, result.b, ox, oy, mpm))]
    to_mm = [1 / mpm, 0, 0, 1 / mpm, -ox / mpm, -oy / mpm]
    for i, check in enumerate(result.keys, start=1):
        solid = _man_to_trimesh(_extrude_man(
            affine_transform(check.polygon, to_mm), 0.0,
            result.params.key_height_mm))
        out.append((f"key {i}", solid))
    return out


def _to_common(mesh, province, ox, oy, mpm):
    """A piece moved from its own print-mm frame into the assembly's."""
    a_, _, _, e, xoff, yoff = province.matrix
    v = np.array(mesh.vertices, dtype=float)
    x_m = (v[:, 0] - xoff) / a_
    y_m = (v[:, 1] - yoff) / e
    v[:, 0] = (x_m - ox) / mpm
    v[:, 1] = (y_m - oy) / mpm
    v[:, 2] -= province.base_z_mm
    moved = trimesh.Trimesh(vertices=v, faces=np.array(mesh.faces),
                            process=False)
    if e < 0:                        # a mirror turns the shell inside out
        moved.invert()
    return moved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_geom(source):
    """``(polygonal geometry in EPSG:3347, source CRS)`` from a path/frame/geom."""
    if isinstance(source, BaseGeometry):
        return _repair(source), None
    frame = source if isinstance(source, gpd.GeoDataFrame) \
        else gpd.read_file(str(source))
    if frame.empty:
        return None, frame.crs
    if frame.crs is None:
        frame = frame.set_crs(_shipped("crs.source"))
    source_crs = frame.crs
    return _repair(unary_union(frame.to_crs(EPSG_METRIC).geometry.values)), \
        source_crs


def _load_mesh(stl):
    if isinstance(stl, trimesh.Trimesh):
        return stl
    loaded = trimesh.load(str(stl), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise ValueError(f"{stl} did not load as a single mesh.")
    return loaded


def _validate(mesh, name):
    """FR-4.7: nothing leaves here that a printer cannot slice."""
    if mesh is None or mesh.is_empty:
        raise ValueError(f"{name}: produced an empty mesh.")
    if not mesh.is_watertight or mesh.volume <= 0.0:
        raise ValueError(
            f"{name}: failed the watertight / positive-volume gate "
            f"(watertight={mesh.is_watertight}, volume={mesh.volume:.4f}).")
