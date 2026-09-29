#!/usr/bin/env python3
"""
LIB_island_bridges.py

Connect islands with straight bridges of a fixed printed width.

Where :func:`lib.LIB_island_merging.merge_islands` closes the whole group by a
buffer and keeps whatever necks appear, this places **deliberate** bridges: a
rectangle of a width you choose, spanning water you choose to span.  The buffer
method's neck width is a *consequence* of the gap and the shape of the facing
coasts, and it thickens every other part of the coastline it touches on the way;
here the width is the specification and the land added is exactly the rectangles
listed in the plan::

    from lib.LIB_island_bridges import BridgeParams, bridge_islands

    geom, plan = bridge_islands(islands, BridgeParams(width_mm=1.0))
    print(len(plan.bridges), "bridges,",
          plan.components_before, "->", plan.components_after, "landmasses")

All lengths are **print-mm** and convert through ``print.scale`` at the moment
of use, as in :class:`lib.LIB_shared_border.SeamParams`.  At 1:8,000,000 a
1 mm bridge is 8 km of ground -- a causeway, not a footbridge -- and 2.5 times
the 0.4 mm nozzle, which is about the thinnest run worth printing.  Geometry is
expected in a projected metre CRS (the project uses EPSG:3347).

Two questions, two different answers
------------------------------------
**Where does one bridge go?**  That is local, and the honest starting point is
``nearest_points``: the shortest crossing minimises span, added land and
unsupported length at once.  It is right most of the time and wrong in three
specific ways, which is why candidates are *scored* rather than taken:

* **Tangential landing.**  The nearest point can be the tip of a spit, so a
  1 mm bridge welds on by a sliver.  Measured by ``anchor``: the area the slab
  actually overlaps the land, against the width x embedment it would get on a
  straight shore.
* **Grazing incidence.**  A bridge meeting the coast at 15 degrees welds along a
  long knife edge and reads as a smear.  Measured by ``incidence_deg``, 90 being
  a square landing.
* **Ill-conditioned minimum.**  Between near-parallel coasts a whole stretch is
  within rounding of the shortest crossing, so the "optimum" is arbitrary and
  moves under any change to the data.  Scoring breaks the tie on something that
  means one thing.
* **Landing on a third island.**  A slab that clips an island it was not
  spanning welds three landmasses where the tree counted on two, so those
  crossings are refused outright rather than scored.  Nothing is lost: hopping
  through that island is cheaper, and the tree takes it.

An editor bridging a *selection* needs one more thing from that last rule: the
islands it did not select are still on the map, and a bridge may not be run
across them either.  They are passed as ``obstacles`` -- geometry to avoid but
never to connect, and not counted as landmasses.  Without them the rule sees
only what it was asked to bridge, and the plan quietly disagrees with the
document it will be applied to.

So each candidate costs::

    cost_mm = span_mm
            + angle_penalty_mm  * cos(incidence)     # 0 square, 1 grazing
            + anchor_penalty_mm * (1 - anchor)       # 0 full lap, 1 no bite

Both penalties are in print-mm, so they trade directly against span: at the
defaults, a fully grazing landing has to save 2 mm of span to be worth taking.

**And it stops at the coast.**  Each end is driven ``embed_mm`` past its landing
so the union with the shore is a lap joint and not a kiss.  That assumes there is
land under the overhang to lap onto, and often there is not: 1 mm is 8 km of
ground at 1:8,000,000, which is thicker than most islets and than every spit.
Driven through one of those the end comes out of the far coast and stands a tab
of bridge, at full width, in open water -- a bridge to nowhere, on the far side
of an island the crossing had already reached, and land that ``area_mm2`` does
not admit to.  So the ends of the bridges the tree chose are trimmed back to the
land actually under them (``_trim_ends``); what a bridge adds is then exactly
its width times its span, wherever it lands.

**How many bridges?**  That is global, and not a question about any pair.
Islands are the nodes of a graph, candidate bridges its edges weighted by
``cost_mm``, and the answer is the **minimum spanning tree**: for n landmasses
exactly n-1 bridges, provably the cheapest set that joins everything.  A chain
of five islands therefore gets four bridges, each to its real nearest
neighbour, with no pairwise rule needed and nothing double-connected.  Edges
longer than ``max_span_mm`` are dropped before the tree is built, which turns it
into a spanning *forest*: clusters out of reach stay separate and are listed in
``BridgePlan.unreachable`` rather than being joined by something unprintable.

When one pair needs two
-----------------------
Two large islands joined at a single point pivot about it.  The test for that is
asked of the landings rather than of the coastline: if two acceptable bridges
could be placed ``hinge_ratio`` times the crossing apart, a single one is a
hinge, and a second is added at ``separation_mm - cost_mm`` -- both millimetres,
so a long baseline trades against a worse landing with no weight to invent.
Measuring the reach the bridges would actually have beats measuring the shore,
which can run close to the other island while facing away from it.  This is a
rigidity argument, and it is the only reason to double up.

**Slenderness is a different problem with a different fix.**  A 1 mm wide, 20 mm
long bridge is a matchstick, and two parallel matchsticks are still matchsticks:
widen it or leave the island separate.  Bridges past ``max_aspect`` are flagged
as ``slender`` and reported, not silently doubled -- and not silently dropped
either, since dropping one disconnects an island, so ``drop_slender`` is opt-in.

Lagoons
-------
A single bridge across a bay can trap open sea into a ring that would read as a
hole in the printed slab; those are filled, exactly as in ``merge_islands``.
Water enclosed *between two bridges of a braced pair* is a real lagoon and is
kept.  The two cases are told apart by the same trick ``merge_islands`` uses to
find its necks -- count how many bridges the ring touches.  One is an accident,
two is a strait you decided to keep.

Cost
----
Pairs are pruned to those within ``max_span_mm`` by bounding box (STRtree), and
within a pair only landings that could still win are scored: a candidate more
than ``angle_penalty_mm + anchor_penalty_mm`` longer than the shortest crossing
cannot beat it whatever its angle and anchorage, so the band is a bound rather
than a knob.  What survives that is capped at ``MAX_CANDIDATES_PER_PAIR``,
shortest first.
"""

