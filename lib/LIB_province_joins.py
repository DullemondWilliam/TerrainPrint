#!/usr/bin/env python3
"""
LIB_province_joins.py

Join every pair of neighbouring province STLs on a map with butterfly keys.

``LIB_butterfly_keys`` joins **two** pieces.  A map has a dozen borders, and
every province has several neighbours, so the questions change: which borders
are there, is each piece on disk still the piece its border was drawn on, do
keys on two borders of one province collide, and how is every slot of a
province cut into **one** output STL.  This module answers them and leaves the
geometry of one key to ``LIB_butterfly_keys``.

    from lib.LIB_province_joins import (auto_plan, cut_all, export,
                                        find_joins, load_pieces, pair_folders)

    pairs, unpaired = pair_folders("Data/JSonProvinces-Bordered", "Data/STL")
    pieces, problems = load_pieces(pairs, params)
    joins = find_joins(pieces, params)
    plan = auto_plan(joins, params)
    result = cut_all(plan, pieces, joins, params)
    export(result, pieces, "Data/STL-Keyed", plan)
    # <province>_keyed.stl (+ .frame.json), butterfly_keys_01.stl …, join_plan.json

The piece on disk must be the piece the border was drawn on
-----------------------------------------------------------
``load_province`` checks an STL against its GeoJSON by footprint overlap, which
catches the wrong province but not a province **re-cleaned after its STL was
built**: a seam moved by a millimetre still overlaps far above the 0.90 it
asks for, and every slot along it would be cut a millimetre off the edge of
the piece.  So each border is also measured against the STLs themselves
(:func:`seam_agreement_mm`): the furthest any point of the seam lies from the
edge of each piece's base.  A terrain STL cut by its boundary agrees to well
under a tenth of a millimetre; a border further off than the slot clearance
marks its STL out of date, and its keys are refused rather than cut.

Placements are points, not positions along a seam
-------------------------------------------------
A :class:`~lib.LIB_butterfly_keys.Placement` means something only against the
exact seam it was made on.  :class:`JoinPlan` keeps each key as a point in
metres with the seam it was placed on, and snaps it back on load.  A border
whose seam has moved since keeps its keys but says so
(:meth:`JoinPlan.seam_moved`) -- the same reasoning the border ledger uses for
"cleaned".

One cut per province
--------------------
Every slot of every border a province is on goes into one ``cut_slots`` call,
and the same slot polygon is handed to both pieces of its border: each loses
only the half under it, so the two halves cannot disagree.  A cut is reused
from a previous result while the province's slots are unchanged, since one
large province is tens of seconds of boolean.

All lengths are **print-mm** through ``KeyParams``; geometry is in the working
metre CRS, as in ``LIB_butterfly_keys``.
"""

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from shapely.affinity import affine_transform
from shapely import wkt
from shapely.ops import unary_union
from shapely.strtree import STRtree

from lib.LIB_border_ledger import (LEDGER_NAME, MATCH_TOLERANCE_M, border_key,
                                   find_borders)
from lib.LIB_butterfly_keys import (KeyParams, KeyResult, _validate, assembly,
                                    auto_place, check_keys, cut_slots,
                                    key_plates, load_province, snap)
from lib.LIB_terrain_stl import frame_matches_config, read_frame

log = logging.getLogger(__name__)

#: The plan's file name, written beside the keyed STLs.
PLAN_NAME = "join_plan.json"
PLAN_VERSION = 1
#: Key plates are written as ``butterfly_keys_01.stl``, ``_02`` …
PLATE_STEM = "butterfly_keys"
KEYED_SUFFIX = "_keyed"
#: Endings the tools in the pipeline add to a province's file name, stripped
#: to pair a GeoJSON with its STL.
NAME_SUFFIXES = ("_simplified", "_trimmed", KEYED_SUFFIX)
#: Pitch the seam is sampled at for the agreement check, print-mm.
_SAMPLE_MM = 0.25
#: A base triangle may sit this fraction of the piece's height above its floor.
_BASE_REL_TOL = 1e-3


# ---------------------------------------------------------------------------
# Names and files
# ---------------------------------------------------------------------------
def province_name(stem: str) -> str:
    """``CAN_Alberta_simplified`` and ``CAN_Alberta_keyed`` are ``CAN_Alberta``."""
    changed = True
    while changed:
        changed = False
        for suffix in NAME_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem, changed = stem[:-len(suffix)], True
    return stem


def display_name(name: str) -> str:
    """``CAN_British Columbia`` reads as ``British Columbia``."""
    prefix, _, rest = name.partition("_")
    return rest if rest and prefix.isupper() else name


