#!/usr/bin/env python3
"""
LIB_seam_fit.py

Where two printed provinces will not mesh along their shared border -- and a
brush for moving that border by hand.

Two pieces that meet along a seam fail to go together in two distinct ways,
and this module looks for both:

1. **Narrow land at the seam.**  Every printed edge comes out wrong by a fixed
   amount per side -- over-extrusion, the elephant's foot of the first layer,
   any clearance the slicer is told to leave.  That loss is the same whatever
   the size of the feature, so it is the *small* ones it ruins: a 1 mm slot
   that loses 0.2 mm off each wall has lost 40% of its width, where a 10 mm
   slot hardly notices.  So the test is a width, not a size::

       W = min_width_mm + 2 * edge_loss_mm

   and any land narrower than ``W`` that reaches to within ``W`` of the seam --
   on either side, since a thin slot in B is a thin tab of A -- is reported,
   out to ``band_mm`` from it.  Measured by
   morphological opening with mitre joins, as in ``LIB_thin_features``.

2. **Tabs that lock.**  A seam that wanders makes jigsaw tabs.  A tab whose
   head is wider than its neck is captured laterally: the two pieces can only
   be dropped together from above, a flared first layer catches on the neck on
   the way down, and because the tab pins the pieces against each other, any
   shrinkage along the seam cannot spread out -- it piles up at the tab.

   Tabs are found by **closing**: dilate B by ``R`` and erode it back, and
   every pocket in B narrower than ``2R`` is filled.  Whatever land of A sits
   in those pockets, touching the seam, is a tab of A -- and the edge of the
   filled pocket, where it crosses A, is the tab's **lid**: the line across its
   mouth.  The same the other way round finds B's tabs.  ``R`` is half of
   ``tab_mm``, the widest head looked for.

   This replaced a baseline drawn by simplifying the seam hard, which could not
   work: Douglas-Peucker keeps any point further off than its tolerance, so the
   baseline ran straight into every tab deeper than that and cut it into
   harmless-looking slivers -- a locking mushroom 8 mm deep came back as four
   pieces, none of which locked.  A closing has no line to get wrong.

   For each tab:

   * ``mouth_mm`` -- the length of its lid;
   * ``neck_mm``  -- the widest disc that can travel from the head out through
     the lid (never more than the lid);
   * ``head_mm``  -- the widest disc that fits inside it anywhere;
   * ``depth_mm`` -- how far it reaches past the lid.

   ``head / neck`` above ``lock_ratio`` is a locking tab.  A finger -- a tab
   reaching ``FINGER_ASPECT`` times further than its neck is wide, and deeper
   than the edge loss -- whose neck is under ``W`` is too narrow, whether or
   not it locks.  A shallow wobble is not a finger, however thin: there is a
   whole province behind it, and nothing to snap off.

All lengths are **print-mm**, converted through ``print.scale`` at the moment of
use, as in :class:`lib.LIB_shared_border.SeamParams`.  Geometry is expected in
the working metre CRS.

    from lib.LIB_seam_fit import FitParams, fit_report
    report = fit_report(alberta, british_columbia)
    print(len(report.locking), "locking tabs,", len(report.narrow), "narrow patches")

Moving the border
-----------------
:func:`push_border` is the by-hand fix: a brush stroke that hands land from one
province to the other.  It moves only land the stroke covers that *touches*
the province growing, so it moves the border rather than creating enclaves, and
it hands land over rather than adding or removing any -- the pair partitions
exactly the ground it did before, so no gap can open.

:func:`solve_fit` is the same fix made automatically.  Every problem gets two
candidate repairs, both made of that same hand-over:

* **cut** -- give the offending land to the other province: a locking tab, a
  thin finger or a narrow patch simply changes hands, and the pocket it sat in
  is filled;
* **fill** -- give the holder the other province's land around it: the
  undercuts beside a locking tab's neck (everything of the other side inside
  the tab's convex hull), or a collar that widens a thin finger or narrow
  patch to the required width.

Each candidate is judged by the fit check itself, run on the land around it:
the one that leaves the fewest problems there wins, the smaller move breaking
a tie, and one that helps nothing is not made.  Neither may move the border
further than ``max_move_mm`` from where it ran -- a cut of a deep tab is a big
change to a real border, and whether that is acceptable is the caller's call,
not the solver's.  Fixing one problem can expose the next, so it goes round
again, up to ``max_rounds``; and if the pair as a whole has not come out with
fewer problems than it went in with, it is handed back untouched.
"""