import logging
import math
from dataclasses import dataclass, field

from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, minimum_spanning_tree
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union

from lib.LIB_island_merging import _fill_new_holes, _polygons
from lib.LIB_project_config import get as _cfg

log = logging.getLogger(__name__)

EPSG_METRIC = _cfg("crs.working")
DEFAULT_SCALE = _cfg("print.scale")

#: Landings scored per pair, shortest crossing first.  The band below is already
#: a proof-backed bound; this is the guard for the case it cannot help with --
#: two long coasts running parallel a hair apart, where thousands of landings are
#: genuinely within reach and near-identical.
MAX_CANDIDATES_PER_PAIR = 240

#: Weight for a pair that already touches.  It has to be non-zero, because a
#: sparse matrix stores no distinction between "zero cost" and "no edge", and
#: dropping these would have the tree bridge islands that are already one
#: landmass.  Any value below the cheapest real bridge does; this is free.
_TOUCHING_COST = 1e-9


@dataclass
class BridgeParams:
    """How bridges are placed.  All lengths are **print-mm**."""
    scale: float = DEFAULT_SCALE                #: map scale denominator
    #: The bridge itself.  ``width_mm`` is the specification -- every bridge is
    #: this wide -- and ``embed_mm`` is how far each end is pushed past the
    #: coastline into the land, so the union is a lap joint and not a kiss.
    width_mm: float = _cfg("bridges.width_mm")
    embed_mm: float = _cfg("bridges.embed_mm")
    #: Refuse to span further than this.  Also the cap that turns the spanning
    #: tree into a forest, so it decides which islands stay separate.
    max_span_mm: float = _cfg("bridges.max_span_mm")
    #: Coastline sampling pitch for candidate landings.  Finer finds better
    #: landings and costs linearly more.
    sample_mm: float = _cfg("bridges.sample_mm")
    #: Scoring, in mm so both trade directly against span.  See the module
    #: docstring for the cost expression.
    angle_penalty_mm: float = _cfg("bridges.angle_penalty_mm")
    anchor_penalty_mm: float = _cfg("bridges.anchor_penalty_mm")
    #: Reject a landing whose overlap with the land is below this fraction of
    #: the width x embedment a straight shore would give (0..1).
    min_anchor: float = _cfg("bridges.min_anchor")
    #: Slenderness limit, as span/width.  Past it a bridge is a matchstick.
    max_aspect: float = _cfg("bridges.max_aspect")
    #: Report slender bridges (default) or refuse them.  Refusing disconnects
    #: whatever they were holding on, which is why it is not the default.
    drop_slender: bool = False
    #: Bracing a hinge: at most this many bridges per pair, added only when two
    #: bridges could be placed ``hinge_ratio`` crossings apart.
    max_per_pair: int = _cfg("bridges.max_per_pair")
    hinge_ratio: float = _cfg("bridges.hinge_ratio")
    work_crs: str = EPSG_METRIC                 #: metric CRS the work is done in

    @property
    def mpm(self) -> float:
        """Metres per print-mm."""
        return self.scale / 1000.0

    @property
    def width_m(self) -> float:
        return self.width_mm * self.mpm

    @property
    def embed_m(self) -> float:
        return self.embed_mm * self.mpm

    @property
    def max_span_m(self) -> float:
        return self.max_span_mm * self.mpm

    @property
    def sample_m(self) -> float:
        return self.sample_mm * self.mpm


