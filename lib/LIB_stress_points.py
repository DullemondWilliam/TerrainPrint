#!/usr/bin/env python3
"""
LIB_stress_points.py

Find the necks that hold a printed piece together, and say which will break.

:mod:`lib.LIB_thin_features` answers *where is the land narrower than W?*  That
is a different question from *where will it break*, and the gap between them is
why this module exists.  Nova Scotia reports 338 thin patches at a 1 mm width and
exactly **one** of them is a stress point; Nunavut has a 1.0 mm neck holding
465 mm² of map that a 1 mm width test passes without comment.  Width is a
property of a place.  Stress is a property of a place **and everything it holds
up**::

    from lib.LIB_stress_points import StressParams, stress_points

    report = stress_points(province, StressParams())
    for neck in report.necks[:3]:
        print(f"{neck.throat_mm:.2f} mm holds {neck.load_mm2:.0f} mm2, "
              f"wants {neck.needed_mm:.2f} mm (severity {neck.severity:.2f})")

All lengths are **print-mm** and convert through ``print.scale`` at the moment of
use, as in :class:`lib.LIB_thin_features.ThinParams`.  Geometry is expected in a
projected metre CRS (the project uses EPSG:3347).  Nothing here edits anything;
this only measures.  :mod:`lib.LIB_island_bridges` is where a neck gets widened.

The width is not an input
-------------------------
Thresholding the land at some width W and asking what the narrow patches touch
does not work, and it fails on the *definition* rather than on precision.  With
"a larger area" taken to mean land at least 3 mm wide, Nova Scotia's real 0.20 mm
neck disappears, because the 54 mm² peninsula it holds is nowhere 3 mm wide.  It
is a larger area by the only test that matters, which is **area**.

So the width comes out rather than in:

1. **The width field.**  Rasterise the land and take a Euclidean distance
   transform; the local width at a pixel is twice its distance to the nearest
   water.  Clamped at ``ceiling_mm``, because there is no printing problem at a
   50 mm throat and the clamp is what makes an edit cheap (see below).
2. **Flood from the thick places down.**  Add land to a union-find in descending
   width order.  Every time an addition joins two standing components, that is a
   neck, and it arrives with its **throat width** (the width at which the gap
   closed, which is the widest cross-section that still severs the two sides),
   the **mass of each side**, and **where** it happened.

No threshold is chosen, every neck is found with its own width, and each is
reported once -- at the width where it is genuinely the bottleneck.

What a neck can hold
--------------------
A printed piece is a flat slab of one thickness of one material, so a neck is a
beam of cross-section ``width x thickness`` and mass is printed **area**, the
same convention :mod:`lib.LIB_island_bridges` states.  Lift a piece and a
peninsula swings sideways and snaps at the waist: in-plane bending, section
modulus ``t*w^2/6``, stress going as **1/w²**.  That is the mode worth drawing,
because it is quadratic in the one quantity the map can show and the user can
change.

The calibration is not invented here.  ``bridges.mass_per_bridge_mm2`` already
says, in this project's own voice, that **1 mm of causeway holds 400 mm² of
map** -- it is what ``_bridges_for`` divides by to decide a join needs a second
bridge.  So::

    needed_mm = bridges.width_mm * sqrt(load_mm2 / mass_per_bridge_mm2)
    severity  = needed_mm / throat_mm

``severity`` is dimensionless and anchored: **1.0 is marginal**, above 1 the neck
is thinner than its load wants, below 1 it is sound.  That anchor is what lets a
colour ramp mean something instead of merely ranking.  The lever arm enters as a
dimensionless slenderness, ``lever_mm / sqrt(load_mm2)``, normalised so a compact
blob against the neck scores 1.0 and never reduces the load below the bridge
calibration.

Measured across Nova Scotia, British Columbia, Newfoundland & Labrador and
Nunavut -- **23 necks, 8 at risk**, worst first: Nunavut 0.28 mm holding 364 mm²
and wanting 1.17 mm (severity 4.3), Nunavut 0.20 mm holding 105 mm² (3.2), Nova
Scotia 0.20 mm holding 49 mm² (2.1), British Columbia 0.40 mm holding 59 mm²
(1.4), Nunavut 1.00 mm holding 457 mm² (1.3), then four more between 1.0 and 1.2
and fifteen below.  A third over the line and two thirds under it: the report
discriminates, which is the whole point of weighing the load at all.

**The load is what hangs off the neck, not what is wider than it.**  The obvious
measure -- the land standing above the throat width -- leaves out the branch's own
coastal fringe, and the undercount grows with the throat: two 36 mm² squares on a
1.6 mm bar read as 20 mm² each, which is under ``min_lobe_mm2``, and the neck
disappears on mass it demonstrably has.  So the load is measured by **cutting the
neck's own blob out and weighing what falls off**.  A real waist is not always a
compact blob -- on Nunavut five necks of fourteen part under that cut and nine do
not -- and where the sides stay joined the figure falls back to the standing mass,
with ``Neck.severed`` saying which measurement you are reading.

Being re-asked cheaply
----------------------
This has to survive being asked again after every brush stroke, so
:func:`stress_points` hands back the raster state it worked on and
:func:`restress` takes it again with a note of what changed.  Rasterising is
*not* what is worth caching -- it is 0.10 s of a 3.5 s run on Nunavut.  The
expensive things are the width field (0.52 s) and the flood (2.6 s), and both are
made local:

**The width field is exactly local.**  Clamped at the ceiling, an edit confined
to a region changes the field only within ``ceiling_mm / 2`` of it: a pixel whose
clamped width is below the ceiling has water within ``ceiling/2``, so if nothing
flipped within ``ceiling/2`` of it its nearest water is the same water, and if its
width is *at* the ceiling the only thing asked of it is that it is thick.
Measured bit-exact over 24 brush edits on three provinces.  The crop must be
padded **twice** as far as the region spliced back, because a crop cannot see the
water beyond its own edge -- with one radius the error inside the window reaches
0.54 mm, which is a five-pixel lie exactly where the answer is read.

**The flood is tiled.**  It depends on the narrow land only through bottleneck
connectivity -- for two lobes, the width of the widest path between them -- and
that composes across a boundary, because any path between two tiles crosses a
cell on their shared edge.  So each tile is summarised once into a small graph:
its *lineages* (the components the banded descent grows inside that tile), the
widths at which they join, and the mass arriving at each width.  Reassembly runs
the same descent over the union of those summaries.  An edit re-summarises only
the tiles it can reach.

Three things that look right and are not
----------------------------------------
Recorded because each cost a prototype:

* **Caching per narrow patch.**  The narrow land is *one patch*: every coastline
  pixel is narrow, so the fringe closes the loop around the province.  Nova
  Scotia has 44 661 of its 44 690 narrow pixels in a single component.  A patch
  is not a local unit.
* **Contracting equal-width plateaus** (exact, and it should shrink the graph):
  1.2x.  Exact Euclidean distances are too varied to collapse.
* **Dynamic minimum spanning tree.**  Lowering the weight of a tree edge invokes
  the cut rule, which needs the best edge crossing a cut whose far side can be
  the whole province.  Not local.

Saying so when it cannot tell
-----------------------------
As in :mod:`lib.LIB_thin_features`, a self-intersecting ring is repaired with
``make_valid`` (``repaired`` records it) and the report comes back with
``measured`` False rather than clean if that fails.  The validity is asked about
**up front** rather than after a throw, which is the one place this differs from
its neighbour and it matters: a boolean operation on a bow-tie raises, so waiting
for the exception works there, but the rasteriser does not complain at all -- it
quietly burns something that is not the province, and the report would be
measured and wrong.  "I could not tell" and
"nothing will break" are not the same answer, and this report is only worth
having if it is believed.  The same applies to the incremental path: ``dirty``
must be a conservative *superset* of what changed, a subset would be silently
wrong, so ``dirty=None`` derives it by XOR and ``StressParams.verify`` recomputes
from scratch and checks.

Cost
----
Measured at the defaults -- a 0.1 mm pixel, a 3.0 mm ceiling, 0.025 mm bands:

================  =======  ========  =========  ======  =====
Province            grid     full      an edit   tiles   held
================  =======  ========  =========  ======  =====
Nova Scotia       0.5 Mpx    0.36 s     87 ms   1.2/35   2 MB
Newfoundland      2.7 Mpx    1.49 s    229 ms  2.2/180  12 MB
British Columbia  3.3 Mpx    1.74 s    243 ms  2.0/209  15 MB
Nunavut          10.7 Mpx    6.72 s    979 ms  1.5/675  51 MB
================  =======  ========  =========  ======  =====

So an edit costs a fifth to a seventh of a full run, and a brush stroke
re-summarises one or two tiles of several hundred.  A full run is threaded with a
progress bar, not called inside a draw loop -- ``UI_border_regions`` is the
precedent -- and the harness prints what each re-ask cost, because an incremental
path that silently goes stale is worse than a slow one.

``band_mm`` is the knob that moves these most, and it moves the *answer* too: at
0.05 mm British Columbia reports three necks where the pixel-by-pixel flood finds
four, because two joins landing in one band are simultaneous and one neck's load
is split across both.  0.025 mm finds all four with their right masses; finer
buys nothing and costs the time twice over.
"""

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import rasterio.features
import shapely
from affine import Affine
from scipy import ndimage, sparse
from scipy.sparse import csgraph
from shapely.geometry import LineString, Point, shape as _shape