import logging
from dataclasses import dataclass, field

from shapely import segmentize
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.affinity import translate
from shapely.ops import unary_union

from lib.LIB_island_bridging import brush_stroke
from lib.LIB_project_config import get as _cfg
from lib.LIB_shared_border import _shared_seam

log = logging.getLogger(__name__)

DEFAULT_SCALE = _cfg("print.scale")

#: A tab counts as a finger -- something that can snap off -- once it reaches
#: this many times further than its neck is wide.
FINGER_ASPECT = 1.5

#: Bisection steps when measuring a neck or a head. Fourteen halvings of a
#: 10 mm tab resolve it to well under a micrometre of print. It was twenty,
#: which bought nothing and made the solver -- which measures tabs hundreds
#: of times -- half as fast again.
_NECK_STEPS = 14

#: How far past a problem a repair reaches, print-mm. A tab's lid comes out of
#: the closing a few micrometres short of the seam, so a cut of exactly the
#: tab left a 5 um sliver across its mouth -- itself a thin finger -- and a
#: fill left a 5 um wall that kept the tab locked. Far below anything that
#: prints, far above the arithmetic.
_SNAP_MM = 0.02

#: The narrowest tab worth calling one, print-mm. A sliver thinner than this
#: everywhere is not a puzzle piece: it does not print on its own side, and
#: the slot it would leave in the other closes up, so the two pieces meet as
#: if the seam ran straight. Without the floor the closing's own arithmetic --
#: a few micrometres along a straight edge -- turned up 13 mm "fingers" a
#: fiftieth of a millimetre wide.
MIN_TAB_HEAD_MM = 0.1


@dataclass
class FitParams:
    """How the seam is checked.  All lengths are **print-mm**."""
    scale: float = DEFAULT_SCALE
    #: Narrowest land that survives as a solid -- the same figure Show thin uses.
    min_width_mm: float = _cfg("thin.width_mm")
    #: What each printed edge loses or gains: over-extrusion, elephant's foot,
    #: slicer clearance. Per side, so a feature loses twice this across.
    edge_loss_mm: float = _cfg("seam.edge_loss_mm")
    #: How far from the seam narrow land counts as the seam's problem.
    band_mm: float = _cfg("seam.fit_band_mm")
    #: The widest tab head looked for. Pockets narrower than this are closed
    #: over, and what fills them is a tab; a bulge wider than it is just the
    #: border going somewhere.
    tab_mm: float = _cfg("seam.fit_tab_mm")
    #: Head wider than neck by more than this factor captures the tab.
    lock_ratio: float = _cfg("seam.lock_ratio")
    #: Tabs smaller than this are noise, not puzzle pieces.
    min_tab_area_mm2: float = 0.01

    @classmethod
    def from_config(cls, **overrides):
        """
        The settings as the config says *now*.

        The field defaults above are read once, when this module is imported,
        so a value a window saves afterwards never reaches ``FitParams()``.
        Anything that has to agree with a setting turned in the same session
        builds its params here.
        """
        values = dict(scale=_cfg("print.scale"),
                      min_width_mm=_cfg("thin.width_mm"),
                      edge_loss_mm=_cfg("seam.edge_loss_mm"),
                      band_mm=_cfg("seam.fit_band_mm"),
                      tab_mm=_cfg("seam.fit_tab_mm"),
                      lock_ratio=_cfg("seam.lock_ratio"))
        values.update(overrides)
        return cls(**values)

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def required_width_mm(self) -> float:
        """The width a feature needs at the seam to still print and mesh."""
        return self.min_width_mm + 2.0 * self.edge_loss_mm


@dataclass
class NarrowPatch:
    """Land at the seam narrower than the required width."""
    geometry: object
    side: str                       #: "a" or "b" -- whose land it is
    area_mm2: float