@dataclass
class Bridge:
    """One placed bridge, with the numbers that chose it."""
    a: int = -1                     #: index into the caller's island list
    b: int = -1
    polygon: object = None          #: the slab, in the work CRS
    line: object = None             #: centreline, coast to coast (no embedment)
    span_mm: float = 0.0            #: water crossed
    width_mm: float = 0.0
    #: Why this landing won.  90 deg is a square landing; ``anchor`` is 1.0 for
    #: a full width x embedment bite out of both shores.
    incidence_deg: float = 90.0
    anchor: float = 1.0
    cost_mm: float = 0.0
    #: 0 for the pair's primary bridge, 1 for a brace added against hinging.
    rank: int = 0
    slender: bool = False           #: span/width past ``max_aspect``

    @property
    def aspect(self) -> float:
        """Span as a multiple of the width -- the matchstick number."""
        return self.span_mm / self.width_mm if self.width_mm else 0.0

    @property
    def area_mm2(self) -> float:
        """Printed land this bridge adds.

        Width x span, not the slab's own area: the embedded ends lie on top of
        land that was already there, so counting them would overstate what the
        bridge costs in material and in coastline.
        """
        return self.width_mm * self.span_mm


@dataclass
class BridgePlan:
    """What :func:`plan_bridges` decided, and why."""
    bridges: list = field(default_factory=list)
    params: object = None
    islands: int = 0                #: land masses considered
    pairs_in_reach: int = 0         #: pairs inside ``max_span_mm``
    pairs_without_landing: int = 0  #: in reach, but no landing passed the floor
    #: Of those, the ones whose landings were refused *mainly* because another
    #: island lay across the crossing -- the most common single cause, counted
    #: so a caller can say "something is in the way" rather than the useless
    #: "no landing was good enough".
    pairs_blocked: int = 0
    components_before: int = 0      #: landmasses as they arrived
    components_after: int = 0       #: landmasses once the bridges are applied
    #: Islands left out of the largest landmass -- too far for ``max_span_mm``.
    unreachable: list = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self.components_after == 1

    @property
    def land_added_mm2(self) -> float:
        return sum(b.area_mm2 for b in self.bridges)

    @property
    def slender(self) -> list:
        return [b for b in self.bridges if b.slender]

    @property
    def braces(self) -> list:
        return [b for b in self.bridges if b.rank > 0]


