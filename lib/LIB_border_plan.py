#!/usr/bin/env python3
"""
LIB_border_plan.py

The Borders stage as data: what is asked for (``BorderSettings``), what
Define makes of it (``BorderDefinition``), and the folder it is saved to and
loaded from.

``define_borders`` simplifies every region in one topology, carves the
corridor between them (``LIB_border_regions``), adds the enclaves the
carving cut off to it and fills its pinholes, and places the dovetail
connectors (``LIB_border_connectors``), held to the rules of
``LIB_connector_rules``.  A ``BorderDefinition`` then answers the questions
the editor asks of it while it is worked on -- each connector's fault, the
pieces the splits cut the bar into, which of them are too big for the bed --
and keeps the answers until what they depend on changes.

``write_borders`` / ``read_borders`` are Save Borders and Load Borders: the
corridor, each trimmed province, the connectors as they were left, the
splits, and the settings they were shaped by.

Pure geometry and files, no Qt: split out of ``UI_terrain_editor.py``.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclasses_fields
from pathlib import Path

import geopandas as gpd
from shapely import STRtree
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize
from shapely.ops import unary_union

from lib.LIB_border_connectors import (CONNECTORS_NAME, ConnectorParams,
                                       _polys, _repair, _safe_union,
                                       generate_lobes, load_connector_params,
                                       lobe_pose)
from lib.LIB_border_regions import build_border_regions, shared_borderlines
from lib.LIB_border_simplification import topo_simplify_two_tolerances
from lib.LIB_border_split import bed_turn
from lib.LIB_connector_rules import (_crowds, _near, _own_fault,
                                     _standing_lobes)
from lib.LIB_project_config import build_params, settings_files
from lib.LIB_project_config import get as _cfg
from lib.LIB_project_config import shipped as _shipped
from lib.LIB_shared_border import _count_short, _count_spikes

#: The working CRS every region is held in (``crs.working``, EPSG:3347).
EPSG_WORKING = _cfg("crs.working")

#: The Borders settings, written beside a saved Borders folder so loading it
#: rebuilds the connectors in the shape they were saved in.
BORDER_SETTINGS_NAME = "borders_settings.json"


#: Where splits are saved beside a Borders folder.
SPLITS_NAME = "splits.geojson"
#: Where the pieces taken out of the bar are saved, as a point in each.
REMOVED_NAME = "removed_pieces.geojson"


def _vertex_count(geom) -> int:
    """Ring vertices in a (Multi)Polygon -- the number simplification moves."""
    return sum(len(poly.exterior.coords)
               + sum(len(ring.coords) for ring in poly.interiors)
               for poly in _polys(geom))


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "region"


def load_region(path):
    """
    One region file as ``(geometry in crs.working, the CRS it arrived in)``.

    Dissolved to one geometry and repaired on the way in (FR-1.6): province
    exports carry rings GEOS calls invalid, and an invalid input is a
    TopologyException three stages later instead of a message now.
    """
    frame = gpd.read_file(str(path))
    if frame.empty:
        raise ValueError(f"{Path(path).name} has no features.")
    if frame.crs is None:                       # bare GeoJSON is WGS 84 by spec
        frame = frame.set_crs(_cfg("crs.source"))
    source_crs = frame.crs
    geom = _repair(_safe_union(list(frame.to_crs(EPSG_WORKING).geometry.values)))
    if geom is None:
        raise ValueError(f"{Path(path).name} has no polygon to work with.")
    return geom, source_crs


@dataclass
class BorderSettings:
    """What the Borders stage is asked for.  Lengths are **print-mm**."""
    coast_tol_mm: float = _shipped("borders.coast_tol_mm")
    internal_tol_mm: float = _shipped("borders.internal_tol_mm")
    min_segment_mm: float = _shipped("seam.min_segment_mm")
    min_angle_deg: float = _shipped("seam.min_angle_deg")
    width_mm: float = _shipped("borders.width_mm")
    #: Below this printed area (mm²) a piece the corridor cuts off from the
    #: rest of its province is added to the border bar (see
    #: :func:`_absorb_enclaves`); 0 keeps every piece.
    enclave_mm2: float = _shipped("borders.enclave_mm2")
    #: Gaps between a printed province and a surrounding region up to this
    #: share of ``width_mm`` are bridged by the corridor (see
    #: ``LIB_border_regions.bridge_gaps``); 0 bridges none.
    bridge_gap_ratio: float = _shipped("borders.bridge_gap_ratio")
    connectors: bool = _shipped("borders.connectors")
    lobe_spacing_mm: float = _shipped("borders.lobe_spacing_mm")
    #: The connector's shape.  Height is the lobe layer, and so the depth of
    #: the pocket it drops into; depth is how far it reaches into the
    #: province; width is the neck at the corridor, and the mouth at the far
    #: end is ``ratio`` times that -- above 1, the wide end is captured.
    lobe_height_mm: float = _shipped("print.dovetail_mm")
    lobe_depth_mm: float = _shipped("borders.lobe_depth_mm")
    lobe_width_mm: float = _shipped("borders.lobe_width_mm")
    lobe_ratio: float = _shipped("borders.lobe_ratio")
    #: The fit in the pocket: how much wider than the connector it is cut on
    #: every side, and how much deeper than the connector layer is tall.
    clearance_mm: float = _shipped("borders.clearance_mm")
    height_clearance_mm: float = _shipped("borders.height_clearance_mm")
    scale: float = _shipped("print.scale")

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    def connector_params(self) -> ConnectorParams:
        return load_connector_params(
            *settings_files(), scale=self.scale,
            spacing_mm=self.lobe_spacing_mm, dovetail_mm=self.lobe_height_mm,
            reach_mm=self.lobe_depth_mm, neck_mm=self.lobe_width_mm,
            mouth_mm=self.lobe_width_mm * self.lobe_ratio,
            clearance_mm=self.clearance_mm,
            height_clearance_mm=self.height_clearance_mm)


#: The config key behind each field of :class:`BorderSettings`.
_BORDER_SETTINGS_KEYS = {
    "coast_tol_mm": "borders.coast_tol_mm",
    "internal_tol_mm": "borders.internal_tol_mm",
    "min_segment_mm": "seam.min_segment_mm",
    "min_angle_deg": "seam.min_angle_deg",
    "width_mm": "borders.width_mm",
    "enclave_mm2": "borders.enclave_mm2",
    "bridge_gap_ratio": "borders.bridge_gap_ratio",
    "connectors": "borders.connectors",
    "lobe_spacing_mm": "borders.lobe_spacing_mm",
    "lobe_height_mm": "print.dovetail_mm",
    "lobe_depth_mm": "borders.lobe_depth_mm",
    "lobe_width_mm": "borders.lobe_width_mm",
    "lobe_ratio": "borders.lobe_ratio",
    "clearance_mm": "borders.clearance_mm",
    "height_clearance_mm": "borders.height_clearance_mm",
    "scale": "print.scale",
}


#: The fields of :class:`BorderSettings` that shape a connector -- and its
#: fit, since the clearance is also the gap kept between neighbouring pockets
#: -- which the window changes on a defined map without defining it again.
SHAPE_FIELDS = ("lobe_height_mm", "lobe_depth_mm", "lobe_width_mm",
                "lobe_ratio", "clearance_mm", "height_clearance_mm")


def load_border_settings(*config_files, **overrides):
    """
    A :class:`BorderSettings` filled from config files, later files winning,
    as the libraries' ``load_*_params`` do.  ``BorderSettings()`` itself is
    the shipped settings and reads no file.
    """
    return build_params(BorderSettings, _BORDER_SETTINGS_KEYS, *config_files,
                        **overrides)


@dataclass
class BorderDefinition:
    """Result of :func:`define_borders`.  Geometry is in ``crs.working``."""
    settings: BorderSettings
    simplified: dict = field(default_factory=dict)  #: every region, simplified
    targets: dict = field(default_factory=dict)     #: trimmed targets
    border: object = None                           #: the corridor
    lobes: list = field(default_factory=list)       #: one owner each
    coastal_segs: list = field(default_factory=list)
    internal_segs: list = field(default_factory=list)
    vertices_before: int = 0
    vertices_after: int = 0
    #: Shared arcs measured against the printer, not against the filters --
    #: the same rule LIB_shared_border reports by.
    short_segments: int = 0
    spikes: int = 0
    #: Area two simplified targets both claimed, before it was handed back.
    #: A shared arc is one line for both sides, but straightened at the
    #: internal tolerance it can cut across a *different* arc -- an island's
    #: coast beside the seam (3.4 mm² of NWT/Nunavut at 0.5 mm).
    overlap_mm2: float = 0.0
    lobes_dropped: int = 0         #: generated lobes the rules refused
    #: Why: fault code (see ``FAULTS``) -> how many.
    drop_reasons: dict = field(default_factory=dict)
    lobes_edited: bool = False     #: moved, added or deleted by hand since
    #: The folder it was read back from (:func:`read_borders`), or "" when
    #: Define made it.
    loaded_from: str = ""
    #: Where the bar is cut into printable pieces: straight lines across the
    #: corridor (:func:`split_at`), planned by hand.  Define makes none --
    #: the bar is never split for you.
    splits: list = field(default_factory=list)
    #: Pieces of the bar the user has taken out, each as a point inside it:
    #: a point rather than the piece, so a removal survives the splits
    #: changing -- the piece the point falls in is the one left out.
    removed: list = field(default_factory=list)
    _pieces: list = field(default=None, repr=False, compare=False,
                          init=False)
    _pieces_key: tuple = field(default=None, repr=False, compare=False,
                               init=False)
    #: piece_lobes() and too_big(), with what they were worked out for:
    #: asked several times a click, and on all of Canada each is a fifth of
    #: a second.
    _held: tuple = field(default=None, repr=False, compare=False, init=False)
    _big: tuple = field(default=None, repr=False, compare=False, init=False)
    #: Pieces the corridor cut off from their province, added to the bar
    #: (see :func:`_absorb_enclaves`), and their printed area.
    enclaves: int = 0
    enclaves_mm2: float = 0.0
    #: The targets merged into one, and which target geometries it was merged
    #: from (see :meth:`land`).
    _land: object = field(default=None, repr=False, compare=False,
                          init=False)
    _land_key: tuple = field(default=(), repr=False, compare=False,
                             init=False)
    #: Per connector, what it breaks on its own and its reach out of the
    #: corridor, keyed by the connector itself (see :meth:`faults`).
    _fault_cache: dict = field(default_factory=dict, repr=False,
                               compare=False, init=False)
    _fault_key: tuple = field(default=(), repr=False, compare=False,
                              init=False)
    _fault_answer: tuple = field(default=None, repr=False, compare=False,
                                 init=False)
    _line: object = field(default=None, repr=False, compare=False,
                          init=False)
    _line_key: tuple = field(default=None, repr=False, compare=False,
                             init=False)

    def land(self):
        """Every target as one geometry, for the connector rules.

        Merged once and kept: on all of Canada the merge is 78 ms, and the
        rules ask for it on every placement -- every mouse move of a drag.
        Keyed on the target geometries themselves, so a definition whose
        targets are replaced merges again rather than serving the old land.
        """
        key = tuple(id(geom) for geom in self.targets.values())
        if self._land is None or self._land_key != key:
            self._land = _safe_union(list(self.targets.values()))
            self._land_key = key
        return self._land

    def faults(self) -> list:
        """Each connector's fault code (a key of ``FAULTS``), or "" for one
        that may stand -- in the order of :attr:`lobes`.

        Every rule, held against every *other* connector, so two that crowd
        each other are both at fault.  Kept, because the window asks after
        every edit and on all of Canada measuring 240 connectors afresh is a
        second: what each breaks on its own is measured once per connector,
        the neighbours come from a spatial index, and the whole answer stands
        until the connectors change.
        """
        if self.border is None:
            return ["" for _ in self.lobes]
        self._check_fault_key()
        ids = (tuple(map(id, self.lobes)), tuple(map(id, self.splits)))
        if self._fault_answer is not None and self._fault_answer[0] == ids:
            return list(self._fault_answer[1])
        p = self.settings.connector_params()
        land = self.land()
        own = []
        for lobe in self.lobes:
            hit = self._fault_cache.get(id(lobe))
            if hit is None or hit[0] is not lobe:
                hit = (lobe, _own_fault(lobe, self, p, land), None)
                self._fault_cache[id(lobe)] = hit
            own.append(hit[1])
        live = set(ids[0])
        for gone in [k for k in self._fault_cache if k not in live]:
            del self._fault_cache[gone]

        codes = list(own)
        gap = p.clearance_mm * p.mpm
        if len(self.lobes) > 1:
            tree = STRtree(self.lobes)
            near = [lobe.buffer(gap, quad_segs=1) for lobe in self.lobes]
            left, right = tree.query(near)
            for i, j in zip(left.tolist(), right.tolist()):
                if j <= i:
                    continue
                a, b = self.lobes[i], self.lobes[j]
                if _crowds(a, self._reach(a), b, self._reach(b), gap):
                    codes[i] = codes[i] or "crowded"
                    codes[j] = codes[j] or "crowded"
        for cut in self.splits:
            for i, lobe in enumerate(self.lobes):
                if not codes[i] and _near(lobe, cut, gap) \
                        and lobe.distance(cut) < gap:
                    codes[i] = "on_split"
        self._fault_answer = (ids, codes)
        return list(codes)

    def _check_fault_key(self) -> None:
        """Forget every measurement made under other settings, another
        corridor or other provinces."""
        key = (self.settings, id(self.border),
               tuple(id(g) for g in self.targets.values()))
        if self._fault_key != key:
            self._fault_cache.clear()
            self._fault_answer = None
            self._fault_key = key

    def _passed(self, lobes) -> None:
        """Note ``lobes`` as breaking no rule of their own: Define has just
        held each to every rule, so measuring them again would be waste."""
        self._check_fault_key()
        for lobe in lobes:
            self._fault_cache[id(lobe)] = (lobe, "", None)

    def pieces(self) -> list:
        """The bar's corridor, cut at every split: one polygon per printed
        piece.  Kept until the corridor or the splits change."""
        if self.border is None:
            return []
        key = (id(self.border), tuple(map(id, self.splits)))
        if self._pieces_key != key:
            # Each part by every cut across it at once: a ring of bar (round a
            # province with corridor on every side) does not come apart at
            # one cut, and split one at a time each would be thrown away.
            # The part's outline and its cuts are noded together and every
            # face inside the part is a piece -- holes and all.
            geoms = []
            for geom in _polys(self.border):
                cuts = [cut for cut in self.splits if geom.intersects(cut)]
                if not cuts:
                    geoms.append(geom)
                    continue
                faces = polygonize(unary_union([geom.boundary, *cuts]))
                geoms += [face for face in faces
                          if geom.contains(face.representative_point())]
            self._pieces, self._pieces_key = geoms, key
        return list(self._pieces)

    def piece_lobes(self) -> list:
        """For each piece, the connectors it carries: each goes with the
        piece holding most of its root."""
        pieces = self.pieces()
        key = (self._pieces_key, tuple(map(id, self.lobes)))
        if self._held is not None and self._held[0] == key:
            return [list(lobes) for lobes in self._held[1]]
        out = [[] for _ in pieces]
        for lobe in self.lobes:
            best, share = None, 0.0
            for i, piece in enumerate(pieces):
                if _near(lobe, piece, 0.0):
                    area = lobe.intersection(piece).area
                    if area > share:
                        best, share = i, area
            if best is not None:
                out[best].append(lobe)
        self._held = (key, out)
        return [list(lobes) for lobes in out]

    def removed_flags(self) -> list:
        """For each piece, whether the user has taken it out of the bar."""
        pieces = self.pieces()
        flags = [False for _ in pieces]
        for point in self.removed:
            # One piece a point: a split added later can run right through
            # it, and it would then lie on the edge of both halves.
            for i, piece in enumerate(pieces):
                if piece.covers(point):
                    flags[i] = True
                    break
        return flags

    def bare_flags(self) -> list:
        """For each piece, whether it is still in the bar yet carries no
        connector: nothing would hold it to a province, so it is not built.
        With connectors turned off no piece is bare -- none has any."""
        out = self.removed_flags()
        if not self.settings.connectors:
            return [False for _ in out]
        return [not lobes and not gone
                for lobes, gone in zip(self.piece_lobes(), out)]

    def built_flags(self) -> list:
        """For each piece, whether it is built: neither taken out by the
        user nor bare of connectors."""
        return [not (gone or bare) for gone, bare
                in zip(self.removed_flags(), self.bare_flags())]

    def kept_lobes(self) -> list:
        """The connectors on pieces still in the bar: a removed piece takes
        its connectors with it -- from the bar, and from the pockets cut for
        them -- so no province is left with a pocket nothing fills."""
        if not self.removed:
            return list(self.lobes)
        gone = {id(lobe)
                for lobes, out in zip(self.piece_lobes(), self.removed_flags())
                if out for lobe in lobes}
        return [lobe for lobe in self.lobes if id(lobe) not in gone]

    def too_big(self, bed_mm=None, margin_mm=None) -> list:
        """Indices of the pieces that will not fit the bed at any turn -- a
        slicer turns a part to fit, diagonally if it must, so each piece is
        tried at every angle (:func:`bed_turn`), connectors included.  A
        piece taken out of the bar, or bare of connectors, is never too big:
        it is not printed."""
        bed = _cfg("print.bed_mm") if bed_mm is None else bed_mm
        margin = _cfg("print.bed_margin_mm") if margin_mm is None \
            else margin_mm
        usable = (bed - 2 * margin) * self.settings.mpm
        pieces, held = self.pieces(), self.piece_lobes()
        key = (self._pieces_key, tuple(map(id, self.lobes)), usable)
        built = self.built_flags()
        if self._big is not None and self._big[0] == key:
            return [i for i in self._big[1] if built[i]]
        big = []
        for i, (piece, lobes) in enumerate(zip(pieces, held)):
            shape = unary_union([piece, *lobes]) if lobes else piece
            if bed_turn(shape, usable) is None:
                big.append(i)
        self._big = (key, big)
        return [i for i in big if built[i]]

    def border_line(self):
        """The border line between the printed provinces -- the shared
        edges of their simplified outlines, which the corridor between two
        of them is carved along -- or None where none touch.  Merged once,
        like :meth:`land`.  A frontier with a surrounding region is not part
        of it: that corridor lies wholly on the surrounding side, and every
        connector's root crosses its line by design."""
        key = tuple(id(self.simplified.get(n)) for n in self.targets)
        if self._line_key != key:
            self._line = shared_borderlines(
                [self.simplified[n] for n in self.targets
                 if self.simplified.get(n) is not None])
            self._line_key = key
        return self._line

    def _reach(self, lobe):
        """``lobe`` outside the corridor, measured once."""
        hit = self._fault_cache[id(lobe)]
        if hit[2] is None:
            hit = (hit[0], hit[1], lobe.difference(self.border))
            self._fault_cache[id(lobe)] = hit
        return hit[2]

    def province_lobes(self, name) -> list:
        """The lobes that key into target ``name`` -- its pockets."""
        geom = self.targets.get(name)
        if geom is None:
            return []
        return [lobe for lobe in self.kept_lobes() if lobe.intersects(geom)]


