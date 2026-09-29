#!/usr/bin/env python3
"""
LIB_shape_scores.py

Score an edited province against the one that was imported.

An island-editing session is a long run of small decisions -- drop this islet,
bridge that strait, rub out a causeway -- and it is easy to finish one without
knowing what was actually given up.  This module answers that in three numbers::

    from lib.LIB_shape_scores import ScoreParams, score_shapes

    scores = score_shapes(original, edited, ScoreParams())
    print(f"{scores.perimeter_change:+.1%} outline, "
          f"{scores.land_change:+.1%} land, "
          f"{scores.agreement:.2%} of the print agrees")

All lengths and areas are **print-mm** and convert through ``print.scale`` at
the moment of use, as in :class:`lib.LIB_shared_border.SeamParams`.  Geometry is
expected in a projected metre CRS (the project uses EPSG:3347).

The three measures answer different questions
---------------------------------------------
**Outline** (``perimeter_mm``) is how much coastline detail the edit cost.  It
is the figure that moves most and moves first: dropping the islets out of
British Columbia costs 13% of its outline and 0.3% of its land, because a small
island is nearly all coast.

**Land** (``land_mm2``) is how much of the province is still there.  On its own
it is reassuring to the point of being misleading -- an edit can erase half the
coastline of a fjord coast for 1.6% of the area, because an inlet is long and
thin.

**Agreement** is the one that says whether any of it will show.  Both versions
are rasterised onto one grid and compared cell by cell, as
intersection-over-union: the share of the printed land the two agree on.  An
edit with 99% agreement is one the printer cannot really tell you about; an
edit at 92% has visibly moved the coast.

The pixel: a quarter of the nozzle
----------------------------------
The grid is sampled at ``nozzle_mm / pixels_per_nozzle`` -- 0.1 print-mm at the
shipped 0.4 mm nozzle, which is 800 m of ground at 1:8,000,000.  A quarter of
the nozzle rather than a whole one: sampled at the nozzle itself, a feature the
printer can just about lay down falls between cells and the comparison cannot
see the very detail it is supposed to be judging.

Agreement is measured over the **union of the land**, not over the whole grid.
Counting the water the two versions agree about would put every score above 99%
and make the number useless -- most of a province's bounding box is sea.

Cost
----
Two rasterisations and three array passes.  Measured at the shipped 0.1 mm
pixel: Prince Edward Island 0.00 s over 0.02 Mpx, Nova Scotia 0.01 s over
0.4 Mpx, British Columbia 0.04 s over 3.0 Mpx, Nunavut 0.17 s over 13.6 Mpx.

Cheap, but not free, and not bounded: the pixel is a printed length, so a finer
print scale shrinks it in metres and the grid grows with the square.  At
1:1,000 -- the lowest the editor's scale box goes -- Nunavut would want 8.7e14
cells.  So the grid is capped at ``MAX_PIXELS`` and the pixel coarsened to fit
rather than the machine being asked for the impossible; ``coarsened`` says when
that happened and ``pixel_mm`` always reports what was actually used.

The outline is the whole boundary
---------------------------------
``perimeter_mm`` is every ring the geometry carries: the outer coastline, any
land border it shares with a neighbour, and the shore of every lake.  For a
province with a straight land border that is not the same thing as coastline --
23% of British Columbia's boundary is the Alberta and Yukon line, which no
island edit can touch -- so a caller comparing provinces should say "outline"
and mean it.  Within one editing session it is the right measure anyway,
because the part that cannot change cancels out of the comparison.
"""

import logging
from dataclasses import dataclass

import numpy as np
import rasterio.features
from affine import Affine

from lib.LIB_project_config import get as _cfg

log = logging.getLogger(__name__)

EPSG_METRIC = _cfg("crs.working")
DEFAULT_SCALE = _cfg("print.scale")

#: Most cells the comparison will rasterise. 64 million is about 128 MB across
#: the two boolean grids and a few hundred milliseconds -- past that the pixel
#: is coarsened instead, because a scale box that goes to 1:1,000 can ask for a
#: grid with more cells than there are atoms in the bed.
MAX_PIXELS = 64_000_000


@dataclass
class ScoreParams:
    """How the comparison is measured.  All lengths are **print-mm**."""
    scale: float = DEFAULT_SCALE                #: map scale denominator
    #: The printer's resolution, and how finely to sample against it. A quarter
    #: of the nozzle resolves what the nozzle can just lay down; sampling at
    #: the nozzle itself would miss exactly that.
    nozzle_mm: float = _cfg("seam.nozzle_mm")
    pixels_per_nozzle: int = 4
    work_crs: str = EPSG_METRIC                 #: metric CRS the work is done in

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def pixel_mm(self) -> float:
        """The grid's cell, in printed millimetres."""
        if self.pixels_per_nozzle <= 0:
            return self.nozzle_mm
        return self.nozzle_mm / self.pixels_per_nozzle

    @property
    def pixel_m(self) -> float:
        return self.pixel_mm * self.mpm