# -- geometry helpers -------------------------------------------------------

def _nearest_part(geom, other):
    """The polygon part of ``geom`` closest to ``other`` (bridges land there)."""
    parts = _polygons(geom)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return min(parts, key=other.distance)


def _ring_samples(ring, pitch_m: float) -> list:
    """Points every ``pitch_m`` along a ring, by arc length.

    Walked by arc length rather than densified, so the count is bounded by the
    coastline's length and not by however many vertices it happens to carry --
    a province coastline can hold thousands within one bridge width, and they
    would all propose the same bridge.
    """
    if pitch_m <= 0 or ring.length <= 0:
        return []
    count = max(int(math.ceil(ring.length / pitch_m)), 3)
    step = ring.length / count
    return [ring.interpolate(i * step) for i in range(count)]


def _tangent(ring, point, eps_m: float):
    """Unit vector along ``ring`` at ``point``, or None if it degenerates.

    Sampled over ``eps_m`` either side rather than from the neighbouring
    vertices, so the answer describes the shore at the scale of the bridge
    instead of at the scale of whatever digitising noise is nearest.
    """
    length = ring.length
    if length <= 0:
        return None
    at = ring.project(point)
    before = ring.interpolate((at - eps_m) % length)
    after = ring.interpolate((at + eps_m) % length)
    dx, dy = after.x - before.x, after.y - before.y
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > 0 else None


def _incidence_deg(ring, point, direction, eps_m: float) -> float:
    """Angle between a bridge and the shore it lands on: 90 is square on."""
    tangent = _tangent(ring, point, eps_m)
    if tangent is None:
        return 90.0
    dot = abs(direction[0] * tangent[0] + direction[1] * tangent[1])
    return math.degrees(math.acos(min(1.0, max(0.0, dot))))


def _slab(p, q, width_m: float, embed_m: float):
    """The bridge rectangle from ``p`` to ``q``, pushed ``embed_m`` into each end.

    Flat caps, so the slab is exactly the rectangle between the two embedded
    ends -- round or square caps would add a width's worth of land nobody asked
    for, which is the thing this module exists to avoid.

    The overhang is a lap joint and assumes there is land under it to lap onto;
    :func:`_trim_ends` takes back the part of it that hangs over water.
    """
    dx, dy = q.x - p.x, q.y - p.y
    span_m = math.hypot(dx, dy)
    if span_m <= 0 or width_m <= 0:
        return None, None, span_m
    ux, uy = dx / span_m, dy / span_m
    ends = LineString([(p.x - ux * embed_m, p.y - uy * embed_m),
                       (q.x + ux * embed_m, q.y + uy * embed_m)])
    slab = ends.buffer(width_m / 2.0, cap_style="flat", join_style="mitre")
    return slab, (ux, uy), span_m