def define_borders(targets: dict, surrounding: dict,
                   settings: BorderSettings | None = None) -> BorderDefinition:
    """
    Simplify every region in one topology, carve the corridor, place the lobes.

    ``targets`` / ``surrounding`` map name -> geometry in ``crs.working``.  Pure
    geometry (no Qt), so it runs on a worker thread and tests call it directly.
    """
    s = settings or BorderSettings()
    if not targets:
        raise ValueError("Add at least one province to print.")
    mpm = s.mpm
    names = list(targets) + list(surrounding)
    roles = dict.fromkeys(targets, "target") | dict.fromkeys(surrounding,
                                                             "surrounding")
    frame = gpd.GeoDataFrame(
        {"name": names},
        geometry=[targets.get(n, surrounding.get(n)) for n in names],
        crs=EPSG_WORKING)

    simp, coastal, internal = topo_simplify_two_tolerances(
        frame, s.coast_tol_mm * mpm, s.internal_tol_mm * mpm,
        min_segment_m=s.min_segment_mm * mpm, min_angle_deg=s.min_angle_deg)

    result = BorderDefinition(settings=s, coastal_segs=coastal,
                              internal_segs=internal)
    for name, geom in zip(simp["name"], simp.geometry):
        # Coastal DP runs with preserve_topology=False and can leave a ring
        # crossing itself (British Columbia does at 0.3 mm); carving needs
        # valid input, so repair here rather than fail inside the union.
        fixed = _repair(geom) if geom is not None else None
        if fixed is None:
            raise ValueError(f"{name}: simplification left no land. Lower "
                             "the coastal tolerance.")
        result.simplified[name] = fixed
    result.vertices_before = sum(_vertex_count(g) for g in frame.geometry)
    result.vertices_after = sum(_vertex_count(g)
                                for g in result.simplified.values())
    nozzle = _cfg("seam.nozzle_mm") * mpm
    spike = _cfg("seam.spike_angle_deg")
    result.short_segments = sum(_count_short(seg.tolist(), nozzle)
                                for seg in internal)
    result.spikes = sum(_count_spikes(seg.tolist(), spike) for seg in internal)

    target_names = [n for n in result.simplified if roles[n] == "target"]
    surround_names = [n for n in result.simplified
                      if roles[n] == "surrounding"]
    result.overlap_mm2 = _resolve_overlaps(result.simplified, target_names,
                                           targets) / mpm ** 2
    simplified_targets = [result.simplified[n] for n in target_names]

    regions = build_border_regions(
        simplified_targets, [result.simplified[n] for n in surround_names],
        border_width_km=s.width_mm * s.scale / 1e6,
        bridge_gap_km=s.bridge_gap_ratio * s.width_mm * s.scale / 1e6)
    for name, trimmed, before in zip(target_names, regions.core,
                                     simplified_targets):
        result.targets[name] = _repair(trimmed) if trimmed is not None \
            else before
    result.border = _repair(regions.border) if regions.border is not None \
        else None
    if result.border is not None and s.enclave_mm2 > 0:
        result.enclaves, area = _absorb_enclaves(
            result, s.enclave_mm2 * mpm ** 2)
        result.enclaves_mm2 = area / mpm ** 2
    if result.border is not None:
        result.border = _fill_pinholes(result.border, result.targets.values(),
                                       PINHOLE_MM2 * mpm ** 2)

    if s.connectors and result.border is not None:
        lobes = generate_lobes(result.border,
                               result.land(),
                               s.connector_params())
        result.lobes, result.drop_reasons = _standing_lobes(
            lobes, result, s.connector_params())
        result._passed(result.lobes)
        result.lobes_dropped = sum(result.drop_reasons.values())
    return result