@dataclass
class Tab:
    """Land of one province in a pocket of the other: a puzzle tab."""
    geometry: object
    side: str                       #: "a" or "b" -- whose land it is
    area_mm2: float
    mouth_mm: float
    neck_mm: float
    head_mm: float
    depth_mm: float
    locks: bool = False
    too_narrow: bool = False
    #: The line across its mouth, and the centre of its widest disc: between
    #: them, the way the tab pulls out -- which is what a repair needs.
    lid: object = field(default=None, repr=False, compare=False)
    head_point: object = field(default=None, repr=False, compare=False)

    @property
    def lock_ratio(self) -> float:
        return self.head_mm / self.neck_mm if self.neck_mm > 0 else float("inf")

    @property
    def is_problem(self) -> bool:
        return self.locks or self.too_narrow


@dataclass
class FitReport:
    """Result of :func:`fit_report`."""
    params: FitParams
    narrow: list = field(default_factory=list)      #: NarrowPatch
    tabs: list = field(default_factory=list)        #: every Tab, problem or not

    @property
    def locking(self) -> list:
        return [t for t in self.tabs if t.locks]

    @property
    def problem_tabs(self) -> list:
        return [t for t in self.tabs if t.is_problem]

    @property
    def narrow_area_mm2(self) -> float:
        return sum(p.area_mm2 for p in self.narrow)

    @property
    def problems(self) -> int:
        return len(self.problem_tabs) + len(self.narrow)

    def worst(self):
        """The geometry most worth looking at first: the tightest lock, else
        the biggest narrow patch, else None."""
        if self.locking:
            return max(self.locking, key=lambda t: t.lock_ratio).geometry
        if self.problem_tabs:
            return min(self.problem_tabs, key=lambda t: t.neck_mm).geometry
        if self.narrow:
            return max(self.narrow, key=lambda p: p.area_mm2).geometry
        return None

    def summary(self) -> str:
        bits = []
        if self.locking:
            bits.append(f"{len(self.locking)} locking tab(s)")
        thin_tabs = [t for t in self.tabs if t.too_narrow and not t.locks]
        if thin_tabs:
            bits.append(f"{len(thin_tabs)} tab(s) under "
                        f"{self.params.required_width_mm:g} mm")
        if self.narrow:
            bits.append(f"{len(self.narrow)} narrow patch(es)")
        return ", ".join(bits) if bits else "meshes cleanly"


def fit_report(geom_a, geom_b, seam=None, params=None):
    """
    Everything along the seam of ``geom_a`` and ``geom_b`` that will not mesh.

    ``seam`` may be passed if already found; otherwise it is found with
    ``LIB_shared_border``'s seam finder.  Two provinces that share no seam get
    an empty report.
    """
    params = params or FitParams()
    report = FitReport(params)
    if geom_a is None or geom_b is None or geom_a.is_empty or geom_b.is_empty:
        return report
    seam = seam if seam is not None else _shared_seam(geom_a, geom_b)
    if seam is None or seam.is_empty:
        return report
    report.narrow = _narrow_patches(geom_a, geom_b, seam, params)
    report.tabs = _tabs(geom_a, geom_b, seam, params)
    return report


# ---------------------------------------------------------------------------
# Narrow land
# ---------------------------------------------------------------------------