def geojson_files(folder) -> list:
    """The province files in ``folder``: not ledgers, plans or STL sidecars."""
    folder = Path(folder)
    found = {p for pattern in ("*.geojson", "*.json") for p in folder.glob(pattern)}
    return sorted(p for p in found
                  if p.name not in (LEDGER_NAME, PLAN_NAME)
                  and not p.name.endswith(".frame.json"))


def stl_files(folder) -> list:
    """The province STLs in ``folder``, leaving out key plates."""
    return sorted(p for p in Path(folder).glob("*.stl")
                  if not p.stem.startswith(f"{PLATE_STEM}_"))


def pair_files(geojsons, stls):
    """
    ``([(name, geojson, stl)], [what could not be paired])``.

    Paired by :func:`province_name`.  Two files that reduce to one name are a
    clash, reported rather than guessed between.
    """
    def index(paths, kind):
        by_name, clashes = {}, []
        for path in map(Path, paths):
            name = province_name(path.stem)
            if name in by_name:
                clashes.append(f"{display_name(name)}: two {kind} files "
                               f"({by_name[name].name}, {path.name})")
            else:
                by_name[name] = path
        return by_name, clashes

    gj, gj_clash = index(geojsons, "GeoJSON")
    st, st_clash = index(stls, "STL")
    pairs = [(name, gj[name], st[name]) for name in sorted(gj) if name in st]
    unpaired = (gj_clash + st_clash
                + [f"{gj[n].name}: no STL" for n in sorted(gj) if n not in st]
                + [f"{st[n].name}: no GeoJSON" for n in sorted(st) if n not in gj])
    return pairs, unpaired


def pair_folders(geojson_dir, stl_dir):
    """:func:`pair_files` over every province file in two folders."""
    return pair_files(geojson_files(geojson_dir), stl_files(stl_dir))


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------
@dataclass
class Piece:
    """One province: its files, and its STL placed over its GeoJSON."""
    name: str
    geojson: Path
    stl: Path
    province: object                 #: LIB_butterfly_keys.Province
    frame: dict = None               #: the STL's sidecar, if it has one
    _outline: object = field(default=None, repr=False, compare=False)

    @property
    def geom(self):
        """The footprint, metres."""
        return self.province.geom_m

    @property
    def label(self) -> str:
        return display_name(self.name)

    def outline_mm(self):
        """The edge of the STL's base, in its own print-mm frame (cached)."""
        if self._outline is None:
            self._outline = base_outline(self.province.mesh)
        return self._outline


def load_pieces(pairs, params=None, config=None, progress=None):
    """
    ``({name: Piece}, [problems])`` for ``(name, geojson, stl)`` pairs.

    A piece whose sidecar says it was built under other terrain settings than
    ``config`` (a merged configuration; the shipped settings if None) is
    refused: it will not meet its neighbours.  A sidecar with no fingerprint
    cannot say, and is accepted.  ``progress(done, total, name)`` is called
    before each piece.
    """
    params = params or KeyParams()
    pieces, problems = {}, []
    for i, (name, geojson, stl) in enumerate(pairs):
        if progress is not None:
            progress(i, len(pairs), name)
        frame = read_frame(stl)
        if frame and "settings_id" in frame \
                and not frame_matches_config(frame, config):
            problems.append(f"{display_name(name)}: built under different "
                            "terrain settings -- rebuild its STL")
            continue
        try:
            province = load_province(stl, geojson, params, name)
        except Exception as exc:          # any read/fit failure is reported
            problems.append(f"{display_name(name)}: {exc}")
            continue
        pieces[name] = Piece(name, Path(geojson), Path(stl), province, frame)
    if progress is not None:
        progress(len(pairs), len(pairs), "")
    return pieces, problems