@dataclass
class ShapeScores:
    """What an edit cost, in three numbers and the arithmetic behind them."""
    perimeter_before_mm: float = 0.0
    perimeter_after_mm: float = 0.0
    land_before_mm2: float = 0.0
    land_after_mm2: float = 0.0
    #: Intersection over union of the printed land, 0..1. 1.0 is identical.
    agreement: float = 1.0
    added_mm2: float = 0.0          #: printed land the edit put there
    lost_mm2: float = 0.0           #: printed land the edit took away
    pixel_mm: float = 0.0           #: the cell actually used
    pixels: int = 0                 #: cells in the grid
    coarsened: bool = False         #: the cell was grown to fit MAX_PIXELS
    #: False when the rasterisation could not be done at all. Not the same as
    #: "nothing changed", and must never be shown as though it were.
    measured: bool = True

    @property
    def perimeter_change(self) -> float:
        """Signed share of the outline gained or lost (-1..)."""
        if self.perimeter_before_mm <= 0:
            return 0.0
        return (self.perimeter_after_mm / self.perimeter_before_mm) - 1.0

    @property
    def land_change(self) -> float:
        if self.land_before_mm2 <= 0:
            return 0.0
        return (self.land_after_mm2 / self.land_before_mm2) - 1.0

    @property
    def identical(self) -> bool:
        return self.measured and self.added_mm2 == 0.0 and self.lost_mm2 == 0.0


def outline_mm(geom, params: ScoreParams = None) -> float:
    """Every ring of a geometry, as printed millimetres."""
    params = params or ScoreParams()
    if geom is None or geom.is_empty:
        return 0.0
    return geom.length / params.mpm


def land_mm2(geom, params: ScoreParams = None) -> float:
    """A geometry's area, as printed square millimetres."""
    params = params or ScoreParams()
    if geom is None or geom.is_empty:
        return 0.0
    return geom.area / params.mpm ** 2


def _grid(before, after, params: ScoreParams):
    """One transform covering both versions, coarsened if it would be huge.

    Both are burned onto the *same* grid, which is the only way the cell counts
    can be subtracted from each other: two grids fitted to two bounding boxes
    would not line up, and every score would carry the offset as error.
    """
    bounds = [g.bounds for g in (before, after) if g is not None
              and not g.is_empty]
    if not bounds:
        return None, 0, 0, params.pixel_m, False
    minx = min(b[0] for b in bounds)
    miny = min(b[1] for b in bounds)
    maxx = max(b[2] for b in bounds)
    maxy = max(b[3] for b in bounds)

    pixel_m = params.pixel_m
    coarsened = False
    if pixel_m <= 0:
        return None, 0, 0, pixel_m, False
    width_m, height_m = max(maxx - minx, pixel_m), max(maxy - miny, pixel_m)
    wanted = (width_m / pixel_m) * (height_m / pixel_m)
    if wanted > MAX_PIXELS:
        # Grow the cell rather than ask for the grid: the score is still
        # meaningful at a coarser pixel, and pixel_mm reports what was used.
        pixel_m *= (wanted / MAX_PIXELS) ** 0.5
        coarsened = True

    width = int(np.ceil(width_m / pixel_m)) + 1
    height = int(np.ceil(height_m / pixel_m)) + 1
    transform = Affine(pixel_m, 0.0, minx, 0.0, -pixel_m, maxy)
    return transform, width, height, pixel_m, coarsened


def _burn(geom, transform, width: int, height: int):
    """A geometry as a boolean grid: True where the printer would lay land."""
    if geom is None or geom.is_empty:
        return np.zeros((height, width), dtype=bool)
    return rasterio.features.rasterize(
        [(geom, 1)], out_shape=(height, width), transform=transform,
        fill=0, dtype="uint8", all_touched=False).astype(bool)


def score_shapes(before, after, params: ScoreParams = None) -> ShapeScores:
    """
    Compare an edited province against the one that was imported.

    Parameters
    ----------
    before, after : Shapely Polygon/MultiPolygon in a projected metre CRS. The
                    document as it arrived, and as it stands now.
    params        : ScoreParams, optional

    Returns
    -------
    A :class:`ShapeScores`.  Nothing is modified; this only measures.
    """
    params = params or ScoreParams()
    scores = ShapeScores(
        perimeter_before_mm=outline_mm(before, params),
        perimeter_after_mm=outline_mm(after, params),
        land_before_mm2=land_mm2(before, params),
        land_after_mm2=land_mm2(after, params),
        pixel_mm=params.pixel_mm)

    transform, width, height, pixel_m, coarsened = _grid(before, after, params)
    if transform is None:
        return scores                   # nothing on either side to compare
    scores.coarsened = coarsened
    scores.pixel_mm = pixel_m / params.mpm
    scores.pixels = width * height

    try:
        grid_before = _burn(before, transform, width, height)
        grid_after = _burn(after, transform, width, height)
    except Exception as exc:
        # Better to say the comparison did not happen than to report an
        # agreement nobody measured.
        log.warning("score_shapes could not rasterise: %s", exc)
        scores.measured = False
        return scores

    cell_mm2 = scores.pixel_mm ** 2
    union = int(np.count_nonzero(grid_before | grid_after))
    intersection = int(np.count_nonzero(grid_before & grid_after))
    scores.added_mm2 = int(np.count_nonzero(grid_after & ~grid_before)) * cell_mm2
    scores.lost_mm2 = int(np.count_nonzero(grid_before & ~grid_after)) * cell_mm2
    # Over the union of the land, not over the grid: most of a province's
    # bounding box is sea, and counting the water both versions agree about
    # would put every score above 99%.
    scores.agreement = (intersection / union) if union else 1.0
    return scores