def _absorb_enclaves(result, max_m2: float) -> tuple:
    """
    Add the small pieces the corridor cut off from their province to the
    border bar: ``(how many, their area in m²)``.

    Carving the corridor can split a province's land.  Where a border
    zig-zags, two arms of the corridor all but meet and leave crumbs of
    province in holes in the bar; where a border meets the coast, the bar
    cuts slivers off between itself and the sea (both on the Quebec /
    Labrador border).  A piece like that has no land of its own to hang on
    to -- printed, it is a loose crumb -- so below ``max_m2`` it becomes
    part of the bar.

    A piece counts only if the carving made it: of each simplified part of
    a province, the largest piece left is the province and the rest are cut
    off.  An island that was an island before carving is never touched,
    however small.
    """
    count, area = 0, 0.0
    taken = []
    for name in list(result.targets):
        trimmed = result.targets[name]
        if trimmed is None:
            continue
        pieces = _polys(trimmed)
        parents = _polys(result.simplified[name])
        keep = []
        by_parent = {}
        for piece in pieces:
            probe = piece.representative_point()
            parent = next((i for i, part in enumerate(parents)
                           if part.covers(probe)), None)
            by_parent.setdefault(parent, []).append(piece)
        for parent, group in by_parent.items():
            group.sort(key=lambda g: g.area, reverse=True)
            main, rest = (group[0], group[1:]) if parent is not None \
                else (None, group)
            if main is not None:
                keep.append(main)
            for piece in rest:
                if parent is not None and piece.area < max_m2 \
                        and piece.distance(result.border) < 1.0:
                    taken.append(piece)
                    count += 1
                    area += piece.area
                else:
                    keep.append(piece)
        if len(keep) != len(pieces):
            result.targets[name] = _repair(unary_union(keep))
    if taken:
        result.border = _repair(unary_union([result.border, *taken]))
    return count, area