def _trim_ends(bridge: Bridge, ga, gb, params: BridgeParams) -> None:
    """Cut a bridge's embedded ends back to the land they were driven into.

    The overhang exists so the union with the coast is a lap joint rather than
    a kiss.  It assumes the island is at least ``embed_mm`` thick where the
    bridge lands, and plenty are not: 1 mm is 8 km of ground at 1:8,000,000, so
    every spit and most islets are thinner than the joint driven into them.
    Untrimmed, the end comes out of the far coast and stands a tab of land --
    full bridge width -- in open water: a bridge to nowhere, and land nobody
    asked for by giving a span.  It also makes ``area_mm2`` a lie, because the
    slab then adds more than width x span.

    What is kept is the crossing -- coast to coast, every bit of it over the
    water the bridge is there to cross -- plus whatever of the slab actually
    lies on the two islands.  So the joint is untouched wherever there was land
    to make it in, and gone where there was not.

    Done here, on the handful of bridges the tree chose, rather than in
    :func:`_candidates` on every landing scored: the shape only matters once a
    bridge is going to be built, and the candidates are thousands.  The
    anchorage that *chose* the landing is measured on the untrimmed slab either
    way -- it asks how much land the end has to hold on to, which is exactly
    what is kept here.
    """
    if params.embed_m <= 0 or bridge.polygon is None or bridge.line is None:
        return
    crossing = bridge.line.buffer(params.width_m / 2.0, cap_style="flat",
                                  join_style="mitre")
    laps = (_polygons(bridge.polygon.intersection(ga))
            + _polygons(bridge.polygon.intersection(gb)))
    trimmed = unary_union([crossing] + laps)
    if not trimmed.is_empty:
        bridge.polygon = trimmed


# -- candidate scoring ------------------------------------------------------

def _pair_bounds(ga, gb, pad_m: float):
    """Bounding box around two islands, padded by the slab's own overhang."""
    ax0, ay0, ax1, ay1 = ga.bounds
    bx0, by0, bx1, by1 = gb.bounds
    return (min(ax0, bx0) - pad_m, min(ay0, by0) - pad_m,
            max(ax1, bx1) + pad_m, max(ay1, by1) + pad_m)


def _candidates(ia: int, ib: int, ga, gb, others, params: BridgeParams,
                tally: dict = None) -> list:
    """Every landing worth considering between two islands, cheapest first.

    ``others`` are the islands the slab may not touch.  A bridge that clips a
    third island is not the edge the spanning tree thinks it is -- it silently
    welds three landmasses, so the count of bridges stops matching the count of
    joins -- and the shorter hops through that island are cheaper anyway, so
    refusing the crossing costs nothing the tree cannot recover.

    ``tally``, if given, is counted up by reason for every landing refused, so
    a caller that ends with nothing can say which limit to loosen.
    """
    def refuse(reason):
        if tally is not None:
            tally[reason] = tally.get(reason, 0) + 1
    gap_m = ga.distance(gb)
    if gap_m <= 0 or gap_m > params.max_span_m:
        return []

    part_a = _nearest_part(ga, gb)
    part_b = _nearest_part(gb, ga)
    if part_a is None or part_b is None:
        return []

    # A landing longer than the shortest crossing plus every penalty it could
    # possibly avoid cannot win, whatever its angle and anchorage. So this band
    # is a bound rather than a tuning knob -- widening it cannot change the
    # answer, only the time taken to reach it.
    slack_m = (params.angle_penalty_mm + params.anchor_penalty_mm) * params.mpm
    bound_m = min(params.max_span_m, gap_m + slack_m)

    landings = [(pt.distance(part_b), pt)
                for pt in _ring_samples(part_a.exterior, params.sample_m)]
    # Sorted on the distance alone: two landings equidistant from B are common
    # on any symmetric coastline, and a plain tuple sort would fall through to
    # comparing the Points themselves, which shapely refuses.
    landings = sorted((pair for pair in landings if pair[0] <= bound_m),
                      key=lambda pair: pair[0])[:MAX_CANDIDATES_PER_PAIR]

    ideal_m2 = params.width_m * params.embed_m
    eps_m = max(params.width_m * 0.5, 1e-6)
    out = []
    for _, p in landings:
        q = nearest_points(p, part_b)[1]
        slab, direction, span_m = _slab(p, q, params.width_m, params.embed_m)
        if slab is None or slab.is_empty or span_m > bound_m:
            continue

        # Anchorage: what the slab actually bites out of each shore, against
        # what it would bite out of a straight one. A spit narrower than the
        # bridge, or a landing that merely grazes, shows up here as a shortfall.
        anchor = 1.0
        if ideal_m2 > 0:
            anchor = min(slab.intersection(part_a).area,
                         slab.intersection(part_b).area) / ideal_m2
            anchor = min(1.0, max(0.0, anchor))
        if anchor < params.min_anchor:
            refuse("anchor")
            continue
        # Asked of the slab with its embedment on, not of the trimmed shape: a
        # crossing whose end would be driven through a third island is refused
        # outright rather than cut back to fit past it. Hopping through that
        # island is cheaper anyway, and the tree takes it.
        if any(slab.intersects(other) for other in others):
            refuse("blocked")       # it would cross a third island on the way
            continue

        incidence = min(_incidence_deg(part_a.exterior, p, direction, eps_m),
                        _incidence_deg(part_b.exterior, q, direction, eps_m))
        span_mm = span_m / params.mpm
        cost_mm = (span_mm
                   + params.angle_penalty_mm * math.cos(math.radians(incidence))
                   + params.anchor_penalty_mm * (1.0 - anchor))
        slender = (params.max_aspect > 0
                   and params.width_mm > 0
                   and span_mm / params.width_mm > params.max_aspect)
        if slender and params.drop_slender:
            refuse("slender")
            continue

        out.append(Bridge(
            a=ia, b=ib, polygon=slab,
            line=LineString([(p.x, p.y), (q.x, q.y)]),
            span_mm=span_mm, width_mm=params.width_mm,
            incidence_deg=incidence, anchor=anchor, cost_mm=cost_mm,
            slender=slender))

    out.sort(key=lambda b: b.cost_mm)
    return out