def base_outline(mesh):
    """
    The rim of a mesh's flat base, as a MultiLineString in its own frame.

    The base edges used by exactly one base triangle -- the coast, lakes and
    every other edge of the piece's underside -- found from the faces alone,
    without the polygon union reconciliation pays for.
    """
    v = np.asarray(mesh.vertices, dtype=float)
    f = np.asarray(mesh.faces)
    if len(f) == 0:
        return None
    z = v[:, 2]
    zmin = float(z.min())
    tol = max((float(z.max()) - zmin) * _BASE_REL_TOL, 1e-6)
    base = f[np.all((z[f] - zmin) <= tol, axis=1)]
    if len(base) == 0:
        return None
    edges = np.sort(np.concatenate([base[:, [0, 1]], base[:, [1, 2]],
                                    base[:, [2, 0]]]), axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    rim = unique[counts == 1]
    if len(rim) == 0:
        return None
    return shapely.multilinestrings(shapely.linestrings(v[rim][:, :, :2]))


def seam_agreement_mm(piece: Piece, seam, params=None) -> float:
    """
    The furthest any point of ``seam`` (metres) lies from the edge of the
    piece's base, in print-mm: about zero when the STL was cut by the GeoJSON
    the seam came from.
    """
    params = params or KeyParams()
    outline = piece.outline_mm()
    if outline is None:
        return math.inf
    seam_mm = affine_transform(seam, piece.province.matrix)
    points = _samples(seam_mm, _SAMPLE_MM)
    segments = shapely.get_parts(outline)
    tree = STRtree(segments)
    _, distances = tree.query_nearest(points, return_distance=True,
                                      all_matches=False)
    return float(np.max(distances)) if len(distances) else math.inf


def agreement_tolerance_mm(params) -> float:
    """How far a seam may sit off a piece's edge: what the slot clearance
    absorbs, and never tighter than the seam tolerance."""
    return max(params.clearance_mm, params.seam_tol_mm)


def _samples(lines, step):
    points = []
    for part in shapely.get_parts(lines):
        n = max(2, int(math.ceil(part.length / step)) + 1)
        points.extend(shapely.line_interpolate_point(
            part, np.linspace(0.0, part.length, n)))
    return np.array(points, dtype=object)


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------
@dataclass
class Join:
    """One border two loaded pieces share."""
    a: str                           #: first province name (sorts first)
    b: str
    seam: object                     #: MultiLineString, metres
    status: str = "unknown"          #: the border ledger's word for it
    agreement_mm: float = 0.0        #: see seam_agreement_mm
    tolerance_mm: float = 0.0

    @property
    def key(self) -> str:
        return border_key(self.a, self.b)

    @property
    def label(self) -> str:
        return f"{display_name(self.a)} — {display_name(self.b)}"

    @property
    def stale(self) -> bool:
        """An STL here is not the piece this border was drawn on."""
        return self.agreement_mm > self.tolerance_mm

    def involves(self, name) -> bool:
        return name in (self.a, self.b)


def find_joins(pieces, params=None, ledger=None) -> list:
    """
    Every border between the loaded pieces, with how well each agrees with
    the STLs.  ``ledger`` (a BorderLedger) adds whether it was cleaned.
    """
    params = params or KeyParams()
    borders = find_borders({name: p.geom for name, p in pieces.items()})
    tolerance = agreement_tolerance_mm(params)
    joins = []
    for border in borders:
        agreement = max(seam_agreement_mm(pieces[name], border.seam, params)
                        for name in (border.a, border.b))
        status = ledger.status(border) if ledger is not None else "unknown"
        joins.append(Join(border.a, border.b, border.seam, status, agreement,
                          tolerance))
    return joins


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
@dataclass
class JoinPlan:
    """Where the keys go: points in metres, by border, with the seam each
    border's keys were placed on."""
    keys: dict = field(default_factory=dict)     #: border key -> [(x, y)]
    seams: dict = field(default_factory=dict)    #: border key -> seam WKT

    def copy(self) -> "JoinPlan":
        return JoinPlan({k: list(v) for k, v in self.keys.items()},
                        dict(self.seams))

    def points(self, key) -> list:
        return list(self.keys.get(key, []))

    def set_points(self, join: Join, points) -> None:
        points = [(float(x), float(y)) for x, y in points]
        if points:
            self.keys[join.key] = points
            self.seams[join.key] = join.seam.wkt
        else:
            self.keys.pop(join.key, None)
            self.seams.pop(join.key, None)

    def placements(self, join: Join) -> list:
        return [snap(join.seam, x, y) for x, y in self.points(join.key)]

    def seam_moved(self, join: Join) -> bool:
        """The border has moved since its keys were placed."""
        recorded = self.seams.get(join.key)
        if recorded is None:
            return False
        try:
            return join.seam.hausdorff_distance(wkt.loads(recorded)) \
                > MATCH_TOLERANCE_M
        except Exception:
            return True

    @property
    def count(self) -> int:
        return sum(len(points) for points in self.keys.values())

    def save(self, path, params=None) -> Path:
        data = {"version": PLAN_VERSION,
                "borders": {key: {"keys": [list(p) for p in points],
                                  "seam_wkt": self.seams.get(key)}
                            for key, points in sorted(self.keys.items())}}
        if params is not None:
            data["params"] = {name: getattr(params, name) for name in
                              ("depth_mm", "neck_mm", "mouth_mm", "reach_mm",
                               "clearance_mm", "height_clearance_mm",
                               "spacing_mm", "spares", "scale")}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path) -> "JoinPlan":
        """The plan in ``path``; empty if there is none or it is unreadable."""
        plan = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            borders = data["borders"]
        except (OSError, ValueError, KeyError, TypeError):
            return plan
        for key, entry in borders.items():
            try:
                points = [(float(x), float(y)) for x, y in entry["keys"]]
            except (KeyError, TypeError, ValueError):
                continue
            if points:
                plan.keys[key] = points
                if entry.get("seam_wkt"):
                    plan.seams[key] = entry["seam_wkt"]
        return plan