#: Holes in the corridor below this printed area (mm²) with no province in
#: them are filled.  Carving and merging leave specks where two outlines meet
#: by rounding -- six along the Quebec/Labrador border, each under 0.001 mm²
#: -- and a speck of nothing in a printed bar is a flaw, not a feature.
PINHOLE_MM2 = 0.01


def _fill_pinholes(border, targets, max_m2: float):
    """``border`` with its specks of hole filled: rings under ``max_m2``
    that hold no land of ``targets``."""
    land = [geom for geom in targets if geom is not None]
    out, changed = [], False
    for poly in _polys(border):
        keep = []
        for ring in poly.interiors:
            hole = Polygon(ring)
            if hole.area < max_m2 and not any(hole.intersection(geom).area
                                              > 0.5 * hole.area
                                              for geom in land):
                changed = True
            else:
                keep.append(ring)
        out.append(Polygon(poly.exterior, keep))
    return _repair(unary_union(out)) if changed else border


def read_borders(folder: Path, settings: "BorderSettings | None" = None):
    """
    A :class:`BorderDefinition` read back from a folder Save Borders wrote:
    the corridor (``borders.geojson``), each trimmed province
    (``name_<province>.geojson``) and the connectors as they were left
    (``connectors.geojson``), hand edits and all.

    The connector settings come from the folder's ``borders_settings.json``
    where there is one, else from ``settings`` (the window's own) -- a
    connector's turn and size are read off its outline against them.
    What Define measured on the way (vertex counts, seam reports, what it
    dropped) is not saved, so it is not there to report.

    Every shape is repaired before it is merged.  Folders saved before
    2026-10 hold each province in the CRS it arrived in (EPSG:4326, for the
    cleaned provinces), and the trip from the working CRS and back moves
    each point by rounding error: where carving left an outline within that
    of itself -- seven places on all of Canada -- it crosses, and merging
    the crossed shape threw.  Repairing touches only those specks.
    """
    folder = Path(folder)
    border_path = folder / "borders.geojson"
    names = sorted(folder.glob("name_*.geojson"))
    if not border_path.exists() or not names:
        raise ValueError(f"{folder} holds no saved borders: it needs "
                         "borders.geojson and a name_<province>.geojson.")
    saved = folder / BORDER_SETTINGS_NAME
    if saved.exists():
        values = json.loads(saved.read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses_fields(BorderSettings)}
        settings = BorderSettings(**{k: v for k, v in values.items()
                                     if k in known})
    settings = settings or BorderSettings()

    def read(path):
        frame = gpd.read_file(path)
        if frame.crs is not None:
            frame = frame.to_crs(EPSG_WORKING)
        return frame

    border = _repair(_safe_union(list(read(border_path).geometry)))
    result = BorderDefinition(settings=settings, border=border,
                              loaded_from=folder.name)
    for path in names:
        frame = read(path)
        name = (str(frame["name"].iloc[0]) if "name" in frame.columns
                else path.stem[len("name_"):])
        geom = _repair(_safe_union(list(frame.geometry)))
        result.targets[name] = geom
        result.simplified[name] = geom
    connectors = folder / CONNECTORS_NAME
    if connectors.exists():
        # As written, corner order and all: a connector's turn and size are
        # read off its corners (lobe_pose), so the ring is never re-oriented.
        result.lobes = [Polygon(geom.exterior.coords)
                        for geom in read(connectors).geometry]
        result.lobes_edited = True
    splits = folder / SPLITS_NAME
    if splits.exists():
        result.splits = [LineString(geom.coords)
                         for geom in read(splits).geometry]
    removed = folder / REMOVED_NAME
    if removed.exists():
        result.removed = list(read(removed).geometry)
    return result


