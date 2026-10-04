"""
UI_terrain_editor.py

A standalone visual harness for the whole terrain pipeline, in three stages:

1 · Import   the DEM, the provinces to print (*targets*), and the regions
             around them (*surrounding* -- context that shapes the borders but
             is never printed).
2 · Borders  ``LIB_border_simplification`` -> ``LIB_border_regions`` ->
             ``LIB_border_connectors.generate_lobes``: simplify every outline in
             one topology (seams cleaned for printing), carve the corridor, place
             the dovetails.
3 · STL      ``LIB_raster_reproject`` -> ``LIB_geotiff_clip`` ->
             ``LIB_terrain_stl`` per province, cut to the province *after* the
             corridor was carved out of it and pocketed for the dovetails; and
             the border bar, ``build_border_mesh`` -> ``split_to_bed``.

Stage 3 is the original single-province harness, and still runs on its own:
with no borders defined it cuts to the province as imported.  The workflow
below is that stage; the Borders stage is described under "Borders" further
down.

Workflow (stage 3)
------------------
1. **Import GeoTIFF …**   — open the DEM.  Only its metadata and a small
                            decimated overview are read, never the whole file,
                            so pointing this at the 7.3 GB ``Canada120m.tif``
                            takes well under a second.
2. **Import Boundary …**  — load the province GeoJSON.  It is drawn over the DEM
                            in red, so you can see what is about to be cut
                            before cutting.
3. **Target CRS**         — the projection the output is built in.  Leave it at
                            EPSG:3347 (the print CRS) unless you know why not;
                            see "Why the projection matters" below.
4. **Resolution / Buffer / Resample / All-touched / Crop** — the sampling and
                            cut parameters.  The status line live-costs each
                            change ("453 x 328 px, 0.6 MB") *before* you commit.
                            **Res** above the DEM's native pitch reduces terrain
                            resolution, and **Resample** decides what survives
                            it: coarsening Labrador 120 m -> 3200 m keeps
                            1578 m of Mount Caubvick with ``max`` and 1175 m
                            with ``bilinear``, against a true 1652 m.  Both
                            apply whether or not the CRS changes.
5. **Reproject + Crop**   — run the raster stage off the UI thread.
6. **Exag / Base / Floor / Cut** — the solid's parameters.  Defaults come from
                            ``project_config.json``'s ``terrain`` section, so
                            every piece of a map is built the same way.
7. **Build STL**          — mesh the cropped DEM into a watertight solid.
8. **Save GeoTIFF … / Save STL …** — keep either stage's output.

The whole run is here: national DEM in, printable province solid out, without
leaving the window or touching QGIS.

Run with:  python UI_terrain_editor.py

Like the other UI_* harnesses this stays independent of the main ``app`` window
so the libs can be pointed at any DEM/GeoJSON pair without setting up a project.

Why the projection matters
--------------------------
``Canada120m.tif`` is EPSG:3979; every border corridor and dovetail in this
project is built in EPSG:3347.  The same province is 175.7 x 137.0 print-mm in
one and 181.1 x 131.1 in the other — a 5.4 x 5.9 mm disagreement against a 1.6 mm
dovetail neck.  Terrain cropped in the DEM's own CRS cannot key into a border
built in the print CRS, which is the failure this harness exists to make visible:
switch **Target CRS** between the two and watch the result change shape.

Order of operations
-------------------
Warp first, cut second.  The coastline cut then happens in the frame that gets
printed, at the resolution that gets printed, so it is exact there rather than
being a source-CRS cut dragged through a warp afterwards.  When a warp runs, the
warp is given the boundary's bounds, so it *is* the bbox crop and the clip only
has to apply the polygon mask.

Two stages, two results
-----------------------
The raster stage (reproject + crop) and the mesh stage (build) are separate
buttons on purpose: the intermediate raster is worth inspecting before spending
seconds meshing it, and changing an exaggeration should not re-warp 7 GB.  So a
raster-parameter change invalidates **both** results, while a mesh-parameter
change invalidates only the solid.

Three things differ from the 2-D geometry harnesses, all forced by the input
being a **very large raster**:

*Nothing is ever read whole.*  The before panel is a decimated overview (GDAL
seeks past the scanlines it does not need, so a full-Canada overview costs
~0.4 s); the warp streams through a ``WarpedVRT`` and the clip in row bands.

*The run writes the file, and Save copies it.*  The libs are what write
GeoTIFFs, so the harness runs straight to a temporary file and Save copies it.
What you inspect is byte-for-byte what you save.

*The after panel may be showing you less than you saved.*  A native-resolution
province is a 400 MB array; matplotlib is handed a strided-down copy above
``MAX_DISPLAY_PX``.  The panel title says so whenever it happens, because a
harness that quietly showed a coarser image than it wrote would be lying about
the thing it exists to check.

Borders
-------
*One topology, every region.*  Targets and surrounding go through
``topo_simplify_two_tolerances`` together, so a seam -- target/target or
target/surrounding -- is one arc both sides reference, and cannot open a gap.
Tolerances are **print-mm** (converted through ``print.scale``), because the
question is what survives the nozzle.  Shared arcs then get
``LIB_shared_border``'s seam clean-up (``seam.min_segment_mm`` /
``seam.min_angle_deg``), and the report measures what is left against the
printer limits (``seam.nozzle_mm`` / ``seam.spike_angle_deg``).

*Simplify, then carve, then cut the terrain.*  ``LIB_border_regions`` wants
simplified input, and the terrain must be cut by the province *after* the
corridor came out of it -- otherwise the solid and the border bar overlap by
half the corridor.  So once borders are defined, stage 3 cuts to the trimmed
province and **Buffer** is locked at 0: growing the cut outward is growing it
into the corridor.

*A connector belongs to one province.*  A lobe whose far end lands in two
targets (near a triple junction) has no single pocket to key into, so it is
dropped and counted (FR-3.7).

*The bed split cannot yet see connectors.*  ``split_to_bed`` cuts the bar
without knowing where the lobes are (FR-3.11 is not enforced), so the status
line counts the connectors a cut went through rather than letting a split lobe
reach the printer unannounced.

*Pockets need the print CRS.*  Lobes are generated in EPSG:3347 and the solid's
``origin_m`` is in whatever CRS the raster stage produced, so pocketing a solid
built in any other CRS is refused rather than carved in the wrong place.
"""

import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import vispy
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
from rasterio.crs import CRS
from rasterio.enums import Resampling
from shapely.affinity import affine_transform
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy,
                               QTabWidget, QVBoxLayout, QWidget)

from lib.LIB_border_connectors import (ConnectorParams, _polys, _repair,
                                       _safe_union, build_border_mesh,
                                       generate_lobes, notch_province)
from lib.LIB_border_regions import build_border_regions
from lib.LIB_border_simplification import topo_simplify_two_tolerances
from lib.LIB_border_split import split_to_bed
from lib.LIB_shared_border import _count_short, _count_spikes
from lib.LIB_geotiff_clip import ClipParams, clip_geotiff
# The cost preview must agree with what the run actually does, so it asks the
# libs for their grids rather than reimplementing the window/warp arithmetic.
from lib.LIB_geotiff_clip import _output_grid as _clip_grid
from lib.LIB_geotiff_clip import _read_window
from lib.LIB_raster_reproject import (ReprojectParams, bounds_for,
                                      reproject_geotiff)
from lib.LIB_raster_reproject import _output_grid as _warp_grid
from lib.LIB_terrain_stl import (DEFAULT_BASE_MM, DEFAULT_EXAGGERATION,
                                 DEFAULT_FLOOR_M, EPSG_METRIC, TerrainParams,
                                 _finish, _triangle_count, _validate, _write,
                                 terrain_to_stl)
from lib.LIB_project_config import get as _cfg

GEOJSON_FILTER = "GeoJSON (*.geojson *.json);;All files (*)"
GEOTIFF_FILTER = "GeoTIFF (*.tif *.tiff);;All files (*)"
STL_FILTER = "STL (*.stl);;All files (*)"

EDGE_COLOUR = "#1B4F72"     # dark blue outline
MARK_COLOUR = "#E6787A"     # red — the boundary about to be cut
DEM_CMAP = "terrain"

#: Target CRS choices. EPSG:3347 (Statistics Canada Lambert) is the print CRS
#: the borders and dovetails are built in; EPSG:3979 is the DEM's own, offered
#: so the difference can be seen rather than argued about.
CRS_CHOICES = ["EPSG:3347", "EPSG:3979"]
#: Warp resampling offered, best-for-terrain first.  ``average`` is the neutral
#: downsample; ``max`` recovers summits a heavy downsample would otherwise lose
#: (1578 m vs 1165 m on Labrador) at the cost of biasing the surface upward.
RESAMPLING_CHOICES = ["average", "max", "nearest", "bilinear", "cubic"]
#: Pixels in the whole-raster overview drawn on the before panel.
OVERVIEW_PX = 1100
#: Above this, the after panel is strided down for display only.
MAX_DISPLAY_PX = 2_000_000
BG_COLOUR = "#FDFEFE"       # near-white 3D scene background
#: Sampling pitch on paper; the Res control is seeded from it via the scale.
DEFAULT_SPACING_MM = _cfg("terrain.spacing_mm")
DEFAULT_RESAMPLING = _cfg("terrain.resampling")