def key_point(seam, placement) -> tuple:
    point = seam.geoms[placement.part].interpolate(placement.s_m)
    return (point.x, point.y)


def auto_plan(joins, params=None, plan=None, only=None) -> JoinPlan:
    """
    ``plan`` (or an empty one) with keys every ``spacing_mm`` along each
    border -- or only the borders whose keys are in ``only``.
    """
    params = params or KeyParams()
    plan = plan.copy() if plan is not None else JoinPlan()
    for join in joins:
        if only is not None and join.key not in only:
            continue
        plan.set_points(join, [key_point(join.seam, p)
                               for p in auto_place(join.seam, params)])
    return plan


def check_plan(plan, pieces, joins, params=None, fits=None) -> dict:
    """
    ``{border key: [KeyCheck]}`` for every key in the plan.

    On top of the pairwise rules (on the land, clear of its neighbours on the
    same border), a key is refused if its slot cuts into a slot accepted on
    another border, into land the seam fit check (``fits``: border key ->
    FitReport) calls too narrow, or if its border's STLs are out of date.
    Borders are taken in order, so the earlier border keeps a contested spot.
    """
    params = params or KeyParams()
    params.validate()
    taken = []
    out = {}
    for join in joins:
        placements = plan.placements(join)
        if not placements:
            out[join.key] = []
            continue
        avoid = list(taken)
        report = (fits or {}).get(join.key)
        if report is not None:
            avoid += [(patch.geometry, "on land too narrow at the seam")
                      for patch in report.narrow]
            avoid += [(tab.geometry, "on a locking tab" if tab.locks
                       else "on a tab too narrow to hold")
                      for tab in report.problem_tabs]
        land = unary_union([pieces[join.a].geom, pieces[join.b].geom])
        checks = check_keys(join.seam, placements, land, params, avoid)
        if join.stale:
            for check in checks:
                if check.ok:
                    check.ok = False
                    check.reason = (f"STL out of date ({join.agreement_mm:.2f} "
                                    "mm off the seam) -- rebuild it")
        taken += [(c.slot, f"overlaps a key on {join.label}")
                  for c in checks if c.ok]
        out[join.key] = checks
    return out


# ---------------------------------------------------------------------------
# Cutting and writing
# ---------------------------------------------------------------------------
@dataclass
class JoinResult:
    """Result of :func:`cut_all`."""
    params: KeyParams
    checks: dict                                    #: border key -> [KeyCheck]
    keyed: dict = field(default_factory=dict)       #: province -> slotted mesh
    slots: dict = field(default_factory=dict)       #: province -> what it was cut with
    failed: dict = field(default_factory=dict)      #: province -> message
    plates: list = field(default_factory=list)      #: key plates, one per bed

    @property
    def key_count(self) -> int:
        return sum(c.ok for checks in self.checks.values() for c in checks)