def _brace(primary: Bridge, candidates: list,
           params: BridgeParams) -> Bridge | None:
    """A second bridge for a pair that would otherwise hinge, or None.

    The hinge test is asked of the landings themselves: how far apart can two
    acceptable bridges actually be put?  That distance is the moment arm the
    brace would have, so measuring it directly beats measuring the coastline
    and hoping -- a shore can run close to the other island while facing
    entirely away from it, and a lake can lie nearer the strait than the coast
    does.  Only landings that already passed the anchor and angle floors are
    counted, so the reach is what is placeable, not what is merely nearby.

    Among those, the brace maximises ``separation_mm - cost_mm``: both are
    millimetres, so the baseline that resists the pivot trades against the
    quality of the landing with no weight to invent.
    """
    if params.max_per_pair < 2 or primary.span_mm <= 0 or not candidates:
        return None

    landfall = Point(primary.line.coords[0])
    options = [(Point(c.line.coords[0]).distance(landfall) / params.mpm, c)
               for c in candidates]
    if max(sep for sep, _ in options) < params.hinge_ratio * primary.span_mm:
        return None    # they meet effectively at a point; nothing to pivot

    apart_mm = 2.0 * params.width_mm    # closer than this and it is one bridge
    best, best_score = None, None
    for separation_mm, cand in options:
        if separation_mm < apart_mm:
            continue
        score = separation_mm - cand.cost_mm
        if best_score is None or score > best_score:
            best, best_score = cand, score
    if best is None:
        return None
    best.rank = 1
    return best


# -- the plan ---------------------------------------------------------------