def _narrow_patches(geom_a, geom_b, seam, params):
    """
    Land within ``band_mm`` of the seam narrower than the required width.

    Each province is clipped to a window around the seam first -- opening all
    of Nunavut to look at fifty kilometres of border would be most of the cost.
    The window is wider than the band by twice the width, because the clip
    edge is itself a cut, and land beside a cut reads as thin within one width
    of it; the margin keeps that artefact outside the band that is reported.
    """
    mpm = params.mpm
    width = params.required_width_mm * mpm
    if width <= 0:
        return []
    band = seam.buffer(params.band_mm * mpm)
    window = seam.buffer(params.band_mm * mpm + 2.0 * width)
    radius = width / 2.0
    out = []
    for side, geom in (("a", geom_a), ("b", geom_b)):
        try:
            local = geom.intersection(window)
            if local.is_empty:
                continue
            opened = local.buffer(-radius, join_style="mitre").buffer(
                radius, join_style="mitre")
            thin = local.difference(opened).intersection(band)
        except Exception as exc:          # a bad ring costs the check, not the run
            log.warning("narrow-land check failed on side %s: %s", side, exc)
            continue
        for part in _polygon_parts(thin):
            area = part.area / mpm ** 2
            # Only narrow land that reaches the seam stops the pieces meshing.
            # An islet two millimetres off the seam's coastal end is thin, but
            # it is the thin-land survey's business, not this one's.
            if area >= params.min_tab_area_mm2 and part.distance(seam) <= width:
                out.append(NarrowPatch(part, side, area))
    return out


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def _tabs(geom_a, geom_b, seam, params):
    """
    Every tab along the seam, either way round, measured.

    Both provinces are clipped to a window around the seam before closing, as
    the narrow-land check does: closing all of Nunavut by 4 mm to look at one
    border would be most of the cost. The clip edge makes pockets of its own,
    but they lie a window's width from the seam, and only pockets that touch
    the seam are tabs.
    """
    mpm = params.mpm
    radius = params.tab_mm * mpm / 2.0
    if radius <= 0:
        return []
    window = seam.buffer(3.0 * radius)
    touch = seam.buffer(mpm * 1e-3)
    try:
        local_a = geom_a.intersection(window)
        local_b = geom_b.intersection(window)
    except Exception as exc:
        log.warning("tab search failed to clip the pair: %s", exc)
        return []
    tabs = []
    for side, holder, pocketed in (("a", local_a, local_b),
                                   ("b", local_b, local_a)):
        try:
            closed = pocketed.buffer(radius).buffer(-radius)
            in_pockets = holder.intersection(closed)
        except Exception as exc:
            log.warning("tab search failed on side %s: %s", side, exc)
            continue
        for face in _polygon_parts(in_pockets):
            if not face.intersects(touch):
                continue
            tab = _measure_tab(face, side, touch, params)
            if tab is not None:
                tabs.append(tab)
    return tabs


def _measure_tab(face, side, touch, params):
    mpm = params.mpm
    area = face.area / mpm ** 2
    if area < params.min_tab_area_mm2:
        return None
    # The lid: the face's edge that is not the seam. Where the tab meets the
    # coast that edge is shore rather than lid, which only makes the mouth
    # look wider -- the safe direction to be wrong in.
    lid = face.boundary.difference(touch)
    if lid.is_empty or lid.length <= 0:
        return None                       # an enclave, not a tab
    head_r, head_centre = _head(face)
    if head_centre is None or 2.0 * head_r / mpm < MIN_TAB_HEAD_MM:
        return None
    neck_r = _neck_radius(face, lid, head_centre, head_r, mpm)
    neck = min(2.0 * neck_r, lid.length)
    depth = max((lid.distance(_pt(c)) for c in face.exterior.coords),
                default=0.0)

    tab = Tab(face, side, area, mouth_mm=lid.length / mpm,
              neck_mm=neck / mpm, head_mm=2.0 * head_r / mpm,
              depth_mm=depth / mpm, lid=lid, head_point=head_centre)
    tab.locks = tab.head_mm > tab.neck_mm * params.lock_ratio
    # Narrow only matters for a finger -- a tab that reaches further than its
    # neck is wide. A shallow wobble is as deep as it is wide, so its "neck" is
    # small for the same reason its depth is, and there is a whole province
    # behind it: nothing there can snap off.
    tab.too_narrow = (tab.neck_mm < params.required_width_mm
                      and tab.depth_mm >= FINGER_ASPECT * tab.neck_mm
                      and tab.depth_mm > params.edge_loss_mm)
    return tab