def _resolve_overlaps(simplified: dict, names: list, originals: dict) -> float:
    """
    Make the simplified targets a partition again; returns the area (m²) fixed.

    Each overlap goes back to whichever province held more of it before
    simplification, so the seam ends up on the side of the land it was
    taken from.  Carving needs disjoint input: an overlap would be read as a
    second, phantom seam.
    """
    total = 0.0
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ga, gb = simplified[a], simplified[b]
            ax0, ay0, ax1, ay1 = ga.bounds
            bx0, by0, bx1, by1 = gb.bounds
            if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
                continue
            both = ga.intersection(gb)
            if both.area <= 0:
                continue
            total += both.area
            held_a = originals[a].intersection(both).area
            held_b = originals[b].intersection(both).area
            loser = b if held_a >= held_b else a
            trimmed = _repair(simplified[loser].difference(both))
            if trimmed is not None:
                simplified[loser] = trimmed
    return total


def write_borders(definition: BorderDefinition, folder: Path) -> list:
    """
    Save Borders: ``borders.geojson`` + ``name_<province>.geojson`` per
    trimmed target -- the folder layout ``build_border_from_dir`` reads --
    with the connectors as they stand (``connectors.geojson``), the splits
    (``splits.geojson``) and the settings (``borders_settings.json``).

    Every file is written in the working CRS (``crs.working``), the frame
    the borders are built in, so a folder reloads exactly as it was saved.
    Writing each province back in the CRS it arrived in -- as this did
    before 2026-10 -- sent every point through a reprojection and back,
    and the rounding crossed outlines that carving had left touching
    themselves.  Every reader of the folder reprojects to the working CRS
    anyway.  Returns the paths written.
    """
    d = definition
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    default_crs = EPSG_WORKING
    written = []

    def write(name, geom, crs, path):
        frame = gpd.GeoDataFrame({"name": [name]}, geometry=[geom],
                                 crs=EPSG_WORKING).to_crs(crs)
        frame.to_file(path, driver="GeoJSON")
        written.append(path)

    if d.border is not None:
        write("border", d.border, default_crs, folder / "borders.geojson")
    # The lobes as they stand, so a hand edit survives the folder being
    # read back (build_border_from_dir uses these rather than its own).
    connectors = folder / CONNECTORS_NAME
    if d.lobes:
        p = d.settings.connector_params()
        poses = [lobe_pose(lobe, d.border, p) for lobe in d.lobes]
        frame = gpd.GeoDataFrame(
            {"name": [f"connector_{i}" for i in range(1, len(d.lobes) + 1)],
             "angle_deg": [round(pose[2], 3) for pose in poses],
             "scale": [round(pose[3], 4) for pose in poses]},
            geometry=list(d.lobes), crs=EPSG_WORKING)
        frame.to_crs(default_crs).to_file(connectors, driver="GeoJSON")
        written.append(connectors)
    elif connectors.exists():
        connectors.unlink()      # a stale set would key into nothing
    for name, geom in d.targets.items():
        write(name, geom, default_crs,
              folder / f"name_{_safe_name(name)}.geojson")
    removed = folder / REMOVED_NAME
    if d.removed:
        gpd.GeoDataFrame(
            {"name": [f"removed_{i}" for i in range(1, len(d.removed) + 1)]},
            geometry=list(d.removed), crs=EPSG_WORKING,
        ).to_crs(default_crs).to_file(removed, driver="GeoJSON")
        written.append(removed)
    elif removed.exists():
        removed.unlink()
    splits = folder / SPLITS_NAME
    if d.splits:
        gpd.GeoDataFrame(
            {"name": [f"split_{i}" for i in range(1, len(d.splits) + 1)]},
            geometry=list(d.splits), crs=EPSG_WORKING,
        ).to_crs(default_crs).to_file(splits, driver="GeoJSON")
        written.append(splits)
    elif splits.exists():
        splits.unlink()          # stale splits would cut the wrong bar
    # The settings the connectors were shaped by, so Load Borders can
    # rebuild them as they were -- a connector's turn and size are read off
    # its outline against these.
    settings = folder / BORDER_SETTINGS_NAME
    settings.write_text(json.dumps(asdict(d.settings), indent=2) + "\n",
                        encoding="utf-8")
    written.append(settings)
    return written