from lib.LIB_project_config import get as _cfg

log = logging.getLogger(__name__)

EPSG_METRIC = _cfg("crs.working")
DEFAULT_SCALE = _cfg("print.scale")

#: Four-connectivity. Land that meets only at a corner is not land you can
#: print across, so a diagonal touch must not count as a join.
STRUCT = ndimage.generate_binary_structure(2, 1)

#: The most grid cells to ask for before coarsening the pixel instead. Nunavut
#: is 10.1 M at a 0.1 mm pixel, so this is headroom rather than a limit anyone
#: should meet.
MAX_PIXELS = 40_000_000

#: How many necks to work up into geometry. A caller wants to be taken to the
#: worst, not handed the list.
WORST_KEPT = 20

#: How far from the throat a neck's blob may reach, as a multiple of the throat
#: width. The blob does two jobs and the tighter one sets this: it is the shaded
#: mark on the overlay, and it is the **cut** the load is weighed by, so it has
#: to be the waist and nothing more. A radius of 1.5 throats is enough to cross
#: a neck of one throat; at 3.0, with a width tolerance on top, the cut ate the
#: ends it was supposed to be weighing -- two 36 mm2 squares on a 1.6 mm bar
#: came out as 16 mm2 each and the neck vanished under min_lobe_mm2.
REGION_REACH = 1.5

#: One over root pi: the slenderness of a disc sitting against its own neck.
#: The normalising case, so that a compact lobe scores 1.0 and the lever arm
#: never argues a load *down* below the bridge calibration.
COMPACT_SLENDERNESS = 1.0 / math.sqrt(math.pi)


# -- parameters -------------------------------------------------------------

@dataclass
class StressParams:
    """What counts as a neck, and what one can hold. All lengths are print-mm."""
    scale: float = DEFAULT_SCALE
    #: The width resolution. Everything narrower than two pixels is invisible to
    #: the field, so this is the floor on any throat that can be reported.
    pixel_mm: float = _cfg("stress.pixel_mm")
    #: Above this width, a join is not a stress point -- and clamping here is
    #: what makes an edit local (see the module docstring).
    ceiling_mm: float = _cfg("stress.ceiling_mm")
    #: Throat resolution, and an accuracy knob rather than only a speed one:
    #: two joins landing in the same band are simultaneous, so one neck's load
    #: can be split across two events. At 0.05 mm British Columbia reports
    #: three necks where the pixel-by-pixel flood finds four.
    band_mm: float = _cfg("stress.band_mm")
    #: The reuse unit. 128 px put Nunavut's reassembly graph at 3% of the pixel
    #: graph; bigger tiles reassemble faster but cost more to re-summarise.
    tile_px: int = _cfg("stress.tile_px")
    #: How much land has to hang on each side before a join is a neck at all.
    #: This is "two *larger* areas" in numbers.
    min_lobe_mm2: float = _cfg("stress.min_lobe_mm2")
    #: Room around the land in the grid, so that growing the coast a little does
    #: not throw the cached raster away.
    margin_mm: float = _cfg("stress.margin_mm")
    thickness_mm: float = _cfg("print.border_thickness_mm")
    #: The calibration, borrowed rather than invented: what one bridge of
    #: ``capacity_at_mm`` is reckoned to hold.
    capacity_mm2: float = _cfg("bridges.mass_per_bridge_mm2")
    capacity_at_mm: float = _cfg("bridges.width_mm")
    #: 0 weighs a load by its mass alone; 1 also by how far out it sits.
    lever_power: float = _cfg("stress.lever_power")
    #: Necks below this severity are measured but not reported.
    report_above: float = _cfg("stress.report_above")
    #: Recompute from scratch and check the incremental answer. For tests, and
    #: for anyone who suspects a stale overlay.
    verify: bool = False
    work_crs: str = EPSG_METRIC

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def nbands(self) -> int:
        """How many band steps make up the ceiling."""
        return max(1, int(round(self.ceiling_mm / self.band_mm)))

    def mm_of_step(self, step: int) -> float:
        """The width a band step stands for."""
        return step * self.band_mm


# -- the cached raster ------------------------------------------------------

@dataclass
class TileSummary:
    """One tile's banded lineage, small enough to keep and cheap to rebuild.

    ``edges`` are joins *inside* the tile, between lineages; ``lobe_edges`` are
    attachments to the thick land, which is global and so is named by one of its
    own pixels rather than by a label that relabelling would move.  ``profile``
    is the mass arriving at each band step, which is what lets the reassembly
    weigh a component without going back to the pixels.
    """
    lineages: int = 0
    profile: object = None          #: (lineages, nbands + 1) mass per band step
    reps: object = None             #: one flat pixel index per lineage
    edges: list = field(default_factory=list)       # (a, b, step, prov_flat)
    lobe_edges: list = field(default_factory=list)  # (lineage, lobe_px, step, prov)
    border: object = None           #: flat indices of narrow pixels on the edge
    border_lineage: object = None   #: their lineages


@dataclass
class StressGrid:
    """The raster and everything derived from it, for the next call to reuse.

    The transform is fixed for the grid's life: a different one cannot share a
    cached tile with this one, so :func:`restress` rebuilds rather than pretend.
    ``level`` is the clamped width as a count of band steps, which is both the
    only resolution the descent uses and a quarter of the memory of a float
    field.
    """
    transform: object
    shape: tuple
    land: object                    #: bool, the printed land
    level: object                   #: uint16, clamped width in band steps
    pixel_mm: float
    ceiling_mm: float
    band_mm: float
    tile_px: int
    tiles: dict = field(default_factory=dict)

    @property
    def nbands(self) -> int:
        return max(1, int(round(self.ceiling_mm / self.band_mm)))

    @property
    def bounds(self) -> tuple:
        """(minx, miny, maxx, maxy) of the grid, margin included."""
        h, w = self.shape
        minx = self.transform.c
        maxy = self.transform.f
        return (minx, maxy + h * self.transform.e, minx + w * self.transform.a,
                maxy)

    @property
    def nbytes(self) -> int:
        """What holding this costs, so a caller can decide to."""
        n = self.land.nbytes + self.level.nbytes
        for tile in self.tiles.values():
            if tile.profile is not None:
                n += tile.profile.nbytes
        return n

    def matches(self, params: StressParams) -> bool:
        """True when a run under ``params`` could reuse this grid at all."""
        return (abs(self.pixel_mm - params.pixel_mm) < 1e-12
                and abs(self.ceiling_mm - params.ceiling_mm) < 1e-12
                and abs(self.band_mm - params.band_mm) < 1e-12
                and int(self.tile_px) == int(params.tile_px))

    def covers(self, geom) -> bool:
        """True when ``geom`` lies inside the grid, margin and all."""
        if geom is None or geom.is_empty:
            return True
        minx, miny, maxx, maxy = geom.bounds
        gminx, gminy, gmaxx, gmaxy = self.bounds
        return (minx >= gminx and miny >= gminy
                and maxx <= gmaxx and maxy <= gmaxy)

    def dirty_from(self, land_after) -> object:
        """Which cells changed, by XOR: a correct superset by construction.

        The caller does not have to have tracked its own edit.  This is the
        safe default precisely because getting ``dirty`` too small is the one
        failure that would be silent.
        """
        return np.asarray(land_after, dtype=bool) ^ self.land