def _head(face):
    """
    The widest disc inside the face: its radius, and a point at its centre.

    Bisected on erosion rather than found with ``polylabel``, whose grid is
    seeded at the face's short side: on a sliver 150 mm long and 0.006 mm deep
    -- a surveyed meridian a hair off true -- that is twenty-five thousand
    cells and two seconds, for a face that is not a tab at all.
    """
    lo, hi = 0.0, min(face.bounds[2] - face.bounds[0],
                      face.bounds[3] - face.bounds[1]) / 2.0
    for _ in range(_NECK_STEPS):
        r = (lo + hi) / 2.0
        if face.buffer(-r).is_empty:
            hi = r
        else:
            lo = r
    core = face.buffer(-lo * 0.999) if lo > 0 else face
    if core.is_empty:
        return 0.0, None
    parts = _polygon_parts(core)
    if not parts:
        return 0.0, None
    return lo, max(parts, key=lambda p: p.area).representative_point()


def _neck_radius(face, lid, head_centre, head_r, mpm):
    """
    The widest disc that can travel from the head out to the lid.

    Bisected on the disc's radius: erode the face by it, and the disc can make
    the trip if the piece of the eroded face that holds the head still comes
    within one radius of the lid -- which is where a disc of that size would
    sit when touching the mouth.
    """
    lo, hi = 0.0, head_r
    for _ in range(_NECK_STEPS):
        r = (lo + hi) / 2.0
        if r <= 0:
            break
        if _reaches(face, lid, head_centre, r, mpm):
            lo = r
        else:
            hi = r
    return lo


def _reaches(face, lid, head_centre, r, mpm):
    eroded = face.buffer(-r)
    if eroded.is_empty:
        return False
    parts = _polygon_parts(eroded)
    holding = [p for p in parts if p.distance(head_centre) <= mpm * 0.02]
    if not holding:
        return False
    return holding[0].distance(lid) <= r * 1.001 + mpm * 1e-4


# ---------------------------------------------------------------------------
# Moving the border by hand
# ---------------------------------------------------------------------------

def push_border(geom_grow, geom_shrink, points, width_mm, scale=DEFAULT_SCALE):
    """
    Hand the land under a brush stroke from ``geom_shrink`` to ``geom_grow``.

    Parameters
    ----------
    geom_grow, geom_shrink : the two provinces, in the working metre CRS.
    points    : the pointer path, as ``[(x, y), ...]``.
    width_mm  : the brush width, in print-mm.

    Returns
    -------
    ``(grown, shrunk)``, or **the very same two objects** if the stroke moves
    nothing -- so a caller can tell a no-op by identity and keep it out of the
    undo history, as with the island editor's brush.

    Only land that touches the growing province moves.  A stroke dragged deep
    into the other one would otherwise make an enclave -- a scrap of A in the
    middle of B -- which is not moving the border, and would not print as a
    piece of A.  The pair still partitions the ground it did: what one loses
    the other gains, exactly.
    """
    stroke = brush_stroke(points, width_mm * scale / 1000.0 / 1000.0)
    grown, shrunk, _moved = transfer(stroke, geom_grow, geom_shrink, scale)
    return grown, shrunk


def transfer(region, geom_grow, geom_shrink, scale=DEFAULT_SCALE):
    """
    Hand ``geom_shrink``'s land inside ``region`` to ``geom_grow``.

    The one way land changes hands here, for the brush and the solver alike.
    Only pieces that touch the growing province move -- anything else would be
    an enclave -- and what one side loses the other gains exactly, so the pair
    goes on partitioning the same ground.

    Returns ``(grown, shrunk, moved)``; on a no-op, **the same two objects**
    and None, so a caller can tell by identity.
    """
    unchanged = (geom_grow, geom_shrink, None)
    if region is None or region.is_empty or geom_grow is None \
            or geom_shrink is None or not region.intersects(geom_shrink):
        return unchanged
    try:
        taken = region.intersection(geom_shrink)
        # Touching, with a hair of slack: the shared edge is exact in principle
        # but floating-point in practice.
        reach = geom_grow.buffer(scale / 1000.0 * 1e-4)
        moved = [p for p in _polygon_parts(taken) if p.intersects(reach)]
        if not moved:
            return unchanged
        moved = unary_union(moved)
        grown = unary_union([geom_grow, moved])
        shrunk = geom_shrink.difference(moved)
    except Exception as exc:
        log.warning("transfer failed: %s", exc)
        return unchanged
    if shrunk.is_empty:
        return unchanged                  # never erase a province outright
    return _as_polygonal(grown), _as_polygonal(shrunk), moved