# -- the Borders stage ------------------------------------------------------
EPSG_WORKING = _cfg("crs.working")
KEEP_COLOUR = "#AED6F1"       # blue  — a trimmed target, what gets printed
RESULT_COLOUR = "#ABEBC6"     # green — the dovetail lobes
SELECT_COLOUR = "#F5B041"     # orange — shared (internal) arcs
SURROUND_COLOUR = "#B3B6B7"   # grey  — surrounding regions, never printed
CORRIDOR_COLOUR = "#E74C3C"   # red   — the carved corridor (UI_border_regions)
BAR_COLOUR = (0.55, 0.57, 0.58, 1.0)   # the border bar in the 3D panel
ROLES = ("target", "surrounding")
#: What the 3D panel shows on the STL stage.
SHOW_CHOICES = ["Terrain", "Border bar", "Assembly"]
#: A lobe counts as reaching into a region past this fraction of its own area,
#: so a lobe grazing a neighbour's corner at float precision is not an owner.
_OWNER_FRAC = 1e-3


def _height_colors(mesh):
    """Per-vertex RGBA (float32) from a terrain ramp over the mesh's z."""
    z = np.asarray(mesh.vertices, dtype=float)[:, 2]
    lo, hi = float(z.min()), float(z.max())
    t = (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)
    return colormaps[DEM_CMAP](t).astype(np.float32)


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
    geom = _repair(unary_union(frame.to_crs(EPSG_WORKING).geometry.values))
    if geom is None:
        raise ValueError(f"{Path(path).name} has no polygon to work with.")
    return geom, source_crs


@dataclass
class BorderSettings:
    """What the Borders stage is asked for.  Lengths are **print-mm**."""
    coast_tol_mm: float = _cfg("borders.coast_tol_mm")
    internal_tol_mm: float = _cfg("borders.internal_tol_mm")
    min_segment_mm: float = _cfg("seam.min_segment_mm")
    min_angle_deg: float = _cfg("seam.min_angle_deg")
    width_mm: float = _cfg("borders.width_mm")
    connectors: bool = True
    lobe_spacing_mm: float = _cfg("borders.lobe_spacing_mm")
    scale: float = _cfg("print.scale")

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    def connector_params(self) -> ConnectorParams:
        return ConnectorParams(scale=self.scale,
                               spacing_mm=self.lobe_spacing_mm)


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
    lobes_dropped: int = 0         #: lobes reaching into two targets (FR-3.7)

    def province_lobes(self, name) -> list:
        """The lobes that key into target ``name`` -- its pockets."""
        geom = self.targets.get(name)
        if geom is None:
            return []
        return [lobe for lobe in self.lobes if lobe.intersects(geom)]


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
        border_width_km=s.width_mm * s.scale / 1e6)
    for name, trimmed, before in zip(target_names, regions.core,
                                     simplified_targets):
        result.targets[name] = _repair(trimmed) if trimmed is not None \
            else before
    result.border = _repair(regions.border) if regions.border is not None \
        else None

    if s.connectors and result.border is not None:
        lobes = generate_lobes(result.border,
                               _safe_union(list(result.targets.values())),
                               s.connector_params())
        result.lobes, result.lobes_dropped = _owned_lobes(
            lobes, result.border, list(result.targets.values()))
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


def _owned_lobes(lobes, border, targets):
    """Keep the lobes whose far end lands in exactly one target (FR-3.7)."""
    kept, dropped = [], 0
    for lobe in lobes:
        reach = lobe.difference(border)
        owners = sum(1 for geom in targets
                     if reach.intersects(geom)
                     and reach.intersection(geom).area > _OWNER_FRAC * lobe.area)
        if owners == 1:
            kept.append(lobe)
        else:
            dropped += 1
    return kept, dropped


@dataclass
class BarResult:
    """The border bar, split for the bed (result of :func:`build_bar`)."""
    mesh: object = None
    pieces: list = field(default_factory=list)
    origin_m: tuple = (0.0, 0.0)   #: metres point at the bar's print-mm (0, 0)
    mpm: float = 0.0
    lobes: int = 0
    lobes_cut: int = 0             #: connectors a bed split went through


def build_bar(definition: BorderDefinition, bed_mm=None,
              margin_mm=None) -> BarResult:
    """
    The border bar (corridor + lobe layer), then split to the bed.

    Built by ``build_border_mesh`` with or without lobes, so the bar always sits
    in one local frame whose metres origin is known -- which is what lets the
    3D panel lay a terrain solid beside it.
    """
    if definition.border is None:
        raise ValueError("No corridor to print: the targets share no border "
                         "and touch no surrounding region.")
    params = definition.settings.connector_params()
    lobes = definition.lobes
    mesh = build_border_mesh(definition.border, lobes, params)
    if mesh is None:
        raise ValueError("The corridor is too thin to extrude; widen it.")
    # build_border_pieces puts print-mm (0, 0) at the bottom-left of the
    # corridor and lobes together; mirror that so the origin is known.
    bottom = _safe_union([definition.border] + list(lobes)) if lobes \
        else definition.border
    origin = tuple(bottom.bounds[:2])
    pieces = split_to_bed(
        mesh, _cfg("print.bed_mm") if bed_mm is None else bed_mm,
        _cfg("print.bed_margin_mm") if margin_mm is None else margin_mm)
    result = BarResult(mesh=mesh, pieces=pieces, origin_m=origin,
                       mpm=params.mpm, lobes=len(lobes))
    result.lobes_cut = _lobes_cut(pieces, lobes, origin, params.mpm)
    return result


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


def build_terrain(raster, boundary, out_path, params, lobes=None,
                  connector=None):
    """
    ``terrain_to_stl``, then the dovetail pockets when ``lobes`` are given.

    The solid's ``origin_m`` *is* its metres -> print-mm frame, so the pockets
    go straight in with ``notch_province`` -- no reconciliation.  That only
    holds when the solid was built in the CRS the lobes were (EPSG:3347).
    """
    if not lobes:
        return terrain_to_stl(raster, boundary, out_path, params)
    result = terrain_to_stl(raster, boundary, None, params)
    if CRS.from_user_input(result.crs) != CRS.from_user_input(EPSG_WORKING):
        raise ValueError(
            f"Pockets need the terrain built in {EPSG_WORKING}, where the "
            f"connectors are; this crop is in {result.crs}. Set CRS to "
            f"{EPSG_WORKING} and re-run the crop.")
    connector = connector or ConnectorParams(scale=params.scale)
    mesh = notch_province(result.mesh, lobes, *result.origin_m, connector)
    _finish(mesh, result, params)
    _validate(result, raster)          # FR-4.7 again: the boolean can break it
    if out_path is not None:
        _write(mesh, result, out_path)
    return result


