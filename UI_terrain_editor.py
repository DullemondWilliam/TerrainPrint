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
             the border bar, in the pieces the splits planned on tab 2.

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
2. **Add Provinces …**    — load the province GeoJSON(s), on tab 1, and pick
                            one under **Province**.  It is drawn over the DEM
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
                            **Auto**, ticked by default, sets **Res** to the
                            finest the province can be built at: the finest
                            whose STL fits ``terrain.max_triangles``, never
                            finer than the DEM -- 3 million by default,
                            set under **Max triangles**.  That limit, not the GDAL
                            **Cache** (6 GB by default, which only makes the
                            run faster), is what bounds the resolution.
5. **Reproject + Crop**   — run the raster stage off the UI thread.
6. **Exag / Base / Floor / Cut** — the solid's parameters.  Defaults come from
                            ``project_config.json``'s ``terrain`` section, so
                            every piece of a map is built the same way.
7. **Build STL**          — mesh the cropped DEM into a watertight solid.
8. **Save GeoTIFF … / Save STL …** — keep either stage's output.  Every
                            save starts in ``paths.stl`` (Data/STL).

The whole run is here: national DEM in, printable province solid out, without
leaving the window or touching QGIS.

Keyboard
--------
The window follows ``docs/UI_GUIDE.md``: every button carries a key, named at
the foot of its tooltip, and a key is live exactly when its button is.
Ctrl+O imports the DEM, Ctrl+P / Ctrl+Shift+P add provinces / surrounding,
Shift+Del removes a region; D defines the borders, E edits connectors, R
resets and Del deletes the selected one; C reproject-and-crops, S builds the
STL and B the border bar; Ctrl+S, Ctrl+Shift+S, Ctrl+B and Ctrl+Shift+B save
the STL, the GeoTIFF, the borders and the border pieces, and Ctrl+L loads
saved borders back.  Home, Alt+Left /
Alt+Right, P and Z work the map's view, and the arrow keys pan it (Shift for
a long stride, Ctrl for a nudge).  Left-drag pans the map too, and the wheel
zooms it.

There is one map, and it shows the latest state of the stage on screen: the
regions as imported until the borders are defined and the borders after; the
DEM until the crop has run and the crop after.  The view is kept while the
CRS stays the same.

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

*Nothing is ever read whole.*  The map shows a decimated overview (GDAL
seeks past the scanlines it does not need, so a full-Canada overview costs
~0.4 s); the warp streams through a ``WarpedVRT`` and the clip in row bands.

*The run writes the file, and Save copies it.*  The libs are what write
GeoTIFFs, so the harness runs straight to a temporary file and Save copies it.
What you inspect is byte-for-byte what you save.

*The map may be showing you less than you saved.*  A native-resolution
province is a 400 MB array; matplotlib is handed a strided-down copy above
``MAX_DISPLAY_PX``.  The map's title says so whenever it happens, because a
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

*The settings are kept.*  Every control on the Borders tab -- the tolerances,
the seam clean-up, the corridor width and enclave limit, the Connectors
group's Place tick,
and the connectors' spacing and shape -- opens on what the config files say and is
written back to ``project_config.local.json`` as it is turned
(``SAVED_BORDER_SETTINGS`` lists the keys), so the next launch starts where
this one left off.

*Simplify, then carve, then cut the terrain.*  ``LIB_border_regions`` wants
simplified input, and the terrain must be cut by the province *after* the
corridor came out of it -- otherwise the solid and the border bar overlap by
half the corridor.  So once borders are defined, stage 3 cuts to the trimmed
province and **Buffer** is locked at 0: growing the cut outward is growing it
into the corridor.

*No crumbs.*  Carving the corridor can cut pieces off a province: between
two arms of a zig-zag border the bar all but closes and leaves crumbs in
holes in it, and where a border meets the coast it cuts slivers off between
itself and the sea (both happen on the Quebec / Labrador border).  A piece
the carving cut off, under **Enclaves** (``borders.enclave_mm2``, 5 mm²), is
added to the border bar instead of printed loose; an island that was an
island before carving is never touched.  Specks of hole the carving leaves in
the bar with no province in them are filled.

*A connector belongs to one province.*  A lobe whose far end lands in two
targets (near a triple junction) has no single pocket to key into, so it is
dropped and counted (FR-3.7).  So is one whose pocket would run out of its
province -- past the coast at a seam's end, or into a neighbour -- and one
that would touch another connector or bring its pocket within a clearance of
another's, and one that would meet the corridor in two places -- near a bend
or a junction its far end can run back into the bar.  Placed by Define, by
hand or by a reshape, every connector is held
to these same rules, and the report says how many each one dropped.
Define holds the ones it places to one more: none may cross the border line
between two printed provinces, as one in the inside corner of Victoria
Island's bend did.  By hand that graze is allowed -- a connector turned past
about 18 degrees puts its root corner over the line by design.

*Connectors have a shape.*  **Height** (the lobe layer, and so the pocket
depth), **Depth** (how far a lobe reaches into its province), **Width** (its
neck at the corridor) and **Ratio** (mouth over neck; above 1 the wide end is
captured) sit in the **Shape** group, beside **Spacing**.  Turned on a defined
map, they reshape every connector where it stands rather than defining the
borders again, so hand edits survive; a connector the new shape no longer
lets stand stays where it was and turns red, and the change is one Undo step.
**Clearance** and **Height clear.**, in **Fit**, set the fit: the pocket is cut that much
wider than the connector on every side, and that much deeper than the
connector layer is tall.  The clearance is also the gap kept between
neighbouring pockets, so widening it can turn red a connector that now
crowds one.

*Connectors can be edited by hand.*  **Edit Connectors** turns the map
into a tool: click a connector to select it, drag one along the corridor
to move it, click beside the corridor to add one on that side; a drag that
starts on empty map still pans it.  A connector is
deleted only once it is selected -- the **Delete** button or the Del key -- so a
stray click cannot lose one.  A placed lobe has
the generated profile and is held to the same rules -- it must reach a province
to print, and only one -- and its far end must stay a clearance clear of every
other lobe's, or two pockets run into one.  A connector put, moved, turned or
sized by hand into a place that breaks a rule is kept all the same and drawn
red until an edit makes it valid -- a drag shows it red before you let go, and
the status line says why.  Define drops the ones it would place invalid, and
nothing is built from a red one: the border bar and a pocketed solid wait
until every connector they would use is valid.