# ---------------------------------------------------------------------------
# Solving automatically
# ---------------------------------------------------------------------------

#: Rounds of repair. Fixing one problem can expose another beside it; four
#: rounds settles every seam in the country, and a fifth has never helped.
MAX_ROUNDS = 4


@dataclass
class Fix:
    """One repair :func:`solve_fit` made, or would have made."""
    kind: str                       #: "lock", "finger" or "narrow"
    side: str                       #: whose land the problem was
    action: str = ""                #: "cut" or "fill"; empty if skipped
    area_mm2: float = 0.0           #: land that changed hands
    move_mm: float = 0.0            #: furthest the border moved
    geometry: object = None         #: where -- the land that moved, or the problem
    reason: str = ""                #: why it was skipped
    grows: str = ""                 #: the side that gained the land


@dataclass
class SolveResult:
    """Result of :func:`solve_fit`."""
    a: object = None
    b: object = None
    fixed: list = field(default_factory=list)       #: Fix, made
    skipped: list = field(default_factory=list)     #: Fix, not made
    problems_before: int = 0
    problems_after: int = 0
    rounds: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.fixed)

    def summary(self) -> str:
        if not self.problems_before:
            return "nothing to solve -- the seam meshes cleanly"
        if not self.fixed:
            why = f"; {len(self.skipped)} left for a hand" \
                if self.skipped else ""
            return f"no repair helped{why}"
        cuts = sum(f.action == "cut" for f in self.fixed)
        fills = len(self.fixed) - cuts
        text = (f"{self.problems_before} → {self.problems_after} problem(s): "
                f"{cuts} cut, {fills} filled")
        if self.skipped:
            text += f", {len(self.skipped)} left for a hand"
        return text


def solve_fit(geom_a, geom_b, params=None, max_move_mm=None,
              max_rounds=MAX_ROUNDS, progress=None):
    """
    Repair what :func:`fit_report` finds, by handing land across the seam.

    Parameters
    ----------
    geom_a, geom_b : the pair, in the working metre CRS.
    params         : FitParams, as for :func:`fit_report`.
    max_move_mm    : the furthest any repair may move the border from where it
                     ran *before solving*, print-mm.  Defaults to the config's
                     ``seam.solve_max_move_mm``.
    progress       : optional ``progress(round, done, total)``, called as each
                     problem is looked at -- Labrador has eighty.

    Returns
    -------
    SolveResult -- with ``a`` and ``b`` the very same objects passed in when
    nothing was changed.
    """
    params = params or FitParams.from_config()
    if max_move_mm is None:
        max_move_mm = _cfg("seam.solve_max_move_mm")
    result = SolveResult(a=geom_a, b=geom_b)
    report = fit_report(geom_a, geom_b, params=params)
    result.problems_before = result.problems_after = report.problems
    if not report.problems:
        return result
    # Moves are measured from the border as it ran before any of them, so
    # four rounds of small moves cannot walk it further than one may go.
    origin = _shared_seam(geom_a, geom_b)

    a, b = geom_a, geom_b
    skipped = {}
    for round_no in range(1, max_rounds + 1):
        result.rounds = round_no
        corridor = _Corridor(a, b, params)
        touched = []                       # land moved this round
        made = 0
        problems = _problems(report)
        for done, problem in enumerate(problems):
            if progress is not None:
                progress(round_no, done, len(problems))
            geom = problem[2]
            # Measured on the pair as it was at the start of the round: a
            # problem next to land already moved is stale, and is looked at
            # again next round on the pair as it now is.
            if any(geom.intersects(t) for t in touched):
                continue
            fix = _best_fix(corridor, problem, params, max_move_mm, origin)
            if fix.action:
                a, b = _apply(a, b, fix, params)
                corridor.apply(fix)
                touched.append(fix.geometry)
                result.fixed.append(fix)
                skipped.pop(_where(geom), None)
                made += 1
            else:
                skipped[_where(geom)] = fix
        report = fit_report(a, b, params=params)
        if not made or not report.problems:
            break

    result.problems_after = report.problems
    if result.problems_after >= result.problems_before:
        # No better overall: hand back the pair untouched rather than a set of
        # moves that traded one problem for another.
        result.a, result.b = geom_a, geom_b
        for fix in result.fixed:
            fix.action, fix.reason = "", "made things no better overall"
        result.skipped = list(skipped.values()) + result.fixed
        result.fixed = []
        result.problems_after = result.problems_before
        return result
    result.a, result.b = a, b
    # Only what is still wrong counts as skipped.
    still = {_where(problem[2]) for problem in _problems(report)}
    result.skipped = [f for key, f in skipped.items() if key in still]
    return result