# -- the answer -------------------------------------------------------------

@dataclass
class Neck:
    """One load-bearing neck: a narrow place and what hangs off it."""
    point: object                   #: where it closes -- the zoom-to
    throat: object                  #: a bar of throat_mm across the neck
    region: object = None           #: the narrow blob around it, for shading
    throat_mm: float = 0.0
    load_mm2: float = 0.0           #: the lighter side's printed area
    other_mm2: float = 0.0
    lever_mm: float = 0.0           #: throat to the branch's centroid
    needed_mm: float = 0.0          #: the width this neck wants
    severity: float = 0.0           #: needed / throat; 1.0 is marginal
    #: True when cutting the neck's own blob actually parted the two sides, so
    #: ``load_mm2`` is the whole of what hangs off here.  False means the cut did
    #: not part them and the load is the mass standing *above* the throat
    #: instead -- an undercount, because it leaves out the branch's own fringe,
    #: and said out loud rather than guessed at.
    severed: bool = False
    #: What :func:`branch_mask` needs to rebuild the held land without this
    #: object having to carry a grid-sized array per neck.
    branch_step: int = 0
    branch_seed: int = -1

    @property
    def at_risk(self) -> bool:
        return self.severity >= 1.0

    def __str__(self) -> str:
        return (f"{self.throat_mm:.2f} mm holding {self.load_mm2:.0f} mm2 "
                f"(wants {self.needed_mm:.2f} mm, severity {self.severity:.2f})")


@dataclass
class StressReport:
    """Where a piece will break, and what it was measured on."""
    necks: list = field(default_factory=list)       #: worst first
    grid: object = None                             #: hand back to restress()
    #: False when the geometry defeated the measurement even after repair. A
    #: report that is not ``measured`` says nothing about the province; it is
    #: not a clean bill of health.
    measured: bool = True
    repaired: bool = False
    rebuilt: bool = True            #: the grid was built, not reused
    tiles_total: int = 0
    tiles_recomputed: int = 0       #: how much of the cache an edit cost
    pixel_mm: float = 0.0           #: what was used, after any coarsening
    coarsened: bool = False
    land_area_mm2: float = 0.0

    @property
    def at_risk(self) -> list:
        return [n for n in self.necks if n.at_risk]

    @property
    def worst(self) -> float:
        return self.necks[0].severity if self.necks else 0.0

    @property
    def sound(self) -> bool:
        """Nothing at risk -- and we were able to tell."""
        return self.measured and not self.at_risk

    def verdict(self) -> str:
        """The one line a harness should show. Never a bare count."""
        if not self.measured:
            return "could not measure this geometry"
        risky = self.at_risk
        if not self.necks:
            return "no load-bearing necks found"
        if not risky:
            return (f"{len(self.necks)} necks, none at risk — "
                    f"worst {self.necks[0]}")
        return f"{len(risky)} of {len(self.necks)} necks at risk — worst {risky[0]}"


# -- the grid ---------------------------------------------------------------

def _grid_for(geom, params: StressParams):
    """A transform and a shape covering ``geom`` with a margin.

    Coarsens the pixel rather than refusing the grid, as
    ``LIB_shape_scores._grid`` does, and the caller is told which pixel it got
    because a coarser one raises the smallest throat the field can resolve.
    """
    minx, miny, maxx, maxy = geom.bounds
    pad = params.margin_mm * params.mpm
    minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
    pixel_mm, coarsened = params.pixel_mm, False
    pixel_m = pixel_mm * params.mpm
    wanted = ((maxx - minx) / pixel_m + 2) * ((maxy - miny) / pixel_m + 2)
    if wanted > MAX_PIXELS:
        pixel_mm *= math.sqrt(wanted / MAX_PIXELS)
        pixel_m = pixel_mm * params.mpm
        coarsened = True
    width = int(math.ceil((maxx - minx) / pixel_m)) + 2
    height = int(math.ceil((maxy - miny) / pixel_m)) + 2
    transform = Affine(pixel_m, 0.0, minx, 0.0, -pixel_m, maxy)
    return transform, (height, width), pixel_mm, coarsened


def _burn(geom, transform, shape):
    """A geometry as a boolean grid: True where the printer would lay land."""
    if geom is None or geom.is_empty:
        return np.zeros(shape, dtype=bool)
    return rasterio.features.rasterize(
        [(geom, 1)], out_shape=shape, transform=transform, fill=0,
        dtype="uint8", all_touched=False).astype(bool)


def _levels(land, pixel_mm, band_mm, nbands, out=None, box=None):
    """The clamped width as band steps: ``floor(width / band_mm)``, capped.

    With ``box`` the distance transform runs on that crop alone.  The caller is
    responsible for the two radii -- compute on a crop padded by the ceiling,
    splice back only the inner half of that -- which is what makes the result
    identical to a full pass.  See the module docstring.
    """
    source = land if box is None else land[box]
    dist = ndimage.distance_transform_edt(source)
    steps = np.floor(2.0 * dist * pixel_mm / band_mm)
    np.clip(steps, 0, nbands, out=steps)
    # uint16, not uint8: a 0.01 mm band makes 300 steps of a 3 mm ceiling, and
    # a uint8 would wrap the thick land round to 44 and call it narrow.
    steps = steps.astype(np.uint16)
    if out is None:
        return steps
    out[box] = steps
    return out


def _lobes(land, level, nbands):
    """The thick land, as one labelled component per lobe, plus pixel counts."""
    thick = land & (level >= nbands)
    lobes, count = ndimage.label(thick, structure=STRUCT)
    return lobes, count, np.bincount(lobes.ravel(), minlength=count + 1)