class RunWorker(QObject):
    """Runs the warp and the clip off the UI thread (seconds, on a real DEM)."""
    done = Signal(object)   # ClipResult
    failed = Signal(str)

    def __init__(self, raster, geom, dst_crs, warp_params, clip_params,
                 warp_path, out_path):
        super().__init__()
        self._raster = raster
        self._geom = geom
        self._dst_crs = dst_crs
        self._warp = warp_params
        self._clip = clip_params
        self._warp_path = warp_path
        self._out_path = out_path

    def run(self) -> None:
        try:
            source = self._raster
            if self._warp is not None:
                reproject_geotiff(self._raster, self._dst_crs, self._warp_path,
                                  self._warp)
                source = self._warp_path
            # The boundary arrives as a geometry already in the destination CRS,
            # so neither lib re-reads or re-projects the GeoJSON.
            result = clip_geotiff(source, self._geom, self._out_path,
                                  self._clip, keep_array=True)
        except Exception as exc:      # any raster/geometry failure reaches the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class MeshWorker(QObject):
    """Runs ``terrain_to_stl`` off the UI thread (the boolean cut is slow),
    then carves the pockets when it is handed lobes."""
    done = Signal(object)   # TerrainResult
    failed = Signal(str)

    def __init__(self, raster, boundary, out_path, params, lobes=None,
                 connector=None):
        super().__init__()
        self._raster = raster
        self._boundary = boundary
        self._out_path = out_path
        self._params = params
        self._lobes = lobes
        self._connector = connector

    def run(self) -> None:
        try:
            result = build_terrain(self._raster, self._boundary,
                                   self._out_path, self._params, self._lobes,
                                   self._connector)
        except Exception as exc:      # any raster/geometry/mesh failure
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class CallWorker(QObject):
    """Runs one pure function off the UI thread -- the Borders stage's
    topology + carve, and the border bar's extrude + split."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args

    def run(self) -> None:
        try:
            result = self._fn(*self._args)
        except Exception as exc:      # any geometry/mesh failure reaches the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class TerrainEditorTester(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Terrain Editor — Visual Tester")
        self.resize(1250, 720)

        # -- state ----------------------------------------------------------
        self.raster_path: Path | None = None
        self.raster_crs = None
        self.raster_bounds = None            #: (l, b, r, t) in the raster CRS
        self.raster_shape = (0, 0)           #: (width, height) in pixels
        self.raster_res = (0.0, 0.0)         #: native metres per pixel
        self.overview = None                 #: masked array for the before panel
        self.overview_extent = None
        self.boundary: gpd.GeoDataFrame | None = None
        self.boundary_path: Path | None = None
        self.result = None                   #: last ClipResult (raster stage)
        self.stl_result = None               #: last TerrainResult (mesh stage)
        self._tempdir = Path(tempfile.mkdtemp(prefix="ui_terrain_editor_"))
        self._thread: QThread | None = None
        self._worker = None                  #: RunWorker, MeshWorker, CallWorker
        self._pockets = 0                    #: pockets carved into the solid
        self._colorbar = None
        self._before_drawn = False   #: has the DEM panel been painted yet?
        self._solid_meshes = []      #: vispy visuals, detached on each rebuild
        # Stage 1/2 state.  Regions are held in crs.working; their files and
        # source CRSs are kept so saving writes back what came in.
        self.targets: dict = {}              #: name -> geometry, printed
        self.surrounding: dict = {}          #: name -> geometry, context only
        self.region_paths: dict = {}         #: name -> Path it was read from
        self.region_crs: dict = {}           #: name -> CRS it arrived in
        self.borders: BorderDefinition | None = None
        self.bar: BarResult | None = None    #: the border bar, split
        self._outline_cache: dict = {}       #: (name, crs) -> reprojected geom
        self._boundary_is_region = False     #: boundary came from the combo?

        central = QWidget()
        root = QVBoxLayout(central)

        # -- stage 1: import -------------------------------------------------
        self.btn_raster = QPushButton("Import GeoTIFF …")
        self.btn_raster.clicked.connect(self.import_geotiff)
        self.btn_add_targets = QPushButton("Add Provinces …")
        self.btn_add_targets.setToolTip(
            "Provinces to print. Several at once; each file is one region.")
        self.btn_add_targets.clicked.connect(
            lambda: self.add_regions_dialog("target"))
        self.btn_add_surrounding = QPushButton("Add Surrounding …")
        self.btn_add_surrounding.setToolTip(
            "Context regions (US states, unprinted provinces). They shape the\n"
            "borders -- the corridor is carved from their side -- but are\n"
            "never printed and get no connectors.")
        self.btn_add_surrounding.clicked.connect(
            lambda: self.add_regions_dialog("surrounding"))
        self.btn_boundary = QPushButton("Import Boundary …")
        self.btn_boundary.setToolTip(
            "Add one province and select it for the STL stage -- the "
            "single-province run.")
        self.btn_boundary.clicked.connect(self.import_boundary)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self.remove_selected_region)
        self.region_list = QListWidget()
        self.region_list.setMaximumHeight(90)

        import_bar = QHBoxLayout()
        for widget in (self.btn_raster, self.btn_add_targets,
                       self.btn_add_surrounding, self.btn_boundary,
                       self.btn_remove):
            import_bar.addWidget(widget)
        import_bar.addStretch(1)
        import_page = QWidget()
        import_layout = QVBoxLayout(import_page)
        import_layout.addLayout(import_bar)
        import_layout.addWidget(self.region_list)

        # -- stage 2: borders ------------------------------------------------
        defaults = BorderSettings()
        self.coast_spin = self._mm_spin(
            defaults.coast_tol_mm, 0.0, 20.0,
            "Simplification tolerance for arcs one region holds alone --\n"
            "coasts and frontiers with nothing printed beyond (print-mm).")
        self.internal_spin = self._mm_spin(
            defaults.internal_tol_mm, 0.0, 20.0,
            "Simplification tolerance for arcs two regions share (print-mm).\n"
            "Both sides use the one arc, so a seam cannot open a gap.")
        self.min_seg_spin = self._mm_spin(
            defaults.min_segment_mm, 0.0, 5.0,
            "Seam clean-up: collapse shared-arc runs shorter than this.\n"
            "Douglas-Peucker keeps a tight zigzag inside its corridor; this\n"
            "removes it. 0 skips the pass (seam.min_segment_mm).")
        self.min_angle_spin = self._spin(
            0.0, 90.0, defaults.min_angle_deg, 0,
            "Seam clean-up: drop shared-arc corners sharper than this.\n"
            "A needle prints as a spur on one side and a slot on the other.\n"
            "0 skips the pass (seam.min_angle_deg).")
        self.min_angle_spin.setSuffix("°")
        self.width_spin = self._mm_spin(
            defaults.width_mm, 0.1, 20.0,
            "Corridor width carved between regions -- the border bar's width.\n"
            "Target/target seams split it evenly; target/surrounding takes it\n"
            "all from the surrounding side.")
        self.chk_connectors = QCheckBox("Connectors")
        self.chk_connectors.setChecked(True)
        self.chk_connectors.setToolTip(
            "Place dovetail lobes on the corridor and pockets in the provinces.")
        self.spacing_spin = self._mm_spin(
            defaults.lobe_spacing_mm, 2.0, 100.0,
            "Gap between dovetail keys along the corridor.")
        for widget in (self.coast_spin, self.internal_spin, self.min_seg_spin,
                       self.min_angle_spin, self.width_spin,
                       self.spacing_spin):
            widget.valueChanged.connect(self._invalidate_borders)
        self.chk_connectors.toggled.connect(self._invalidate_borders)
        self.btn_define = QPushButton("Define Borders")
        self.btn_define.clicked.connect(self.define)
        self.btn_define.setEnabled(False)
        self.btn_save_borders = QPushButton("Save Borders …")
        self.btn_save_borders.setToolTip(
            "borders.geojson + name_<province>.geojson, the layout the\n"
            "dovetail tools read (build_border_from_dir).")
        self.btn_save_borders.clicked.connect(self.save_borders)
        self.btn_save_borders.setEnabled(False)
        self.border_report = QLabel("Add provinces on the Import tab.")
        self.border_report.setWordWrap(True)
        self.border_report.setStyleSheet("color:#555;")

        border_bar = QHBoxLayout()
        for label, widget in (("Coast:", self.coast_spin),
                              ("Internal:", self.internal_spin),
                              ("Min seg:", self.min_seg_spin),
                              ("Min angle:", self.min_angle_spin),
                              ("Width:", self.width_spin)):
            border_bar.addWidget(QLabel(label))
            border_bar.addWidget(widget)
        border_bar.addWidget(self.chk_connectors)
        border_bar.addWidget(QLabel("Spacing:"))
        border_bar.addWidget(self.spacing_spin)
        border_bar.addWidget(self.btn_define)
        border_bar.addWidget(self.btn_save_borders)
        border_bar.addStretch(1)
        borders_page = QWidget()
        borders_layout = QVBoxLayout(borders_page)
        borders_layout.addLayout(border_bar)
        borders_layout.addWidget(self.border_report)

        # -- stage 3: STL -- the original harness ----------------------------
        toolbar = QHBoxLayout()
        self.province_combo = QComboBox()
        self.province_combo.setMinimumContentsLength(14)
        self.province_combo.setToolTip(
            "The province the crop and the solid are built for. Once borders\n"
            "are defined it is cut to the province with the corridor removed.")
        self.province_combo.currentTextChanged.connect(self._select_province)

        self.crs_combo = QComboBox()
        self.crs_combo.setEditable(True)
        self.crs_combo.addItems(CRS_CHOICES)
        self.crs_combo.setToolTip(
            "CRS the output is built in.\nEPSG:3347 is this project's print CRS "
            "— the one the borders and dovetails\nare built in. Leaving the DEM "
            "in EPSG:3979 misplaces a province by ~5 mm.")
        self.crs_combo.currentTextChanged.connect(self._invalidate_raster)

        # 0 m means "native resolution" — the libs' own convention.
        # Seeded from terrain.spacing_mm through the scale: 0.4 mm at 1:8M is
        # 3200 m, so the default Res is the configured print pitch, not a magic
        # number that could drift from the meshing stage's idea of spacing.
        self.res_spin = self._spin(
            0.0, 100_000.0, DEFAULT_SPACING_MM * _cfg("print.scale") / 1000.0, 0,
                                   "Metres per pixel; 0 = the DEM's native "
                                   "resolution.\n1:8,000,000 with a 0.4 mm "
                                   "nozzle wants ~3200 m.")
        self.res_spin.setSingleStep(100.0)
        self.res_spin.setSpecialValueText("native")
        self.res_spin.setSuffix(" m/px")
        self.res_spin.valueChanged.connect(self._invalidate_raster)

        self.buf_spin = self._spin(0.0, 100_000.0, 0.0, 0,
                                   "Grow the boundary outward before cutting, "
                                   "to keep a coastal lip")
        self.buf_spin.setSingleStep(100.0)
        self.buf_spin.setSuffix(" m")
        self.buf_spin.valueChanged.connect(self._invalidate_raster)

        self.resample_combo = QComboBox()
        self.resample_combo.addItems(RESAMPLING_CHOICES)
        self.resample_combo.setCurrentText(DEFAULT_RESAMPLING)
        self.resample_combo.setToolTip(
            "How the warp resamples.\n"
            "Warping straight to print resolution is a big downsample, so the "
            "choice matters:\n"
            "  max       keeps summits (1578 m on Labrador)\n"
            "  average   the neutral downsample (1265 m)\n"
            "  bilinear  loses the most (1165 m; true peak is 1652 m)\n"
            "Applies whenever the output is coarser than the DEM, whether or "
            "not the CRS changes.")
        self.resample_combo.currentTextChanged.connect(self._invalidate_raster)

        self.chk_touched = QCheckBox("All-touched")
        self.chk_touched.setToolTip(
            "Keep pixels the boundary merely touches, not only those it covers")
        self.chk_touched.toggled.connect(self._invalidate_raster)
        self.chk_crop = QCheckBox("Crop")
        self.chk_crop.setChecked(True)
        self.chk_crop.setToolTip(
            "Crop to the boundary's bounding box.\nUntick to keep the DEM's "
            "full extent, which shows where the province sits in it.")
        self.chk_crop.toggled.connect(self._invalidate_raster)

        self.cache_spin = self._spin(0.0, 8192.0, 0.0, 0,
                                     "Cap GDAL's block cache for the run.\n"
                                     "0 leaves GDAL's default (5% of RAM); on "
                                     "a striped DEM that is the difference\n"
                                     "between a ~1.9 GB and a ~256 MB peak.")
        self.cache_spin.setSingleStep(64.0)
        self.cache_spin.setSpecialValueText("default")
        self.cache_spin.setSuffix(" MB")

        self.btn_run = QPushButton("Reproject + Crop")
        self.btn_run.clicked.connect(self.run)
        self.btn_run.setEnabled(False)
        self.btn_save = QPushButton("Save GeoTIFF …")
        self.btn_save.clicked.connect(self.save_geotiff)
        self.btn_save.setEnabled(False)

        toolbar.addWidget(QLabel("Province:"))
        toolbar.addWidget(self.province_combo)
        toolbar.addWidget(QLabel("CRS:"))
        toolbar.addWidget(self.crs_combo)
        toolbar.addWidget(QLabel("Res:"))
        toolbar.addWidget(self.res_spin)
        toolbar.addWidget(QLabel("Buffer:"))
        toolbar.addWidget(self.buf_spin)
        toolbar.addWidget(QLabel("Resample:"))
        toolbar.addWidget(self.resample_combo)
        toolbar.addWidget(self.chk_touched)
        toolbar.addWidget(self.chk_crop)
        toolbar.addWidget(QLabel("Cache:"))
        toolbar.addWidget(self.cache_spin)
        toolbar.addWidget(self.btn_run)
        toolbar.addWidget(self.btn_save)
        toolbar.addStretch(1)

        # -- second row: the mesh stage -------------------------------------
        # A second row rather than one long one: these controls act on a
        # different stage with a different invalidation rule, and putting them
        # beside the raster controls would imply they re-warp the DEM.
        mesh_bar = QHBoxLayout()
        self.exag_spin = self._spin(
            1.0, 200.0, DEFAULT_EXAGGERATION, 1,
            "Vertical exaggeration (terrain.exaggeration).\n"
            "At 1:8,000,000 true scale (x1) is 0.20 mm of relief on\n"
            "Newfoundland — invisible. Share one value across a whole map.")
        self.exag_spin.setSingleStep(5.0)
        self.exag_spin.valueChanged.connect(self._invalidate_mesh)

        self.base_spin = self._spin(
            0.1, 50.0, DEFAULT_BASE_MM, 2,
            "Solid base under the terrain (terrain.base_mm).\n"
            "Must exceed the dovetail pocket depth, or the pocket cuts\n"
            "through the bottom of the piece.")
        self.base_spin.setSingleStep(0.5)
        self.base_spin.setSuffix(" mm")
        self.base_spin.valueChanged.connect(self._invalidate_mesh)

        self.floor_spin = self._spin(
            -1000.0, 9000.0, DEFAULT_FLOOR_M, 0,
            "Elevation the base measures from (terrain.floor_m).\n"
            "Share one value across a map, or neighbouring pieces step\n"
            "at the seam.")
        self.floor_spin.setSingleStep(50.0)
        self.floor_spin.setSuffix(" m")
        self.floor_spin.valueChanged.connect(self._invalidate_mesh)

        self.chk_cut = QCheckBox("Cut coastline")
        self.chk_cut.setChecked(True)
        self.chk_cut.setToolTip(
            "Cut the solid to the boundary.\n"
            "Untick for the rectangular slab DEMto3D produces, to compare\n"
            "old and new pieces.")
        self.chk_cut.toggled.connect(self._invalidate_mesh)

        self.chk_pockets = QCheckBox("Pockets")
        self.chk_pockets.setChecked(True)
        self.chk_pockets.setToolTip(
            "Carve the dovetail pockets this province's connectors key into.\n"
            "Needs borders defined with connectors, and the crop in "
            f"{EPSG_WORKING}.")
        self.chk_pockets.toggled.connect(self._invalidate_mesh)

        self.btn_build = QPushButton("Build STL")
        self.btn_build.clicked.connect(self.build_stl)
        self.btn_build.setEnabled(False)
        self.btn_save_stl = QPushButton("Save STL …")
        self.btn_save_stl.clicked.connect(self.save_stl)
        self.btn_save_stl.setEnabled(False)

        mesh_bar.addWidget(QLabel("Mesh:"))
        mesh_bar.addWidget(QLabel("Exag:"))
        mesh_bar.addWidget(self.exag_spin)
        mesh_bar.addWidget(QLabel("Base:"))
        mesh_bar.addWidget(self.base_spin)
        mesh_bar.addWidget(QLabel("Floor:"))
        mesh_bar.addWidget(self.floor_spin)
        mesh_bar.addWidget(self.chk_cut)
        mesh_bar.addWidget(self.chk_pockets)
        mesh_bar.addWidget(self.btn_build)
        mesh_bar.addWidget(self.btn_save_stl)
        mesh_bar.addStretch(1)

        # -- third row: the border bar --------------------------------------
        # Built once for the whole map, not per province, so it has its own
        # row and its own result; nothing on the rows above stales it.
        self.btn_build_bar = QPushButton("Build Border STL")
        self.btn_build_bar.setToolTip(
            "Extrude the corridor (and its connector layer), then split it\n"
            f"to the {_cfg('print.bed_mm'):.0f} mm bed (print.bed_mm, "
            "print.bed_margin_mm).")
        self.btn_build_bar.clicked.connect(self.build_border_bar)
        self.btn_build_bar.setEnabled(False)
        self.btn_save_bar = QPushButton("Save Border Pieces …")
        self.btn_save_bar.clicked.connect(self.save_border_pieces)
        self.btn_save_bar.setEnabled(False)
        self.show_combo = QComboBox()
        self.show_combo.addItems(SHOW_CHOICES)
        self.show_combo.setToolTip(
            "What the 3D panel shows. Assembly lays the solid beside the bar\n"
            "in one frame, where a pocket that missed its lobe is visible.")
        self.show_combo.currentTextChanged.connect(self._refresh_3d)

        bar_row = QHBoxLayout()
        bar_row.addWidget(QLabel("Border:"))
        bar_row.addWidget(self.btn_build_bar)
        bar_row.addWidget(self.btn_save_bar)
        bar_row.addWidget(QLabel("3D:"))
        bar_row.addWidget(self.show_combo)
        bar_row.addStretch(1)

        stl_page = QWidget()
        stl_layout = QVBoxLayout(stl_page)
        stl_layout.addLayout(toolbar)
        stl_layout.addLayout(mesh_bar)
        stl_layout.addLayout(bar_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(import_page, "1 · Import")
        self.tabs.addTab(borders_page, "2 · Borders")
        self.tabs.addTab(stl_page, "3 · STL")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs)

        # -- map: before | after --------------------------------------------
        # Deliberately NOT sharex/sharey. Once a warp is in play the panels are
        # in different CRSs — Newfoundland sits at x ~1.5e6 in EPSG:3979 and
        # ~7.1e6 in EPSG:3347 — so shared limits would drive the result clean off
        # the canvas. They are linked explicitly instead, and only when the two
        # panels really are in the same frame (see _link_views).
        self.figure = Figure(figsize=(11, 6))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax_before = self.figure.add_subplot(1, 2, 1)
        self.ax_after = self.figure.add_subplot(1, 2, 2)
        self._reset_axes("Import a GeoTIFF to begin", "")

        self.mpl_toolbar = NavigationToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)

        # DEM + cropped raster on the left, the finished solid on the right.
        # The 3-D panel is vispy, not mplot3d: a province is ~200k triangles and
        # matplotlib re-sorts every one of them on each rotate.
        panels = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.mpl_toolbar)
        left.addWidget(self.canvas, stretch=1)
        panels.addLayout(left, stretch=2)

        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("3D preview")
        self.solid_title.setStyleSheet("color:#555;")
        right = QVBoxLayout()
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)
        root.addLayout(panels, stretch=1)

        self.status = QLabel("No DEM loaded.")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        return canvas, view

    @staticmethod
    def _spin(lo, hi, value, decimals, tip) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setToolTip(tip)
        return spin

    @classmethod
    def _mm_spin(cls, value, lo, hi, tip) -> QDoubleSpinBox:
        """A print-mm length, to the 0.05 mm a 0.4 mm nozzle can mean."""
        spin = cls._spin(lo, hi, value, 2, tip)
        spin.setSingleStep(0.05)
        spin.setSuffix(" mm")
        return spin

    # -- stage 1: regions ---------------------------------------------------
    def add_regions_dialog(self, role: str) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Add {role} regions", self._regions_dir(), GEOJSON_FILTER)
        if paths:
            self.add_regions([Path(p) for p in paths], role)

    @staticmethod
    def _regions_dir() -> str:
        """Where cleaned provinces land -- the right input (FR-4.3)."""
        out = Path(__file__).resolve().parent / _cfg("paths.provinces_out")
        return str(out if out.is_dir() else Path.cwd())

    def add_regions(self, paths, role: str) -> list:
        """
        Read each file as one ``role`` region; returns the names added.

        Re-adding a name replaces it, in whichever role it now arrives, so a
        region cannot be both printed and context (FR-1.8).  A file that will
        not read is reported and skipped rather than failing the batch.
        """
        assert role in ROLES
        added, failures = [], []
        for path in paths:
            path = Path(path)
            try:
                geom, source_crs = load_region(path)
            except Exception as exc:
                failures.append(f"{path.name}: {exc}")
                continue
            name = path.stem
            self.targets.pop(name, None)
            self.surrounding.pop(name, None)
            (self.targets if role == "target" else self.surrounding)[name] = geom
            self.region_paths[name] = path
            self.region_crs[name] = source_crs
            added.append(name)
        if failures:
            QMessageBox.warning(self, "Some regions were not added",
                                "\n".join(failures))
        if added:
            self._regions_changed()
        return added

    def remove_selected_region(self) -> None:
        item = self.region_list.currentItem()
        if item is not None:
            self.remove_region(item.data(Qt.UserRole))

    def remove_region(self, name: str) -> None:
        if self.targets.pop(name, None) is None \
                and self.surrounding.pop(name, None) is None:
            return
        self.region_paths.pop(name, None)
        self.region_crs.pop(name, None)
        self._regions_changed()

    def _regions_changed(self) -> None:
        """Any region added or removed: stage 2 and all of stage 3 go stale."""
        self._outline_cache.clear()
        self.borders = None
        self._invalidate_bar()
        self._refresh_region_list()
        self._refresh_provinces()
        self._update_border_controls()
        self.border_report.setText(self._regions_text())
        if self._showing_borders():
            self._draw_borders()
        else:
            self._draw_before(preserve_view=True)

    def _refresh_region_list(self) -> None:
        self.region_list.clear()
        for role, regions in zip(ROLES, (self.targets, self.surrounding)):
            for name, geom in regions.items():
                self.region_list.addItem(
                    f"{name} — {role}, {_vertex_count(geom):,} vertices")
                self.region_list.item(self.region_list.count() - 1) \
                    .setData(Qt.UserRole, name)

    def _regions_text(self) -> str:
        if not self.targets:
            return "Add provinces on the Import tab."
        names = list(self.targets)
        geoms = list(self.targets.values())
        pairs, lonely = 0, []
        for i, name in enumerate(names):
            touches = False
            for j, other in enumerate(geoms):
                if i != j and geoms[i].intersects(other):
                    touches = True
                    pairs += j > i
            if not touches and not any(geoms[i].intersects(g)
                                       for g in self.surrounding.values()):
                lonely.append(name)
        text = (f"{len(names)} province(s) to print, {len(self.surrounding)} "
                f"surrounding; {pairs} printed pair(s) share a border.")
        if lonely:
            # Legitimate (an island), but it gets no seam, so say so.
            text += f" Touching nothing, so no corridor: {', '.join(lonely)}."
        return text + " Set the tolerances and click ‘Define Borders’."

    # -- stage 2: borders ---------------------------------------------------
    def border_settings(self) -> BorderSettings:
        return BorderSettings(
            coast_tol_mm=self.coast_spin.value(),
            internal_tol_mm=self.internal_spin.value(),
            min_segment_mm=self.min_seg_spin.value(),
            min_angle_deg=self.min_angle_spin.value(),
            width_mm=self.width_spin.value(),
            connectors=self.chk_connectors.isChecked(),
            lobe_spacing_mm=self.spacing_spin.value(),
            scale=_cfg("print.scale"))

    def define(self) -> None:
        """Run the Borders stage off the UI thread."""
        if self._thread is not None or not self.targets:
            return
        self._set_busy(True)
        self.border_report.setText("Simplifying, carving the corridor, "
                                   "placing connectors …")
        self._launch(CallWorker(define_borders, dict(self.targets),
                                dict(self.surrounding),
                                self.border_settings()),
                     self._on_borders)

    def _on_borders(self, definition) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.apply_borders(definition)

    def apply_borders(self, definition: BorderDefinition) -> None:
        """Adopt a border definition: report it, draw it, hand it to stage 3."""
        self.borders = definition
        self._invalidate_bar()
        self._update_border_controls()
        self.border_report.setText(self._borders_text())
        # The selected province is now the trimmed one, so the crop and the
        # solid describe land that is no longer the province.
        self._select_province(self.province_combo.currentText())
        if self._showing_borders():
            self._draw_borders()

    def _borders_text(self) -> str:
        d = self.borders
        s = d.settings
        text = (f"{len(d.simplified)} regions: {d.vertices_before:,} → "
                f"{d.vertices_after:,} vertices. Seams: {d.short_segments} "
                f"run(s) under {_cfg('seam.nozzle_mm'):.2f} mm, {d.spikes} "
                f"spike(s) under {_cfg('seam.spike_angle_deg'):.0f}°. "
                + (f"{d.overlap_mm2:.2f} mm² claimed by two provinces, handed "
                   "back to the one that held it." if d.overlap_mm2 > 1e-4
                   else "No overlap."))
        if d.border is None:
            text += " No corridor: the provinces touch nothing."
        else:
            text += f" Corridor {s.width_mm:.2f} mm"
            if s.connectors:
                text += f", {len(d.lobes)} connector(s)"
                if d.lobes_dropped:
                    text += (f" ({d.lobes_dropped} dropped: they reached "
                             "into two provinces)")
            text += "."
        return text + " Save them, or build the STLs on tab 3."

    def _invalidate_borders(self) -> None:
        """A stage-2 setting changed: the definition and everything built from
        it describe borders that are no longer asked for."""
        if self.borders is None and self.bar is None:
            return
        self.borders = None
        self._invalidate_bar()
        self._update_border_controls()
        self.border_report.setText(self._regions_text())
        self._select_province(self.province_combo.currentText())
        if self._showing_borders():
            self._draw_borders()

    def _update_border_controls(self) -> None:
        busy = self._thread is not None
        self.tabs.setTabEnabled(1, bool(self.targets))
        self.btn_define.setEnabled(bool(self.targets) and not busy)
        self.btn_save_borders.setEnabled(self.borders is not None and not busy)
        self.btn_build_bar.setEnabled(
            self.borders is not None and self.borders.border is not None
            and not busy)
        self.btn_save_bar.setEnabled(self.bar is not None and not busy)
        # Buffer grows the cut outward, and outward is the corridor.
        if self.borders is not None:
            self.buf_spin.blockSignals(True)
            self.buf_spin.setValue(0.0)
            self.buf_spin.blockSignals(False)
        self.buf_spin.setEnabled(self.borders is None and not busy)

    def save_borders(self) -> None:
        if self.borders is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save borders into folder", str(Path.cwd()))
        if not folder:
            return
        try:
            written = self.write_borders(Path(folder))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(written)} file(s) to {folder}.")

    def write_borders(self, folder: Path) -> list:
        """
        ``borders.geojson`` + ``name_<province>.geojson`` per trimmed target,
        each in the CRS its province arrived in -- the folder layout
        ``build_border_from_dir`` and ``fit_dovetails_from_dir`` read.
        """
        folder.mkdir(parents=True, exist_ok=True)
        default_crs = next(iter(self.region_crs.values()), EPSG_WORKING)
        written = []

        def write(name, geom, crs, path):
            frame = gpd.GeoDataFrame({"name": [name]}, geometry=[geom],
                                     crs=EPSG_WORKING).to_crs(crs)
            frame.to_file(path, driver="GeoJSON")
            written.append(path)

        if self.borders.border is not None:
            write("border", self.borders.border, default_crs,
                  folder / "borders.geojson")
        for name, geom in self.borders.targets.items():
            write(name, geom, self.region_crs.get(name, default_crs),
                  folder / f"name_{_safe_name(name)}.geojson")
        return written

    # -- stage 3: which province --------------------------------------------
    def _refresh_provinces(self) -> None:
        """Rebuild the Province combo, keeping the selection where it can."""
        current = self.province_combo.currentText()
        self.province_combo.blockSignals(True)
        self.province_combo.clear()
        self.province_combo.addItems(list(self.targets))
        if current in self.targets:
            self.province_combo.setCurrentText(current)
        self.province_combo.blockSignals(False)
        self._select_province(self.province_combo.currentText())

    def province_geom(self, name: str):
        """What stage 3 cuts to: the trimmed province once borders exist."""
        if self.borders is not None and name in self.borders.targets:
            return self.borders.targets[name]
        return self.targets.get(name)

    def _select_province(self, name: str) -> None:
        """Make ``name`` the boundary the crop and the solid are cut to."""
        geom = self.province_geom(name) if name else None
        if geom is not None:
            self.boundary = gpd.GeoDataFrame(geometry=[geom], crs=EPSG_WORKING)
            self.boundary_path = self.region_paths.get(name)
            self._boundary_is_region = True
        elif self._boundary_is_region:
            # The province it came from is gone. A boundary set some other way
            # is left alone -- nothing on this tab put it there.
            self.boundary = None
            self.boundary_path = None
            self._boundary_is_region = False
        self._update_border_controls()
        self._invalidate_raster()
        if not self._showing_borders():
            self._draw_before(preserve_view=True)

    def province_lobes(self) -> list:
        """The lobes the selected province's pockets key into, if asked for."""
        if self.borders is None or not self.chk_pockets.isChecked():
            return []
        return self.borders.province_lobes(self.province_combo.currentText())

    # -- stage 3: the border bar --------------------------------------------
    def build_border_bar(self) -> None:
        if self._thread is not None or self.borders is None:
            return
        self._set_busy(True)
        self.status.setText("Extruding the border and splitting it to the "
                            "bed …")
        self._launch(CallWorker(build_bar, self.borders), self._on_bar)

    def _on_bar(self, bar) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.apply_bar(bar)

    def apply_bar(self, bar: BarResult) -> None:
        self.bar = bar
        self._update_border_controls()
        w, h, _ = bar.mesh.extents
        text = (f"Border bar {w:.0f} x {h:.0f} mm, {bar.lobes} connector(s), "
                f"split into {len(bar.pieces)} bed piece(s).")
        if bar.lobes_cut:
            # FR-3.11 is not enforced by the splitter; never let it pass quietly.
            text += (f" WARNING: {bar.lobes_cut} connector(s) cut by a split "
                     "-- the splitter cannot yet avoid them.")
        self.status.setText(text)
        if self.show_combo.currentText() == SHOW_CHOICES[0]:
            self.show_combo.setCurrentText(SHOW_CHOICES[1])
        else:
            self._refresh_3d()

    def _invalidate_bar(self) -> None:
        self.bar = None
        if hasattr(self, "btn_save_bar"):
            self._update_border_controls()
            self._refresh_3d()

    def save_border_pieces(self) -> None:
        if self.bar is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Save border pieces into folder", str(Path.cwd()))
        if not folder:
            return
        try:
            for i, piece in enumerate(self.bar.pieces, start=1):
                piece.export(str(Path(folder) / f"border_{i:03d}.stl"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(f"Saved {len(self.bar.pieces)} border piece(s) "
                            f"to {folder}.")

    # -- tabs ---------------------------------------------------------------
    def _showing_borders(self) -> bool:
        return hasattr(self, "tabs") and self.tabs.currentIndex() == 1

    def _on_tab(self, index: int) -> None:
        """Tab 2 draws the borders; tabs 1 and 3 share the raster view."""
        if index == 1:
            self._draw_borders()
            # The borders view is in another frame; a later "keep the view"
            # on the raster panel must not restore its limits there.
            self._before_drawn = False
        else:
            self._draw_before()
            self._draw_after()

    # -- CRS ----------------------------------------------------------------
    def target_crs(self):
        """The chosen destination CRS, or None if it is not parseable yet."""
        text = self.crs_combo.currentText().strip()
        if not text:
            return None
        try:
            return CRS.from_user_input(text)
        except Exception:
            return None          # half-typed EPSG code; the preview just waits

    def needs_warp(self) -> bool:
        """Whether the output lands in a different CRS than the DEM.

        This is the *frame* question, so it is what the panel-linking and the
        CRS labels key off — not what decides which code path runs.
        """
        dst = self.target_crs()
        return (dst is not None and self.raster_crs is not None
                and dst != self.raster_crs)

    def reduction(self) -> float:
        """Requested output resolution as a multiple of the DEM's native one."""
        native = self.raster_res[0]
        target = self.res_spin.value()
        if not native or target <= 0:
            return 1.0
        return max(1.0, target / native)

    def needs_resample(self) -> bool:
        """Whether the run goes through the warper rather than plain decimation.

        A reprojection obviously needs it — but so does a pure **downsample**.
        ``LIB_geotiff_clip`` can only decimate nearest-neighbour (GDAL's RasterIO
        path cannot skip nodata), which costs 1363 m of Mount Caubvick against
        the 1578 m ``max`` recovers.  Routing every coarsening through the
        warper is what keeps the Resample control meaningful instead of
        silently inert whenever the target CRS happened to match the DEM's.
        """
        return self.needs_warp() or self.reduction() > 1.001

    # -- axes helpers -------------------------------------------------------
    def _reset_axes(self, before_title: str, after_title: str) -> None:
        for ax, title in ((self.ax_before, before_title),
                          (self.ax_after, after_title)):
            self._clear_ax(ax, title)
        self.canvas.draw_idle()

    @staticmethod
    def _clear_ax(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])

    def _link_views(self) -> None:
        """Match the after panel to the before panel, when both are one frame.

        Replaces the ``sharex``/``sharey`` the other harnesses use, which cannot
        express "aligned only while the projections agree".
        """
        if not self._showing_borders() and (self.needs_warp()
                                            or self.result is None):
            return
        self.ax_after.set_xlim(self.ax_before.get_xlim())
        self.ax_after.set_ylim(self.ax_before.get_ylim())

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        if ax is self.ax_before:
            self._link_views()
        self.canvas.draw_idle()

    # -- imports ------------------------------------------------------------
    def import_geotiff(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import DEM GeoTIFF", str(Path.cwd()), GEOTIFF_FILTER)
        if not path:
            return
        try:
            self._load_raster(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Could not open the GeoTIFF", str(exc))
            return

        self._invalidate()
        self._draw_before()
        self.status.setText(
            f"{self.raster_path.name}: {self.raster_shape[0]}x"
            f"{self.raster_shape[1]} @ {self.raster_res[0]:.0f} m, "
            f"{self.raster_crs}. Import a boundary to crop against.")

    def _load_raster(self, path: Path) -> None:
        """
        Read the DEM's metadata and one decimated overview — never the file.

        A whole-raster overview is cheap even at 7.3 GB: the source is striped,
        so GDAL seeks straight past the scanlines the decimation skips.
        """
        with rasterio.open(str(path)) as ds:
            self.raster_crs = ds.crs
            self.raster_bounds = tuple(ds.bounds)
            self.raster_shape = (ds.width, ds.height)
            self.raster_res = ds.res
            factor = max(ds.width / OVERVIEW_PX, ds.height / OVERVIEW_PX, 1.0)
            shape = (max(1, int(ds.height / factor)),
                     max(1, int(ds.width / factor)))
            data = ds.read(1, out_shape=shape, resampling=Resampling.nearest,
                           masked=True)
        self.raster_path = path
        self.overview = data
        left, bottom, right, top = self.raster_bounds
        self.overview_extent = (left, right, bottom, top)

    def import_boundary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import boundary GeoJSON", str(Path.cwd()), GEOJSON_FILTER)
        if not path:
            return
        added = self.add_regions([Path(path)], "target")
        if not added:
            return
        # The single-province run: add it, select it, go straight to stage 3.
        self.province_combo.setCurrentText(added[0])
        self.status.setText(f"Boundary: {Path(path).name}. "
                            + self._cost_text())

    def _boundary_geom(self, crs=None):
        """The boundary dissolved to one geometry in ``crs``, buffered."""
        crs = crs if crs is not None else self.raster_crs
        if self.boundary is None or crs is None:
            return None
        frame = self.boundary
        # The province GeoJSON is WGS84 while the DEM is EPSG:3979 and the print
        # CRS is EPSG:3347; without this the read window comes out empty.
        if frame.crs is not None and frame.crs != crs:
            frame = frame.to_crs(crs)
        geom = unary_union(frame.geometry.values)
        buffer_m = self.buf_spin.value()
        return geom.buffer(buffer_m) if buffer_m else geom

    def _output_geom(self):
        """The boundary in whichever CRS the output will be built in."""
        return self._boundary_geom(self.target_crs() if self.needs_warp()
                                   else self.raster_crs)

    # -- the plan both the preview and the run follow -----------------------
    def _plan(self):
        """
        The (warp_params | None, clip_params) the current settings imply.

        Preview and run share this so they cannot disagree.  When a warp runs it
        is handed the boundary's bounds, so the warp *is* the bbox crop and the
        clip is left to apply only the polygon mask — which is also why the
        final grid equals the warped grid exactly, and the preview can promise
        the number the run produces.
        """
        res = self.res_spin.value()
        cache = int(self.cache_spin.value())
        if not self.needs_resample():
            return None, ClipParams(crop=self.chk_crop.isChecked(),
                                    all_touched=self.chk_touched.isChecked(),
                                    target_res_m=res, gdal_cache_mb=cache)
        # On a pure downsample this is the raster's own CRS, so the warp
        # changes only scale — exactly what downsample_geotiff does.
        dst = self.target_crs()
        bounds = (bounds_for(self._boundary_geom(dst), dst)
                  if self.chk_crop.isChecked() else None)
        warp = ReprojectParams(dst_res_m=res, bounds=bounds,
                               resampling=Resampling[
                                   self.resample_combo.currentText()],
                               gdal_cache_mb=cache)
        # crop=False: the warp already cut to the bbox, and target_res_m=0
        # because the warp already produced the requested resolution.
        clip = ClipParams(crop=False, all_touched=self.chk_touched.isChecked(),
                          target_res_m=0.0, gdal_cache_mb=cache)
        return warp, clip

    def _cost_text(self) -> str:
        """
        What the current settings would cost, asked of the libs' own grid math.

        Re-deriving the grids here would let the preview drift from the run it
        is previewing, so this opens the dataset (metadata only, ~20 ms) and
        calls the same helpers the libs use.
        """
        if self.raster_path is None or self.boundary is None:
            return "Set the parameters, then click ‘Reproject + Crop’."
        dst = self.target_crs()
        if dst is None:
            return "Target CRS not recognised yet."
        warp, clip = self._plan()
        try:
            with rasterio.open(str(self.raster_path)) as ds:
                if warp is not None:
                    grid = _warp_grid(ds, dst, warp)
                else:
                    window = _read_window(ds, self._boundary_geom(),
                                          clip.crop)
                    grid = _clip_grid(ds, window, clip)
        except ValueError as exc:          # disjoint boundary, usually a CRS slip
            return f"Cannot run: {exc}"
        except Exception:
            return "Set the parameters, then click ‘Reproject + Crop’."
        mb = grid.width * grid.height * 4 / 2 ** 20
        factor = self.reduction()
        if warp is None:
            step = f"crop in {self.raster_crs} at native resolution"
        elif self.needs_warp():
            step = f"warp {self.raster_crs} → {dst}, then crop"
            if factor > 1.001:
                step += (f" (also {factor:.0f}x coarser, "
                         f"{self.resample_combo.currentText()})")
        else:
            step = (f"downsample {self.raster_res[0]:.0f} m → "
                    f"{self.res_spin.value():.0f} m ({factor:.0f}x, "
                    f"{self.resample_combo.currentText()}), then crop")
        return (f"Would produce {grid.width} x {grid.height} px "
                f"@ {abs(grid.transform.a):.0f} m ({mb:.1f} MB) — {step}. "
                "Click ‘Reproject + Crop’.")

    def _invalidate_mesh(self) -> None:
        """A mesh-parameter change stales the solid but keeps the raster.

        Re-warping 7 GB because someone nudged the exaggeration would be absurd,
        and the cropped raster is unaffected by anything on the mesh row.
        """
        self.stl_result = None
        self.btn_save_stl.setEnabled(False)
        self.btn_build.setEnabled(self.result is not None)
        self._refresh_3d()
        if self.raster_path is not None:
            self.status.setText(self._cost_text())

    def _invalidate_raster(self) -> None:
        """A raster-parameter change stales both stages.

        The solid is meshed *from* the cropped raster, so a new crop makes the
        old solid describe ground that is no longer on the table.
        """
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_run.setEnabled(
            self.raster_path is not None and self.boundary is not None)
        if not self._showing_borders():     # that panel is the borders' now
            self._drop_colorbar()
            self._clear_ax(self.ax_after, "Not yet run")
            self.canvas.draw_idle()
        self._invalidate_mesh()

    #: Kept as the name the imports call, which stale both stages.
    _invalidate = _invalidate_raster

    # -- run ----------------------------------------------------------------
    def run(self) -> None:
        # Guard against launching a second run while one is in flight.
        if self._thread is not None:
            return
        geom = self._output_geom()
        if geom is None:
            return
        warp, clip = self._plan()
        stem = self._stem()

        self._set_busy(True)
        self.status.setText("Reprojecting and cropping …" if warp is not None
                            else "Cropping …")

        self._launch(RunWorker(
            str(self.raster_path), geom, self.target_crs(), warp, clip,
            str(self._tempdir / f"{stem}_warp.tif"),
            str(self._tempdir / f"{stem}_crop.tif")), self._on_done)

    def _launch(self, worker, on_done) -> None:
        """Run ``worker`` on a fresh thread; failures all land in _on_failed."""
        self._thread = QThread(self)
        self._worker = worker
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.done.connect(on_done)
        worker.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, result) -> None:
        self.result = result
        self._teardown_thread()
        self._set_busy(False)
        # Before panel first: the result's range now drives the shared colour
        # ramp so the DEM has to be repainted onto it (keeping wherever the user
        # panned to), and _draw_after's _link_views reads this panel's limits --
        # repainting it afterwards would leave the link one draw out of date.
        self._draw_before(preserve_view=True)
        self._draw_after()
        self.btn_save.setEnabled(True)
        z = ("no valid pixels" if result.zmin is None
             else f"{result.zmin:.0f}..{result.zmax:.0f} m")
        crs_note = (f" in {self.target_crs()}" if self.needs_warp()
                    else f" in {self.raster_crs}")
        self.btn_build.setEnabled(True)
        self.status.setText(
            f"Cropped: {result.width} x {result.height} px @ "
            f"{result.res[0]:.0f} m{crs_note}, {result.coverage * 100:.1f}% "
            f"covered, elevation {z}. Save it, or click 'Build STL'.")

    # -- mesh stage ---------------------------------------------------------
    def _terrain_params(self) -> TerrainParams:
        return TerrainParams(
            scale=_cfg("print.scale"),
            z_exaggeration=self.exag_spin.value(),
            base_height_mm=self.base_spin.value(),
            floor_m=self.floor_spin.value(),
            buffer_mm=self.buf_spin.value(),
            # The cropped raster is in whatever CRS the raster stage produced,
            # which may deliberately be the DEM's own; the guard belongs to the
            # stage that chose it, so it is waived rather than duplicated here.
            expect_crs=None)

    def cut_geom(self):
        """The boundary the build will cut with, honouring the tick.

        A named method rather than an expression inside :meth:`build_stl`, so a
        test can ask the window what it would cut with instead of restating the
        rule — a restated rule is one the tests stop actually checking.
        """
        return self._output_geom() if self.chk_cut.isChecked() else None

    def build_stl(self) -> None:
        """Mesh the cropped raster — the second half of the pipeline."""
        if self._thread is not None or self.result is None:
            return
        out_path = self._tempdir / f"{self._stem()}.stl"
        boundary = self.cut_geom()
        lobes = self.province_lobes()

        self._set_busy(True)
        self.status.setText("Meshing and carving pockets …" if lobes
                            else "Meshing …")
        self._pockets = len(lobes)
        self._launch(MeshWorker(self.result.path, boundary, str(out_path),
                                self._terrain_params(), lobes,
                                self.borders.settings.connector_params()
                                if lobes else None), self._on_meshed)

    def _on_meshed(self, result) -> None:
        self.stl_result = result
        self._teardown_thread()
        self._set_busy(False)
        w, h, z = result.extent_mm
        self._refresh_3d()
        self.btn_save_stl.setEnabled(True)
        relief = z - result.base_height_mm
        pockets = (f", {self._pockets} dovetail pocket(s)" if self._pockets
                   else "")
        self.status.setText(
            f"Built {result.triangles:,} triangles, {w:.0f} x {h:.0f} x "
            f"{z:.1f} mm ({relief:.1f} mm of relief on a "
            f"{result.base_height_mm:.1f} mm base){pockets}, "
            f"{'fits' if result.bed_fits else 'DOES NOT FIT'} the bed, "
            f"watertight={result.watertight}. Inspect in 3D, then save.")

    def _refresh_3d(self) -> None:
        """Draw what the 3D selector asks for, from whatever has been built."""
        if not hasattr(self, "show_combo"):
            return                                   # still constructing
        show = self.show_combo.currentText()
        solid = self.stl_result.mesh if self.stl_result is not None else None
        bar = self.bar.mesh if self.bar is not None else None
        if show == "Border bar":
            title = ("Not yet built" if bar is None else
                     f"Border bar — {len(self.bar.pieces)} bed piece(s)")
            self._show_meshes(title, [(bar, None)])
        elif show == "Assembly":
            self._show_meshes(*self._assembly())
        elif solid is None:
            self._show_solid("Not yet built", None)
        else:
            w, h, z = self.stl_result.extent_mm
            self._show_solid(f"{self.stl_result.triangles:,} triangles — "
                             f"{w:.0f} x {h:.0f} x {z:.1f} mm", solid)

    def _assembly(self):
        """The solid and the bar in one print-mm frame -- the bar's.

        Each was built at its own local origin; the metres origins say where
        those sit, so the offset between them is all it takes to lay them
        side by side as they will be assembled.
        """
        if self.bar is None or self.stl_result is None:
            return ("Assembly needs both the border bar and a solid", [])
        solid = self.stl_result.mesh.copy()
        dx = (self.stl_result.origin_m[0] - self.bar.origin_m[0]) / self.bar.mpm
        dy = (self.stl_result.origin_m[1] - self.bar.origin_m[1]) / self.bar.mpm
        solid.apply_translation([dx, dy, 0.0])
        return ("Assembly — solid laid into the border bar",
                [(self.bar.mesh, None), (solid, _height_colors(solid))])

    def _show_meshes(self, title: str, meshes) -> None:
        """Draw several meshes; ``None`` colours means the flat bar grey."""
        self.solid_title.setText(title)
        for visual in self._solid_meshes:
            visual.parent = None
        self._solid_meshes.clear()
        for mesh, colours in meshes:
            if mesh is None or mesh.is_empty:
                continue
            kwargs = ({"vertex_colors": colours} if colours is not None
                      else {"color": BAR_COLOUR})
            self._solid_meshes.append(scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces), shading="flat",
                parent=self.solid_view.scene, **kwargs))
        if self._solid_meshes:
            self.solid_view.camera.set_range()
        self.solid_canvas.update()

    def _show_solid(self, title: str, mesh) -> None:
        self.solid_title.setText(title)
        for visual in self._solid_meshes:
            visual.parent = None
        self._solid_meshes.clear()
        if mesh is not None and not mesh.is_empty:
            visual = scene.visuals.Mesh(
                vertices=np.asarray(mesh.vertices, dtype=np.float32),
                faces=np.asarray(mesh.faces),
                vertex_colors=_height_colors(mesh), shading="flat",
                parent=self.solid_view.scene)
            self._solid_meshes.append(visual)
            self.solid_view.camera.set_range()
        self.solid_canvas.update()

    def save_stl(self) -> None:
        if self.stl_result is None or self.stl_result.path is None:
            return
        default = str(Path.cwd() / f"{self._stem()}.stl")
        path, _ = QFileDialog.getSaveFileName(self, "Save STL", default,
                                              STL_FILTER)
        if not path:
            return
        target = Path(path)
        try:
            # The lib wrote both files; copying keeps what you inspected
            # byte-for-byte, frame sidecar included.
            shutil.copyfile(self.stl_result.path, target)
            shutil.copyfile(self.stl_result.frame_path,
                            target.with_suffix(".frame.json"))
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {self.stl_result.triangles:,} triangles to {target.name}, "
            f"with its frame in {target.with_suffix('.frame.json').name}.")

    def _on_failed(self, message: str) -> None:
        self._teardown_thread()
        self._set_busy(False)
        QMessageBox.critical(self, "Run failed", message)
        self.status.setText(f"Run failed: {message}")

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._worker = None
        self._thread = None

    def _set_busy(self, busy: bool) -> None:
        """Toggle the indeterminate progress bar and lock the controls."""
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        self.progress.setValue(0 if busy else 1)
        for w in (self.btn_raster, self.btn_boundary, self.btn_run,
                  self.btn_save, self.crs_combo, self.res_spin, self.buf_spin,
                  self.cache_spin, self.chk_touched, self.chk_crop,
                  self.resample_combo, self.exag_spin, self.base_spin,
                  self.floor_spin, self.chk_cut, self.btn_build,
                  self.btn_save_stl, self.btn_add_targets,
                  self.btn_add_surrounding, self.btn_remove,
                  self.province_combo, self.coast_spin, self.internal_spin,
                  self.min_seg_spin, self.min_angle_spin, self.width_spin,
                  self.chk_connectors, self.spacing_spin, self.chk_pockets,
                  self.btn_define, self.btn_save_borders, self.btn_build_bar,
                  self.btn_save_bar):
            w.setEnabled(not busy)
        if not busy:      # re-apply the preconditions the blanket enable ignored
            self.btn_run.setEnabled(
                self.raster_path is not None and self.boundary is not None)
            self.btn_save.setEnabled(self.result is not None)
            self.btn_build.setEnabled(self.result is not None)
            self.btn_save_stl.setEnabled(self.stl_result is not None)
            self._update_border_controls()

    # -- drawing ------------------------------------------------------------
    def _colour_limits(self):
        """
        One elevation ramp for both panels, so they can actually be compared.

        The panels show the same ground, so scaling them independently would
        paint identical elevations different colours — the before panel would
        also be crushed into the bottom of the ramp, because the whole-Canada
        overview runs to Mount Logan's 5500 m while a province rarely clears
        1500 m.  So once there is a result, its range drives both panels and the
        rest of the DEM is allowed to saturate.  Before that, percentiles keep
        the overview readable without a few summits flattening everything else.
        """
        if self.result is not None and self.result.zmin is not None:
            return self.result.zmin, self.result.zmax
        if self.overview is None:
            return None, None
        valid = self.overview.compressed()
        if valid.size == 0:
            return None, None
        return float(np.percentile(valid, 2)), float(np.percentile(valid, 98))

    def _draw_before(self, preserve_view: bool = False) -> None:
        # Only a panel that has actually been drawn has a view worth keeping.
        # Preserving a never-drawn axis would restore matplotlib's default
        # (0, 1) limits over an extent measured in millions of metres, leaving
        # the panel zoomed into a one-metre square of flat colour.
        preserve_view = preserve_view and self._before_drawn
        if preserve_view:
            xlim, ylim = self.ax_before.get_xlim(), self.ax_before.get_ylim()
        frame = self.raster_crs or CRS.from_user_input(EPSG_WORKING)
        title = f"Regions — {frame}" if self.overview is None \
            else "DEM overview"
        if self.overview is not None:
            title = (f"DEM overview ({self.overview.shape[1]}x"
                     f"{self.overview.shape[0]} of {self.raster_shape[0]}x"
                     f"{self.raster_shape[1]}) — {self.raster_crs}")
        self._clear_ax(self.ax_before, title)
        vmin, vmax = self._colour_limits()
        if self.overview is not None:
            self.ax_before.imshow(self.overview, cmap=DEM_CMAP,
                                  extent=self.overview_extent, origin="upper",
                                  vmin=vmin, vmax=vmax)
        # Drawn in the DEM's CRS, because that is the panel's frame — the after
        # panel is where the target CRS shows up.  Context first and thinner,
        # so the province being cut draws on top of it.
        selected = (self.province_combo.currentText()
                    if self._boundary_is_region else None)
        for name, geom in self.surrounding.items():
            self._plot_outline(self.ax_before,
                               self._outline_in(name, geom, frame),
                               colour=SURROUND_COLOUR, width=0.8)
        for name, geom in self.targets.items():
            if name != selected:
                self._plot_outline(self.ax_before,
                                   self._outline_in(name, geom, frame),
                                   colour=MARK_COLOUR, width=0.5)
        geom = self._boundary_geom(frame)
        if geom is not None:
            self._plot_outline(self.ax_before, geom)
        if preserve_view:
            self.ax_before.set_xlim(xlim)
            self.ax_before.set_ylim(ylim)
        self._before_drawn = (self.overview is not None
                              or bool(self.targets or self.surrounding))
        self.canvas.draw_idle()

    def _outline_in(self, name, geom, crs):
        """A region reprojected for display, cached -- a province is ~100k
        vertices and the panel redraws on every crop."""
        key = (name, str(crs))
        if key not in self._outline_cache:
            self._outline_cache[key] = (
                geom if CRS.from_user_input(crs) == CRS.from_user_input(
                    EPSG_WORKING)
                else gpd.GeoSeries([geom], crs=EPSG_WORKING).to_crs(crs).iloc[0])
        return self._outline_cache[key]

    def _draw_borders(self) -> None:
        """Tab 2: the regions as imported | as defined, both in crs.working."""
        self._drop_colorbar()
        ax, after = self.ax_before, self.ax_after
        self._clear_ax(ax, f"As imported — {len(self.targets)} to print, "
                           f"{len(self.surrounding)} surrounding")
        for geom in self.surrounding.values():
            self._fill(ax, geom, SURROUND_COLOUR)
        for geom in self.targets.values():
            self._fill(ax, geom, KEEP_COLOUR)
        ax.autoscale_view()

        d = self.borders
        if d is None:
            self._clear_ax(after, "Not yet defined")
        else:
            title = f"Defined — {d.settings.width_mm:.2f} mm corridor"
            if d.settings.connectors:
                title += f", {len(d.lobes)} connector(s)"
            self._clear_ax(after, title)
            for name, geom in d.simplified.items():
                if name not in d.targets:
                    self._fill(after, geom, SURROUND_COLOUR)
            for geom in d.targets.values():
                self._fill(after, geom, KEEP_COLOUR)
            if d.border is not None:
                self._fill(after, d.border, CORRIDOR_COLOUR, alpha=0.85)
            for lobe in d.lobes:
                self._fill(after, lobe, RESULT_COLOUR, edge=EDGE_COLOUR)
            for colour, segs in ((EDGE_COLOUR, d.coastal_segs),
                                 (SELECT_COLOUR, d.internal_segs)):
                for seg in segs:
                    if len(seg) >= 2:
                        after.plot(seg[:, 0], seg[:, 1], color=colour,
                                   linewidth=0.6)
            after.set_xlim(ax.get_xlim())
            after.set_ylim(ax.get_ylim())
        self.canvas.draw_idle()

    @staticmethod
    def _fill(ax, geom, face, edge=None, alpha=1.0) -> None:
        """Fill a (Multi)Polygon, holes and all, without geopandas plotting."""
        for poly in _polys(geom):
            poly = orient(poly, 1.0)      # CCW shell, CW holes: nonzero fill
            path = MplPath.make_compound_path(*(
                MplPath(np.asarray(ring.coords)[:, :2], closed=True)
                for ring in (poly.exterior, *poly.interiors)))
            ax.add_patch(PathPatch(path, facecolor=face, alpha=alpha,
                                   edgecolor=edge or "none", linewidth=0.4))

    def _draw_after(self) -> None:
        self._drop_colorbar()
        if self.result is None or self.result.data is None:
            self._clear_ax(self.ax_after, "Not yet run")
            self.canvas.draw_idle()
            return

        data, step = self._for_display(self.result.data)
        crs = self.target_crs() if self.needs_warp() else self.raster_crs
        title = (f"{self.result.width}x{self.result.height} @ "
                 f"{self.result.res[0]:.0f} m — {crs}")
        if step > 1:
            # Never let the panel imply the saved file is this coarse.
            title += f"  (shown 1:{step} — saved file is full resolution)"
        self._clear_ax(self.ax_after, title)

        left, bottom, right, top = self.result.bounds_m
        masked = np.ma.masked_equal(data, self.result.nodata)
        vmin, vmax = self._colour_limits()
        image = self.ax_after.imshow(masked, cmap=DEM_CMAP,
                                     extent=(left, right, bottom, top),
                                     origin="upper", vmin=vmin, vmax=vmax)
        self._colorbar = self.figure.colorbar(
            image, ax=self.ax_after, fraction=0.046, label="elevation (m)")
        self._link_views()
        self.canvas.draw_idle()

    def _drop_colorbar(self) -> None:
        """Remove the elevation colorbar *before* its panel is cleared.

        ``ax.clear()`` on the after panel orphans the colorbar, and a later
        ``remove()`` then dies inside matplotlib -- which made a second crop
        crash whenever any raster setting had changed in between.
        """
        if self._colorbar is not None:
            self._colorbar.remove()
            self._colorbar = None

    @staticmethod
    def _plot_outline(ax, geom, colour=MARK_COLOUR, width=1.2) -> None:
        """
        Draw a boundary as rings only, so the DEM stays visible through it.

        Drawn with bare ``ax.plot`` rather than geopandas: ``.plot()`` reaches
        for ``pyplot``, which this harness never uses (it drives a bare
        ``Figure``), and warns about GUI threads for its trouble.
        """
        polygons = (geom.geoms if geom.geom_type.startswith("Multi")
                    else [geom])
        for poly in polygons:
            for ring in (poly.exterior, *poly.interiors):
                xs, ys = ring.xy
                ax.plot(xs, ys, color=colour, linewidth=width)

    @staticmethod
    def _for_display(data):
        """Stride a large result down for matplotlib; returns (array, step)."""
        pixels = data.size
        if pixels <= MAX_DISPLAY_PX:
            return data, 1
        step = int(np.ceil(np.sqrt(pixels / MAX_DISPLAY_PX)))
        return data[::step, ::step], step

    # -- save ---------------------------------------------------------------
    def _stem(self) -> str:
        return self.boundary_path.stem if self.boundary_path else "terrain"

    def save_geotiff(self) -> None:
        if self.result is None or self.result.path is None:
            return
        default = str(Path.cwd() / f"{self._stem()}_crop.tif")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save GeoTIFF", default, GEOTIFF_FILTER)
        if not path:
            return
        try:
            # The libs already wrote the real file; copying it means the saved
            # raster is exactly the one just inspected.
            shutil.copyfile(self.result.path, path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.status.setText(
            f"Saved {self.result.width} x {self.result.height} px to "
            f"{Path(path).name}.")

    def closeEvent(self, event) -> None:
        self._teardown_thread()
        shutil.rmtree(self._tempdir, ignore_errors=True)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    vispy.use(app="pyside6")          # before the window builds its scene
    window = TerrainEditorTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