def cut_all(plan, pieces, joins, params=None, checks=None, fits=None,
            base_mm=None, previous=None, progress=None) -> JoinResult:
    """
    Every piece with all its slots cut, and the keys on bed plates.

    ``checks`` from :func:`check_plan` if already made.  ``base_mm`` (the
    terrain base) refuses a slot deeper than the base it is cut into.  A
    piece whose slots match ``previous`` (a JoinResult over the same loaded
    pieces) keeps that cut.  A piece that fails is listed in
    :attr:`JoinResult.failed` and the rest are still cut.
    """
    params = params or KeyParams()
    params.validate()
    if base_mm is not None and params.depth_mm >= base_mm:
        raise ValueError(
            f"Slot depth {params.depth_mm:g} mm would cut through the "
            f"{base_mm:g} mm base: the slot would open on the terrain side.")
    if checks is None:
        checks = check_plan(plan, pieces, joins, params, fits)
    slots = {name: [] for name in pieces}
    for join in joins:
        for check in checks.get(join.key, []):
            if check.ok:
                slots[join.a].append(check.slot)
                slots[join.b].append(check.slot)

    result = JoinResult(params, checks)
    names = sorted(pieces)
    for i, name in enumerate(names):
        if progress is not None:
            progress(i, len(names), name)
        signature = (params.depth_mm,
                     tuple(sorted(slot.wkb_hex for slot in slots[name])))
        if previous is not None and previous.slots.get(name) == signature \
                and name in previous.keyed:
            result.keyed[name] = previous.keyed[name]
            result.slots[name] = signature
            continue
        try:
            mesh = cut_slots(pieces[name].province, slots[name], params) \
                if slots[name] else pieces[name].province.mesh
            _validate(mesh, name)
        except Exception as exc:          # one bad piece does not stop the rest
            result.failed[name] = str(exc)
            continue
        result.keyed[name] = mesh
        result.slots[name] = signature
    if progress is not None:
        progress(len(names), len(names), "")

    count = result.key_count
    result.plates = key_plates(count + params.spares if count else 0, params)
    for i, plate in enumerate(result.plates, start=1):
        _validate(plate, f"key plate {i}")
    log.info("Cut %d key(s) into %d piece(s), %d failed; %d plate(s).",
             count, len(result.keyed), len(result.failed), len(result.plates))
    return result


def export(result: JoinResult, pieces, out_dir, plan=None) -> list:
    """
    Write ``<province>_keyed.stl`` (with a copy of its frame sidecar -- the
    slots were cut in that frame, so it still holds), every key plate, and the
    plan.  Plates from an earlier export are removed first, so the folder
    never holds more plates than this run made.  Returns the paths written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, mesh in sorted(result.keyed.items()):
        path = out_dir / f"{name}{KEYED_SUFFIX}.stl"
        mesh.export(str(path))
        paths.append(path)
        frame = pieces[name].frame
        if frame:
            path.with_suffix(".frame.json").write_text(
                json.dumps(frame, indent=2), encoding="utf-8")
    for old in out_dir.glob(f"{PLATE_STEM}_*.stl"):
        old.unlink()
    for i, plate in enumerate(result.plates, start=1):
        path = out_dir / f"{PLATE_STEM}_{i:02d}.stl"
        plate.export(str(path))
        paths.append(path)
    if plan is not None:
        paths.append(plan.save(out_dir / PLAN_NAME, result.params))
    return paths


def join_assembly(result: JoinResult, pieces, join: Join, band_mm=None):
    """
    ``(label, mesh)`` for one border: both slotted pieces cropped to a band
    around the seam, and its keys in place -- one print-mm frame, base at 0.

    Cropped because a pair of whole provinces is a million triangles to draw
    to look at a few keys.  ``band_mm`` defaults to four key reaches.
    """
    params = result.params
    for name in (join.a, join.b):
        if name not in result.keyed:
            raise ValueError(f"{display_name(name)} has not been cut.")
    pair = KeyResult(a=pieces[join.a].province, b=pieces[join.b].province,
                     keyed_a=result.keyed[join.a], keyed_b=result.keyed[join.b],
                     keys=[c for c in result.checks.get(join.key, []) if c.ok],
                     params=params)
    parts = assembly(pair)
    band = 4.0 * params.reach_mm if band_mm is None else band_mm
    ox, oy = pair.origin_m
    mpm = params.mpm
    x0, y0, x1, y1 = join.seam.bounds
    bounds = ((x0 - ox) / mpm - band, (y0 - oy) / mpm - band,
              (x1 - ox) / mpm + band, (y1 - oy) / mpm + band)
    cropped = [(label, _crop(mesh, bounds)) for label, mesh in parts[:2]]
    return [(label, mesh) for label, mesh in cropped if mesh is not None] \
        + parts[2:]


def _crop(mesh, bounds):
    """The part of a mesh inside an XY box, left open where it is cut."""
    x0, y0, x1, y1 = bounds
    for origin, normal in (((x0, 0, 0), (1, 0, 0)), ((x1, 0, 0), (-1, 0, 0)),
                           ((0, y0, 0), (0, 1, 0)), ((0, y1, 0), (0, -1, 0))):
        if mesh is None or len(mesh.faces) == 0:
            return None
        mesh = mesh.slice_plane(origin, normal)
    return mesh if mesh is not None and len(mesh.faces) else None