def plan_bridges(islands, params: BridgeParams = None,
                 obstacles=None) -> BridgePlan:
    """
    Decide where the bridges go and how many there are.

    Parameters
    ----------
    islands   : iterable of Shapely Polygon/MultiPolygon in a projected metre
                CRS (the project uses EPSG:3347).  None/empty entries are
                skipped and never referenced; ``Bridge.a``/``Bridge.b`` index
                this sequence as it was passed in.
    params    : BridgeParams, optional
    obstacles : geometry a bridge may not cross but is not there to connect --
                for an editor bridging a selection, every island that was not
                selected.  They are never nodes of the graph and never counted
                as landmasses; they only refuse crossings.

    Returns
    -------
    A :class:`BridgePlan`.  Nothing is modified -- :func:`apply_bridges` does
    that -- so the plan can be inspected, drawn or argued with first.
    """
    params = params or BridgeParams()
    avoid = [g for g in (obstacles or []) if g is not None and not g.is_empty]
    present = [(i, g) for i, g in enumerate(islands)
               if g is not None and not g.is_empty]
    plan = BridgePlan(params=params, islands=len(present))
    if len(present) < 2:
        plan.components_before = plan.components_after = len(present)
        return plan
    if params.width_m <= 0 or params.max_span_m <= 0:
        log.warning("width_mm and max_span_mm must both be positive; "
                    "no bridges planned")
        plan.components_before = plan.components_after = len(present)
        return plan

    source, geoms = zip(*present)
    count = len(geoms)

    # Pairs within reach, by bounding box first: the exact distance between two
    # province coastlines is not cheap, and most pairs are nowhere near.
    tree = STRtree(list(geoms))
    avoid_tree = STRtree(avoid) if avoid else None
    pairs = set()
    for k, geom in enumerate(geoms):
        minx, miny, maxx, maxy = geom.bounds
        near = tree.query(box(minx - params.max_span_m, miny - params.max_span_m,
                              maxx + params.max_span_m, maxy + params.max_span_m))
        for j in near:
            j = int(j)
            if j != k:
                pairs.add((min(k, j), max(k, j)))

    rows, cols, costs = [], [], []
    touch_rows, touch_cols, touch_vals = [], [], []
    best: dict = {}
    for k, j in sorted(pairs):
        distance_m = geoms[k].distance(geoms[j])
        if distance_m <= 0:
            # Already one landmass. It still belongs in the graph, or the tree
            # would spend a bridge joining what is already joined.
            touch_rows.append(k)
            touch_cols.append(j)
            touch_vals.append(_TOUCHING_COST)
            continue
        if distance_m > params.max_span_m:
            continue
        plan.pairs_in_reach += 1
        # Islands the slab could reach across on its way. Worked out once per
        # pair, and empty for most of them, so the per-candidate test below is
        # usually free.
        reach_m = params.width_m / 2.0 + params.embed_m
        pair_box = box(*_pair_bounds(geoms[k], geoms[j], reach_m))
        others = [geoms[int(m)] for m in tree.query(pair_box)
                  if int(m) not in (k, j)]
        if avoid_tree is not None:
            others.extend(avoid[int(m)] for m in avoid_tree.query(pair_box))
        tally: dict = {}
        try:
            candidates = _candidates(k, j, geoms[k], geoms[j], others, params,
                                     tally)
        except Exception as exc:
            log.warning("bridge search failed for pair (%s, %s): %s",
                        source[k], source[j], exc)
            candidates = []
        if not candidates:
            plan.pairs_without_landing += 1
            if tally and max(tally, key=tally.get) == "blocked":
                plan.pairs_blocked += 1
            continue
        best[(k, j)] = candidates
        rows.append(k)
        cols.append(j)
        costs.append(candidates[0].cost_mm)

    def _graph(r, c, v):
        """Symmetric sparse graph over the islands (scipy wants undirected)."""
        return csr_matrix((list(v) + list(v), (list(r) + list(c),
                                               list(c) + list(r))),
                          shape=(count, count))

    plan.components_before = connected_components(
        _graph(touch_rows, touch_cols, touch_vals), directed=False)[0]

    # The heart of it: the cheapest set of bridges that joins everything. n-1
    # edges for n landmasses, and a forest rather than a tree once the pairs out
    # of reach have been left out of the graph.
    graph = _graph(touch_rows + rows, touch_cols + cols, touch_vals + costs)
    chosen = minimum_spanning_tree(graph)

    for k, j in zip(*chosen.nonzero()):
        key = (min(int(k), int(j)), max(int(k), int(j)))
        candidates = best.get(key)
        if not candidates:
            continue            # a touching pair: already one landmass
        primary = candidates[0]
        primary.a, primary.b = source[key[0]], source[key[1]]
        plan.bridges.append(primary)
        brace = _brace(primary, candidates[1:], params)
        if brace is not None:
            brace.a, brace.b = source[key[0]], source[key[1]]
            plan.bridges.append(brace)

    lookup = {original: k for k, original in enumerate(source)}
    for bridge in plan.bridges:
        _trim_ends(bridge, geoms[lookup[bridge.a]], geoms[lookup[bridge.b]],
                   params)
    linked = [(lookup[b.a], lookup[b.b]) for b in plan.bridges]
    bridged = _graph(touch_rows + [k for k, _ in linked],
                     touch_cols + [j for _, j in linked],
                     touch_vals + [1.0] * len(linked))
    plan.components_after, labels = connected_components(bridged,
                                                         directed=False)
    plan.unreachable = _unreachable(labels, source)
    plan.bridges.sort(key=lambda b: (b.cost_mm, b.rank))
    return plan