class _Corridor:
    """
    Both provinces clipped to a strip along the seam, for trying repairs on.

    Every candidate repair is judged by running the fit check around it, and
    clipping the whole of Québec to a small window for each one was most of a
    fifty-second solve. Clipping once a round to the strip every repair lies
    in, then clipping the strip, costs a fraction of that. Repairs that are
    made are applied here as well as to the real pair, so the strip stays
    true for the rest of the round.
    """

    def __init__(self, a, b, params):
        seam = _shared_seam(a, b)
        reach = (params.tab_mm * 3.0 + params.band_mm
                 + 2.0 * params.required_width_mm) * params.mpm
        strip = seam.buffer(reach) if seam is not None else None
        self.a = _as_polygonal(a.intersection(strip)) if strip else a
        self.b = _as_polygonal(b.intersection(strip)) if strip else b

    def pair(self, grow_side, grown, shrunk):
        return (grown, shrunk) if grow_side == "a" else (shrunk, grown)

    def sides(self, grow_side):
        return (self.a, self.b) if grow_side == "a" else (self.b, self.a)

    def apply(self, fix):
        grow, shrink = self.sides(fix.grows)
        grown, shrunk, _ = transfer(fix.geometry, grow, shrink)
        self.a, self.b = self.pair(fix.grows, grown, shrunk)


def _problems(report):
    """Every problem as ``(kind, side, geometry, tab)``, worst kinds first;
    ``tab`` is None for narrow land."""
    out = [("lock", t.side, t.geometry, t) for t in report.locking]
    out += [("finger", t.side, t.geometry, t) for t in report.tabs
            if t.too_narrow and not t.locks]
    out += [("narrow", p.side, p.geometry, None)
            for p in sorted(report.narrow, key=lambda p: -p.area_mm2)]
    return out


def _where(geom):
    """A key for a problem's place, stable across rounds to within a metre."""
    point = geom.representative_point()
    return round(point.x), round(point.y)


def _best_fix(corridor, problem, params, max_move_mm, origin):
    """Try a cut and a fill; keep the one the fit check likes better."""
    kind, side, geom, tab = problem
    mpm = params.mpm
    width = params.required_width_mm * mpm
    focus = geom.buffer(width)
    baseline = _local_problems(corridor.a, corridor.b, focus, params)
    # Only a tab's lid falls short of the seam; narrow land has no lid, and
    # snapping it only cut fresh notches beside it.
    snapped = (geom.buffer(_SNAP_MM * mpm, join_style="mitre")
               if tab is not None else geom)
    tried = []
    for action, region, grow_side in (
            ("cut", snapped, "b" if side == "a" else "a"),
            ("fill", _fill_region(kind, snapped, tab, params), side)):
        grow, shrink = corridor.sides(grow_side)
        grown, shrunk, moved = transfer(region, grow, shrink, params.scale)
        if moved is None:
            continue
        move = _move_mm(moved, origin, mpm)
        if move > max_move_mm:
            tried.append((None, action, move, moved, grow_side))
            continue
        new_a, new_b = corridor.pair(grow_side, grown, shrunk)
        left = _local_problems(new_a, new_b, focus.union(moved.buffer(width)),
                               params)
        tried.append((left, action, move, moved, grow_side))

    usable = [t for t in tried if t[0] is not None and t[0] < baseline]
    if not usable:
        too_far = [t for t in tried if t[0] is None]
        if tried and len(too_far) == len(tried):
            reason = (f"every repair moves the border "
                      f"{min(t[2] for t in too_far):.1f} mm or more")
        else:
            reason = "no repair made it better"
        return Fix(kind, side, geometry=geom, reason=reason)
    left, action, move, moved, grow_side = min(
        usable, key=lambda t: (t[0], t[3].area))
    return Fix(kind, side, action=action, area_mm2=moved.area / mpm ** 2,
               move_mm=move, geometry=moved, grows=grow_side)