def _tile_slices(shape, tile_px):
    """Every tile of the grid, as (key, (row slice, col slice))."""
    h, w = shape
    out = []
    for ty in range(0, -(-h // tile_px)):
        for tx in range(0, -(-w // tile_px)):
            out.append(((ty, tx),
                        (slice(ty * tile_px, min(h, (ty + 1) * tile_px)),
                         slice(tx * tile_px, min(w, (tx + 1) * tile_px)))))
    return out


# -- one tile ---------------------------------------------------------------

def _summarise_tile(land, level, lobes, box, shape, nbands, pixel_mm):
    """Grow the banded descent inside one tile and keep only its shape.

    The descent is the same one the whole province gets, run on a crop: at each
    band step, label what is present and see which of the previous step's
    components have joined.  What comes out is per *lineage* -- a component
    followed down the bands -- rather than per pixel, which is the reduction the
    reassembly lives on.

    Lobes are not part of the lineage structure.  They are thick, hence present
    at every step, and they span tiles, so they are external terminals named by
    one of their own pixels: a label would be moved by the next relabelling and
    a cached tile would then point at the wrong lobe.
    """
    px = pixel_mm * pixel_mm
    rows, cols = box
    lv = level[rows, cols]
    inside = land[rows, cols]
    narrow = inside & (lv < nbands) & (lv > 0)
    summary = TileSummary(profile=np.zeros((0, nbands + 1), np.float64),
                          reps=np.zeros(0, np.int64),
                          border=np.zeros(0, np.int64),
                          border_lineage=np.zeros(0, np.int64))
    if not narrow.any():
        return summary

    h, w = lv.shape
    flat0 = rows.start * shape[1] + cols.start
    # local (row, col) -> flat index in the whole grid
    local_flat = (np.arange(h)[:, None] * shape[1]
                  + np.arange(w)[None, :] + flat0)

    lineage_of = np.full((h, w), -1, np.int64)
    profile, reps, edges = [], [], []
    prev = np.zeros((h, w), np.int32)
    prev_lineage = np.zeros(1, np.int64)

    # Only steps a narrow pixel can appear at: nbands is the thick land.
    for step in range(nbands - 1, 0, -1):
        mask = narrow & (lv >= step)
        if not mask.any():
            prev, prev_lineage = np.zeros((h, w), np.int32), np.zeros(1, np.int64)
            continue
        lab, n = ndimage.label(mask, structure=STRUCT)
        new = mask & (prev == 0)

        # Which of the previous step's components each new component contains,
        # in one vectorised pass over the overlap.
        seen = mask & (prev > 0)
        to_lineage = np.full(n + 1, -1, np.int64)
        groups = {}
        if seen.any():
            pair = np.unique(prev[seen].astype(np.int64) * (n + 1) + lab[seen])
            old, cur = np.divmod(pair, n + 1)
            for o, c in zip(prev_lineage[old].tolist(), cur.tolist()):
                groups.setdefault(c, set()).add(o)

        # A new pixel touching two different old components is where a join
        # physically happened; that is the throat, and it is what the bar and
        # the zoom-to are drawn from.
        joiners = None
        if groups and any(len(v) > 1 for v in groups.values()):
            lo = np.full((h, w), np.iinfo(np.int32).max, np.int32)
            hi = np.zeros((h, w), np.int32)
            for sl_a, sl_b in (((slice(1, None), slice(None)), (slice(0, -1), slice(None))),
                               ((slice(0, -1), slice(None)), (slice(1, None), slice(None))),
                               ((slice(None), slice(1, None)), (slice(None), slice(0, -1))),
                               ((slice(None), slice(0, -1)), (slice(None), slice(1, None)))):
                nb = prev[sl_b]
                here_lo, here_hi = lo[sl_a], hi[sl_a]
                take = nb > 0
                np.minimum(here_lo, np.where(take, nb, here_lo), out=here_lo)
                np.maximum(here_hi, np.where(take, nb, here_hi), out=here_hi)
            joiners = new & (hi > 0) & (lo != hi)

        for c, members in groups.items():
            members = sorted(members)
            keep = members[0]
            if len(members) > 1:
                prov = -1
                if joiners is not None:
                    hit = np.flatnonzero((joiners & (lab == c)).ravel())
                    if hit.size:
                        prov = int(local_flat.ravel()[hit[0]])
                if prov < 0:
                    hit = np.flatnonzero((new & (lab == c)).ravel())
                    prov = int(local_flat.ravel()[hit[0]]) if hit.size else reps[keep]
                for other in members[1:]:
                    edges.append((keep, other, step, prov))
            to_lineage[c] = keep

        # Components with nothing beneath them start a lineage of their own.
        fresh = [c for c in range(1, n + 1) if to_lineage[c] < 0]
        for c in fresh:
            hit = np.flatnonzero((lab == c).ravel())
            to_lineage[c] = len(profile)
            profile.append(np.zeros(nbands + 1))
            reps.append(int(local_flat.ravel()[hit[0]]))

        # Mass arriving at this step, attributed to the lineage it joins. All
        # of it, not only its own component's share: from this step down these
        # pixels are inseparable from the lineage, so the attribution is exact.
        if new.any():
            counts = np.bincount(lab[new], minlength=n + 1)
            for c in np.flatnonzero(counts):
                if c:
                    profile[to_lineage[c]][step] += counts[c] * px
            lineage_of[new] = to_lineage[lab[new]]

        prev, prev_lineage = lab, to_lineage

    summary.lineages = len(profile)
    summary.profile = (np.vstack(profile) if profile
                       else np.zeros((0, nbands + 1)))
    summary.reps = np.asarray(reps, np.int64)
    summary.edges = edges

    # Attachments to the thick land, named by a lobe pixel rather than a label.
    lobe_crop = lobes[rows, cols]
    seen_pairs = {}
    for sl_a, sl_b in (((slice(1, None), slice(None)), (slice(0, -1), slice(None))),
                       ((slice(0, -1), slice(None)), (slice(1, None), slice(None))),
                       ((slice(None), slice(1, None)), (slice(None), slice(0, -1))),
                       ((slice(None), slice(0, -1)), (slice(None), slice(1, None)))):
        mine, theirs = lineage_of[sl_a], lobe_crop[sl_b]
        hit = (mine >= 0) & (theirs > 0)
        if not hit.any():
            continue
        lin = mine[hit]
        lob = theirs[hit]
        step = lv[sl_a][hit].astype(np.int64)
        prov = local_flat[sl_a][hit]
        pix = local_flat[sl_b][hit]
        for i in range(lin.size):
            key = (int(lin[i]), int(lob[i]))
            best = seen_pairs.get(key)
            if best is None or step[i] > best[1]:
                seen_pairs[key] = (int(pix[i]), int(step[i]), int(prov[i]))
    summary.lobe_edges = [(lin, px_, st, pr)
                          for (lin, _lob), (px_, st, pr) in seen_pairs.items()]

    # The tile's own edge: these pixels are how it reaches its neighbours.
    rim = np.zeros((h, w), bool)
    rim[0, :] = rim[-1, :] = True
    rim[:, 0] = rim[:, -1] = True
    on_rim = rim & (lineage_of >= 0)
    summary.border = local_flat[on_rim].astype(np.int64)
    summary.border_lineage = lineage_of[on_rim].astype(np.int64)
    return summary


# -- putting the tiles back together ---------------------------------------

def _crossing_edges(land, level, nbands, tile_px, shape):
    """Adjacent narrow pixels that fall in different tiles, with their step.

    These are the only places a path can leave a tile, which is why a tile
    summary only has to keep the pixels on its rim.
    """
    narrow = land & (level < nbands) & (level > 0)
    rows = np.arange(shape[0]) // tile_px
    cols = np.arange(shape[1]) // tile_px
    out_a, out_b, out_step = [], [], []
    for axis in (0, 1):
        if axis == 0:
            a = (slice(0, -1), slice(None))
            b = (slice(1, None), slice(None))
            same = (rows[:-1] == rows[1:])[:, None]
        else:
            a = (slice(None), slice(0, -1))
            b = (slice(None), slice(1, None))
            same = (cols[:-1] == cols[1:])[None, :]
        hit = narrow[a] & narrow[b] & ~same
        if not hit.any():
            continue
        flat = (np.arange(shape[0])[:, None] * shape[1]
                + np.arange(shape[1])[None, :])
        out_a.append(flat[a][hit])
        out_b.append(flat[b][hit])
        out_step.append(np.minimum(level[a][hit], level[b][hit]).astype(np.int64))
    if not out_a:
        return (np.zeros(0, np.int64),) * 3
    return (np.concatenate(out_a), np.concatenate(out_b),
            np.concatenate(out_step))


def _assemble(grid, lobes, nlobe, lobe_px, params: StressParams):
    """Every tile's summary plus the lobes, as one graph over band steps.

    Returns the node table (mass per band step, a representative pixel each) and
    the edge list, which is what the descent runs on.
    """
    nbands = params.nbands
    px = grid.pixel_mm * grid.pixel_mm
    # Lobes come first, so a lobe's node id is its label minus one.
    offsets, total = {}, nlobe
    for key, tile in grid.tiles.items():
        offsets[key] = total
        total += tile.lineages

    profile = np.zeros((total, nbands + 1), np.float32)
    reps = np.full(total, -1, np.int64)
    if nlobe:
        profile[:nlobe, nbands] = lobe_px[1:] * px
        # A representative pixel per lobe, for seeding a branch later. Any of
        # its pixels will do, so this is a plain scatter -- last write wins --
        # rather than a minimum, which over ten million cells costs 0.09 s to
        # answer a question nothing asked.
        first = np.full(nlobe + 1, -1, np.int64)
        flat = lobes.ravel()
        idx = np.flatnonzero(flat)
        first[flat[idx]] = idx
        reps[:nlobe] = first[1:]

    rows, cols, steps, provs = [], [], [], []
    for key, tile in grid.tiles.items():
        base = offsets[key]
        if tile.lineages:
            profile[base:base + tile.lineages] += tile.profile
            reps[base:base + tile.lineages] = tile.reps
        for a, b, step, prov in tile.edges:
            rows.append(base + a)
            cols.append(base + b)
            steps.append(step)
            provs.append(prov)
        for lin, lobe_px_flat, step, prov in tile.lobe_edges:
            lobe_id = int(lobes.ravel()[lobe_px_flat])
            if lobe_id <= 0:
                continue        # that pixel is no longer thick; nothing to join
            rows.append(base + lin)
            cols.append(lobe_id - 1)
            steps.append(step)
            provs.append(prov)

    # Tile to tile, through the rim pixels each summary kept.
    border = np.concatenate([t.border for t in grid.tiles.values()]
                            or [np.zeros(0, np.int64)])
    border_node = np.concatenate(
        [t.border_lineage + offsets[k] for k, t in grid.tiles.items()]
        or [np.zeros(0, np.int64)])
    if border.size:
        order = np.argsort(border)
        border, border_node = border[order], border_node[order]
        ca, cb, cstep = _crossing_edges(grid.land, grid.level, nbands,
                                        grid.tile_px, grid.shape)
        if ca.size:
            ia = np.searchsorted(border, ca)
            ib = np.searchsorted(border, cb)
            ok = ((ia < border.size) & (ib < border.size)
                  & (border[np.minimum(ia, border.size - 1)] == ca)
                  & (border[np.minimum(ib, border.size - 1)] == cb))
            rows.extend(border_node[ia[ok]].tolist())
            cols.extend(border_node[ib[ok]].tolist())
            steps.extend(cstep[ok].tolist())
            provs.extend(ca[ok].tolist())

    edges = (np.asarray(rows, np.int64), np.asarray(cols, np.int64),
             np.asarray(steps, np.int64), np.asarray(provs, np.int64))
    return profile, reps, edges, total


def _descend(profile, reps, edges, nnode, params: StressParams):
    """The banded descent: where two standing components first join.

    ``connected_components`` does the work in C, once per band step, over a
    prefix of the edges sorted by the step they activate at.  What comes back is
    one event per join, with the throat step, the two masses *before* it, and a
    pixel to put the mark on.

    Two masses come out of each join, and the difference matters.  ``a_mm2`` is
    what is *standing* at the throat -- everything wider than it -- which is a
    lower bound on the branch, because the branch's own coastal fringe is
    narrower than the throat and has not arrived yet.  ``a_upper`` is every pixel
    that ever drains into those nodes, which is an upper bound.  The join is
    dropped here only when the **upper** bound is too small, so the pre-filter
    cannot lose a neck; :func:`_score` then measures the truth by cutting, for
    the handful of candidates worth the labelling.

    Dropping early is not merely tidier.  Nunavut makes some three thousand
    joins and a dozen are necks, and locating the pixel a join happened at costs
    a pass over the active edges -- paying that three thousand times was a third
    of the run.  The pixel is looked up from one gather per band step instead.
    """
    rows, cols, steps, provs = edges
    nbands = params.nbands
    # Mass available at a step is everything that has arrived at or above it;
    # column 1 is therefore every node's eventual total.
    avail = np.cumsum(profile[:, ::-1], axis=1)[:, ::-1]
    if rows.size == 0:
        return []
    order = np.argsort(-steps, kind="stable")
    rows, cols, steps, provs = (rows[order], cols[order], steps[order],
                                provs[order])
    ones = np.ones(rows.size)
    floor = params.min_lobe_mm2
    events = []
    prev_lab = prev_area = prev_upper = prev_rep = None
    for step in range(nbands, 0, -1):
        k = int(np.searchsorted(-steps, -step, side="right"))
        graph = sparse.coo_matrix((ones[:k], (rows[:k], cols[:k])),
                                  shape=(nnode, nnode)).tocsr()
        ncc, lab = csgraph.connected_components(graph, directed=False)
        area = np.bincount(lab, weights=avail[:, step], minlength=ncc)
        upper = np.bincount(lab, weights=avail[:, 1], minlength=ncc)
        # One representative node per component, for seeding a branch.
        rep_of = np.full(ncc, -1, np.int64)
        rep_of[lab[::-1]] = np.arange(nnode)[::-1]
        if prev_lab is not None:
            pair = np.unique(prev_lab.astype(np.int64) * ncc + lab)
            old, cur = np.divmod(pair, ncc)
            keep = prev_area[old] > 0
            old, cur = old[keep], cur[keep]
            o = np.argsort(cur, kind="stable")
            old, cur = old[o], cur[o]
            start = np.flatnonzero(np.r_[True, cur[1:] != cur[:-1]])
            found = None
            for s, e in zip(start, np.r_[start[1:], cur.size]):
                if e - s < 2:
                    continue        # nothing joined here
                members = old[s:e]
                big = members[np.argsort(-prev_area[members])]
                for other in big[1:]:
                    if min(prev_upper[big[0]], prev_upper[other]) < floor:
                        continue    # nothing large enough is being held up
                    if found is None:
                        found = _meeting_places(rows[:k], cols[:k], provs[:k],
                                                prev_lab)
                    events.append(dict(
                        step=step,
                        a_mm2=float(prev_area[big[0]]),
                        b_mm2=float(prev_area[other]),
                        a_upper=float(prev_upper[big[0]]),
                        b_upper=float(prev_upper[other]),
                        a_node=int(prev_rep[big[0]]),
                        b_node=int(prev_rep[other]),
                        prov=found.get((min(big[0], other), max(big[0], other)),
                                       -1),
                    ))
        prev_lab, prev_area, prev_upper, prev_rep = lab, area, upper, rep_of
    return events


def _meeting_places(rows, cols, provs, prev_lab):
    """Where each pair of the step above met, as {(a, b): pixel}.

    One gather over the active edges for the whole band step, rather than one
    per join: on a province that is thousands of passes saved, and the answer is
    the same pixel either way.
    """
    la, lb = prev_lab[rows], prev_lab[cols]
    hit = np.flatnonzero(la != lb)
    out = {}
    for a, b, prov in zip(la[hit].tolist(), lb[hit].tolist(),
                          provs[hit].tolist()):
        out.setdefault((min(a, b), max(a, b)), prov)
    return out


# -- scoring ----------------------------------------------------------------

def _needed_mm(load_mm2, lever_mm, params: StressParams):
    """The width a neck carrying this load wants, and how slender the load is."""
    if load_mm2 <= 0:
        return 0.0, 0.0
    slender = lever_mm / math.sqrt(load_mm2)
    factor = max(1.0, slender / COMPACT_SLENDERNESS) ** params.lever_power
    needed = params.capacity_at_mm * math.sqrt(
        load_mm2 * factor / params.capacity_mm2)
    return needed, slender


def _xy(flat, grid):
    """A flat pixel index as a point in the working CRS (cell centre)."""
    row, col = divmod(int(flat), grid.shape[1])
    t = grid.transform
    return (t.c + (col + 0.5) * t.a, t.f + (row + 0.5) * t.e)


def _throat_bar(flat, throat_mm, grid, params: StressParams):
    """A bar of ``throat_mm`` across the neck, pointing at the nearest water.

    At a throat the two nearest shores lie on opposite sides, so the direction
    to the nearest water *is* the direction the land is narrowest in -- which is
    the line the piece would break along.  Measured on a crop, because one
    ``return_indices`` pass over a province costs more than every neck is worth.
    """
    row, col = divmod(int(flat), grid.shape[1])
    pad = int(math.ceil(throat_mm / grid.pixel_mm)) + 3
    r0, r1 = max(0, row - pad), min(grid.shape[0], row + pad + 1)
    c0, c1 = max(0, col - pad), min(grid.shape[1], col + pad + 1)
    crop = grid.land[r0:r1, c0:c1]
    x, y = _xy(flat, grid)
    half = throat_mm * params.mpm / 2.0
    dy = dx = 0.0
    if not crop.all():
        _, idx = ndimage.distance_transform_edt(crop, return_indices=True)
        wr = int(idx[0][row - r0, col - c0])
        wc = int(idx[1][row - r0, col - c0])
        dr, dc = wr - (row - r0), wc - (col - c0)
        norm = math.hypot(dr, dc)
        if norm > 0:
            # Rows run the opposite way to y, hence the sign on dy.
            dx, dy = dc / norm, -dr / norm
    if dx == 0.0 and dy == 0.0:
        dx, dy = 1.0, 0.0
    return LineString([(x - dx * half, y - dy * half),
                       (x + dx * half, y + dy * half)])


def _region_mask(flat, step, grid, params: StressParams):
    """The narrow blob around a throat, as a mask on a crop, and its box.

    Bounded on purpose, and in two ways.  The narrow land of a province is one
    connected fringe, so an unbounded "narrow patch around here" would paint the
    entire coastline; and the blob is also the **cut** the load is measured by
    (see :func:`_branch`), so it has to be the neck and not the neighbourhood.
    """
    row, col = divmod(int(flat), grid.shape[1])
    reach_mm = max(REGION_REACH * params.mm_of_step(step), 4.0 * grid.pixel_mm)
    pad = int(math.ceil(reach_mm / grid.pixel_mm))
    r0, r1 = max(0, row - pad), min(grid.shape[0], row + pad + 1)
    c0, c1 = max(0, col - pad), min(grid.shape[1], col + pad + 1)
    box = (slice(r0, r1), slice(c0, c1))
    # Exactly the throat's own level: across the waist every pixel is at or
    # below it, so the blob is a complete cross-section and the cut severs.
    mask = grid.land[box] & (grid.level[box] <= step) & (grid.level[box] > 0)
    if not mask[row - r0, col - c0]:
        mask[row - r0, col - c0] = True
    lab, _ = ndimage.label(mask, structure=STRUCT)
    return lab == lab[row - r0, col - c0], box


def _region_geom(mask, box, grid):
    """A crop mask as geometry in the working CRS, for shading."""
    t = grid.transform
    crop = Affine(t.a, t.b, t.c + box[1].start * t.a, t.d, t.e,
                  t.f + box[0].start * t.e)
    parts = [_shape(geom) for geom, value in rasterio.features.shapes(
        mask.astype(np.uint8), mask=mask, transform=crop) if value]
    return shapely.union_all(parts) if parts else None


def _labels_above(step, grid):
    """The land wider than ``step``, labelled: one component per standing piece.

    Done at ``step + 1``, so a component here is the same component the descent
    weighed and its area is the mass the event reported.  Used by
    :func:`branch_mask` to rebuild the land behind a neck whose cut did not
    sever; :func:`_branch` reaches the same answer through a window instead.
    """
    return ndimage.label(grid.land & (grid.level > step), structure=STRUCT)[0]


def _component(lab, which):
    """One labelled component's mask, area in pixels, and centre in cells."""
    mask = lab == which
    count = int(mask.sum())
    if not count:
        return None, 0, None
    # Row and column means by marginal sums rather than by np.nonzero: a branch
    # can be most of a province, and listing its pixels to average them costs
    # more than the labelling that found it.
    centre = (float((mask.sum(axis=1) * np.arange(mask.shape[0])).sum()) / count,
              float((mask.sum(axis=0) * np.arange(mask.shape[1])).sum()) / count)
    return mask, count, centre


def _grow_to_fit(grid, seed, other_seed, keep_crop):
    """Find the seed's component by a window that doubles until it holds it.

    ``keep_crop(box)`` hands back the boolean land of that crop with whatever has
    been removed already removed.  The window stops as soon as the component
    around the seed does not touch the crop's edge, at which point it is provably
    whole -- and since the side worth weighing is the *lighter* one, that is
    nearly always a small crop.  Labelling a whole province to find it costs
    0.1 s a time on Nunavut, which has fourteen necks and twelve distinct throats.

    Returns ``(mask, box, count, centre, parted)``: ``parted`` is True when the
    far seed is demonstrably *not* in the same component.
    """
    height, width = grid.shape
    srow, scol = divmod(int(seed), width)
    orow, ocol = (divmod(int(other_seed), width)
                  if other_seed is not None and other_seed >= 0 else (-1, -1))
    pad = FIRST_WINDOW_PX
    while True:
        r0, r1 = max(0, srow - pad), min(height, srow + pad + 1)
        c0, c1 = max(0, scol - pad), min(width, scol + pad + 1)
        box = (slice(r0, r1), slice(c0, c1))
        whole = (r0 == 0 and c0 == 0 and r1 == height and c1 == width)
        lab, _ = ndimage.label(keep_crop(box), structure=STRUCT)
        mine = int(lab[srow - r0, scol - c0])
        if mine <= 0:
            return None, box, 0, None, False
        mask, count, centre = _component(lab, mine)
        touches = bool(mask[0, :].any() or mask[-1, :].any()
                       or mask[:, 0].any() or mask[:, -1].any())
        if whole or not touches:
            inside = (r0 <= orow < r1 and c0 <= ocol < c1)
            theirs = int(lab[orow - r0, ocol - c0]) if inside else 0
            parted = bool(count) and mine != theirs
            return (mask, box, count,
                    (centre[0] + r0, centre[1] + c0) if centre else None, parted)
        pad *= 2


def _branch(seed, other_seed, region_mask, box, step, grid, standing_other=0.0,
            land_lab=None, land_areas=None):
    """The land a neck holds, and how much of it there is.

    **Cut the throat, do not threshold the province.**  The obvious measure --
    the component standing above the throat width -- counts only the mass
    *wider* than the throat, so it leaves out the branch's own coastal fringe:
    a 6 mm square on a 1.6 mm bar reads as 20 mm2 rather than its real 36 mm2,
    and a neck can fall under ``min_lobe_mm2`` on mass it demonstrably has.  So
    the measurement is the one the physics asks for: take the land, remove the
    neck's own narrow blob, and weigh what falls off.

    That can fail, and it says so when it does.  A real waist is not always a
    compact blob -- on Nunavut five necks of fourteen part under this cut and
    nine do not -- and where the two sides stay joined the answer falls back to
    the component standing above the throat.  That is an undercount rather than a
    guess, and ``severed`` records which measurement you are reading.

    Both paths search by :func:`_grow_to_fit`, so neither pays for the province
    to find a peninsula.

    Returns ``(mask, box, area_mm2, other_mm2, centre, severed)``, where ``box``
    says which crop the mask belongs to.
    """
    px = grid.pixel_mm ** 2
    cut_mm2 = float(region_mask.sum()) * px

    def cut_crop(crop_box):
        crop = grid.land[crop_box].copy()
        r0, c0 = crop_box[0].start, crop_box[1].start
        rr0, rr1 = max(r0, box[0].start), min(crop_box[0].stop, box[0].stop)
        cc0, cc1 = max(c0, box[1].start), min(crop_box[1].stop, box[1].stop)
        if rr0 < rr1 and cc0 < cc1:
            crop[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] &= ~region_mask[
                rr0 - box[0].start:rr1 - box[0].start,
                cc0 - box[1].start:cc1 - box[1].start]
        return crop

    mask, crop_box, count, centre, parted = _grow_to_fit(
        grid, seed, other_seed, cut_crop)
    if parted and count:
        # The far side by subtraction: severing partitions the landmass into this
        # branch, the cut, and the rest, so the rest needs no second search.
        other = 0.0
        if land_lab is not None and land_areas is not None:
            piece = int(land_areas[int(land_lab.ravel()[int(seed)])])
            other = max(0.0, piece * px - count * px - cut_mm2)
        return mask, crop_box, count * px, other, centre, True

    # Not severed: what stood above the throat, which is exactly the mass the
    # descent reported -- so the far side is the figure it already has.
    def above_crop(crop_box):
        return grid.land[crop_box] & (grid.level[crop_box] > step)

    mask, crop_box, count, centre, _ = _grow_to_fit(
        grid, seed, other_seed, above_crop)
    if not count:
        return None, None, 0.0, 0.0, None, False
    return mask, crop_box, count * px, standing_other, centre, False


def _score(events, grid, params: StressParams):
    """Events into necks: filter, weigh, and work up the worst into geometry."""
    keep = []
    for ev in events:
        if ev["prov"] < 0:
            continue
        if min(ev["a_upper"], ev["b_upper"]) < params.min_lobe_mm2:
            continue            # not two larger areas; nothing is being held
        lighter = ev["b_mm2"] <= ev["a_mm2"]
        ev["seed"] = ev["b_node"] if lighter else ev["a_node"]
        ev["other_seed"] = ev["a_node"] if lighter else ev["b_node"]
        keep.append(ev)

    # Rank on the *upper* bound over throat squared -- a monotone stand-in for
    # severity -- so the candidates worked up into geometry are optimistically
    # chosen.  Ranking on the standing mass instead would sort a wide throat
    # down on its own missing fringe, which is the thing being corrected for.
    keep.sort(key=lambda e: -min(e["a_upper"], e["b_upper"])
              / max(params.mm_of_step(e["step"]), 1e-9) ** 2)
    # One labelling of the land, shared: it is what lets the far side of a cut
    # be had by subtraction instead of by a second search.
    land_lab = land_areas = None
    if keep:
        land_lab, _ = ndimage.label(grid.land, structure=STRUCT)
        land_areas = np.bincount(land_lab.ravel())
    necks = []
    for ev in keep[:WORST_KEPT]:
        step = ev["step"]
        throat_mm = params.mm_of_step(step)
        seed_pixel = ev["seed"]
        if seed_pixel is None or seed_pixel < 0:
            continue
        region_mask, box = _region_mask(ev["prov"], step, grid, params)
        _, _, area_mm2, other_mm2, centre, severed = _branch(
            seed_pixel, ev["other_seed"], region_mask, box, step, grid,
            standing_other=max(ev["a_mm2"], ev["b_mm2"]),
            land_lab=land_lab, land_areas=land_areas)
        if centre is None:
            continue
        # A cut that severs can hand back the *heavier* side when the two
        # measures disagree about which is which; the load is the lighter one.
        if severed and 0.0 < other_mm2 < area_mm2:
            area_mm2, other_mm2 = other_mm2, area_mm2
            seed_pixel, ev["other_seed"] = ev["other_seed"], seed_pixel
        if area_mm2 < params.min_lobe_mm2 or other_mm2 < params.min_lobe_mm2:
            continue            # the cut disagrees: not two larger areas
        x, y = _xy(ev["prov"], grid)
        cx = grid.transform.c + (centre[1] + 0.5) * grid.transform.a
        cy = grid.transform.f + (centre[0] + 0.5) * grid.transform.e
        lever_mm = math.hypot(cx - x, cy - y) / params.mpm
        needed, _ = _needed_mm(area_mm2, lever_mm, params)
        severity = needed / throat_mm if throat_mm > 0 else 0.0
        if severity < params.report_above:
            continue
        necks.append(Neck(
            point=Point(x, y),
            throat=_throat_bar(ev["prov"], throat_mm, grid, params),
            region=_region_geom(region_mask, box, grid),
            throat_mm=throat_mm, load_mm2=area_mm2,
            other_mm2=other_mm2, lever_mm=lever_mm,
            needed_mm=needed, severity=severity, severed=severed,
            branch_step=step, branch_seed=int(seed_pixel)))
    necks.sort(key=lambda n: -n.severity)
    return necks


# -- the public calls -------------------------------------------------------

def _rep_pixel_for_node(node, reps):
    """A pixel to seed a branch from, given a node id."""
    if node is None or node < 0 or node >= reps.size:
        return -1
    return int(reps[node])


def _run(grid, params: StressParams, dirty_tiles=None):
    """Summarise what has to be summarised, reassemble, descend, score."""
    nbands = params.nbands
    lobes, nlobe, lobe_px = _lobes(grid.land, grid.level, nbands)
    slices = _tile_slices(grid.shape, grid.tile_px)
    recomputed = 0
    for key, box in slices:
        if dirty_tiles is not None and key not in dirty_tiles and key in grid.tiles:
            continue
        grid.tiles[key] = _summarise_tile(grid.land, grid.level, lobes, box,
                                          grid.shape, nbands, grid.pixel_mm)
        recomputed += 1
    profile, reps, edges, nnode = _assemble(grid, lobes, nlobe, lobe_px, params)
    events = _descend(profile, reps, edges, nnode, params)
    for ev in events:
        ev["a_node"] = _rep_pixel_for_node(ev["a_node"], reps)
        ev["b_node"] = _rep_pixel_for_node(ev["b_node"], reps)
    necks = _score(events, grid, params)
    return necks, len(slices), recomputed


def stress_points(geom, params: StressParams = None) -> StressReport:
    """
    Find the load-bearing necks in one province, as it will print.

    Parameters
    ----------
    geom   : Shapely Polygon/MultiPolygon in a projected metre CRS (the project
             uses EPSG:3347).  One province, as it will be printed: the piece's
             own outline is what has to survive being handled, so a province is
             measured on its own and not as part of a country.
    params : StressParams, optional

    Returns
    -------
    A :class:`StressReport`, worst neck first.  ``report.grid`` is the raster
    state; hand it to :func:`restress` after an edit rather than calling this
    again.  Nothing is modified; this only measures.
    """
    params = params or StressParams()
    report = StressReport(pixel_mm=params.pixel_mm)
    if geom is None or geom.is_empty:
        return report

    # Asked up front, not after a throw. A self-intersecting ring does not make
    # the rasteriser complain -- it quietly burns something that is not the
    # province, and the report would come back measured and wrong. Waiting for
    # an exception works where the geometry operation is a boolean, as in
    # LIB_thin_features; here nothing would ever raise.
    working, repaired = geom, False
    if not working.is_valid:
        log.info("stress_points repairing invalid geometry")
        try:
            working = shapely.make_valid(geom)
            repaired = True
        except Exception as exc:
            log.warning("stress_points could not repair: %s", exc)
            report.measured = False
            return report
        if working is None or working.is_empty:
            report.measured = False
            return report

    for attempt in range(2):
        try:
            transform, shape, pixel_mm, coarsened = _grid_for(working, params)
            land = _burn(working, transform, shape)
            break
        except Exception as exc:
            if attempt:
                log.warning("stress_points could not rasterise: %s", exc)
                report.measured = False
                return report
            log.info("stress_points repairing invalid geometry: %s", exc)
            try:
                working = shapely.make_valid(geom)
            except Exception as repair_exc:
                log.warning("stress_points could not repair: %s", repair_exc)
                report.measured = False
                return report
            repaired = True

    grid = StressGrid(transform=transform, shape=shape, land=land,
                      level=_levels(land, pixel_mm, params.band_mm,
                                    params.nbands),
                      pixel_mm=pixel_mm, ceiling_mm=params.ceiling_mm,
                      band_mm=params.band_mm, tile_px=int(params.tile_px))
    necks, total, done = _run(grid, params)
    report.necks = necks
    report.grid = grid
    report.repaired = repaired
    report.rebuilt = True
    report.tiles_total = total
    report.tiles_recomputed = done
    report.pixel_mm = pixel_mm
    report.coarsened = coarsened
    report.land_area_mm2 = float(land.sum()) * pixel_mm ** 2
    return report


def restress(grid: StressGrid, land=None, geom=None, dirty=None,
             params: StressParams = None) -> StressReport:
    """
    Ask again after an edit, paying only for what the edit could reach.

    Parameters
    ----------
    grid  : the :class:`StressGrid` from a previous call.
    land  : the new land as a boolean grid of the same shape, or
    geom  : the new geometry, which is burned onto the cached transform.
    dirty : which cells may have changed, as a boolean grid.  **Must be a
            conservative superset**; too small is silently wrong, which is the
            one failure this module must not have.  ``None`` derives it by XOR
            against the cached land, which is correct by construction.
    params: StressParams, optional.

    Returns
    -------
    A :class:`StressReport`.  ``report.rebuilt`` is True when the grid could not
    be reused and everything was done from scratch -- a different pixel, ceiling,
    band or tile size, or an edit that reaches outside the grid.  A caller is
    never left to guess whether it got the fast path.
    """
    params = params or StressParams()
    if grid is None or not grid.matches(params):
        return stress_points(geom, params)
    if land is None:
        if geom is None:
            raise ValueError("restress needs either land or geom")
        if not grid.covers(geom):
            return stress_points(geom, params)
        land = _burn(geom, grid.transform, grid.shape)
    land = np.asarray(land, dtype=bool)
    if land.shape != tuple(grid.shape):
        return stress_points(geom, params)

    if dirty is None:
        dirty = grid.dirty_from(land)
    dirty = np.asarray(dirty, dtype=bool)
    if dirty.shape != tuple(grid.shape):
        raise ValueError("dirty must have the grid's shape")

    nbands = params.nbands
    report = StressReport(pixel_mm=grid.pixel_mm, rebuilt=False)

    # Two radii: compute the field on a crop padded by the ceiling, splice back
    # only the inner half of that. One radius is not enough -- a crop cannot see
    # the water beyond its own edge.
    splice_px = int(math.ceil((grid.ceiling_mm / 2.0) / grid.pixel_mm)) + 1
    compute_px = 2 * splice_px
    grid.land = land
    boxes = ndimage.find_objects(ndimage.label(dirty, structure=STRUCT)[0])
    touched = []
    for box in boxes:
        s_box = tuple(slice(max(0, s.start - splice_px),
                            min(d, s.stop + splice_px))
                      for s, d in zip(box, grid.shape))
        c_box = tuple(slice(max(0, s.start - compute_px),
                            min(d, s.stop + compute_px))
                      for s, d in zip(box, grid.shape))
        crop = _levels(land, grid.pixel_mm, grid.band_mm, nbands, box=c_box)
        inner = tuple(slice(s.start - c.start, s.stop - c.start)
                      for s, c in zip(s_box, c_box))
        grid.level[s_box] = crop[inner]
        touched.append(s_box)

    # Any tile whose own cells or whose one-pixel halo moved has to be redone.
    tile_px = grid.tile_px
    dirty_tiles = set()
    for box in touched:
        for ty in range((box[0].start - 1) // tile_px,
                        (box[0].stop + 1) // tile_px + 1):
            for tx in range((box[1].start - 1) // tile_px,
                            (box[1].stop + 1) // tile_px + 1):
                if ty >= 0 and tx >= 0:
                    dirty_tiles.add((ty, tx))

    necks, total, done = _run(grid, params, dirty_tiles=dirty_tiles)
    report.necks = necks
    report.grid = grid
    report.tiles_total = total
    report.tiles_recomputed = done
    report.land_area_mm2 = float(land.sum()) * grid.pixel_mm ** 2

    if params.verify:
        _verify(grid, land, necks, params)
    return report


def _verify(grid: StressGrid, land, necks, params: StressParams) -> None:
    """Redo the whole thing and insist the incremental answer is the same.

    Checked in two places, because two different things can go wrong: a spliced
    width field that is not what a full pass would have produced, and a cached
    tile that should have been thrown away.  An under-reported ``dirty`` shows up
    here and nowhere else -- which is the point, since the alternative is a
    confident wrong answer.
    """
    fresh = StressGrid(transform=grid.transform, shape=grid.shape,
                       land=np.asarray(land, dtype=bool).copy(),
                       level=_levels(land, grid.pixel_mm, grid.band_mm,
                                     params.nbands),
                       pixel_mm=grid.pixel_mm, ceiling_mm=grid.ceiling_mm,
                       band_mm=grid.band_mm, tile_px=grid.tile_px)
    want, _, _ = _run(fresh, params)
    if not np.array_equal(fresh.level, grid.level):
        bad = int(np.count_nonzero(fresh.level != grid.level))
        raise AssertionError(
            f"restress: the spliced width field differs in {bad} cells")
    got = [(round(n.severity, 9), round(n.throat_mm, 9), round(n.load_mm2, 6))
           for n in necks]
    expected = [(round(n.severity, 9), round(n.throat_mm, 9),
                 round(n.load_mm2, 6)) for n in want]
    if got != expected:
        raise AssertionError(
            f"restress: {len(got)} necks, a full run says {len(expected)}"
            f" ({got[:3]} against {expected[:3]})")


def severity_field(report: StressReport, grid: StressGrid = None):
    """
    The gradient the overlay draws: severity per cell, zero where nothing holds.

    Each neck paints its own region, worst winning where two overlap, so the
    ramp reads as *this* neck's severity rather than as an average of the
    neighbourhood.  Returns ``(array, transform)``; the array is zero away from
    the necks, which is what a caller masks on.
    """
    grid = grid or (report.grid if report else None)
    if grid is None:
        return None, None
    out = np.zeros(grid.shape, np.float32)
    for neck in report.necks:
        if neck.region is None or neck.region.is_empty:
            continue
        mask = rasterio.features.rasterize(
            [(neck.region, 1)], out_shape=grid.shape, transform=grid.transform,
            fill=0, dtype="uint8").astype(bool)
        np.maximum(out, np.where(mask, neck.severity, 0.0).astype(np.float32),
                   out=out)
    return out, grid.transform


def width_field(report: StressReport, grid: StressGrid = None):
    """The clamped width in print-mm, for the faint layer under the necks."""
    grid = grid or (report.grid if report else None)
    if grid is None:
        return None, None
    out = grid.level.astype(np.float32) * grid.band_mm
    out[~grid.land] = 0.0
    return out, grid.transform


def branch_mask(report: StressReport, neck: Neck, grid: StressGrid = None):
    """The land one neck holds, as a boolean grid — "show me what falls off"."""
    grid = grid or (report.grid if report else None)
    if grid is None or neck is None or neck.branch_seed < 0:
        return None
    if not neck.severed or neck.region is None or neck.region.is_empty:
        lab = _labels_above(neck.branch_step, grid)
        which = int(lab.ravel()[neck.branch_seed])
        return (lab == which) if which > 0 else None
    cut = rasterio.features.rasterize(
        [(neck.region, 1)], out_shape=grid.shape, transform=grid.transform,
        fill=0, dtype="uint8").astype(bool)
    lab, _ = ndimage.label(grid.land & ~cut, structure=STRUCT)
    which = int(lab.ravel()[neck.branch_seed])
    return (lab == which) if which > 0 else None


#: Half-width of the first window :func:`_branch` looks for a branch in, in
#: cells. Big enough that a small island's neck is answered in one pass, small
#: enough that it is a fraction of a province.
FIRST_WINDOW_PX = 96