def _unreachable(labels, source) -> list:
    """Islands left outside the largest landmass once the bridges are in."""
    if len(labels) == 0:
        return []
    counts: dict = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    biggest = max(counts, key=lambda lab: (counts[lab], -lab))
    return [source[k] for k, label in enumerate(labels) if label != biggest]


# -- applying it ------------------------------------------------------------

def _lagoons(merged, slabs) -> list:
    """Rings enclosed by two or more bridges -- water a braced pair keeps.

    The same test ``merge_islands`` uses to tell a connecting neck from a filled
    bay, turned on the holes instead: a ring touching one bridge is a strait
    that bridge closed by accident, and a ring touching two is the lagoon
    between them.
    """
    kept = []
    for part in _polygons(merged):
        for ring in part.interiors:
            pocket = Polygon(ring)
            if sum(pocket.intersects(slab) for slab in slabs) >= 2:
                kept.append(pocket)
    return kept


def apply_bridges(islands, bridges, keep_lagoons: bool = True):
    """
    Union a plan's bridges onto the islands.

    Parameters
    ----------
    islands       : the same iterable handed to :func:`plan_bridges`.
    bridges       : a :class:`BridgePlan`, or a list of :class:`Bridge`.
    keep_lagoons  : keep water enclosed between two bridges of a braced pair.
                    Straits closed by a single bridge are filled either way.

    Returns
    -------
    The bridged geometry, or None if there was nothing to bridge.  On failure
    the plain union is returned, so no island is ever lost to an exception.
    """
    if isinstance(bridges, BridgePlan):
        bridges = bridges.bridges
    present = [g for g in islands if g is not None and not g.is_empty]
    if not present:
        return None
    slabs = [b.polygon for b in bridges
             if b.polygon is not None and not b.polygon.is_empty]
    if not slabs:
        return unary_union(present)

    try:
        originals = unary_union(present)
        merged = unary_union([originals, *slabs])
        # The islands' own lakes are theirs; a strait the bridge happened to
        # enclose is not, and reads as a border inside the landmass.
        lakes = [Polygon(ring) for g in present
                 for part in _polygons(g) for ring in part.interiors]
        keep = list(lakes)
        if keep_lagoons:
            keep.extend(_lagoons(merged, slabs))
        merged = _fill_new_holes(merged, unary_union(keep) if keep else None)
    except Exception as exc:
        log.warning("apply_bridges failed: %s", exc)
        return unary_union(present)

    if merged is None or merged.is_empty:
        return unary_union(present)
    return merged


def bridge_islands(islands, params: BridgeParams = None,
                   keep_lagoons: bool = True):
    """
    Plan and apply in one call.

    Returns ``(geometry, plan)`` -- the plan too, because "which islands are
    still separate, and why" is the question a caller asks next.
    """
    plan = plan_bridges(islands, params)
    return apply_bridges(islands, plan, keep_lagoons=keep_lagoons), plan