*A connector can be turned and sized on its own.*  Click (or right-click) one
to select it.  **Angle** turns it about where it meets the corridor, up to 45° either
way; **Depth** sets how far it reaches, its neck and mouth growing with it so
it keeps its shape.  ] and [ turn it 5°, } and { 1°, + and - change the depth
0.25 mm (a held key is one Undo step), and dragging the dot at its far end
swings it round.  A turned connector must keep half its root in
the corridor and a neck of at least two nozzle widths.  Its turn and size are
read off its own outline, so they survive a move, a reshape from the Shape
group, Undo, and Save Borders (which writes them as ``angle_deg`` and
``scale``).  Every edit is one Undo (Ctrl+Z; Ctrl+Shift+Z or Ctrl+Y redoes)
step and stales the border bar and a pocketed solid.  **Define** (in the
Borders group) places them afresh and discards the edits.  **Save …** writes
them, with the settings they were shaped by (``borders_settings.json``), to
``connectors.geojson``, which ``build_border_from_dir`` uses in place of
generating its own.  **Load …** reads such a folder back -- the corridor, the
trimmed provinces and the connectors as they were left, with the settings --
so a session of hand edits can be picked up again; the provinces must be
imported first.

*The bar stands as tall as the terrain.*  With a DEM imported, **Build Border
STL** warps it to ``crs.working`` over the corridor at the crop's **Res** and
**Resample**, and lifts the bar's top onto it through the solid's own
elevation -> mm rule (**Exag**, **Base**, **Floor**) -- so where the bar meets
a solid, both are the height of the ground there.  It never drops below
``print.border_thickness_mm``, which the connector layer needs; with no DEM the
top is flat at that thickness.  Changing any of those settings stales a draped
bar.

*The bar is cut where you plan it, never for you.*  **Edit Splits** (X) on
the Borders tab puts a split straight across the corridor where you click;
click one to select it, and **Remove** (Backspace) takes it away.  Each is one
Undo step, saved and loaded with the borders (``splits.geojson``), and Define
starts with none.  A split is refused through a connector (FR-3.11), and a
connector moved onto one turns red.  **Build Border STL** extrudes each piece
between the splits on its own, all in one frame so they meet where they
were cut; a piece too big for the bed -- tried at every turn, diagonally
included, as a slicer would place it -- is filled red on the map and
counted, and still built -- the map draws the bar slate, and a piece too big
red, and so does the 3D view.  Splits may sit close together (one on each arm of
a junction), but not so close the piece between is too small to build.

*Pockets need the print CRS.*  Lobes are generated in EPSG:3347 and the solid's
``origin_m`` is in whatever CRS the raster stage produced, so pocketing a solid
built in any other CRS is refused rather than carved in the wrong place.
"""

import logging
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from dataclasses import fields as dataclasses_fields
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import trimesh
import vispy
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.backends.backend_qtagg import \
    NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib.collections import PolyCollection
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
from rasterio.crs import CRS
from rasterio.enums import Resampling
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely import STRtree
from shapely.ops import split as split_geom
from shapely.ops import unary_union
from vispy import scene

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QListWidget, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from lib.LIB_border_connectors import (CONNECTORS_NAME,
                                       MIN_PART_AREA_MM2, ConnectorParams,
                                       _edge_frame, _polys, _repair, _rings,
                                       _safe_union,
                                       build_border_pieces, generate_lobes,
                                       load_connector_params, lobe_at,
                                       lobe_pose,
                                       notch_province)
from lib.LIB_border_regions import build_border_regions, shared_borderlines
from lib.LIB_border_split import bed_turn
from lib.LIB_border_simplification import topo_simplify_two_tolerances
from lib.LIB_shared_border import _count_short, _count_spikes
from lib.LIB_geotiff_clip import ClipParams, clip_geotiff
# The cost preview must agree with what the run actually does, so it asks the
# libs for their grids rather than reimplementing the window/warp arithmetic.
from lib.LIB_geotiff_clip import _output_grid as _clip_grid
from lib.LIB_geotiff_clip import _read_window
from lib.LIB_raster_reproject import (ReprojectParams, bounds_for,
                                      reproject_geotiff)
from lib.LIB_raster_reproject import _output_grid as _warp_grid
from lib.LIB_terrain_stl import (EPSG_METRIC, TerrainParams, _finish,
                                 _triangle_count, _validate, _write,
                                 load_terrain_params, surface_sampler,
                                 terrain_to_stl)
from lib.LIB_project_config import get as _cfg
from lib.LIB_project_config import (build_params, load_files, settings_files,
                                     update_local)
from lib.LIB_project_config import shipped as _shipped

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
#: Pixels in the whole-raster overview drawn on the map.
OVERVIEW_PX = 1100
#: Above this, the crop is strided down for display only.
MAX_DISPLAY_PX = 2_000_000
BG_COLOUR = "#FDFEFE"       # near-white 3D scene background
#: Where every save dialog starts: the project's STL folder, next to this
#: script (``paths.stl``).
DEFAULT_SAVE_DIR = Path(__file__).resolve().parent / _cfg("paths.stl")
#: GDAL's block cache for a run, in MB.  6 GB: the run is faster the more of
#: a striped DEM's scanlines stay in memory, and the cache never changes the
#: pixels that come out -- only how often they are read again.
DEFAULT_CACHE_MB = 6144
#: The Borders settings, written beside a saved Borders folder so loading it
#: rebuilds the connectors in the shape they were saved in.
BORDER_SETTINGS_NAME = "borders_settings.json"
#: Sampling pitch on paper; the Res control is seeded from it via the scale.
DEFAULT_SPACING_MM = _shipped("terrain.spacing_mm")
DEFAULT_RESAMPLING = _shipped("terrain.resampling")

# -- the Borders stage ------------------------------------------------------
EPSG_WORKING = _cfg("crs.working")
KEEP_COLOUR = "#AED6F1"       # blue  — a trimmed target, what gets printed
RESULT_COLOUR = "#ABEBC6"     # green — the dovetail lobes
SELECT_COLOUR = "#F5B041"     # orange — shared (internal) arcs
SURROUND_COLOUR = "#B3B6B7"   # grey  — surrounding regions, never printed
#: The carved corridor -- the border bar -- on the map.  Slate rather than
#: the red it once was: red on this map means "this will not do" (a
#: connector that breaks a rule, a piece too big for the bed), and a bar red
#: all over said that of every piece.
CORRIDOR_COLOUR = "#5D6D7E"   # slate — the border bar, a piece that fits
TOO_BIG_COLOUR = "#C0392B"    # red   — a piece of bar too big for the bed
BAR_COLOUR = (0.55, 0.57, 0.58, 1.0)   # the border bar in the 3D panel
#: ...and a piece of it too big for the bed, there too.
TOO_BIG_BAR_COLOUR = (0.75, 0.22, 0.17, 1.0)
ROLES = ("target", "surrounding")
#: What the 3D panel shows on the STL stage.
SHOW_CHOICES = ["Terrain", "Border bar", "Assembly"]
DRAG_COLOUR = "#1E8449"       # dark green — where a dragged connector lands
REFUSED_COLOUR = "#C0392B"    # red — where it cannot
INVALID_COLOUR = REFUSED_COLOUR   # a connector that breaks a rule, until fixed
PICKED_COLOUR = "#7D3C98"     # purple — the selected connector and its handle
SPLIT_COLOUR = "#17202A"      # near-black — a planned split across the bar
#: A split is a straight cut across the corridor, found by walking in from the
#: edge nearest the click.  One that has not come out the other side within
#: this many corridor widths is running *along* the corridor, not across it.
SPLIT_REACH_WIDTHS = 3.0
#: Where splits are saved beside a Borders folder.
SPLITS_NAME = "splits.geojson"
#: How near, in screen pixels, a click must be to a connector to pick it.
PICK_PX = 8
#: "Leave the selection as it is", where None would mean "select nothing".
_KEEP = object()

#: How far a selected connector turns either way from square to its edge.
ANGLE_LIMIT_DEG = 45.0
#: What one press of ] / [ turns it, and of } / { (Shift) for fine work.
ANGLE_STEP_DEG = 5.0
FINE_ANGLE_STEP_DEG = 1.0
#: What one press of + / - adds to or takes from its depth.
DEPTH_STEP_MM = 0.25
#: Presses closer together than this are one Undo step: a held key is one
#: gesture, not a dozen.
KEY_GESTURE_S = 0.5
#: A press that travels further than this is a drag, not a click.
CLICK_SLOP_PX = 4
#: A lobe counts as reaching into a region past this fraction of its own area,
#: so a lobe grazing a neighbour's corner at float precision is not an owner.
_OWNER_FRAC = 1e-3
#: How much of a pocket, as a fraction of its lobe's area, may lie outside its
#: province before it counts as running out: only the slivers where the
#: carved province edge and the corridor edge disagree by rounding.
_ESCAPE_FRAC = 1e-3

# -- the window's chrome (docs/UI_GUIDE.md) ---------------------------------
#: How many points larger than the interface default the status line runs.
STATUS_POINT_BUMP = 2
#: How far one arrow-key press pans, as a fraction of the visible span; Shift
#: crosses the view in two presses, Ctrl nudges.
PAN_STEP = 0.10
PAN_STEP_COARSE = 0.50
PAN_STEP_FINE = 0.02

#: The navigation tools worth keeping, the tooltips worth rewording, and the
#: keys they answer to -- the same as the island editor's.  Module level
#: because a class-body comprehension cannot see its own class attributes.
_NAV_KEEP = ("Home", "Back", "Forward", "Pan", "Zoom")
_NAV_TIPS = {"Back": "Back to the previous view (this is not Undo)",
             "Forward": "Forward to the next view (this is not Redo)"}
_NAV_KEYS = {"Home": "Home", "Back": "Alt+Left", "Forward": "Alt+Right",
             "Pan": "P", "Zoom": "Z"}


class MapToolbar(NavigationToolbar):
    """
    Matplotlib's navigation toolbar, minus the tools that fight this window.

    ``Subplots`` and ``Customize`` edit the axes the window lays out, and
    ``Save`` writes a PNG beside three buttons that save real outputs.
    """

    toolitems = [(text, _NAV_TIPS.get(text, tip), image, callback)
                 for text, tip, image, callback in NavigationToolbar.toolitems
                 if text in _NAV_KEEP]


# The map keeps ``aspect="equal", adjustable="datalim"`` so it always fills the
# canvas; matplotlib logs a warning each time it widens the view to do that,
# which here is the intended behaviour on every zoom and resize.
logging.getLogger("matplotlib.axes._base").addFilter(
    lambda record: "to fulfill fixed data aspect" not in record.getMessage())


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
    coast_tol_mm: float = _shipped("borders.coast_tol_mm")
    internal_tol_mm: float = _shipped("borders.internal_tol_mm")
    min_segment_mm: float = _shipped("seam.min_segment_mm")
    min_angle_deg: float = _shipped("seam.min_angle_deg")
    width_mm: float = _shipped("borders.width_mm")
    #: Below this printed area (mm²) a piece the corridor cuts off from the
    #: rest of its province is added to the border bar (see
    #: :func:`_absorb_enclaves`); 0 keeps every piece.
    enclave_mm2: float = _shipped("borders.enclave_mm2")
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

#: The Borders stage's controls, each written back to its config key as it is
#: turned, so the next launch starts where this one left off.  Written to
#: ``project_config.local.json`` -- what this desk works at -- leaving the
#: tracked file the project's shipped numbers, as UI_island_editor does.
#:
#: Three keys are read by more than this window: ``seam.min_segment_mm`` and
#: ``seam.min_angle_deg`` by LIB_shared_border's seam clean-up, and
#: ``print.dovetail_mm`` -- the connector Height -- as the pocket depth the
#: terrain Base must clear.
SAVED_BORDER_SETTINGS = (
    ("borders.coast_tol_mm", "coast_spin"),
    ("borders.internal_tol_mm", "internal_spin"),
    ("seam.min_segment_mm", "min_seg_spin"),
    ("seam.min_angle_deg", "min_angle_spin"),
    ("borders.width_mm", "width_spin"),
    ("borders.enclave_mm2", "enclave_spin"),
    ("borders.connectors", "chk_connectors"),
    ("borders.lobe_spacing_mm", "spacing_spin"),
    ("print.dovetail_mm", "lobe_height_spin"),
    ("borders.lobe_depth_mm", "lobe_depth_spin"),
    ("borders.lobe_width_mm", "lobe_width_spin"),
    ("borders.lobe_ratio", "lobe_ratio_spin"),
    ("borders.clearance_mm", "lobe_clear_spin"),
    ("borders.height_clearance_mm", "lobe_height_clear_spin"),
)

#: The Solid group's controls kept the same way.  Only the triangle limit:
#: what this machine and its slicer can take is a preference of the desk,
#: where the exaggeration, base and floor are the map's and stay shared.
SAVED_TERRAIN_SETTINGS = (
    ("terrain.max_triangles", "tri_spin"),
)

#: The triangle limit's range and step: a few thousand is a test fixture,
#: fifty million a ~2.5 GB STL no slicer will open.
MAX_TRIANGLES_RANGE = (10_000, 50_000_000)
MAX_TRIANGLES_STEP = 250_000

#: How long after the last turn of a box the settings are written, in ms: one
#: write for a held arrow key rather than one per step.
SETTINGS_WRITE_MS = 500

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
    _pieces: list = field(default=None, repr=False, compare=False,
                          init=False)
    _pieces_key: tuple = field(default=None, repr=False, compare=False,
                               init=False)
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
            geoms = list(_polys(self.border))
            for cut in self.splits:
                out = []
                for geom in geoms:
                    if geom.intersects(cut):
                        out += _polys(split_geom(geom, cut))
                    else:
                        out.append(geom)
                geoms = out
            self._pieces, self._pieces_key = geoms, key
        return list(self._pieces)

    def piece_lobes(self) -> list:
        """For each piece, the connectors it carries: each goes with the
        piece holding most of its root."""
        pieces = self.pieces()
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
        return out

    def too_big(self, bed_mm=None, margin_mm=None) -> list:
        """Indices of the pieces that will not fit the bed at any turn -- a
        slicer turns a part to fit, diagonally if it must, so each piece is
        tried at every angle (:func:`bed_turn`), connectors included."""
        bed = _cfg("print.bed_mm") if bed_mm is None else bed_mm
        margin = _cfg("print.bed_margin_mm") if margin_mm is None \
            else margin_mm
        usable = (bed - 2 * margin) * self.settings.mpm
        big = []
        for i, (piece, lobes) in enumerate(zip(self.pieces(),
                                               self.piece_lobes())):
            shape = unary_union([piece, *lobes]) if lobes else piece
            if bed_turn(shape, usable) is None:
                big.append(i)
        return big

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

    border = _repair(unary_union(list(read(border_path).geometry)))
    result = BorderDefinition(settings=settings, border=border,
                              loaded_from=folder.name)
    for path in names:
        frame = read(path)
        name = (str(frame["name"].iloc[0]) if "name" in frame.columns
                else path.stem[len("name_"):])
        geom = _repair(unary_union(list(frame.geometry)))
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


def _standing_lobes(lobes, definition, p: ConnectorParams):
    """
    The generated ``lobes`` that may stand, in order, and why the rest may
    not: ``(kept, {fault code: count})``.

    Each is held to the rules a hand-placed connector is (:func:`_fault`)
    against the ones already kept, so no two pockets run together and none
    leaves the province it keys into.  First come, first kept: along a seam
    that is the order they were spaced in.

    And to one more, here only: it must not cross the border line between
    two printed provinces.  In the inside corner of a bend -- Victoria
    Island's NWT/Nunavut seam has one -- a connector hung from one leg can
    swing its root over the other leg's line.  By hand the same graze is
    allowed: a connector turned past about 18 degrees puts its root corner
    over the line by design, and the bar it sits in is one piece.
    """
    land = definition.land()
    line = definition.border_line()
    kept, reasons = [], {}
    for lobe in lobes:
        code = _fault(lobe, definition, kept, p, land)
        if not code and line is not None and lobe.intersects(line):
            code = "crosses_line"
        if code:
            reasons[code] = reasons.get(code, 0) + 1
        else:
            kept.append(lobe)
    return kept, reasons


def _owners(lobe, border, targets) -> int:
    """How many of ``targets`` the lobe's far end reaches into."""
    reach = lobe.difference(border)
    return sum(1 for geom in targets
               if reach.intersects(geom)
               and reach.intersection(geom).area > _OWNER_FRAC * lobe.area)


def place_connector(definition: BorderDefinition, x: float, y: float,
                    ignore: int | None = None, angle_deg: float = 0.0,
                    scale: float = 1.0, allow_faults: bool = False):
    """
    The connector a click at (x, y) puts on the corridor edge nearest it, and
    why not when it cannot go there: ``(lobe, "")`` or ``(None, reason)``.

    A hand-placed lobe is held to the rules a generated one is -- it reaches a
    province to print, and only one (FR-3.7) -- and to one more: its far end
    stays a clearance clear of every other lobe's, or two pockets run into
    one.  ``ignore`` is the index of the lobe being moved, which may land
    where it already was.  ``angle_deg`` and ``scale`` turn and size it, as
    in :func:`lobe_at`.

    ``allow_faults`` hands the connector back even when it breaks a rule,
    as ``(lobe, reason)``: placed by hand, it is kept and drawn red until an
    edit makes it valid.  Only a connector that cannot be built at all is
    ``(None, reason)``.
    """
    d = definition
    if d.border is None:
        return None, "There is no corridor to put a connector on."
    p = d.settings.connector_params()
    lobe = lobe_at(d.border, x, y, p, angle_deg, scale)
    if lobe is None:
        return None, "No connector fits there."
    others = [other for i, other in enumerate(d.lobes) if i != ignore]
    why = _connector_fault(lobe, d, others, p, d.land())
    if allow_faults:
        return lobe, why
    return (None, why) if why else (lobe, "")


def split_at(definition, x: float, y: float):
    """
    The split a click at (x, y) puts across the corridor, and why not when it
    cannot go there: ``(line, "")`` or ``(None, reason)``.

    Straight across: through the corridor edge nearest the click, square to
    it, from the bar's outer edge to its outer edge on the other side -- over
    any hole the bar has there (where it rings an island of province), since
    a cut that stops at a hole leaves the ring whole.  Refused where it does
    not come out within ``SPLIT_REACH_WIDTHS`` corridor widths -- a click at
    a junction, or running along a bend -- and where it would pass within a
    clearance of a connector, which it would cut in two, and where it would
    leave a piece under the bar's sliver floor (``MIN_PART_AREA_MM2``), which
    would not be built.  Splits may otherwise sit as close as they like: one
    on each arm is how a junction is taken apart.  The line runs a hair past
    both edges, so it divides the corridor cleanly.
    """
    d = definition
    if d.border is None:
        return None, "There is no corridor to split."
    p = d.settings.connector_params()
    point = Point(x, y)
    part = min(_polys(d.border), key=point.distance)
    ring = min([part.exterior, *part.interiors], key=point.distance)
    frame = _edge_frame(ring, ring.project(point), d.border, p.mpm)
    if frame is None:
        return None, "No clean cut across the corridor here."
    at, _, (nx, ny) = frame
    eps = 0.01 * p.mpm
    reach = SPLIT_REACH_WIDTHS * d.settings.width_mm * p.mpm
    # Both ways from the edge: into the bar, and -- from a hole's edge --
    # back across the hole to the bar on its far side.  Holes filled, so
    # the stretch through the click runs outer edge to outer edge.
    probe = LineString([(at.x + nx * reach, at.y + ny * reach),
                        (at.x - nx * reach, at.y - ny * reach)])
    filled = Polygon(part.exterior)
    inside = [g for g in _lines(probe.intersection(filled))
              if g.distance(at) < 2 * eps]
    outer = part.exterior
    if not inside or inside[0].length >= 2 * reach - 4 * eps or any(
            outer.distance(Point(c)) > 2 * eps
            for c in (inside[0].coords[0], inside[0].coords[-1])):
        return None, ("No clean cut across the corridor here: try a "
                      "straighter stretch, away from a junction.")
    (x0, y0), (x1, y1) = inside[0].coords[0], inside[0].coords[-1]
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    cut = LineString([(x0 - ux * eps, y0 - uy * eps),
                      (x1 + ux * eps, y1 + uy * eps)])
    gap = p.clearance_mm * p.mpm
    if any(cut.distance(lobe) < gap for lobe in d.lobes):
        return None, "It would cut through a connector."
    # Splits may sit as close as they like -- one on each arm of a junction
    # is the usual way to take a junction apart -- but not so close that the
    # piece between falls under the sliver floor the bar is built to, where
    # it would not be built at all and that stretch of bar would be lost.
    floor = MIN_PART_AREA_MM2 * p.mpm ** 2
    trial = replace(d, splits=d.splits + [cut])
    if any(piece.area < floor for piece in trial.pieces()):
        return None, (f"It would leave a piece under {MIN_PART_AREA_MM2:g} "
                      "mm², too small to build -- move it a little further "
                      "from the next split or the end of the bar.")
    return cut, ""


def _lines(geom) -> list:
    """The LineStrings in ``geom``."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in _lines(part)]
    return []


#: Why a connector cannot stand: fault code -> what a refused placement says
#: (``place_connector`` without ``allow_faults``).
FAULTS = {
    "no_province": ("That edge faces no province to print -- a surrounding "
                    "region has nothing to key into."),
    "two_provinces": ("It would reach into two provinces, with no one pocket "
                      "to key into."),
    "escapes": ("Its pocket would run out of the province -- past the coast "
                "or into a neighbour."),
    "crowded": "It would run into another connector.",
    "detached": ("It would come loose from the bar: too little of its root "
                 "is left in the corridor."),
    "recrosses": ("It would meet the corridor in two places -- its far end "
                  "runs back into the bar past a bend or a junction."),
    "on_split": "It would sit on a split, and be cut in two.",
    "thin": "Its neck would be too thin to print.",
}

#: The same, for a connector already placed: what the status line says of a
#: red one.
STANDING_FAULTS = {
    "no_province": "it faces no province to print",
    "two_provinces": "it reaches into two provinces",
    "escapes": "its pocket runs out of the province",
    "crowded": "it runs into another connector",
    "detached": "too little of its root is in the corridor",
    "recrosses": "it meets the corridor in two places",
    "on_split": "it sits on a split, and would be cut in two",
    "thin": "its neck is too thin to print",
}

#: The same, as the Borders report counts generated connectors it dropped.
FAULT_COUNTS = {
    "no_province": "faced no province",
    "two_provinces": "reached into two provinces",
    "escapes": "ran out of their province",
    "crowded": "ran into another connector",
    "detached": "came loose from the bar",
    "recrosses": "met the corridor in two places",
    "crosses_line": "crossed the border line between two provinces",
    "on_split": "sat on a split",
    "thin": "were too thin to print",
}

#: How much of a connector's root must stay in the corridor, as a fraction of
#: what an unturned one of its size has there, for the bar to hold it.
_ROOT_HOLD_FRAC = 0.5


def _pieces(geom, lobe) -> int:
    """How many separate pieces of ``geom`` count, against ``lobe``'s size:
    a sliver where two outlines disagree by rounding is not a crossing."""
    return sum(1 for poly in _polys(geom)
               if poly.area > _OWNER_FRAC * lobe.area)


def _connector_fault(lobe, definition, others, p, land) -> str:
    """Why ``lobe`` cannot stand beside ``others``, or "" when it can.
    ``land`` is the union of the targets, passed in so a caller checking
    many lobes builds it once."""
    code = _fault(lobe, definition, others, p, land)
    return FAULTS[code] if code else ""


def _fault(lobe, definition, others, p, land) -> str:
    """
    The code (a key of ``FAULTS``) of the first rule ``lobe`` breaks beside
    ``others``, or "" when it breaks none.

    * It must reach a province to print, and only one (FR-3.7).
    * Its pocket -- the lobe grown by the clearance, outside the corridor --
      must lie inside that one province: a pocket that crosses the coast or a
      frontier opens in the side of the piece.
    * It must not touch another connector, and its pocket must stay a
      clearance clear of every other's, or two pockets run into one.
    * Turned or shrunk by hand, it must keep enough root in the corridor to
      hang on to the bar, and a neck the printer can hold.
    * It must meet the corridor in one place only -- its root.  Near a bend
      or a junction the far end can run back into the bar, which fuses the
      connector to the bar there and leaves its pocket with a side open.
    """
    code = _own_fault(lobe, definition, p, land)
    if code:
        return code
    reach = lobe.difference(definition.border)
    gap = p.clearance_mm * p.mpm
    for other in others:
        if _crowds(lobe, reach, other, other.difference(definition.border)
                   if _near(lobe, other, gap) else None, gap):
            return "crowded"
    return ""


def _own_fault(lobe, definition, p, land) -> str:
    """The first rule ``lobe`` breaks on its own -- every rule but
    crowding, which needs the others -- or ""."""
    d = definition
    c = lobe.exterior.coords
    neck_m = Point(c[0]).distance(Point(c[3]))
    floor_m = min(2 * _cfg("seam.nozzle_mm"), p.neck_mm) * p.mpm
    if neck_m < floor_m * (1 - 1e-6):
        return "thin"
    root_m2 = p.overlap_mm * p.mpm * neck_m          # an unturned root's area
    in_bar = lobe.intersection(d.border)
    if in_bar.area < _ROOT_HOLD_FRAC * root_m2:
        return "detached"
    if _pieces(in_bar, lobe) > 1 \
            or _pieces(lobe.difference(d.border), lobe) > 1:
        return "recrosses"
    if lobe.intersection(land).area < p.min_overlap_frac * lobe.area:
        return "no_province"
    targets = list(d.targets.values())
    if _owners(lobe, d.border, targets) != 1:
        return "two_provinces"
    gap = p.clearance_mm * p.mpm
    pocket = lobe.buffer(gap).difference(d.border)
    owner = max(targets, key=lambda geom: pocket.intersection(geom).area)
    if pocket.difference(owner).area > _ESCAPE_FRAC * lobe.area:
        return "escapes"
    return ""


def _near(lobe, other, gap) -> bool:
    """Whether ``other``'s box comes within ``gap`` of ``lobe``'s: the cheap
    test that spares the geometry for the many that are nowhere near."""
    x0, y0, x1, y1 = lobe.bounds
    ox0, oy0, ox1, oy1 = other.bounds
    return not (ox1 < x0 - gap or ox0 > x1 + gap
                or oy1 < y0 - gap or oy0 > y1 + gap)


def _crowds(lobe, reach, other, other_reach, gap) -> bool:
    """Whether two connectors touch, or their pockets come within ``gap``.
    ``other_reach`` is None when the two are nowhere near."""
    if other_reach is None:
        return False
    return lobe.intersects(other) or reach.distance(other_reach) < gap


def reshape_connectors(definition: BorderDefinition,
                       settings: BorderSettings):
    """
    ``definition``'s connectors in the shape ``settings`` asks for, each where
    it stood: ``(lobes, dropped)``.

    A lobe is rebuilt at its anchor on the corridor edge, turned and sized as
    it was (:func:`lobe_pose`), so a connector put, turned or resized by hand
    stays as it was left.  One the new shape no longer lets stand -- grown into
    a neighbour, or now reaching into two provinces -- is kept, and drawn red
    until it is fixed; only one that cannot be built at all is dropped and
    counted.  The rest keep their order.
    """
    d = definition
    if d.border is None or not d.lobes:
        return list(d.lobes), 0
    old, new = d.settings.connector_params(), settings.connector_params()
    kept, dropped = [], 0
    for lobe in d.lobes:
        x, y, angle, scale = lobe_pose(lobe, d.border, old)
        reshaped = lobe_at(d.border, x, y, new, angle, scale)
        if reshaped is None:
            dropped += 1
        else:
            kept.append(reshaped)
    return kept, dropped


@dataclass
class BarResult:
    """The border bar, split for the bed (result of :func:`build_bar`)."""
    mesh: object = None
    pieces: list = field(default_factory=list)
    origin_m: tuple = (0.0, 0.0)   #: metres point at the bar's print-mm (0, 0)
    mpm: float = 0.0
    lobes: int = 0
    lobes_cut: int = 0             #: connectors a split went through
    draped: bool = False           #: top follows the terrain, not flat
    too_big: int = 0               #: pieces that will not fit the bed
    #: Per solid in ``pieces``: whether it is part of a piece too big for
    #: the bed -- the 3D panel draws those red.
    piece_too_big: list = field(default_factory=list)


def build_bar(definition: BorderDefinition, bed_mm=None,
              margin_mm=None, surface=None) -> BarResult:
    """
    The border bar (corridor + lobe layer), in the pieces the splits plan.

    Nothing is split for you: each piece is a stretch of corridor between the
    splits drawn on the Borders tab (:meth:`BorderDefinition.pieces`), with
    the connectors it carries, extruded on its own.  All of them share one
    local frame whose metres origin is known -- so they lie where they will
    join, and the 3D panel can lay a terrain solid beside them.  A piece too
    big for the bed is built all the same and counted (``too_big``):
    ``bed_mm`` and ``margin_mm`` are what it is measured against.

    ``surface`` -- ``(xs_m, ys_m) -> print-mm``, from :func:`bar_surface` --
    drapes the bar's top over the terrain so it stands as tall as the solids
    either side of it; without one the top is flat at the bar's thickness.
    """
    if definition.border is None:
        raise ValueError("No corridor to print: the targets share no border "
                         "and touch no surrounding region.")
    params = definition.settings.connector_params()
    lobes = definition.lobes
    # Print-mm (0, 0) at the bottom-left of the corridor and lobes together,
    # for every piece: they stay where they lie relative to each other.
    bottom = _safe_union([definition.border] + list(lobes)) if lobes \
        else definition.border
    origin = tuple(bottom.bounds[:2])
    big = set(definition.too_big(bed_mm, margin_mm))
    pieces, flags = [], []
    for i, (piece, mine) in enumerate(zip(definition.pieces(),
                                          definition.piece_lobes())):
        solids = build_border_pieces(piece, mine, params, surface,
                                     origin_m=origin)
        pieces += solids
        flags += [i in big] * len(solids)
    if not pieces:
        raise ValueError("The corridor is too thin to extrude; widen it.")
    mesh = pieces[0] if len(pieces) == 1 else trimesh.util.concatenate(pieces)
    result = BarResult(mesh=mesh, pieces=pieces, origin_m=origin,
                       mpm=params.mpm, lobes=len(lobes),
                       draped=surface is not None, too_big=len(big),
                       piece_too_big=flags)
    result.lobes_cut = _lobes_cut(pieces, lobes, origin, params.mpm)
    return result


def bar_surface(raster, corridor, warp: ReprojectParams,
                terrain: TerrainParams, warp_path):
    """
    The terrain's height over the corridor, for :func:`build_bar` to drape on.

    The DEM is warped to the working CRS -- where the corridor is -- over the
    corridor's extent, at the resolution and resampling the crop uses, then
    read through the solid's own elevation -> mm rule.  So the bar's top meets
    each solid at the height that solid has at the seam, whichever province's
    crop it came from.
    """
    pad = 2 * max(warp.dst_res_m, 1.0)
    params = replace(warp, bounds=bounds_for(corridor, EPSG_WORKING, pad))
    reproject_geotiff(raster, EPSG_WORKING, warp_path, params)
    return surface_sampler(warp_path, terrain)


def build_draped_bar(definition: BorderDefinition, raster, warp, terrain,
                     warp_path) -> BarResult:
    """:func:`build_bar` with its top draped over ``raster``."""
    return build_bar(definition, surface=bar_surface(
        raster, definition.border, warp, terrain, warp_path))


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
    # The sidecar fingerprints the settings the piece is built under: the
    # ones this desk has saved, not merely the shipped ones.
    config = load_files(*settings_files())
    if not lobes:
        return terrain_to_stl(raster, boundary, out_path, params, config)
    result = terrain_to_stl(raster, boundary, None, params, config)
    if CRS.from_user_input(result.crs) != CRS.from_user_input(EPSG_WORKING):
        raise ValueError(
            f"Pockets need the terrain built in {EPSG_WORKING}, where the "
            f"connectors are; this crop is in {result.crs}. Set CRS to "
            f"{EPSG_WORKING} and re-run the crop.")
    connector = connector or load_connector_params(*settings_files(),
                                                   scale=params.scale)
    if connector.pocket_depth_mm >= params.base_height_mm:
        raise ValueError(
            f"The pockets are {connector.pocket_depth_mm:g} mm deep (Height "
            f"plus Height clear.), which cuts through the "
            f"{params.base_height_mm:g} mm Base. Raise the Base or make the "
            "connectors shallower.")
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
        self.resize(1250, 720)

        # -- state ----------------------------------------------------------
        self.raster_path: Path | None = None
        self.raster_crs = None
        self.raster_bounds = None            #: (l, b, r, t) in the raster CRS
        self.raster_shape = (0, 0)           #: (width, height) in pixels
        self.raster_res = (0.0, 0.0)         #: native metres per pixel
        self.overview = None                 #: masked array for the map
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
        #: The CRS of what the map last drew, while it drew something.  A
        #: redraw keeps the view only in the same frame: limits carried from
        #: EPSG:3979 into EPSG:3347 would point at another part of the globe.
        self._view_crs = None
        self._grab = None                    #: an in-progress drag-pan
        #: The definition the Borders map was last drawn from, and the
        #: artists of its connector layer (see ``_draw_lobes``).
        self._drawn_borders = None
        self._lobe_artists: list = []
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
        #: Borders settings turned but not yet written, and the timer that
        #: writes them.  See SAVED_BORDER_SETTINGS.
        self._pending_settings: dict = {}
        self._settings_timer = QTimer(self)
        self._settings_timer.setSingleShot(True)
        self._settings_timer.setInterval(SETTINGS_WRITE_MS)
        self._settings_timer.timeout.connect(self._write_settings)
        self._boundary_is_region = False     #: boundary came from the combo?
        # Connector editing: undo/redo hold whole lobe lists, the press is in
        # screen pixels, the drag is the index of the lobe being moved.
        self._selected_split: int | None = None   #: the split clicked
        self._lobe_undo: list = []
        self._lobe_redo: list = []
        self._press = None
        self._drag: int | None = None
        self._drag_artist = None
        self._selected: int | None = None    #: the connector right-clicked
        self._turning = False                #: dragging its handle
        self._last_key_edit = 0.0            #: when a key last turned/sized it

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 4)   # give the canvas the spare pixels
        root.setSpacing(4)

        # The canvas is built ahead of the controls, as in the island editor:
        # every button hands the keyboard back to it (see ``_button``).
        # One map, showing the latest state of the stage on screen (see
        # ``_draw_map``) rather than a before | after pair.
        self.figure = Figure(figsize=(11, 6))
        # Room for the title above, and nothing else: the map takes the rest.
        self.figure.subplots_adjust(left=0.005, right=0.995, bottom=0.005,
                                    top=0.95)
        self.canvas = FigureCanvasQTAgg(self.figure)
        # Embedded, the canvas keeps Qt's NoFocus: the map keys and the arrow
        # pan would never reach it.
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self._clear_ax(self.ax, "Import a GeoTIFF to begin")
        self.mpl_toolbar = MapToolbar(self.canvas, self)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("key_press_event", self._on_key)

        # -- stage 1: import -------------------------------------------------
        self.btn_raster = self._button("Import GeoTIFF …", self.import_geotiff)
        self.btn_raster.setToolTip(
            "Open the DEM. Only its metadata and a small overview are read, "
            "so even the 7.3 GB Canada120m.tif opens in under a second.")
        self.btn_add_targets = self._button(
            "Add Provinces …", lambda: self.add_regions_dialog("target"))
        self.btn_add_targets.setToolTip(
            "Provinces to print. Several at once; each file is one region.")
        self.btn_add_surrounding = self._button(
            "Add Surrounding …", lambda: self.add_regions_dialog("surrounding"))
        self.btn_add_surrounding.setToolTip(
            "Context regions (US states, unprinted provinces). They shape the "
            "borders -- the corridor is carved from their side -- but are "
            "never printed and get no connectors.")
        self.btn_remove = self._button("Remove", self.remove_selected_region)
        self.btn_remove.setToolTip("Drop the region selected in the list.")
        self.region_list = QListWidget()
        self.region_list.setMaximumHeight(90)

        self.grp_dem = self._group("DEM", self.btn_raster)
        self.grp_regions = self._group(
            "Regions", self.btn_add_targets, self.btn_add_surrounding,
            self.btn_remove)
        import_page = QWidget()
        import_layout = QVBoxLayout(import_page)
        import_layout.setContentsMargins(4, 4, 4, 4)   # docs/UI_GUIDE.md §1
        import_layout.setSpacing(4)
        import_layout.addLayout(self._row(self.grp_dem, self.grp_regions))
        import_layout.addWidget(self.region_list)

        # -- stage 2: borders ------------------------------------------------
        defaults = load_border_settings(*settings_files())
        self.coast_spin = self._mm_spin(
            defaults.coast_tol_mm, 0.0, 20.0,
            "Simplification tolerance for arcs one region holds alone -- "
            "coasts and frontiers with nothing printed beyond, in printed "
            "millimetres (borders.coast_tol_mm).")
        self.internal_spin = self._mm_spin(
            defaults.internal_tol_mm, 0.0, 20.0,
            "Simplification tolerance for arcs two regions share, in printed "
            "millimetres. Both sides use the one arc, so a seam cannot open a "
            "gap (borders.internal_tol_mm).")
        self.min_seg_spin = self._mm_spin(
            defaults.min_segment_mm, 0.0, 5.0,
            "Collapse shared-arc runs shorter than this. Douglas-Peucker keeps "
            "a tight zigzag inside its corridor; this removes it. 0 skips the "
            "pass (seam.min_segment_mm).")
        self.min_angle_spin = self._spin(
            0.0, 90.0, defaults.min_angle_deg, 0,
            "Drop shared-arc corners sharper than this. A needle prints as a "
            "spur on one side and a slot on the other. 0 skips the pass "
            "(seam.min_angle_deg).")
        self.width_spin = self._mm_spin(
            defaults.width_mm, 0.1, 20.0,
            "Corridor width carved between regions -- the border bar's width. "
            "Target/target seams split it evenly; target/surrounding takes it "
            "all from the surrounding side (borders.width_mm).")
        self.enclave_spin = self._spin(
            0.0, 100.0, defaults.enclave_mm2, 1,
            "A piece of a province the corridor cuts off from the rest of its "
            "land -- between two arms of a zig-zag border, or between the bar "
            "and the sea -- is added to the border bar below this printed "
            "area, rather than printed as a loose crumb. Islands that were "
            "islands before are never touched. 0 keeps every piece "
            "(borders.enclave_mm2).")
        self.enclave_spin.setSingleStep(0.5)
        self.chk_connectors = QCheckBox("Place")
        self.chk_connectors.setChecked(defaults.connectors)
        self.chk_connectors.setToolTip(
            "Place dovetail connectors on the corridor and pockets in the "
            "provinces when the borders are defined.")
        self.spacing_spin = self._mm_spin(
            defaults.lobe_spacing_mm, 2.0, 100.0,
            "Gap between connectors along the corridor.")
        for widget in (self.coast_spin, self.internal_spin, self.min_seg_spin,
                       self.min_angle_spin, self.width_spin,
                       self.enclave_spin, self.spacing_spin):
            widget.valueChanged.connect(self._invalidate_borders)
        self.chk_connectors.toggled.connect(self._invalidate_borders)
        # The connector's shape.  Unlike the settings above these do not stale
        # the definition: the lobes are reshaped where they stand, so the ones
        # put by hand survive.  Keyboard tracking is off so typing "2.5" is
        # one reshape, not three.
        thickness = _cfg("print.border_thickness_mm")
        self.lobe_height_spin = self._mm_spin(
            defaults.lobe_height_mm, 0.2, max(0.3, thickness - 0.2),
            "Height of the connector layer under the border bar -- and so the "
            "depth of the pocket it drops into in the province's back. Under "
            f"the {thickness:.1f} mm bar (print.border_thickness_mm); keep it "
            "under the terrain Base, or the pocket cuts through.")
        self.lobe_depth_spin = self._mm_spin(
            defaults.lobe_depth_mm, 0.5, 20.0,
            "How far each connector reaches into its province from the "
            "corridor edge.")
        self.lobe_width_spin = self._mm_spin(
            defaults.lobe_width_mm, 0.4, 20.0,
            "Width of the connector's neck, at the corridor edge -- its "
            "narrowest, and so the part that must survive printing.")
        self.lobe_ratio_spin = self._spin(
            1.05, 5.0, defaults.lobe_ratio, 2,
            "Width at the far end over the width at the neck. Above 1 the "
            "wide end is captured behind the neck, which is what locks the "
            "pieces together; 2 is a 1.6 mm neck opening to 3.2 mm.")
        self.lobe_ratio_spin.setSingleStep(0.1)
        self.lobe_clear_spin = self._mm_spin(
            defaults.clearance_mm, 0.0, 2.0,
            "How much wider than the connector its pocket is cut, on every "
            "side, so a printed connector drops in. Also the gap kept between "
            "neighbouring pockets (borders.clearance_mm).")
        self.lobe_height_clear_spin = self._mm_spin(
            defaults.height_clearance_mm, 0.0, 2.0,
            "How much deeper than the connector layer is tall its pocket is "
            "cut, so the bar sits flat on the bed rather than on the "
            "connectors. Height plus this must stay under the terrain Base "
            "(borders.height_clearance_mm).")
        for widget in self._shape_spins():
            widget.setKeyboardTracking(False)
            widget.valueChanged.connect(self._on_connector_shape)
        self.btn_define = self._button("Define", self.define,
                                       enabled=False)
        self.btn_define.setToolTip(
            "Simplify every outline in one topology, carve the corridor and "
            "place the connectors. Places them afresh, discarding any edited "
            "by hand.")
        self.btn_save_borders = self._button("Save …",
                                             self.save_borders, enabled=False)
        self.btn_load_borders = self._button("Load …", self.load_borders,
                                             enabled=False)
        self.btn_load_borders.setToolTip(
            "Reload a folder Save wrote -- the corridor, the trimmed provinces "
            "and the connectors as they were left, hand edits and all -- with "
            "the Borders settings they were made with. The provinces must be "
            "imported first.")
        self.btn_save_borders.setToolTip(
            "Write borders.geojson and name_<province>.geojson, the layout the "
            "dovetail tools read (build_border_from_dir).")
        self.btn_edit_lobes = self._button("Edit Connectors", None,
                                           enabled=False)
        self.btn_edit_lobes.setCheckable(True)
        self.btn_edit_lobes.setToolTip(
            "Edit the connectors by hand on the map:\n"
            "  click a connector to select it: turn it, set its depth or\n"
            "    delete it under Selected connector, or turn and size it with\n"
            "    ] [ } { + - or by dragging the dot at its far end;\n"
            "    Del deletes it, Esc lets it go\n"
            "  drag a connector to move it along the corridor\n"
            "  click beside the corridor to add one on that side\n\n"
            "Define Borders places them afresh, discarding what was edited.")
        self.btn_edit_lobes.toggled.connect(self._on_edit_toggled)
        self.btn_lobe_undo = self._button("Undo", self.undo_connectors)
        self.btn_lobe_undo.setToolTip("Step back through the connector edits.")
        self.btn_lobe_redo = self._button("Redo", self.redo_connectors)
        self.btn_lobe_redo.setToolTip(
            "Step forward again through the connector edits.")
        self.btn_edit_splits = self._button("Edit Splits", None,
                                            enabled=False)
        self.btn_edit_splits.setCheckable(True)
        self.btn_edit_splits.setToolTip(
            "Plan where the border bar is cut into printable pieces -- it is "
            "never cut for you. With this on, click the corridor to put a "
            "split straight across it there, click a split to select it, and "
            "Remove takes the selected one away; drag to pan. A split is "
            "refused through a connector. The bar is slate where a piece fits "
            "the bed and red where it is too big, here and in the 3D view.")
        self.btn_edit_splits.toggled.connect(self._on_splits_toggled)
        self.btn_remove_split = self._button("Remove", self.remove_split,
                                             enabled=False)
        self.btn_remove_split.setToolTip("Remove the selected split.")
        self.lbl_pieces = QLabel()
        self.lbl_pieces.setToolTip(
            "How many pieces the bar prints in, and how many of them will not "
            "fit the bed (print.bed_mm less print.bed_margin_mm each side) "
            "at any turn -- diagonally included, as a slicer would place it.")
        self.lbl_pieces.setFixedWidth(
            self.lbl_pieces.fontMetrics().horizontalAdvance(
                "888 pieces, 888 too big") + 6)
        self.border_report = QLabel("Add provinces on the Import tab.")
        self.border_report.setWordWrap(True)

        # The one connector selected: its own turn and depth.
        self.sel_label = QLabel("none")
        # Its text changes on every pick, so it holds the width of the widest
        # thing it can say rather than sliding the spins along.
        self.sel_label.setFixedWidth(max(
            self.sel_label.fontMetrics().horizontalAdvance(text)
            for text in ("none", "connector 8888")) + 4)
        self.sel_angle_spin = self._spin(
            -ANGLE_LIMIT_DEG, ANGLE_LIMIT_DEG, 0.0, 1,
            "Turn the selected connector about where it meets the corridor "
            "(0 is square to the edge). ] / [ turn it "
            f"{ANGLE_STEP_DEG:g}°, }} / {{ {FINE_ANGLE_STEP_DEG:g}°; "
            "or drag the dot at its far end.")
        self.sel_angle_spin.setSingleStep(ANGLE_STEP_DEG)
        self.sel_depth_spin = self._mm_spin(
            defaults.lobe_depth_mm, 0.5, 20.0,
            "How far the selected connector reaches into its province. Its "
            "neck and mouth grow with it, so it keeps its shape. + / - change "
            f"it by {DEPTH_STEP_MM:g} mm.")
        self.sel_depth_spin.setSingleStep(DEPTH_STEP_MM)
        for spin in (self.sel_angle_spin, self.sel_depth_spin):
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._on_selected_spins)
        self.btn_sel_reset = self._button("Reset", self.reset_selected,
                                          enabled=False)
        self.btn_sel_reset.setToolTip(
            "Square to the edge and the size the Shape group sets.")
        self.btn_sel_delete = self._button("Delete", self.delete_selected,
                                           enabled=False)
        self.btn_sel_delete.setToolTip("Delete the selected connector.")
        for widget in (self.sel_angle_spin, self.sel_depth_spin):
            widget.setEnabled(False)          # until one is selected

        self.grp_simplify = self._group(
            "Simplify", "Coast (mm)", self.coast_spin,
            "Shared (mm)", self.internal_spin)
        self.grp_seams = self._group(
            "Seam clean-up", "Min seg (mm)", self.min_seg_spin,
            "Min angle (°)", self.min_angle_spin)
        self.grp_corridor = self._group(
            "Corridor", "Width (mm)", self.width_spin,
            "Enclaves (mm²)", self.enclave_spin)
        self.grp_borders = self._group(
            "Borders", self.btn_define, self.btn_save_borders,
            self.btn_load_borders)
        self.grp_connectors = self._group(
            "Connectors", self.chk_connectors, "Spacing (mm)",
            self.spacing_spin)
        self.grp_shape = self._group(
            "Shape", "Height (mm)", self.lobe_height_spin,
            "Depth (mm)", self.lobe_depth_spin,
            "Width (mm)", self.lobe_width_spin,
            "Ratio", self.lobe_ratio_spin)
        self.grp_fit = self._group(
            "Fit", "Clearance (mm)", self.lobe_clear_spin,
            "Height clear. (mm)", self.lobe_height_clear_spin)
        self.grp_by_hand = self._group(
            "By hand", self.btn_edit_lobes, self.btn_lobe_undo,
            self.btn_lobe_redo)
        self.grp_splits = self._group(
            "Splits", self.btn_edit_splits, self.btn_remove_split,
            self.lbl_pieces)
        self.grp_selected = self._group(
            "Selected connector", self.sel_label,
            "Angle (°)", self.sel_angle_spin,
            "Depth (mm)", self.sel_depth_spin,
            self.btn_sel_reset, self.btn_sel_delete)

        # Row 1 defines the borders; row 2 is the connectors on them, a row
        # of their own: where they go and what shape they are; row 3 is the
        # editing by hand, and the one connector it has selected.
        borders_page = QWidget()
        borders_layout = QVBoxLayout(borders_page)
        borders_layout.setContentsMargins(4, 4, 4, 4)   # docs/UI_GUIDE.md §1
        borders_layout.setSpacing(4)
        borders_layout.addLayout(self._row(
            self.grp_simplify, self.grp_seams, self.grp_corridor,
            self.grp_borders))
        borders_layout.addLayout(self._row(
            self.grp_connectors, self.grp_shape, self.grp_fit))
        borders_layout.addLayout(self._row(self.grp_by_hand,
                                           self.grp_selected,
                                           self.grp_splits))
        borders_layout.addWidget(self.border_report)

        # -- stage 3: STL -- the original harness ----------------------------
        self.province_combo = QComboBox()
        self.province_combo.setMinimumContentsLength(14)
        self.province_combo.setToolTip(
            "The province the crop and the solid are built for. Once borders "
            "are defined it is cut to the province with the corridor removed.")
        self.province_combo.currentTextChanged.connect(self._select_province)

        self.crs_combo = QComboBox()
        self.crs_combo.setEditable(True)
        self.crs_combo.addItems(CRS_CHOICES)
        self.crs_combo.setToolTip(
            "CRS the output is built in. EPSG:3347 is this project's print "
            "CRS — the one the borders and dovetails are built in. Leaving the "
            "DEM in EPSG:3979 misplaces a province by ~5 mm.")
        self.crs_combo.currentTextChanged.connect(self._invalidate_raster)

        # 0 m means "native resolution" — the libs' own convention.
        # Seeded from terrain.spacing_mm through the scale: 0.4 mm at 1:8M is
        # 3200 m, so the default Res is the configured print pitch, not a magic
        # number that could drift from the meshing stage's idea of spacing.
        # It keeps its suffix: "native" and "3200 m/px" must read alike.
        self.res_spin = self._spin(
            0.0, 100_000.0,
            _cfg("terrain.spacing_mm") * _cfg("print.scale") / 1000.0, 0,
            "Ground metres per output pixel; 0 is the DEM's native "
            "resolution. 1:8,000,000 with a 0.4 mm nozzle wants ~3200 m.")
        self.res_spin.setSingleStep(100.0)
        self.res_spin.setSpecialValueText("native")
        self.res_spin.setSuffix(" m/px")
        self.res_spin.valueChanged.connect(self._on_res_typed)
        self.chk_res_auto = QCheckBox("Auto")
        self.chk_res_auto.setChecked(True)
        self.chk_res_auto.setToolTip(
            "Use the finest resolution this province can be built at: the "
            "finest whose STL still fits the triangle limit "
            "(terrain.max_triangles), and never finer than the DEM itself. A "
            "small province comes out at the DEM's own pitch, a large one "
            "coarser. Choosing a Res by hand turns this off.")
        self.chk_res_auto.toggled.connect(self._invalidate_raster)

        self.buf_spin = self._spin(
            0.0, 100_000.0, 0.0, 0,
            "Grow the boundary outward before cutting, to keep a coastal lip. "
            "Locked at 0 once borders are defined: outward is the corridor.")
        self.buf_spin.setSingleStep(100.0)
        self.buf_spin.valueChanged.connect(self._invalidate_raster)

        self.resample_combo = QComboBox()
        self.resample_combo.addItems(RESAMPLING_CHOICES)
        self.resample_combo.setCurrentText(_cfg("terrain.resampling"))
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
            "Keep pixels the boundary merely touches, not only those it covers.")
        self.chk_touched.toggled.connect(self._invalidate_raster)
        self.chk_crop = QCheckBox("Crop")
        self.chk_crop.setChecked(True)
        self.chk_crop.setToolTip(
            "Crop to the boundary's bounding box. Untick to keep the DEM's "
            "full extent, which shows where the province sits in it.")
        self.chk_crop.toggled.connect(self._invalidate_raster)

        # Keeps its suffix, like Res: "default" and "256 MB" must read alike.
        self.cache_spin = self._spin(
            0.0, 8192.0, DEFAULT_CACHE_MB, 0,
            "GDAL's block cache for the run. More is faster -- a striped DEM "
            "re-reads whole scanlines when they fall out of it -- and it never "
            "changes the pixels that come out, nor the finest resolution you "
            "can build: that is set by the STL's triangle limit (see Auto). "
            "6 GB by default; 0 leaves GDAL's own (5% of RAM).")
        self.cache_spin.setSingleStep(64.0)
        self.cache_spin.setSpecialValueText("default")
        self.cache_spin.setSuffix(" MB")

        self.btn_run = self._button("Reproject + Crop", self.run,
                                    enabled=False)
        self.btn_run.setToolTip(
            "Warp the DEM to the CRS and Res, and cut it to the province, off "
            "the UI thread. Changing any setting in this group stales the "
            "result and the solid built from it.")
        self.btn_save = self._button("Save GeoTIFF …", self.save_geotiff,
                                     enabled=False)
        self.btn_save.setToolTip(
            "Keep the cropped raster: a copy of exactly the file inspected.")

        # A row of its own for the mesh stage: these controls act on a
        # different stage with a different invalidation rule, and putting them
        # beside the raster controls would imply they re-warp the DEM.
        saved = load_terrain_params(*settings_files())
        self.exag_spin = self._spin(
            1.0, 200.0, saved.z_exaggeration, 1,
            "Vertical exaggeration (terrain.exaggeration). At 1:8,000,000 "
            "true scale (x1) is 0.20 mm of relief on Newfoundland — "
            "invisible. Share one value across a whole map.")
        self.exag_spin.setSingleStep(5.0)
        self.exag_spin.valueChanged.connect(self._invalidate_mesh)

        self.base_spin = self._spin(
            0.1, 50.0, saved.base_height_mm, 2,
            "Solid base under the terrain (terrain.base_mm). Must exceed the "
            "dovetail pocket depth, or the pocket cuts through the bottom of "
            "the piece.")
        self.base_spin.setSingleStep(0.5)
        self.base_spin.valueChanged.connect(self._invalidate_mesh)

        self.floor_spin = self._spin(
            -1000.0, 9000.0, saved.floor_m, 0,
            "Elevation the base measures from (terrain.floor_m). Share one "
            "value across a map, or neighbouring pieces step at the seam.")
        self.floor_spin.setSingleStep(50.0)
        self.floor_spin.valueChanged.connect(self._invalidate_mesh)

        self.tri_spin = QSpinBox()
        self.tri_spin.setRange(*MAX_TRIANGLES_RANGE)
        self.tri_spin.setSingleStep(MAX_TRIANGLES_STEP)
        self.tri_spin.setGroupSeparatorShown(True)
        self.tri_spin.setValue(saved.max_triangles)
        self.tri_spin.setKeyboardTracking(False)
        self.tri_spin.setToolTip(
            "The most triangles a solid's grid may have -- counted over the "
            "province's whole bounding box, so a solid cut to its coast comes "
            "out well under it. Build STL refuses beyond it, and Auto Res "
            "picks the finest resolution under it.\n\n"
            "Doubling it makes a province about 1.4x finer, and doubles the "
            "STL (about 48 MB a million triangles), the build time, the memory "
            "and the 3D preview's work; the slicer slows with it. Measured: "
            "Quebec at 3M is 1995 m, 73 MB and 3.9 s to build, against "
            "2820 m, 38 MB and 2.0 s at 1.5M.\n\n"
            "At 1:8,000,000 a 0.4 mm nozzle resolves about 3200 m: finer than "
            "that the extra detail is mostly smaller than the printer can lay "
            "down (terrain.max_triangles, kept for next time).")
        self.tri_spin.valueChanged.connect(self._on_max_triangles)

        self.chk_cut = QCheckBox("Cut coastline")
        self.chk_cut.setChecked(True)
        self.chk_cut.setToolTip(
            "Cut the solid to the boundary. Untick for the rectangular slab "
            "DEMto3D produces, to compare old and new pieces.")
        self.chk_cut.toggled.connect(self._invalidate_mesh)

        self.chk_pockets = QCheckBox("Pockets")
        self.chk_pockets.setChecked(True)
        self.chk_pockets.setToolTip(
            "Carve the dovetail pockets this province's connectors key into. "
            "Needs borders defined with connectors, and the crop in "
            f"{EPSG_WORKING}.")
        self.chk_pockets.toggled.connect(self._invalidate_mesh)

        self.btn_build = self._button("Build STL", self.build_stl,
                                      enabled=False)
        self.btn_build.setToolTip(
            "Mesh the cropped raster into a watertight solid.")
        self.btn_save_stl = self._button("Save STL …", self.save_stl,
                                         enabled=False)
        self.btn_save_stl.setToolTip("Keep the solid as an STL.")

        # Built once for the whole map, not per province, so it has its own
        # group and its own result.  Draped, it stands on the terrain heights
        # the groups above set, so those stale it too.
        self.btn_build_bar = self._button("Build Border STL",
                                          self.build_border_bar, enabled=False)
        self.btn_build_bar.setToolTip(
            "Extrude the corridor (and its connector layer), then split it to "
            f"the {_cfg('print.bed_mm'):.0f} mm bed (print.bed_mm, "
            "print.bed_margin_mm).\n\n"
            "With a DEM imported the top is draped over the terrain, read at "
            "the Res / Resample / Exag / Base / Floor the solids use, so it "
            "stands as tall as the solids either side. Without one it is flat "
            "at print.border_thickness_mm.")
        self.btn_save_bar = self._button("Save Border Pieces …",
                                         self.save_border_pieces, enabled=False)
        self.btn_save_bar.setToolTip(
            "Write each bed-sized piece of the border bar as its own STL.")

        self.grp_province = self._group("Province", self.province_combo)
        # The raster stage in two groups: the frame and pitch it is warped
        # to, then how it is cut -- and the run, which ends with the cut.
        self.grp_warp = self._group(
            "Warp", "CRS", self.crs_combo, "Res", self.res_spin,
            self.chk_res_auto,
            "Resample", self.resample_combo, "Cache", self.cache_spin)
        self.grp_cut = self._group(
            "Cut", "Buffer (m)", self.buf_spin, self.chk_touched,
            self.chk_crop, self.btn_run, self.btn_save)
        self.grp_solid = self._group(
            "Solid", "Exag", self.exag_spin, "Base (mm)", self.base_spin,
            "Floor (m)", self.floor_spin, "Max triangles", self.tri_spin,
            self.chk_cut, self.chk_pockets,
            self.btn_build, self.btn_save_stl)
        self.grp_bar = self._group(
            "Border bar", self.btn_build_bar, self.btn_save_bar)

        stl_page = QWidget()
        stl_layout = QVBoxLayout(stl_page)
        stl_layout.setContentsMargins(4, 4, 4, 4)   # docs/UI_GUIDE.md §1
        stl_layout.setSpacing(4)
        stl_layout.addLayout(self._row(self.grp_province, self.grp_warp))
        # The bar beside the cut rather than the solid: it is built once for
        # the whole map, and the solid's row is full with its own controls.
        stl_layout.addLayout(self._row(self.grp_cut, self.grp_bar))
        stl_layout.addLayout(self._row(self.grp_solid))

        self.tabs = QTabWidget()
        self.tabs.addTab(import_page, "1 · Import")
        self.tabs.addTab(borders_page, "2 · Borders")
        self.tabs.addTab(stl_page, "3 · STL")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Maximum)
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs)

        # -- the maps: before | after, and the 3D panel ----------------------
        # DEM + cropped raster on the left, the finished solid on the right.
        # The 3-D panel is vispy, not mplot3d: a province is ~200k triangles and
        # matplotlib re-sorts every one of them on each rotate.
        panels = QHBoxLayout()
        panels.setContentsMargins(0, 0, 0, 0)
        panels.setSpacing(8)
        panels.addWidget(self.canvas, stretch=2)

        self.solid_canvas, self.solid_view = self._make_scene()
        self.solid_title = QLabel("3D preview")
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(self.solid_title)
        right.addWidget(self.solid_canvas.native, stretch=1)
        panels.addLayout(right, stretch=1)
        root.addLayout(panels, stretch=1)

        # -- under the maps: what they say, and how they are looked at -------
        # The view tools belong to the maps, not to any stage, so they sit
        # against them, on the status line's row rather than one of their own:
        # height taken here comes straight off the maps.
        self.show_combo = QComboBox()
        self.show_combo.addItems(SHOW_CHOICES)
        self.show_combo.setToolTip(
            "What the 3D panel shows. Assembly lays the solid beside the bar "
            "in one frame, where a pocket that missed its lobe is visible.")
        self.show_combo.currentTextChanged.connect(self._refresh_3d)

        self.status = QLabel("No DEM loaded.")
        self._emphasise(self.status)
        # May shrink below its text, or a long report would set the window's
        # minimum width from the bottom row.
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Preferred)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(4)
        foot.addWidget(self.status, stretch=1)
        for widget in (self.mpl_toolbar, self._separator(), QLabel("3D view"),
                       self.show_combo):
            foot.addWidget(widget)
        root.addLayout(foot)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.setCentralWidget(central)

        # Arrow keys pan the maps, bound to the canvas so that Up/Down still
        # step a focused spin box (see ``_bind_pan``).
        for key, (dx, dy) in ((Qt.Key.Key_Left, (-1, 0)),
                              (Qt.Key.Key_Right, (1, 0)),
                              (Qt.Key.Key_Down, (0, -1)),
                              (Qt.Key.Key_Up, (0, 1))):
            for modifier, step in ((0, PAN_STEP),
                                   (Qt.Modifier.SHIFT.value, PAN_STEP_COARSE),
                                   (Qt.Modifier.CTRL.value, PAN_STEP_FINE)):
                self._bind_pan(modifier | key.value, dx * step, dy * step)

        # Every sequence the platform lists for undo/redo, not just the first:
        # on Windows StandardKey.Redo is Ctrl+Y alone.
        self._bind(QKeySequence.StandardKey.Undo, self.undo_connectors,
                   "Ctrl+Z")
        self._bind(QKeySequence.StandardKey.Redo, self.redo_connectors,
                   "Ctrl+Shift+Z", "Ctrl+Y")
        # …and keep the text entries from swallowing them (see eventFilter).
        for entry in (self.coast_spin, self.internal_spin, self.min_seg_spin,
                      self.min_angle_spin, self.width_spin,
                      self.enclave_spin, self.spacing_spin,
                      *self._shape_spins(), self.sel_angle_spin,
                      self.sel_depth_spin, self.res_spin, self.buf_spin,
                      self.cache_spin, self.exag_spin, self.base_spin,
                      self.floor_spin, self.tri_spin,
                      self.crs_combo.lineEdit()):
            entry.installEventFilter(self)

        # Wired last, so that building the window is not mistaken for
        # somebody choosing the values the controls were built on.
        self._wire_saved_settings()
        self._wire_shortcuts()
        self._update_border_controls()
        self._update_title()

    @staticmethod
    def _make_scene():
        canvas = scene.SceneCanvas(keys="interactive", bgcolor=BG_COLOUR,
                                   show=False)
        view = canvas.central_widget.add_view()
        view.camera = scene.TurntableCamera(fov=0, elevation=28, azimuth=-55)
        # vispy's orthographic depth range defaults to +/-1e6: too coarse to
        # resolve a 1 mm dovetail pocket against the bar laid into it, so the
        # Assembly view drew pieces over lobes that were really in place.
        view.camera.depth_value = 1000.0
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
        """A print-mm length, to the 0.05 mm a 0.4 mm nozzle can mean.

        No suffix: the unit is in the label beside it (docs/UI_GUIDE.md §3).
        """
        spin = cls._spin(lo, hi, value, 2, tip)
        spin.setSingleStep(0.05)
        return spin

    def _button(self, text, slot, enabled=True) -> QPushButton:
        b = QPushButton(text)
        if slot is not None:
            b.clicked.connect(slot)
        # Hand the keyboard back to the map once the button has done its work,
        # so the arrow keys pan rather than walk the focus along the row.
        b.clicked.connect(lambda *_: self.canvas.setFocus())
        b.setEnabled(enabled)
        return b

    @staticmethod
    def _group(title: str, *contents) -> QGroupBox:
        """A titled box of controls, the title naming what they act on.

        Bare strings become labels, so a group reads in source the way it
        reads on screen. The box is also the enable handle for everything
        inside it (see ``_set_busy``).
        """
        box = QGroupBox(title)
        row = QHBoxLayout(box)
        row.setContentsMargins(8, 2, 8, 4)
        row.setSpacing(4)
        for item in contents:
            row.addWidget(QLabel(item) if isinstance(item, str) else item)
        return box

    @staticmethod
    def _row(*groups) -> QHBoxLayout:
        """One row of groups, packed to the left."""
        row = QHBoxLayout()
        row.setSpacing(4)
        for group in groups:
            row.addWidget(group)
        row.addStretch(1)
        return row

    def _groups(self) -> tuple:
        """Every group of controls, on every tab."""
        return (self.grp_dem, self.grp_regions, self.grp_simplify,
                self.grp_seams, self.grp_corridor, self.grp_borders,
                self.grp_connectors, self.grp_shape, self.grp_fit,
                self.grp_by_hand, self.grp_selected, self.grp_splits,
                self.grp_province,
                self.grp_warp, self.grp_cut, self.grp_solid, self.grp_bar)

    @staticmethod
    def _separator() -> QFrame:
        """A vertical rule, so a row's parts are divided, not just spaced."""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    @staticmethod
    def _emphasise(label: QLabel) -> None:
        """Make a label carry: larger than the chrome, at full palette contrast.

        No colour is set: a hardcoded grey is legible on one theme only.
        """
        font = label.font()
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(size + STATUS_POINT_BUMP)
        else:                            # a font defined in pixels, not points
            font.setPixelSize(font.pixelSize() + STATUS_POINT_BUMP * 2)
        label.setFont(font)

    # -- keyboard -----------------------------------------------------------
    def eventFilter(self, obj, event):
        """Stop a focused entry from claiming undo/redo for its own text.

        A ``QLineEdit`` (and every spin box edits through one) accepts the
        ``ShortcutOverride`` for Ctrl+Z, so with one focused the key would
        roll back a typed digit instead of the last connector edit.
        """
        if event.type() == QEvent.Type.ShortcutOverride and (
                event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            return True
        return super().eventFilter(obj, event)

    def _bind(self, standard_key, slot, *extra) -> None:
        """Wire ``slot`` to the platform bindings for ``standard_key`` plus
        ``extra`` literal sequences, skipping duplicates."""
        seqs = list(QKeySequence.keyBindings(standard_key))
        seqs += [QKeySequence(s) for s in extra]
        seen = set()
        for seq in seqs:
            text = seq.toString()
            if text in seen:
                continue
            seen.add(text)
            QShortcut(seq, self, activated=slot)

    def _wire_shortcuts(self) -> None:
        """Give every button a key, and say so in its tooltip.

        One table, so what else has claimed a key is answerable at a glance.
        The keys hang on the buttons, so a disabled button is a dead key and
        a button on a tab not showing cannot be fired.  File actions take
        modified keys; the work of each stage takes the initial that tells it
        apart.  Undo and Redo are annotated but not rebound (see ``_bind``).
        """
        for widget, sequence in (
                (self.btn_raster, "Ctrl+O"),
                (self.btn_add_targets, "Ctrl+P"),
                (self.btn_add_surrounding, "Ctrl+Shift+P"),
                (self.btn_remove, "Shift+Del"),
                (self.btn_define, "D"),
                (self.btn_save_borders, "Ctrl+B"),
                (self.btn_load_borders, "Ctrl+L"),
                (self.btn_edit_lobes, "E"),
                (self.btn_edit_splits, "X"),
                (self.btn_remove_split, "Backspace"),
                (self.btn_sel_reset, "R"),
                (self.btn_sel_delete, QKeySequence.StandardKey.Delete),
                (self.btn_run, "C"),
                (self.btn_save, "Ctrl+Shift+S"),
                (self.btn_build, "S"),
                (self.btn_save_stl, "Ctrl+S"),
                (self.btn_build_bar, "B"),
                (self.btn_save_bar, "Ctrl+Shift+B"),
        ):
            widget.setShortcut(QKeySequence(sequence))
            self._hint(widget, widget.shortcut())
        self._hint(self.btn_lobe_undo, QKeySequence.StandardKey.Undo)
        self._hint(self.btn_lobe_redo, "Ctrl+Shift+Z")
        for action in self.mpl_toolbar.actions():
            sequence = _NAV_KEYS.get(action.text())
            if sequence is not None:
                action.setShortcut(QKeySequence(sequence))
                self._hint(action, action.shortcut())

    @staticmethod
    def _hint(widget, sequence) -> None:
        """Append ``sequence`` to ``widget``'s tooltip, as the desktop spells it."""
        keys = QKeySequence(sequence).toString(
            QKeySequence.SequenceFormat.NativeText)
        tip = widget.toolTip()
        tail = "Shortcut: " + keys
        widget.setToolTip(f"{tip}\n\n{tail}" if tip else tail)

    def _bind_pan(self, sequence, fx: float, fy: float) -> None:
        """Pan by ``(fx, fy)`` of the view on a key, while the canvas has focus."""
        shortcut = QShortcut(QKeySequence(sequence), self.canvas,
                             activated=lambda: self._pan_by(fx, fy))
        shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)

    def _pan_by(self, fx: float, fy: float) -> None:
        """Pan the map by a fraction of its visible span."""
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        dx, dy = (x1 - x0) * fx, (y1 - y0) * fy
        self.ax.set_xlim(x0 + dx, x1 + dx)
        self.ax.set_ylim(y0 + dy, y1 + dy)
        self.canvas.draw_idle()

    def _update_title(self) -> None:
        name = self.raster_path.name if self.raster_path else "(no file)"
        self.setWindowTitle(f"Terrain Editor — {name}")

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
        self._draw_map()

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
        return load_border_settings(
            *settings_files(),
            coast_tol_mm=self.coast_spin.value(),
            internal_tol_mm=self.internal_spin.value(),
            min_segment_mm=self.min_seg_spin.value(),
            min_angle_deg=self.min_angle_spin.value(),
            width_mm=self.width_spin.value(),
            enclave_mm2=self.enclave_spin.value(),
            connectors=self.chk_connectors.isChecked(),
            lobe_spacing_mm=self.spacing_spin.value(),
            **self._shape_values())

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
        self._selected = None
        self._selected_split = None
        self._lobe_undo.clear()
        self._lobe_redo.clear()
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
        if d.loaded_from:
            # What Define measured on the way is not saved, so there is no
            # vertex count or seam report to give -- only where they came from.
            text = (f"Loaded from {d.loaded_from}: {len(d.targets)} "
                    "province(s).")
        else:
            text = self._define_text()
        return self._corridor_text(text)

    def _define_text(self) -> str:
        """What Define measured: simplification, seams, overlaps."""
        d = self.borders
        text = (f"{len(d.simplified)} regions: {d.vertices_before:,} → "
                f"{d.vertices_after:,} vertices. Seams: {d.short_segments} "
                f"run(s) under {_cfg('seam.nozzle_mm'):.2f} mm, {d.spikes} "
                f"spike(s) under {_cfg('seam.spike_angle_deg'):.0f}°. "
                + (f"{d.overlap_mm2:.2f} mm² claimed by two provinces, handed "
                   "back to the one that held it." if d.overlap_mm2 > 1e-4
                   else "No overlap."))
        return text

    def _corridor_text(self, text: str) -> str:
        """The corridor and its connectors, after ``text``."""
        d = self.borders
        s = d.settings
        if d.border is None:
            text += " No corridor: the provinces touch nothing."
        else:
            text += (f" Bar in {self._pieces_text()} "
                     f"({len(d.splits)} split(s)).")
            text += f" Corridor {s.width_mm:.2f} mm"
            if d.enclaves:
                text += (f", {d.enclaves} enclave(s) "
                         f"({d.enclaves_mm2:.2f} mm²) added to it")
            if s.connectors:
                text += f", {len(d.lobes)} connector(s)"
                bad = sum(1 for code in d.faults() if code)
                if bad:
                    text += f", {bad} invalid (red)"
                if d.lobes_edited:
                    text += " (edited by hand)"
                elif d.lobes_dropped:
                    why = ", ".join(f"{n} {FAULT_COUNTS[code]}" for code, n
                                    in d.drop_reasons.items())
                    text += f" ({d.lobes_dropped} dropped: {why})"
            text += "."
        return text + " Save them, or build the STLs on tab 3."

    def _invalidate_borders(self) -> None:
        """A stage-2 setting changed: the definition and everything built from
        it describe borders that are no longer asked for."""
        if self.borders is None and self.bar is None:
            return
        self.borders = None
        self._lobe_undo.clear()
        self._lobe_redo.clear()
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
        self.btn_load_borders.setEnabled(bool(self.targets) and not busy)
        self.btn_build_bar.setEnabled(
            self.borders is not None and self.borders.border is not None
            and not busy)
        self.btn_save_bar.setEnabled(self.bar is not None and not busy)
        editable = self.connectors_editable() and not busy
        self.btn_edit_lobes.setEnabled(editable)
        if not self.connectors_editable() and self.btn_edit_lobes.isChecked():
            self.btn_edit_lobes.setChecked(False)
        splittable = self.splits_editable() and not busy
        self.btn_edit_splits.setEnabled(splittable)
        if not self.splits_editable() and self.btn_edit_splits.isChecked():
            self.btn_edit_splits.setChecked(False)
        if self._selected_split is not None and (
                self.borders is None
                or self._selected_split >= len(self.borders.splits)):
            self._selected_split = None
        self.btn_remove_split.setEnabled(
            splittable and self._selected_split is not None)
        self.lbl_pieces.setText(self._pieces_text())
        editable = editable or splittable
        self.btn_lobe_undo.setEnabled(editable and bool(self._lobe_undo))
        if hasattr(self, "sel_angle_spin"):
            self._sync_selected()
        self.btn_lobe_redo.setEnabled(editable and bool(self._lobe_redo))
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
            self, "Save borders into folder", self._save_dir())
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
        ``build_border_from_dir`` reads.
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
        # The lobes as they stand, so a hand edit survives the folder being
        # read back (build_border_from_dir uses these rather than its own).
        connectors = folder / CONNECTORS_NAME
        if self.borders.lobes:
            p = self.borders.settings.connector_params()
            poses = [lobe_pose(lobe, self.borders.border, p)
                     for lobe in self.borders.lobes]
            frame = gpd.GeoDataFrame(
                {"name": [f"connector_{i}" for i in
                          range(1, len(self.borders.lobes) + 1)],
                 "angle_deg": [round(pose[2], 3) for pose in poses],
                 "scale": [round(pose[3], 4) for pose in poses]},
                geometry=list(self.borders.lobes), crs=EPSG_WORKING)
            frame.to_crs(default_crs).to_file(connectors, driver="GeoJSON")
            written.append(connectors)
        elif connectors.exists():
            connectors.unlink()      # a stale set would key into nothing
        for name, geom in self.borders.targets.items():
            write(name, geom, self.region_crs.get(name, default_crs),
                  folder / f"name_{_safe_name(name)}.geojson")
        splits = folder / SPLITS_NAME
        if self.borders.splits:
            gpd.GeoDataFrame(
                {"name": [f"split_{i}" for i in
                          range(1, len(self.borders.splits) + 1)]},
                geometry=list(self.borders.splits), crs=EPSG_WORKING,
            ).to_crs(default_crs).to_file(splits, driver="GeoJSON")
            written.append(splits)
        elif splits.exists():
            splits.unlink()          # stale splits would cut the wrong bar
        # The settings the connectors were shaped by, so Load Borders can
        # rebuild them as they were -- a connector's turn and size are read
        # off its outline against these.
        settings = folder / BORDER_SETTINGS_NAME
        settings.write_text(json.dumps(asdict(self.borders.settings),
                                       indent=2) + "\n", encoding="utf-8")
        written.append(settings)
        return written

    def load_borders(self) -> None:
        """Reload a folder Save Borders wrote, hand edits and all."""
        if self._thread is not None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Load borders from folder", self._save_dir())
        if not folder:
            return
        try:
            definition = read_borders(Path(folder), self.border_settings())
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return
        if not self.adopt_borders(definition):
            return
        self.status.setText(
            f"Loaded {len(definition.targets)} province(s) and "
            f"{len(definition.lobes)} connector(s) from {Path(folder).name}."
            f"{self._faults_text()}")

    def adopt_borders(self, definition) -> bool:
        """Make a loaded definition the one on screen, settings and all.

        Its provinces must be the ones imported: a border carved for other
        provinces would key into land that is not on the table.
        """
        missing = sorted(set(definition.targets) - set(self.targets))
        if missing:
            self.status.setText(
                "Borders not loaded: add these provinces on the Import tab "
                f"first -- {', '.join(missing)}.")
            return False
        # The controls take the saved settings without reading them as a
        # change: a change would discard the definition being loaded.
        s = definition.settings
        for widget, value in ((self.coast_spin, s.coast_tol_mm),
                              (self.internal_spin, s.internal_tol_mm),
                              (self.min_seg_spin, s.min_segment_mm),
                              (self.min_angle_spin, s.min_angle_deg),
                              (self.width_spin, s.width_mm),
                              (self.enclave_spin, s.enclave_mm2),
                              (self.spacing_spin, s.lobe_spacing_mm),
                              *zip(self._shape_spins(),
                                   (getattr(s, f) for f in SHAPE_FIELDS))):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.chk_connectors.blockSignals(True)
        self.chk_connectors.setChecked(s.connectors)
        self.chk_connectors.blockSignals(False)
        # Everything outside the definition comes from the window: the
        # surrounding regions are drawn as context, as Define draws them.
        for name, geom in self.surrounding.items():
            definition.simplified.setdefault(name, geom)
        self.apply_borders(definition)
        return True

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

    def province_lobes(self) -> list:
        """The lobes the selected province's pockets key into, if asked for."""
        if self.borders is None or not self.chk_pockets.isChecked():
            return []
        return self.borders.province_lobes(self.province_combo.currentText())

    # -- stage 3: the border bar --------------------------------------------
    def _invalid_lobes(self, lobes) -> int:
        """How many of ``lobes`` are red."""
        faults = dict(zip(map(id, self.borders.lobes), self.borders.faults()))
        return sum(1 for lobe in lobes if faults.get(id(lobe)))

    def build_border_bar(self) -> None:
        if self._thread is not None or self.borders is None:
            return
        bad = self._invalid_lobes(self.borders.lobes)
        if bad:
            self.status.setText(
                f"Border bar not built: {bad} connector(s) are invalid (red). "
                "Move, turn, resize or delete them on the Borders tab first.")
            return
        self._set_busy(True)
        if self.raster_path is None:
            self.status.setText("Extruding the border and splitting it to the "
                                "bed …")
            self._launch(CallWorker(build_bar, self.borders), self._on_bar)
            return
        self.status.setText("Draping the border over the DEM, extruding it "
                            "and splitting it to the bed …")
        self._launch(CallWorker(build_draped_bar, self.borders,
                                str(self.raster_path), self._bar_warp(),
                                self._terrain_params(),
                                str(self._tempdir / "border_surface.tif")),
                     self._on_bar)

    def _bar_warp(self) -> ReprojectParams:
        """The warp the bar's surface is read through: the crop's own
        resolution and resampling, so the bar and the solids agree."""
        return ReprojectParams(
            dst_res_m=self.res_spin.value(),
            resampling=Resampling[self.resample_combo.currentText()],
            gdal_cache_mb=int(self.cache_spin.value()))

    def _on_bar(self, bar) -> None:
        self._teardown_thread()
        self._set_busy(False)
        self.apply_bar(bar)

    def apply_bar(self, bar: BarResult) -> None:
        self.bar = bar
        self._update_border_controls()
        w, h, d = bar.mesh.extents
        top = (f"top draped over the terrain, up to {d:.1f} mm"
               if bar.draped else
               f"flat {d:.1f} mm top -- import a DEM to drape it")
        text = (f"Border bar {w:.0f} x {h:.0f} mm, {top}, {bar.lobes} "
                f"connector(s), in {len(bar.pieces)} piece(s) as planned.")
        if bar.too_big:
            text += (f" {bar.too_big} piece(s) will not fit the bed: add "
                     "splits on the Borders tab (Edit Splits).")
        if bar.lobes_cut:
            # Splits refuse connectors, and a connector on one is red and
            # stops the build -- but never let it pass quietly.
            text += f" WARNING: {bar.lobes_cut} connector(s) cut by a split."
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
            self, "Save border pieces into folder", self._save_dir())
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

    # -- stage 2: editing connectors by hand ---------------------------------
    def connectors_editable(self) -> bool:
        d = self.borders
        return (d is not None and d.border is not None
                and d.settings.connectors)

    def _editing(self) -> bool:
        return (self.btn_edit_lobes.isChecked() and self._showing_borders()
                and self._thread is None and self.connectors_editable())

    def splits_editable(self) -> bool:
        return self.borders is not None and self.borders.border is not None

    def _editing_splits(self) -> bool:
        return (self.btn_edit_splits.isChecked() and self._showing_borders()
                and self._thread is None and self.splits_editable())

    def _on_splits_toggled(self, on: bool) -> None:
        if on:
            # One tool on the map at a time: a click is a split or a
            # connector, never both.
            self.btn_edit_lobes.setChecked(False)
            mode = str(self.mpl_toolbar.mode)
            if "pan" in mode:
                self.mpl_toolbar.pan()
            elif "zoom" in mode:
                self.mpl_toolbar.zoom()
            self.status.setText(
                "Editing splits: click the corridor to cut the bar there, "
                f"click a split to select it, then Remove. "
                f"{self._pieces_text()}.")
        elif self._selected_split is not None:
            self.select_split(None)

    def split_near(self, x: float, y: float) -> int | None:
        """The index of the split under (x, y), nearest first."""
        if self.borders is None or not self.borders.splits:
            return None
        point = Point(x, y)
        dist, index = min((cut.distance(point), i)
                          for i, cut in enumerate(self.borders.splits))
        return index if dist <= self._pick_distance() else None

    def click_splits(self, x: float, y: float) -> None:
        """A click with Edit Splits on: select the split under it, or put a
        new one across the corridor there."""
        index = self.split_near(x, y)
        if index is not None:
            self.select_split(index)
            return
        p = self.borders.settings.connector_params()
        if self.borders.border.distance(Point(x, y)) > self._pick_distance():
            self.status.setText("Click on the corridor to split the bar "
                                "there.")
            return
        self.add_split(x, y)

    def add_split(self, x: float, y: float) -> bool:
        cut, why = split_at(self.borders, x, y)
        if cut is None:
            self.status.setText(f"No split added: {why}")
            return False
        self._commit_splits(self.borders.splits + [cut], "Added a split.",
                            selected=len(self.borders.splits))
        return True

    def remove_split(self) -> None:
        index = self._selected_split
        if index is None or not self.splits_editable():
            return
        splits = list(self.borders.splits)
        del splits[index]
        self._commit_splits(splits, "Removed a split.", selected=None)

    def select_split(self, index: int | None) -> None:
        if self.borders is None or index is None \
                or not 0 <= index < len(self.borders.splits):
            index = None
        self._selected_split = index
        self._update_border_controls()
        if self._showing_borders():
            self._draw_lobes()

    def _commit_splits(self, splits: list, message: str,
                       selected=_KEEP) -> None:
        """Make ``splits`` the bar's splits, as one undoable step."""
        self._lobe_undo.append(self._snapshot())
        self._lobe_redo.clear()
        self.borders.splits = list(splits)
        if selected is not _KEEP:
            self._selected_split = selected
        self.borders.lobes_edited = True
        # The connectors stay; set again so the bar, report and map follow.
        self._set_lobes(self.borders.lobes, message)

    def _pieces_text(self) -> str:
        """How many pieces the bar prints in, and how many will not fit."""
        d = self.borders
        if d is None or d.border is None:
            return "no bar yet"
        n, big = len(d.pieces()), len(d.too_big())
        text = f"{n} piece{'s' if n != 1 else ''}"
        return text + (f", {big} too big" if big else ", all fit")

    def _on_edit_toggled(self, on: bool) -> None:
        if on:
            self.btn_edit_splits.setChecked(False)
        if not on:
            if self._selected is not None:
                self.select_connector(None)
            return
        # Pan or zoom would take the clicks; the edit tool asked for them.
        mode = str(self.mpl_toolbar.mode)
        if "pan" in mode:
            self.mpl_toolbar.pan()
        elif "zoom" in mode:
            self.mpl_toolbar.zoom()
        self.status.setText(
            "Editing connectors: click one to select it (then Delete, or turn "
            "or resize it), drag one to move it, click beside the corridor to "
            "add one.")

    def connector_at(self, x: float, y: float) -> int | None:
        """The index of the connector under (x, y), nearest first."""
        if self.borders is None or not self.borders.lobes:
            return None
        point = Point(x, y)
        dist, index = min((lobe.distance(point), i)
                          for i, lobe in enumerate(self.borders.lobes))
        return index if dist <= self._pick_distance() else None

    def _pick_distance(self) -> float:
        width_px = max(self.ax.get_window_extent().width, 1.0)
        x0, x1 = self.ax.get_xlim()
        return abs(x1 - x0) / width_px * PICK_PX

    def click_connectors(self, x: float, y: float) -> None:
        """A click with the tool on: select the connector under it, or add one
        on the corridor edge beside it.  Never a delete: that is the Delete
        button's alone, on a connector already selected."""
        index = self.connector_at(x, y)
        if index is not None:
            self.select_connector(index)
            return
        p = self.borders.settings.connector_params()
        reach = p.reach_mm * p.mpm + self._pick_distance()
        if self.borders.border.distance(Point(x, y)) > reach:
            self.status.setText("Click beside the corridor to add a "
                                "connector there.")
            return
        self.add_connector(x, y)

    def add_connector(self, x: float, y: float) -> bool:
        """Add a connector at the edge beside (x, y).  One that breaks a rule
        is added all the same, and drawn red until it is fixed."""
        lobe, why = place_connector(self.borders, x, y, allow_faults=True)
        if lobe is None:
            self.status.setText(f"No connector added: {why}")
            return False
        self._commit_lobes(self.borders.lobes + [lobe], "Added a connector.",
                           selected=len(self.borders.lobes))
        return True

    def delete_connector(self, index: int) -> None:
        lobes = list(self.borders.lobes)
        del lobes[index]
        selected = self._selected
        if selected == index:
            selected = None
        elif selected is not None and selected > index:
            selected -= 1
        self._commit_lobes(lobes, "Deleted a connector.", selected=selected)

    def move_connector(self, index: int, x: float, y: float) -> bool:
        """Slide connector ``index`` to the edge beside (x, y), turned and
        sized as it was."""
        _, _, angle, scale = self.connector_pose(index)
        lobe, why = place_connector(self.borders, x, y, ignore=index,
                                    angle_deg=angle, scale=scale,
                                    allow_faults=True)
        if lobe is None:
            self.status.setText(f"Connector not moved: {why}")
            return False
        lobes = list(self.borders.lobes)
        lobes[index] = lobe
        self._commit_lobes(lobes, "Moved a connector.", selected=index)
        return True

    # -- the selected connector ----------------------------------------------
    def connector_pose(self, index: int) -> tuple:
        """``(x, y, angle_deg, scale)`` of connector ``index``."""
        d = self.borders
        return lobe_pose(d.lobes[index], d.border,
                         d.settings.connector_params())

    def _faults_text(self) -> str:
        """What the status line adds about red connectors: why the selected
        one is, and how many there are."""
        if self.borders is None:
            return ""
        faults = self.borders.faults()
        bad = sum(1 for code in faults if code)
        if not bad:
            return ""
        text = f" {bad} invalid (red)"
        index = self._selected
        if index is not None and index < len(faults) and faults[index]:
            text += (f"; this one because "
                     f"{STANDING_FAULTS[faults[index]]}")
        return text + "."

    def select_connector(self, index: int | None) -> None:
        """Select connector ``index`` (None for none) for turning and sizing."""
        if self.borders is None or index is None \
                or not 0 <= index < len(self.borders.lobes):
            index = None
        self._selected = index
        if index is not None and self.borders.faults()[index]:
            self.status.setText(
                f"Connector {index + 1} is invalid (red): "
                f"{STANDING_FAULTS[self.borders.faults()[index]]}.")
        self._sync_selected()
        if self._showing_borders():
            self._draw_lobes()

    def _sync_selected(self) -> None:
        """Selected connector shows the selected connector, or is greyed."""
        d = self.borders
        if self._selected is not None and (
                d is None or self._selected >= len(d.lobes)):
            self._selected = None
        on = self._selected is not None and self._thread is None
        for widget in (self.sel_angle_spin, self.sel_depth_spin,
                       self.btn_sel_reset, self.btn_sel_delete):
            widget.setEnabled(on)
        if self._selected is None:
            self.sel_label.setText("Selected: none")
            return
        _, _, angle, scale = self.connector_pose(self._selected)
        reach = d.settings.connector_params().reach_mm
        self.sel_label.setText(f"Selected: connector {self._selected + 1}")
        for spin, value in ((self.sel_angle_spin, angle),
                            (self.sel_depth_spin, reach * scale)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def pose_selected(self, angle_deg: float, scale: float,
                      message: str, gesture: bool = False) -> bool:
        """
        Turn and size the selected connector where it stands, as one Undo
        step -- or, for a key held down (``gesture``), as part of the step
        the last press began.  One that breaks a rule is kept, red.
        """
        index = self._selected
        if index is None or not self.connectors_editable():
            return False
        angle_deg = max(-ANGLE_LIMIT_DEG, min(ANGLE_LIMIT_DEG, angle_deg))
        x, y, _, _ = self.connector_pose(index)
        lobe, why = place_connector(self.borders, x, y, ignore=index,
                                    angle_deg=angle_deg, scale=scale,
                                    allow_faults=True)
        if lobe is None:
            self.status.setText(f"Connector not changed: {why}")
            self._sync_selected()
            return False
        lobes = list(self.borders.lobes)
        lobes[index] = lobe
        now = time.monotonic()
        if gesture and now - self._last_key_edit < KEY_GESTURE_S:
            self._set_lobes(lobes, message)       # the same Undo step
        else:
            self._commit_lobes(lobes, message)
        self._last_key_edit = now if gesture else 0.0
        return True

    def _on_selected_spins(self) -> None:
        if self._selected is None:
            return
        reach = self.borders.settings.connector_params().reach_mm
        self.pose_selected(self.sel_angle_spin.value(),
                           self.sel_depth_spin.value() / reach,
                           "Changed a connector.")

    def turn_selected(self, step_deg: float, gesture: bool = False) -> bool:
        if self._selected is None:
            return False
        _, _, angle, scale = self.connector_pose(self._selected)
        return self.pose_selected(angle + step_deg, scale,
                                  "Turned a connector.", gesture)

    def deepen_selected(self, step_mm: float, gesture: bool = False) -> bool:
        """Grow (or shrink) the selected connector by ``step_mm`` of depth;
        its neck and mouth follow, so it keeps its shape."""
        if self._selected is None:
            return False
        _, _, angle, scale = self.connector_pose(self._selected)
        reach = self.borders.settings.connector_params().reach_mm
        depth = max(0.5, reach * scale + step_mm)
        return self.pose_selected(angle, depth / reach,
                                  "Resized a connector.", gesture)

    def reset_selected(self) -> bool:
        return self.pose_selected(0.0, 1.0, "Reset a connector.")

    def delete_selected(self) -> None:
        if self._selected is not None and self.connectors_editable():
            self.delete_connector(self._selected)

    def _handle_at(self, index: int) -> tuple:
        """Where the turning handle of connector ``index`` is drawn: the
        middle of its far end."""
        c = self.borders.lobes[index].exterior.coords
        return (c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2

    def _angle_toward(self, index: int, x: float, y: float) -> float:
        """The angle that points connector ``index`` at (x, y), measured
        from square to its edge as :func:`lobe_pose` measures it."""
        ax_, ay_, angle, _ = self.connector_pose(index)
        hx, hy = self._handle_at(index)
        here = np.degrees(np.arctan2(hy - ay_, hx - ax_))
        there = np.degrees(np.arctan2(y - ay_, x - ax_))
        turn = (there - here + 180.0) % 360.0 - 180.0
        return angle + turn

    def _on_key(self, event) -> None:
        """Keys on the map act on the selected connector -- or, editing
        splits, Esc lets the selected split go."""
        if self._editing_splits() and event.key == "escape":
            self.select_split(None)
            return
        if not self._editing() or self._selected is None:
            return
        key = event.key
        if key == "escape":
            self.select_connector(None)
        elif key == "delete":
            self.delete_selected()
        elif key in ("]", "["):
            self.turn_selected(ANGLE_STEP_DEG if key == "]"
                               else -ANGLE_STEP_DEG, gesture=True)
        elif key in ("}", "{"):
            self.turn_selected(FINE_ANGLE_STEP_DEG if key == "}"
                               else -FINE_ANGLE_STEP_DEG, gesture=True)
        elif key in ("+", "="):
            self.deepen_selected(DEPTH_STEP_MM, gesture=True)
        elif key in ("-", "_"):
            self.deepen_selected(-DEPTH_STEP_MM, gesture=True)

    def _shape_spins(self) -> tuple:
        return (self.lobe_height_spin, self.lobe_depth_spin,
                self.lobe_width_spin, self.lobe_ratio_spin,
                self.lobe_clear_spin, self.lobe_height_clear_spin)

    def _shape_values(self) -> dict:
        return dict(zip(SHAPE_FIELDS,
                        (spin.value() for spin in self._shape_spins())))

    def _on_connector_shape(self) -> None:
        """A shape spin turned: reshape the connectors where they stand, as
        one undoable step.  With no borders defined yet the next Define
        picks the value up from the spins, and there is nothing to do."""
        if not self.connectors_editable() or self._thread is not None:
            return
        settings = replace(self.borders.settings, **self._shape_values())
        if settings == self.borders.settings:
            return
        lobes, dropped = reshape_connectors(self.borders, settings)
        message = "Connectors reshaped."
        if dropped:
            message += f" {dropped} dropped: no connector fits there."
        self._lobe_undo.append(self._snapshot())
        self._lobe_redo.clear()
        self.borders.settings = settings
        self._set_lobes(lobes, message, edited=self.borders.lobes_edited)

    def _snapshot(self):
        return (self.borders.lobes, self.borders.settings,
                list(self.borders.splits))

    def _restore(self, snapshot, message: str) -> None:
        lobes, settings, splits = snapshot
        edited = self.borders.lobes_edited
        self.borders.settings = settings
        self.borders.splits = list(splits)
        if self._selected_split is not None \
                and self._selected_split >= len(splits):
            self._selected_split = None
        for spin, value in zip(self._shape_spins(),
                               (getattr(settings, f) for f in SHAPE_FIELDS)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._set_lobes(lobes, message, edited=edited)

    def _commit_lobes(self, lobes: list, message: str,
                      selected=_KEEP) -> None:
        """Make ``lobes`` the connectors, as one undoable step."""
        self._lobe_undo.append(self._snapshot())
        self._lobe_redo.clear()
        self._set_lobes(lobes, message, selected=selected)

    def undo_connectors(self) -> None:
        if self._lobe_undo and self._thread is None and self.borders:
            self._lobe_redo.append(self._snapshot())
            self._restore(self._lobe_undo.pop(), "Undone.")

    def redo_connectors(self) -> None:
        if self._lobe_redo and self._thread is None and self.borders:
            self._lobe_undo.append(self._snapshot())
            self._restore(self._lobe_redo.pop(), "Redone.")

    def _set_lobes(self, lobes: list, message: str,
                   edited: bool = True, selected=_KEEP) -> None:
        """Adopt a lobe list: the bar and any pocketed solid were built from
        the old one, so both go stale."""
        count = len(self.borders.lobes)
        self.borders.lobes = list(lobes)
        self.borders.lobes_edited = edited
        if self._selected is not None and (selected is _KEEP and
                                           len(lobes) != count):
            selected = None      # an index into a list that is not the same
        if selected is not _KEEP:
            self._selected = selected
        self._invalidate_bar()
        if self.stl_result is not None and self._pockets:
            self._invalidate_mesh()
        self._update_border_controls()
        self.border_report.setText(self._borders_text())
        self.status.setText(f"{message} {len(self.borders.lobes)} "
                            f"connector(s).{self._faults_text()}")
        self._sync_selected()
        if self._showing_borders():
            self._draw_lobes()

    # -- map events ---------------------------------------------------------
    def _on_press(self, event) -> None:
        """Left-drag pans the map; with Edit Connectors on, a press on a
        connector (or the selected one's handle) drags that instead."""
        self._turning = False
        self._grab = None
        self._press = None
        if (event.inaxes is not self.ax or event.xdata is None
                or self.mpl_toolbar.mode):
            return
        if event.button == 3:            # right-click: select, or deselect
            if self._editing():
                self.select_connector(
                    self.connector_at(event.xdata, event.ydata))
            return
        if event.button != 1:
            return
        self.canvas.setFocus()
        self._press = (event.x, event.y)
        if self._editing():
            if self._selected is not None and Point(
                    self._handle_at(self._selected)).distance(
                        Point(event.xdata, event.ydata)) \
                    <= self._pick_distance():
                self._drag, self._turning = self._selected, True
                return
            self._drag = self.connector_at(event.xdata, event.ydata)
        if self._drag is None:
            # Ground units per screen pixel, read once: the limits move
            # under the pointer as it pans.  Equal aspect, so one serves both.
            self._grab = (event.x, event.y, self.ax.get_xlim(),
                          self.ax.get_ylim(), self._per_px())

    def _per_px(self) -> float:
        """Ground units under one screen pixel, across the map."""
        width_px = max(self.ax.get_window_extent().width, 1.0)
        x0, x1 = self.ax.get_xlim()
        return abs(x1 - x0) / width_px

    def _pan_to(self, event) -> None:
        """Move the view with a drag that started on empty map."""
        x, y, (x0, x1), (y0, y1), per_px = self._grab
        dx, dy = (event.x - x) * per_px, (event.y - y) * per_px
        self.ax.set_xlim(x0 - dx, x1 - dx)
        self.ax.set_ylim(y0 - dy, y1 - dy)
        self.canvas.draw_idle()

    def _moved(self, event) -> bool:
        return self._press is not None and \
            abs(event.x - self._press[0]) + abs(event.y - self._press[1]) \
            > CLICK_SLOP_PX

    def _on_motion(self, event) -> None:
        """Pan with a drag on empty map; outline where a dragged connector
        would land, red if it cannot."""
        if self._grab is not None:
            if self._moved(event):
                self._pan_to(event)
            return
        if (self._drag is None or event.inaxes is not self.ax
                or event.xdata is None or not self._moved(event)):
            return
        x, y, angle, scale = self.connector_pose(self._drag)
        if self._turning:
            angle = max(-ANGLE_LIMIT_DEG, min(ANGLE_LIMIT_DEG,
                        self._angle_toward(self._drag, event.xdata,
                                           event.ydata)))
        else:
            x, y = event.xdata, event.ydata
        lobe, _ = place_connector(self.borders, x, y, ignore=self._drag,
                                  angle_deg=angle, scale=scale)
        shape = lobe or lobe_at(self.borders.border, x, y,
                                self.borders.settings.connector_params(),
                                angle, scale)
        if self._drag_artist is not None:
            self._drag_artist.remove()
            self._drag_artist = None
        if shape is not None:
            xs, ys = shape.exterior.xy
            (self._drag_artist,) = self.ax.plot(
                xs, ys, linewidth=1.6, zorder=10,
                color=DRAG_COLOUR if lobe is not None else REFUSED_COLOUR)
        self.canvas.draw_idle()

    def _on_release(self, event) -> None:
        moved = self._moved(event)
        press, drag, turning = self._press, self._drag, self._turning
        self._press = self._drag = self._grab = None
        self._turning = False
        if self._drag_artist is not None:
            self._drag_artist.remove()
            self._drag_artist = None
            self.canvas.draw_idle()
        if (press is not None and not moved and event.button == 1
                and event.xdata is not None and event.inaxes is self.ax
                and self._editing_splits()):
            self.click_splits(event.xdata, event.ydata)
            return
        if (press is None or event.button != 1 or event.xdata is None
                or event.inaxes is not self.ax or not self._editing()):
            return
        if not moved:
            self.click_connectors(event.xdata, event.ydata)
        elif turning:
            _, _, _, scale = self.connector_pose(drag)
            self.pose_selected(
                self._angle_toward(drag, event.xdata, event.ydata), scale,
                "Turned a connector.")
        elif drag is not None:
            self.move_connector(drag, event.xdata, event.ydata)

    # -- tabs ---------------------------------------------------------------
    def _showing_borders(self) -> bool:
        return hasattr(self, "tabs") and self.tabs.currentIndex() == 1

    def _on_tab(self, index: int) -> None:
        """Tab 2 shows the borders; tabs 1 and 3 the raster."""
        self._draw_map()

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

    @staticmethod
    def _clear_ax(ax, title: str) -> None:
        ax.clear()
        ax.set_title(title, fontsize=11)
        # ``datalim`` keeps the scale equal by widening the view rather than
        # shrinking the axes, so the map fills the canvas edge to edge.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])

    def _on_scroll(self, event) -> None:
        """Zoom toward the pointer (wheel up = in)."""
        ax = event.inaxes
        if ax is not self.ax or event.xdata is None or event.ydata is None:
            return
        scale = 1 / 1.2 if event.button == "up" else 1.2
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        xd, yd = event.xdata, event.ydata
        ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
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
        self._draw_map(preserve_view=False)
        self.status.setText(
            f"{self.raster_path.name}: {self.raster_shape[0]}x"
            f"{self.raster_shape[1]} @ {self.raster_res[0]:.0f} m, "
            f"{self.raster_crs}. Add a province to crop against.")

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
        self._update_title()
        self.overview = data
        left, bottom, right, top = self.raster_bounds
        self.overview_extent = (left, right, bottom, top)

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
        if self.bar is not None and self.bar.draped:
            self._invalidate_bar()    # it stands on the same terrain heights
        self._refresh_3d()
        if self.raster_path is not None:
            self.status.setText(self._cost_text())

    def _on_max_triangles(self, _value) -> None:
        """A new limit moves Auto's resolution, and so the crop; with Auto
        off it only stales the solid built under the old one."""
        if self.chk_res_auto.isChecked():
            self._invalidate_raster()
        else:
            self._invalidate_mesh()

    def _on_res_typed(self, _value) -> None:
        """A Res chosen by hand: Auto stands down, then the crop is stale."""
        if self.chk_res_auto.isChecked():
            self.chk_res_auto.blockSignals(True)
            self.chk_res_auto.setChecked(False)
            self.chk_res_auto.blockSignals(False)
        self._invalidate_raster()

    def finest_res(self) -> float | None:
        """
        The finest resolution (m/px) the selected province can be built at,
        or None when there is no DEM or boundary to size it against.

        Finest such that the solid's grid fits ``max_triangles`` -- the limit
        Build STL refuses beyond, counted the way the lib counts it, over the
        crop's whole grid -- and never finer than the DEM's own pitch.  The
        grid at each candidate comes from the libs' own grid helpers, so this
        is the grid the run will produce, not an estimate of it.
        """
        if self.raster_path is None or self.boundary is None \
                or self.target_crs() is None:
            return None
        native = float(self.raster_res[0]) or 1.0
        budget = self._terrain_params().max_triangles
        dst = self.target_crs()
        crop = self.chk_crop.isChecked()
        try:
            with rasterio.open(str(self.raster_path)) as ds:
                bounds = (bounds_for(self._boundary_geom(dst), dst)
                          if crop else None)
                window = _read_window(ds, self._boundary_geom(), crop)
                same_crs = not self.needs_warp()

                def triangles(res):
                    if same_crs and res <= native * 1.001:
                        grid = _clip_grid(ds, window, ClipParams(
                            crop=crop, target_res_m=0.0))
                    else:
                        grid = _warp_grid(ds, dst, ReprojectParams(
                            dst_res_m=res, bounds=bounds))
                    return _triangle_count(grid.width, grid.height)

                if triangles(native) <= budget:
                    return 0.0 if same_crs else native
                lo, hi = native, native * 2
                while triangles(hi) > budget:
                    lo, hi = hi, hi * 2
                for _ in range(40):           # to well under a metre
                    if hi - lo < 0.5:
                        break
                    mid = (lo + hi) / 2
                    lo, hi = (lo, mid) if triangles(mid) <= budget \
                        else (mid, hi)
                res = float(np.ceil(hi))
                while triangles(res) > budget:
                    res += 1.0
                return res
        except Exception:
            return None                       # the preview says what is wrong

    def _update_auto_res(self) -> None:
        """With Auto on, put the finest buildable resolution in Res."""
        if not self.chk_res_auto.isChecked():
            return
        res = self.finest_res()
        if res is None:
            return
        self.res_spin.blockSignals(True)
        self.res_spin.setValue(res)
        self.res_spin.blockSignals(False)

    def _invalidate_raster(self) -> None:
        """A raster-parameter change stales both stages.

        The solid is meshed *from* the cropped raster, so a new crop makes the
        old solid describe ground that is no longer on the table.  With Auto
        on, the resolution is worked out again first: what the province can
        be built at depends on the province, the CRS and the crop.
        """
        self._update_auto_res()
        self.result = None
        self.btn_save.setEnabled(False)
        self.btn_run.setEnabled(
            self.raster_path is not None and self.boundary is not None)
        if not self._showing_borders():     # back to the DEM it came from
            self._draw_map()
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
        # The map moves on to the crop, keeping the view where the frame did
        # not change.
        self._draw_map()
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
        return load_terrain_params(
            *settings_files(),
            z_exaggeration=self.exag_spin.value(),
            base_height_mm=self.base_spin.value(),
            floor_m=self.floor_spin.value(),
            max_triangles=self.tri_spin.value(),
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
        bad = self._invalid_lobes(lobes) if lobes else 0
        if bad:
            self.status.setText(
                f"STL not built: {bad} of this province's connectors are "
                "invalid (red), and a pocket is cut for each. Fix them on the "
                "Borders tab, or untick Pockets.")
            return

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
            f"{self._fit_text(result)}, "
            f"watertight={result.watertight}. Inspect in 3D, then save.")

    @staticmethod
    def _fit_text(result) -> str:
        """Whether a solid fits the bed, and turned how far."""
        if not result.bed_fits:
            return "DOES NOT FIT the bed at any turn"
        angle = getattr(result, "bed_angle_deg", 0.0) or 0.0
        return ("fits the bed" if angle < 0.05
                else f"fits the bed turned {angle:.0f}°")

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
            if bar is not None and self.bar.too_big:
                title += f", {self.bar.too_big} too big for the bed (red)"
            self._show_meshes(title, [(bar, self._bar_colours())])
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
                [(self.bar.mesh, self._bar_colours()),
                 (solid, _height_colors(solid))])

    def _bar_colours(self):
        """Per-vertex colours for the bar: grey, and red on each piece too
        big for the bed -- or None (all grey) when every piece fits.

        The bar's mesh is its pieces concatenated in order, so their vertex
        counts, in order, say which vertices are whose.
        """
        bar = self.bar
        if bar is None or not any(bar.piece_too_big) \
                or len(bar.piece_too_big) != len(bar.pieces):
            return None
        if len(bar.pieces) == 1:
            counts = [len(bar.mesh.vertices)]
        else:
            counts = [len(piece.vertices) for piece in bar.pieces]
        if sum(counts) != len(bar.mesh.vertices):
            return None                    # not the concatenation: stay grey
        return np.vstack([np.tile(TOO_BIG_BAR_COLOUR if big else BAR_COLOUR,
                                  (n, 1))
                          for n, big in zip(counts, bar.piece_too_big)]
                         ).astype(np.float32)

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
        default = str(Path(self._save_dir()) / f"{self._stem()}.stl")
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
        # The groups are the enable handles: each control keeps its own flag
        # underneath, so unlocking a group gives back what it had.
        for group in self._groups():
            group.setEnabled(not busy)
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
        The elevation ramp for the map.

        A crop is coloured over its own range.  The overview is coloured over
        its 2nd to 98th percentiles, so a few summits do not flatten the rest:
        the whole-Canada overview runs to Mount Logan's 5500 m while a
        province rarely clears 1500 m.
        """
        if self.result is not None and self.result.zmin is not None:
            return self.result.zmin, self.result.zmax
        if self.overview is None:
            return None, None
        valid = self.overview.compressed()
        if valid.size == 0:
            return None, None
        return float(np.percentile(valid, 2)), float(np.percentile(valid, 98))

    def _draw_map(self, preserve_view: bool = True) -> None:
        """Draw the latest state of the stage on screen.

        The Borders tab shows the borders as defined, or the regions as
        imported until they are; the other tabs show the crop once it has
        run, or the DEM it is cut from until then.  ``preserve_view`` keeps
        the view where it is, but only when the new drawing is in the frame
        of the last one.
        """
        if self._showing_borders():
            self._draw_borders(preserve_view)
        else:
            self._draw_raster(preserve_view)

    def _keep_view(self, frame, preserve_view: bool):
        """The limits to restore after a redraw in ``frame``, or None."""
        if (preserve_view and self._view_crs is not None and frame is not None
                and CRS.from_user_input(frame) == self._view_crs):
            return self.ax.get_xlim(), self.ax.get_ylim()
        return None

    def _settle_view(self, frame, view, drew: bool) -> None:
        """Restore ``view`` if there is one, and note what frame is shown."""
        if view is not None:
            self.ax.set_xlim(view[0])
            self.ax.set_ylim(view[1])
        self._view_crs = (CRS.from_user_input(frame)
                          if drew and frame is not None else None)
        self.canvas.draw_idle()

    def _draw_raster(self, preserve_view: bool = True) -> None:
        """The crop if it has run, else the DEM overview; outlines on top."""
        self._drawn_borders = None       # the borders are no longer on screen
        self._lobe_artists = []
        cropped = self.result is not None and self.result.data is not None
        if cropped:
            frame = self.target_crs() if self.needs_warp() else self.raster_crs
        else:
            frame = self.raster_crs or CRS.from_user_input(EPSG_WORKING)
        view = self._keep_view(frame, preserve_view)
        self._drop_colorbar()
        vmin, vmax = self._colour_limits()
        image = None
        if cropped:
            data, step = self._for_display(self.result.data)
            title = (f"Cropped — {self.result.width}x{self.result.height} @ "
                     f"{self.result.res[0]:.0f} m — {frame}")
            if step > 1:
                # Never let the map imply the saved file is this coarse.
                title += f"  (shown 1:{step} — saved file is full resolution)"
            self._clear_ax(self.ax, title)
            left, bottom, right, top = self.result.bounds_m
            image = self.ax.imshow(
                np.ma.masked_equal(data, self.result.nodata), cmap=DEM_CMAP,
                extent=(left, right, bottom, top), origin="upper",
                vmin=vmin, vmax=vmax)
        elif self.overview is not None:
            self._clear_ax(self.ax, (
                f"DEM overview ({self.overview.shape[1]}x"
                f"{self.overview.shape[0]} of {self.raster_shape[0]}x"
                f"{self.raster_shape[1]}) — {self.raster_crs}"))
            image = self.ax.imshow(self.overview, cmap=DEM_CMAP,
                                   extent=self.overview_extent,
                                   origin="upper", vmin=vmin, vmax=vmax)
        else:
            self._clear_ax(self.ax, f"Regions — {frame}")
        if image is not None:
            self._colorbar = self.figure.colorbar(
                image, ax=self.ax, fraction=0.046, label="elevation (m)")
        # Context first and thinner, so the province being cut draws on top.
        # Once cropped only the province is outlined: the crop is the context.
        selected = (self.province_combo.currentText()
                    if self._boundary_is_region else None)
        if not cropped:
            for name, geom in self.surrounding.items():
                self._plot_outline(self.ax, self._outline_in(name, geom, frame),
                                   colour=SURROUND_COLOUR, width=0.8)
            for name, geom in self.targets.items():
                if name != selected:
                    self._plot_outline(self.ax,
                                       self._outline_in(name, geom, frame),
                                       colour=MARK_COLOUR, width=0.5)
        geom = self._boundary_geom(frame)
        if geom is not None:
            self._plot_outline(self.ax, geom)
        self._settle_view(frame, view, cropped or self.overview is not None
                          or bool(self.targets or self.surrounding))

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

    def _draw_borders(self, preserve_view: bool = True) -> None:
        """Tab 2: the borders as defined, or the regions as imported until
        they are -- both in crs.working.

        ``preserve_view`` keeps the map where it is -- a connector edit is
        made zoomed in, and must not throw the map back to full extent.
        """
        frame = EPSG_WORKING
        view = self._keep_view(frame, preserve_view)
        self._drop_colorbar()
        ax = self.ax
        self._drag_artist = None         # cleared with the axes below
        self._lobe_artists = []          # likewise
        d = self.borders
        self._drawn_borders = d
        if d is None:
            self._clear_ax(ax, f"As imported — {len(self.targets)} to print, "
                               f"{len(self.surrounding)} surrounding")
            for geom in self.surrounding.values():
                self._fill(ax, geom, SURROUND_COLOUR)
            for geom in self.targets.values():
                self._fill(ax, geom, KEEP_COLOUR)
        else:
            self._clear_ax(ax, self._borders_title())
            for name, geom in d.simplified.items():
                if name not in d.targets:
                    self._fill(ax, geom, SURROUND_COLOUR)
            for geom in d.targets.values():
                self._fill(ax, geom, KEEP_COLOUR)
            if d.border is not None:
                self._fill(ax, d.border, CORRIDOR_COLOUR, alpha=0.85)
            self._lobe_artists = self._lobe_layer()
            for colour, segs in ((EDGE_COLOUR, d.coastal_segs),
                                 (SELECT_COLOUR, d.internal_segs)):
                for seg in segs:
                    if len(seg) >= 2:
                        ax.plot(seg[:, 0], seg[:, 1], color=colour,
                                linewidth=0.6)
        ax.autoscale_view()
        self._settle_view(frame, view, bool(d is not None or self.targets
                                            or self.surrounding))

    def _borders_title(self) -> str:
        d = self.borders
        title = f"Defined — {d.settings.width_mm:.2f} mm corridor"
        if d.settings.connectors:
            title += f", {len(d.lobes)} connector(s)"
        return title

    def _lobe_layer(self) -> list:
        """Draw the connectors and the selection; returns what was drawn.

        Not counted into the map's extent: they sit on the corridor, which
        is, and a redraw of this layer alone must not move the view.
        """
        d, ax = self.borders, self.ax
        drawn = []
        if d.lobes:
            # One artist for the lot: a connector is a plain quadrilateral,
            # and 240 patches added one by one were most of an edit's cost.
            # Red until valid: a connector placed by hand may break a rule.
            lobes = PolyCollection(
                [np.asarray(lobe.exterior.coords)[:, :2] for lobe in d.lobes],
                facecolors=[INVALID_COLOUR if code else RESULT_COLOUR
                            for code in d.faults()],
                edgecolors=EDGE_COLOUR, linewidths=0.4, zorder=1.5)
            ax.add_collection(lobes, autolim=False)
            drawn.append(lobes)
        # A piece too big for the bed, filled red over the slate bar -- the
        # reason to add a split -- and under the connectors it carries.
        pieces = d.pieces()
        for i in d.too_big():
            for patch in self._fill(ax, pieces[i], TOO_BIG_COLOUR,
                                    alpha=0.9, extent=False):
                patch.set_zorder(1.2)
                drawn.append(patch)
        for i, cut in enumerate(d.splits):
            xs, ys = cut.xy
            drawn += ax.plot(xs, ys, linewidth=2.6, zorder=9,
                             color=PICKED_COLOUR if i == self._selected_split
                             else SPLIT_COLOUR,
                             scalex=False, scaley=False)
        if self._selected is not None and self._selected < len(d.lobes):
            xs, ys = d.lobes[self._selected].exterior.xy
            drawn += ax.plot(xs, ys, color=PICKED_COLOUR, linewidth=2.0,
                             zorder=9, scalex=False, scaley=False)
            hx, hy = self._handle_at(self._selected)
            drawn += ax.plot([hx], [hy], "o", color=PICKED_COLOUR,
                             markersize=6, zorder=9, scalex=False,
                             scaley=False)
        return drawn

    def _draw_lobes(self) -> None:
        """Redraw the connectors alone, over the map already drawn.

        An edit changes the connectors and nothing under them, so the
        provinces and the corridor stay as they are: on all of Canada a full
        redraw is ~250 shapes and this is the few that moved.  Falls back to
        the full redraw when the map on screen is not this definition's.
        """
        if self.borders is None or self._drawn_borders is not self.borders:
            self._draw_borders(preserve_view=True)
            return
        for artist in self._lobe_artists:
            artist.remove()
        self._lobe_artists = self._lobe_layer()
        self.ax.set_title(self._borders_title(), fontsize=11)
        self.canvas.draw_idle()

    @staticmethod
    def _fill(ax, geom, face, edge=None, alpha=1.0, extent=True) -> list:
        """Fill a (Multi)Polygon, holes and all, without geopandas plotting;
        returns the patches.

        Added with ``add_artist`` rather than ``add_patch``: the latter works
        out the map's extent by walking every segment of the outline in
        Python, which on all of Canada was 0.8 s of a 0.9 s redraw.  The
        extent is taken from the geometry's bounds instead, when ``extent``.
        """
        patches = []
        for poly in _polys(geom):
            poly = orient(poly, 1.0)      # CCW shell, CW holes: nonzero fill
            path = MplPath.make_compound_path(*(
                MplPath(np.asarray(ring.coords)[:, :2], closed=True)
                for ring in (poly.exterior, *poly.interiors)))
            patch = PathPatch(path, facecolor=face, alpha=alpha,
                              edgecolor=edge or "none", linewidth=0.4)
            ax.add_artist(patch)
            patches.append(patch)
            if extent:
                x0, y0, x1, y1 = poly.bounds
                ax.update_datalim([(x0, y0), (x1, y1)])
        return patches


    def _drop_colorbar(self) -> None:
        """Remove the elevation colorbar *before* its panel is cleared.

        ``ax.clear()`` on the map orphans the colorbar, and a later
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
    @staticmethod
    def _save_dir() -> str:
        """Where every save dialog starts: ``paths.stl`` (Data/STL), made if
        it is not there yet."""
        try:
            DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
            return str(DEFAULT_SAVE_DIR)
        except OSError:
            return str(Path.cwd())

    def _stem(self) -> str:
        return self.boundary_path.stem if self.boundary_path else "terrain"

    def save_geotiff(self) -> None:
        if self.result is None or self.result.path is None:
            return
        default = str(Path(self._save_dir()) / f"{self._stem()}_crop.tif")
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

    def _wire_saved_settings(self) -> None:
        """Write each Borders control back to its key as it is turned.

        Wired after the controls took their saved values, so building the
        window is not mistaken for somebody choosing them.
        """
        for key, control in SAVED_BORDER_SETTINGS + SAVED_TERRAIN_SETTINGS:
            widget = getattr(self, control)
            signal = (widget.toggled if isinstance(widget, QCheckBox)
                      else widget.valueChanged)
            signal.connect(lambda value, key=key: self._remember(key, value))

    def _remember(self, key: str, value) -> None:
        """Note a setting to be written, and restart the clock on writing."""
        self._pending_settings[key] = (value if isinstance(value, (bool, int))
                                       else float(value))
        self._settings_timer.start()

    def _write_settings(self) -> None:
        """Write what has been turned since the last write, and forget it.

        ``update_local`` merges one key at a time, so this cannot clobber what
        another window saved; it swallows its own write errors.
        """
        pending, self._pending_settings = self._pending_settings, {}
        for key, value in pending.items():
            update_local(key, value)

    def closeEvent(self, event) -> None:
        # A box turned in the last half second has not been written yet.
        self._settings_timer.stop()
        self._write_settings()
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