def _apply(a, b, fix, params):
    grow, shrink = (a, b) if fix.grows == "a" else (b, a)
    grown, shrunk, _ = transfer(fix.geometry, grow, shrink, params.scale)
    return (grown, shrunk) if fix.grows == "a" else (shrunk, grown)


def _fill_region(kind, geom, tab, params):
    """The region a fill would take from the other province."""
    if kind == "lock":
        # The undercuts: everything of the other side the tab would sweep
        # through if it were pulled straight out through its mouth. With them
        # gone the tab is nowhere wider than its way out, and lifts free.
        #
        # Not the tab's convex hull, which was tried first: on a mushroom the
        # hull runs diagonally from the neck to the corners of the head, so
        # the mouth stays as narrow as it was and the tab still locks. The
        # hull of the tab and a copy of it moved back past its own mouth does
        # cover the corners.
        return _swept(geom, tab, params)
    # A thin finger or patch: a collar wide enough to bring it to the
    # required width, measured from its own middle.
    # A thin finger or patch: a collar wide enough to bring it to the
    # required width, measured from its own middle.
    return geom.buffer(params.required_width_mm * params.mpm / 2.0,
                       join_style="mitre")


def _swept(geom, tab, params):
    """The tab and everything it passes through on its way out of the pocket."""
    if tab is None or tab.lid is None or tab.head_point is None:
        return geom.convex_hull
    out = tab.lid.centroid
    dx, dy = out.x - tab.head_point.x, out.y - tab.head_point.y
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 0:
        return geom.convex_hull
    reach = tab.depth_mm * params.mpm + _SNAP_MM * params.mpm
    shifted = translate(geom, dx / length * reach, dy / length * reach)
    return unary_union([geom, shifted]).convex_hull


def _move_mm(moved, origin, mpm):
    """
    How far from the original seam the moved land reaches, print-mm.

    Measured along the land's edges at a tenth of a millimetre, not only at
    its vertices: a fill's far edge is a straight hull line between two seam
    vertices, so every vertex sits on the seam and the vertices alone report
    a move of nothing however far the middle of that line runs.
    """
    if origin is None or moved is None:
        return 0.0
    far = 0.0
    for part in _polygon_parts(moved):
        ring = segmentize(part.exterior, 0.1 * mpm)
        for coord in ring.coords:
            far = max(far, origin.distance(_pt(coord)))
    return far / mpm


def _local_problems(a, b, focus, params):
    """
    How many problems the fit check finds around ``focus``.

    Run on the corridor clipped again to a window about the problem. The clip
    is a cut across both provinces a window's distance away, where it makes
    nothing narrow that was not; and only problems touching the focus count.
    """
    # Room for the closing to see the whole pocket around the focus: its
    # radius is half the largest tab, so three quarters of it is enough.
    window = focus.buffer(params.tab_mm * params.mpm * 0.75)
    try:
        local_a = _as_polygonal(a.intersection(window))
        local_b = _as_polygonal(b.intersection(window))
    except Exception:
        return float("inf")
    report = fit_report(local_a, local_b, params=params)
    return sum(1 for problem in _problems(report)
               if problem[2].intersects(focus))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pt(coord):
    return Point(coord[0], coord[1])


def _polygon_parts(geom):
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)
            and not g.is_empty] + [
        p for g in getattr(geom, "geoms", [])
        if not isinstance(g, Polygon) for p in _polygon_parts(g)]


def _as_polygonal(geom):
    """Drop stray lines and points a boolean can leave on a shared edge."""
    parts = _polygon_parts(geom)
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)
