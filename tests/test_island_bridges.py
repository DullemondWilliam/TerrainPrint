"""
test_island_bridges.py

Unit tests for LIB_island_bridges.py — straight bridges of a fixed printed
width between islands.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_bridges.py -v

Two things are worth guarding here, and they are different in kind.

**How many bridges** is a graph claim, twice over.  The minimum spanning tree
must spend exactly one bridge per landmass it absorbs, so a chain of four
islands costs three bridges and not the six a pairwise rule would place.  Then
each of those joins is weighed: cut its edge, and the lighter half of the tree
is the branch it carries, so a join holding half an archipelago is owed more
than a join holding an islet.  Both are claims about the *graph*, and both
break silently — six bridges still look right on a map, and so does one bridge
under a landmass that will snap it.

**Where one bridge goes** is a scoring claim: the shortest crossing is only the
starting point, and the tests below pin the three cases where the scorer must
overrule it — a landing on a spit, a grazing landing, and the pair that needs
bracing rather than a better single bridge.

**What a bridge is made of** is a third claim, and the quietest: a bridge is a
rectangle with a semicircle on each end, centred on its two landings, and the
only thing that may be taken off it is a piece the land has *severed* from its
own crossing.  Both halves of that matter.  An end driven through an island
thinner than the cap comes out of the far coast, which is land nobody asked for
— and an end that laps over the bay beside its landing has not left the water
it was already crossing, so cutting it back there only breaks the shape.  The
tests below hold the shape and the severance apart, because an earlier rule
that kept "the cap where it lies on land" satisfied the second claim by
destroying the first.

Geometry is in metres (EPSG:3347) and parameters are print-mm, so the fixtures
are written in millimetres and multiplied by ``MM``.
"""

import math
import unittest

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from lib.LIB_island_bridges import (BridgeParams, Bridge, apply_bridges,
                                    bridge_islands, plan_bridges)
from lib.LIB_island_merging import _polygons


def _square(x_mm, y_mm, size_mm, mm):
    return box(x_mm * mm, y_mm * mm, (x_mm + size_mm) * mm, (y_mm + size_mm) * mm)


class BridgeTestCase(unittest.TestCase):
    """Shared fixtures: one print-mm is 8 km at the project's 1:8,000,000."""

    MM = 8000.0        # metres per print-mm

    def square(self, x_mm, y_mm, size_mm=4.0):
        return _square(x_mm, y_mm, size_mm, self.MM)


class TestHowManyBridges(BridgeTestCase):
    """The spanning tree: the cheapest set of bridges that joins everything."""

    def test_chain_costs_one_bridge_per_join(self):
        # Four islands in a row, 2 mm of water between neighbours. A pairwise
        # rule would bridge every pair within reach; the tree spends three.
        islands = [self.square(i * 6.0, 0.0) for i in range(4)]
        plan = plan_bridges(islands)
        self.assertEqual(len(plan.bridges), 3)
        self.assertEqual(plan.components_before, 4)
        self.assertEqual(plan.components_after, 1)
        self.assertTrue(plan.connected)

    def test_chain_really_becomes_one_landmass(self):
        islands = [self.square(i * 6.0, 0.0) for i in range(4)]
        geom, _ = bridge_islands(islands)
        self.assertEqual(len(_polygons(geom)), 1)

    def test_no_pair_is_bridged_twice_over(self):
        # Three islands close enough that all three pairs are in reach: the
        # tree must take two edges, not close the triangle.
        islands = [self.square(0.0, 0.0), self.square(6.0, 0.0),
                   self.square(3.0, 6.0)]
        plan = plan_bridges(islands, BridgeParams(max_per_pair=1))
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual(plan.components_after, 1)

    def test_islands_out_of_reach_leave_a_forest(self):
        islands = [self.square(i * 6.0, 0.0) for i in range(4)]
        islands.append(self.square(100.0, 0.0))     # 80 mm of open water
        plan = plan_bridges(islands)
        self.assertEqual(len(plan.bridges), 3)
        self.assertEqual(plan.components_after, 2)
        self.assertEqual(plan.unreachable, [4])
        self.assertFalse(plan.connected)

    def test_touching_islands_are_already_one_landmass(self):
        # Sharing an edge is not something a bridge should be spent on.
        a = self.square(0.0, 0.0)
        b = self.square(4.0, 0.0)
        plan = plan_bridges([a, b])
        self.assertEqual(plan.bridges, [])
        self.assertEqual(plan.components_before, 1)
        self.assertEqual(plan.components_after, 1)

    def test_a_touching_pair_does_not_hide_a_third_island(self):
        # The free edge must not crowd the real one out of the tree.
        a = self.square(0.0, 0.0)
        b = self.square(4.0, 0.0)          # touches a
        c = self.square(10.0, 0.0)         # 2 mm from b
        plan = plan_bridges([a, b, c])
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(plan.components_before, 2)
        self.assertEqual(plan.components_after, 1)

    def test_max_span_decides_what_stays_separate(self):
        islands = [self.square(0.0, 0.0), self.square(10.0, 0.0)]   # 6 mm apart
        self.assertEqual(plan_bridges(islands,
                                      BridgeParams(max_span_mm=5.0)).bridges, [])
        self.assertEqual(len(plan_bridges(
            islands, BridgeParams(max_span_mm=8.0)).bridges), 1)

    def test_indices_are_into_the_callers_list(self):
        # None/empty entries are skipped, but must not shift the numbering —
        # a caller looks its islands back up by these.
        islands = [None, self.square(0.0, 0.0), Polygon(),
                   self.square(6.0, 0.0)]
        plan = plan_bridges(islands)
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual({plan.bridges[0].a, plan.bridges[0].b}, {1, 3})

    def test_fewer_than_two_islands_plans_nothing(self):
        self.assertEqual(plan_bridges([]).bridges, [])
        self.assertEqual(plan_bridges([self.square(0.0, 0.0)]).bridges, [])


class TestWhereTheBridgeGoes(BridgeTestCase):
    """The scorer: span first, but not span alone."""

    def test_the_shortest_square_crossing_is_taken(self):
        plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)])
        bridge = plan.bridges[0]
        self.assertAlmostEqual(bridge.span_mm, 2.0, places=6)
        self.assertAlmostEqual(bridge.incidence_deg, 90.0, places=6)
        self.assertAlmostEqual(bridge.anchor, 1.0, places=6)
        self.assertAlmostEqual(bridge.cost_mm, 2.0, places=6)

    def test_the_bridge_is_the_width_it_was_asked_for(self):
        for width_mm in (0.4, 1.0, 2.5):
            plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)],
                                BridgeParams(width_mm=width_mm))
            bridge, params = plan.bridges[0], plan.params
            # The crossing, plus the semicircle each end finishes in.
            cap_mm2 = params.cap_area_m2 / params.mpm ** 2
            expected = width_mm * 2.0 + 2.0 * cap_mm2
            self.assertAlmostEqual(
                bridge.polygon.area / params.mpm ** 2, expected, places=4)
            self.assertAlmostEqual(bridge.width_mm, width_mm, places=6)
            # And nowhere wider than that: the caps are tangent to the sides.
            # This crossing runs east-west, so the width is the y extent.
            minx, miny, maxx, maxy = bridge.polygon.bounds
            self.assertAlmostEqual((maxy - miny) / params.mpm, width_mm,
                                   places=6)

    def test_land_added_counts_only_the_water_covered(self):
        # The embedded ends sit on land that was already there.
        plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)],
                            BridgeParams(width_mm=1.0))
        self.assertAlmostEqual(plan.land_added_mm2, 2.0, places=4)

    def test_a_landing_on_a_spit_is_refused_for_one_on_the_body(self):
        # The spit tip is the nearest land (2.1 mm) but a 1 mm bridge welds to
        # a 0.1 mm needle there; the body edge is 4 mm away and solid.
        spit = Polygon([(0, 0), (4 * self.MM, 0),
                        (4 * self.MM, 1.95 * self.MM), (5.9 * self.MM, 1.95 * self.MM),
                        (5.9 * self.MM, 2.05 * self.MM), (4 * self.MM, 2.05 * self.MM),
                        (4 * self.MM, 4 * self.MM), (0, 4 * self.MM)])
        neighbour = self.square(8.0, 0.0)
        # Sampled finely enough that the tip is genuinely on offer.
        params = BridgeParams(sample_mm=0.02, width_mm=1.0)
        bridge = plan_bridges([spit, neighbour], params).bridges[0]
        self.assertAlmostEqual(bridge.span_mm, 4.0, places=2)
        self.assertGreaterEqual(bridge.anchor, params.min_anchor)

        # With the floor removed, the needle wins on span alone — which is the
        # whole reason the floor is there.
        cheap = plan_bridges([spit, neighbour],
                             BridgeParams(sample_mm=0.02, min_anchor=0.0))
        self.assertAlmostEqual(cheap.bridges[0].span_mm, 2.1, places=2)
        self.assertLess(cheap.bridges[0].anchor, 0.2)

    def test_a_grazing_landing_loses_to_a_squarer_one(self):
        # A staircase coast offers a short grazing crossing off its step and a
        # slightly longer square one off its face.
        graze = Polygon([(0, 0), (6 * self.MM, 0), (6 * self.MM, 1 * self.MM),
                         (2 * self.MM, 1 * self.MM), (2 * self.MM, 6 * self.MM),
                         (0, 6 * self.MM)])
        neighbour = box(8 * self.MM, 0, 12 * self.MM, 6 * self.MM)
        bridge = plan_bridges([graze, neighbour]).bridges[0]
        self.assertGreater(bridge.incidence_deg, 60.0)

    def test_the_penalties_can_be_switched_off(self):
        # With both weights at zero the scorer is pure span, so the bound the
        # search relies on collapses to the shortest crossing.
        islands = [self.square(0.0, 0.0), self.square(6.0, 0.0)]
        plan = plan_bridges(islands, BridgeParams(angle_penalty_mm=0.0,
                                                  anchor_penalty_mm=0.0))
        self.assertAlmostEqual(plan.bridges[0].cost_mm,
                               plan.bridges[0].span_mm, places=6)

    def test_zero_width_plans_nothing(self):
        plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)],
                            BridgeParams(width_mm=0.0))
        self.assertEqual(plan.bridges, [])


class TestTheEndCaps(BridgeTestCase):
    """Each end is a semicircle on the landing, and it stops at the coast.

    The cap is half the bridge's width, centred on the landing and tangent to
    the sides, so it reaches half a width past the coastline into the land --
    which is what makes the union a joint rather than a kiss.  It assumes there
    is land under it, and an island thinner than half a bridge has not got any:
    the end then comes out of the far coast and stands a tab of bridge in open
    water, on the far side of an island the crossing had already reached.

    What comes off is decided by *severance* -- water the cap would fill but
    can only reach through land -- and never by trimming the cap to the shape
    of the coast.  ``TestARaggedCoast`` is the half of this that a straight
    fixture cannot state.

    Half of the default 1 mm bridge is 4 km of ground at 1:8,000,000, so the
    fixtures below are built either side of 0.5 mm.  The numbers are print-mm.
    """

    SPAN_MM = 2.0            # water between the body and the island it bridges

    def rect(self, x0, y0, x1, y1):
        return box(x0 * self.MM, y0 * self.MM, x1 * self.MM, y1 * self.MM)

    def body(self):
        return self.rect(0, 0, 10, 10)

    def island(self, thickness_mm):
        """An island of the given thickness, ``SPAN_MM`` east of the body."""
        near = 10 + self.SPAN_MM
        return self.rect(near, 0, near + thickness_mm, 10)

    def one_bridge(self, islands, **kw):
        """The plan, forced to a single bridge so the brace cannot confuse an
        area."""
        kw.setdefault("max_per_pair", 1)
        plan = plan_bridges(islands, BridgeParams(**kw))
        self.assertEqual(len(plan.bridges), 1)
        return plan

    def added_mm2(self, plan, islands):
        """Land the bridges put down that was not already there, in mm²."""
        land = unary_union(list(islands))
        slabs = unary_union([b.polygon for b in plan.bridges])
        return slabs.difference(land).area / self.MM ** 2

    def test_a_bridge_adds_only_the_water_it_crosses(self):
        # Half a width is 0.5 mm here, so the first two are islands too thin to
        # take the joint and the rest swallow it whole.
        for thickness in (0.25, 0.35, 0.5, 1.0, 6.0):
            with self.subTest(island_mm=thickness):
                islands = [self.body(), self.island(thickness)]
                plan = self.one_bridge(islands, width_mm=1.0)
                self.assertAlmostEqual(
                    self.added_mm2(plan, islands),
                    plan.bridges[0].width_mm * self.SPAN_MM, places=3)

    def test_the_end_does_not_come_out_of_the_far_coast(self):
        # The defect, stated in the one way it shows on the map: land east of
        # an island whose east coast is the last land there was.
        islands = [self.body(), self.island(0.3)]
        plan = self.one_bridge(islands, width_mm=1.0)
        east_coast = islands[1].bounds[2]
        self.assertLessEqual(plan.bridges[0].polygon.bounds[2],
                             east_coast + 1e-6)
        merged = apply_bridges(islands, plan.bridges)
        self.assertLessEqual(merged.bounds[2], east_coast + 1e-6)

    def test_the_joint_is_left_alone_where_there_is_land_to_make_it(self):
        # Trimming is not a change of policy: an island thick enough to hold
        # the cap keeps every bit of it, so the weld is as deep as it was.
        params = BridgeParams(width_mm=1.0, max_per_pair=1)
        islands = [self.body(), self.island(6.0)]
        plan = plan_bridges(islands, params)
        cap_mm2 = params.cap_area_m2 / params.mpm ** 2
        expected = params.width_mm * self.SPAN_MM + 2 * cap_mm2
        self.assertAlmostEqual(
            plan.bridges[0].polygon.area / params.mpm ** 2, expected, places=3)

    def test_a_thin_island_is_still_joined(self):
        # The trim must take the tab and leave the joint: what is cut away was
        # over water, so the weld cannot depend on it.
        for thickness in (0.25, 0.35):
            with self.subTest(island_mm=thickness):
                islands = [self.body(), self.island(thickness)]
                plan = self.one_bridge(islands, width_mm=1.0)
                self.assertEqual(plan.components_after, 1)
                merged = apply_bridges(islands, plan.bridges)
                self.assertEqual(len(_polygons(merged)), 1)

    def test_an_island_too_thin_to_hold_the_joint_is_refused(self):
        # Where this stops being a trim and becomes a refusal. The anchorage
        # floor asks for half the cap, and a straight-sided island 0.2 mm thick
        # can bury 49.5% of a 0.5 mm one -- so the landing is turned away
        # rather than welded on by a sliver and cut back to it.
        plan = plan_bridges([self.body(), self.island(0.2)],
                            BridgeParams(width_mm=1.0, max_per_pair=1))
        self.assertEqual(plan.bridges, [])
        self.assertEqual(plan.pairs_without_landing, 1)
        self.assertEqual(plan.pairs_blocked, 0)     # nothing was in the way

    def test_the_plan_does_not_overstate_what_it_adds(self):
        # ``land_added_mm2`` is width x span by construction; on a thin island
        # the untrimmed slab laid down more than that and said otherwise.
        islands = [self.body(), self.island(0.3)]
        plan = self.one_bridge(islands, width_mm=1.0)
        self.assertAlmostEqual(plan.land_added_mm2,
                               self.added_mm2(plan, islands), places=3)

    def test_a_landing_on_a_long_thin_arm_does_not_sprout_a_tab(self):
        # Not every thin landing is a whole islet. This one is a 0.4 mm
        # peninsula, 20 mm of it, joined to its island only at the far north
        # end -- far enough that the scorer cannot buy a better anchorage by
        # landing near the join, the way it can on a short spit. So every
        # landing in reach has 0.4 mm of land and then open water behind it,
        # against a cap that reaches 0.5 mm.
        arm = self.rect(12, 0, 12.4, 20)
        island = unary_union([arm, self.rect(12, 20, 24, 30)])
        islands = [self.rect(0, 0, 10, 10), island]
        plan = self.one_bridge(islands, width_mm=1.0)
        landing = plan.bridges[0]
        self.assertAlmostEqual(landing.span_mm, self.SPAN_MM, places=3)
        self.assertLess(landing.anchor, 1.0)        # it did land on the thin arm
        self.assertAlmostEqual(self.added_mm2(plan, islands),
                               1.0 * self.SPAN_MM, places=3)
        # ... and the water east of the arm is still water.
        self.assertLessEqual(landing.polygon.bounds[2], 12.4 * self.MM + 1e-6)

    def test_the_end_is_a_semicircle_centred_on_the_landing(self):
        # The shape itself, asked of the outline: every point of the slab's
        # boundary beyond the landing is half a width from it, and none is
        # further -- which is what "a semicircle around the anchor point"
        # means, and what keeps the bridge inside the width it was given.
        islands = [self.body(), self.island(6.0)]
        plan = self.one_bridge(islands, width_mm=1.0)
        bridge = plan.bridges[0]
        radius = plan.params.cap_m
        landing = Point(bridge.line.coords[0])
        beyond = [Point(xy) for xy in bridge.polygon.exterior.coords
                  if xy[0] < landing.x - 1e-6]          # west of the landing
        self.assertGreater(len(beyond), 8, "the cap should be a curve")
        for point in beyond:
            self.assertAlmostEqual(point.distance(landing) / radius, 1.0,
                                   places=2)

    def test_the_trim_does_not_move_the_bridge(self):
        # Same landing, same span, same anchorage as before it was cut back:
        # the shape changes, the decision does not.
        islands = [self.body(), self.island(0.3)]
        plan = self.one_bridge(islands, width_mm=1.0)
        bridge = plan.bridges[0]
        self.assertAlmostEqual(bridge.span_mm, self.SPAN_MM, places=3)
        # 0.3 mm of a 0.5 mm cap: the segment of the half-disc within the
        # island, over the whole half-disc.
        self.assertGreater(bridge.anchor, plan.params.min_anchor)
        self.assertLess(bridge.anchor, 1.0)
        self.assertTrue(bridge.polygon.contains(bridge.line.centroid))


class TestARaggedCoast(BridgeTestCase):
    """A real coastline turns inside the cap, and the cap survives it.

    This is the case a straight-edged fixture cannot show, and the one that
    went wrong.  Trimming each cap back to the land it lay on is identical to
    leaving it alone on a straight shore, and on a coast that turns every few
    hundred metres it hands back a comb: on British Columbia's largest forty
    islands that rule left **27 of 63 bridges in more than one piece and 21
    with holes in them**, and gnawed something off every single one.  Deciding
    by severance instead leaves 52 of the 63 untouched, none in pieces, none
    holed.

    A bridge is a rectangle with a semicircle on each end.  A notch beside its
    landing holds the same water it is already crossing, so filling it is the
    joint's fillet and not an invention; a pocket the land closes off is
    somewhere else, and stays water.
    """

    #: A notched coast, shallow enough that a landing on it still anchors:
    #: 0.15 mm is 1.2 km of ground, and the cap that spans three of them is
    #: 0.5 mm.
    NOTCH_MM = 0.15

    def comb(self):
        """An island whose west coast is notched all the way up."""
        body = box(12 * self.MM, 0, 22 * self.MM, 10 * self.MM)
        notches = [
            box(12 * self.MM, (i * 2 * self.NOTCH_MM) * self.MM,
                (12 + self.NOTCH_MM) * self.MM,
                (i * 2 * self.NOTCH_MM + self.NOTCH_MM) * self.MM)
            for i in range(int(10 / (2 * self.NOTCH_MM)))]
        return body.difference(unary_union(notches))

    def islands(self):
        return [box(0, 0, 10 * self.MM, 10 * self.MM), self.comb()]

    def plan(self, islands=None):
        return plan_bridges(islands or self.islands(),
                            BridgeParams(width_mm=1.0, max_per_pair=1))

    def test_the_bridge_is_still_the_capsule_it_was_built_as(self):
        plan = self.plan()
        bridge = plan.bridges[0]
        capsule = bridge.line.buffer(plan.params.width_m / 2.0,
                                     cap_style="round")
        self.assertAlmostEqual(bridge.polygon.area / capsule.area, 1.0,
                               places=6)

    def test_it_is_one_piece_with_no_holes(self):
        # The comb is what tore it into several and punched holes in it.
        parts = _polygons(self.plan().bridges[0].polygon)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0].interiors), 0)

    def test_the_notches_under_the_cap_are_filled(self):
        # They open on the water the bridge is crossing, so they are part of
        # the joint rather than somewhere else.
        islands = self.islands()
        bridge = self.plan(islands).bridges[0]
        filled = bridge.polygon.difference(unary_union(islands))
        self.assertGreater(filled.area / self.MM ** 2, bridge.area_mm2 + 0.01,
                           "the cap should have filled some notches")

    def test_and_a_pocket_the_land_closes_off_is_not(self):
        # The other half of the rule: a lake inside the island, under the cap,
        # is water the crossing cannot reach without going through land. The
        # bridge leaves it alone and keeps the hole.
        #
        # The west island is a 1 mm bar, so the landing is forced to the one
        # place its own cap is buried whole -- the midline -- and the lake can
        # be put where the far cap will find it.
        west = box(0, 4.5 * self.MM, 10 * self.MM, 5.5 * self.MM)
        lake = box(12.15 * self.MM, 4.85 * self.MM,
                   12.35 * self.MM, 5.15 * self.MM)
        east = box(12 * self.MM, 0, 22 * self.MM, 10 * self.MM).difference(lake)
        bridge = self.plan([west, east]).bridges[0]
        holes = [ring for part in _polygons(bridge.polygon)
                 for ring in part.interiors]
        self.assertEqual(len(holes), 1, "the lake should still be a hole")
        self.assertAlmostEqual(Polygon(holes[0]).area / lake.area, 1.0,
                               places=3)


class TestABridgeThroughAnIsland(BridgeTestCase):
    """No bridge passes through an island and out the other side.

    Two ways that can happen, and they are guarded in two different places.  A
    crossing laid *across* a third island is refused when the landing is scored
    (see :class:`TestThirdIslands` and :class:`TestObstacles`); an end driven
    *through* the island it lands on is trimmed once the bridge is chosen (see
    :class:`TestTheEndCaps`).  These state the shared claim end to end, on
    the arrangement that provokes both at once: three islands in a line, the
    middle one thin.
    """

    def line_of_three(self, middle_mm=0.6):
        west = box(0, 0, 10 * self.MM, 10 * self.MM)
        middle = box(12 * self.MM, 0, (12 + middle_mm) * self.MM, 10 * self.MM)
        east = box((14 + middle_mm) * self.MM, 0,
                   (24 + middle_mm) * self.MM, 10 * self.MM)
        return [west, middle, east]

    def test_the_far_island_is_reached_by_hopping_not_by_spearing(self):
        islands = self.line_of_three()
        plan = plan_bridges(islands, BridgeParams(max_per_pair=1))
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual({(b.a, b.b) for b in plan.bridges}, {(0, 1), (1, 2)})

    def test_no_bridge_lays_land_on_an_island_it_does_not_join(self):
        islands = self.line_of_three()
        plan = plan_bridges(islands, BridgeParams(max_per_pair=1))
        for bridge in plan.bridges:
            for index, island in enumerate(islands):
                if index in (bridge.a, bridge.b):
                    continue
                self.assertFalse(bridge.polygon.intersects(island))

    def test_the_water_either_side_of_the_thin_island_stays_water(self):
        # The heart of it: every bridge ends at a coast. Sum the land, and the
        # three islands plus two crossings is all there is.
        islands = self.line_of_three()
        plan = plan_bridges(islands, BridgeParams(max_per_pair=1))
        merged = apply_bridges(islands, plan.bridges)
        expected = (sum(g.area for g in islands) / self.MM ** 2
                    + plan.land_added_mm2)
        self.assertAlmostEqual(merged.area / self.MM ** 2, expected, places=3)

    def test_an_unselected_thin_island_is_not_speared_either(self):
        # The same line, but the middle island is not part of the job: there is
        # then no crossing at all, rather than one driven through it.
        west, middle, east = self.line_of_three()
        plan = plan_bridges([west, east], BridgeParams(max_per_pair=1),
                            obstacles=[middle])
        self.assertEqual(plan.bridges, [])
        self.assertEqual(plan.pairs_blocked, 1)


class TestThirdIslands(BridgeTestCase):
    """A bridge joins the two islands it is an edge between, and no others."""

    def archipelago(self):
        """Nine islands on a grid, close enough that crossings are possible."""
        return [self.square(x * 6.0, y * 6.0, 4.0)
                for y in range(3) for x in range(3)]

    def test_no_bridge_touches_an_island_it_does_not_join(self):
        # The invariant the spanning tree depends on: one edge, two landmasses.
        islands = self.archipelago()
        plan = plan_bridges(islands)
        self.assertTrue(plan.bridges)
        for bridge in plan.bridges:
            for index, island in enumerate(islands):
                if index in (bridge.a, bridge.b):
                    continue
                self.assertFalse(
                    bridge.polygon.intersects(island),
                    f"bridge {bridge.a}-{bridge.b} also lands on island {index}")

    def test_the_plan_predicts_the_applied_landmass_count(self):
        # If a slab could clip a third island the count would drift, and the
        # plan would be describing a different result from the one you get.
        islands = self.archipelago()
        islands.append(self.square(100.0, 100.0))       # far out of reach
        geom, plan = bridge_islands(islands)
        self.assertEqual(plan.components_after, len(_polygons(geom)))

    def test_a_blocked_crossing_is_refused_not_scored(self):
        # Asked directly: the same pair, with and without something in the way.
        from lib.LIB_island_bridges import _candidates
        a = box(0, 0, 4 * self.MM, 20 * self.MM)
        b = box(6 * self.MM, 0, 10 * self.MM, 20 * self.MM)
        blocker = box(4.5 * self.MM, 0, 5.5 * self.MM, 20 * self.MM)
        params = BridgeParams()
        self.assertTrue(_candidates(0, 1, a, b, [], params))
        self.assertEqual(_candidates(0, 1, a, b, [blocker], params), [])


class TestObstacles(BridgeTestCase):
    """Islands to avoid but not to connect — an editor bridging a selection.

    Without these the third-island rule sees only what it was asked to bridge,
    so a bridge between two selected islands can be run straight across an
    unselected one and the plan disagrees with the document it lands in.
    """

    def strait(self):
        """Two islands 2 mm apart, with an islet sitting in the water."""
        return (self.square(0.0, 0.0), self.square(6.0, 0.0),
                box(4.5 * self.MM, 0, 5.5 * self.MM, 4 * self.MM))

    def test_a_bridge_will_not_cross_an_obstacle(self):
        a, b, islet = self.strait()
        self.assertEqual(len(plan_bridges([a, b]).bridges), 1)
        self.assertEqual(plan_bridges([a, b], obstacles=[islet]).bridges, [])

    def test_an_obstacle_out_of_the_way_changes_nothing(self):
        a, b, _ = self.strait()
        far = self.square(50.0, 0.0)
        plan = plan_bridges([a, b], obstacles=[far])
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(plan.pairs_blocked, 0)

    def test_an_obstacle_is_never_a_landmass(self):
        # It is scenery, not a node: it cannot be bridged to, and it must not
        # inflate the counts the caller reports.
        a, b, islet = self.strait()
        plan = plan_bridges([a, b], obstacles=[islet])
        self.assertEqual(plan.islands, 2)
        self.assertEqual(plan.components_before, 2)

    def test_being_blocked_is_reported_as_such(self):
        # "No landing was good enough" is useless advice; "something is in the
        # way" tells the user what to do about it.
        a, b, islet = self.strait()
        plan = plan_bridges([a, b], obstacles=[islet])
        self.assertEqual(plan.pairs_in_reach, 1)
        self.assertEqual(plan.pairs_without_landing, 1)
        self.assertEqual(plan.pairs_blocked, 1)

    def test_a_failure_to_anchor_is_not_reported_as_blocked(self):
        # The other reason a pair gets nothing, and it wants the opposite fix.
        a = self.square(0.0, 0.0)
        tiny = box(6 * self.MM, 1.9 * self.MM, 6.2 * self.MM, 2.1 * self.MM)
        plan = plan_bridges([a, tiny])
        self.assertEqual(plan.bridges, [])
        self.assertEqual(plan.pairs_without_landing, 1)
        self.assertEqual(plan.pairs_blocked, 0)

    def test_an_obstacle_can_be_routed_around(self):
        # Blocking the shortest crossing is not the same as blocking the pair:
        # if the scorer can go round, it should.
        a = box(0, 0, 4 * self.MM, 20 * self.MM)
        b = box(6 * self.MM, 0, 10 * self.MM, 20 * self.MM)
        plug = box(4.5 * self.MM, 8 * self.MM, 5.5 * self.MM, 12 * self.MM)
        plan = plan_bridges([a, b], obstacles=[plug])
        self.assertTrue(plan.bridges)           # bars this long are braced too
        self.assertEqual(plan.components_after, 1)
        for bridge in plan.bridges:
            self.assertFalse(bridge.polygon.intersects(plug))

    def test_no_obstacles_is_the_same_as_an_empty_list(self):
        a, b, _ = self.strait()
        self.assertEqual(len(plan_bridges([a, b], obstacles=[]).bridges),
                         len(plan_bridges([a, b]).bridges))
        self.assertEqual(len(plan_bridges([a, b], obstacles=None).bridges), 1)

    def test_empty_entries_among_the_obstacles_are_ignored(self):
        a, b, islet = self.strait()
        plan = plan_bridges([a, b], obstacles=[None, Polygon(), islet])
        self.assertEqual(plan.bridges, [])


class TestWhereTheExtraBridgesGo(BridgeTestCase):
    """Given that a join is owed two, the second one needs a baseline.

    How many a join gets is the mass rule's answer (``TestTheMassRule``);
    everything here is about the other half, which is still a rigidity
    argument: two bridges an inch apart resist a pivot, two side by side are
    one wider bridge with a slot in it.  So the fixtures below ask for two
    outright, by telling the mass rule that a bar's worth of land is enough for
    two, and then look at where they went.
    """

    def bars(self):
        """Two 40 mm bars facing each other across 2 mm — a hinge."""
        return (box(0, 0, 40 * self.MM, 4 * self.MM),
                box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM))

    def braced(self, **kw):
        """The bars, with the mass rule asking for two. Each bar is 160 mm²."""
        kw.setdefault("mass_per_bridge_mm2", 100.0)
        return plan_bridges(list(self.bars()), BridgeParams(**kw))

    def test_a_long_facing_pair_gets_its_second_bridge(self):
        plan = self.braced()
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual([b.rank for b in sorted(plan.bridges,
                                                 key=lambda b: b.rank)], [0, 1])
        self.assertEqual(plan.joins_short_of_support, 0)

    def test_the_second_is_placed_for_the_longest_baseline(self):
        plan = self.braced()
        xs = sorted(b.line.coords[0][0] / self.MM for b in plan.bridges)
        # One near each end of the 40 mm facing span, not two in the middle.
        self.assertLess(xs[0], 5.0)
        self.assertGreater(xs[1], 35.0)

    def test_a_third_does_not_pile_in_beside_the_second(self):
        # Asked for three, the spacing is measured to the nearest bridge
        # already placed, so they spread along the baseline instead of
        # clustering at the two ends that scored best first.
        plan = self.braced(mass_per_bridge_mm2=60.0, max_per_pair=3)
        xs = sorted(b.line.coords[0][0] / self.MM for b in plan.bridges)
        self.assertEqual(len(xs), 3)
        self.assertGreater(min(b - a for a, b in zip(xs, xs[1:])), 5.0)

    def test_a_pair_meeting_at_a_point_keeps_its_single_bridge(self):
        # Facing span 4 mm against a 2 mm crossing is below hinge_ratio, so
        # there is nowhere to put a second however heavy the branch. The fix is
        # a wider bridge, and the plan says so by counting the join.
        plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)],
                            BridgeParams(mass_per_bridge_mm2=1.0))
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(plan.bridges[0].rank, 0)
        self.assertEqual(plan.joins_short_of_support, 1)

    def test_a_second_bridge_can_be_switched_off(self):
        plan = self.braced(max_per_pair=1)
        self.assertEqual(len(plan.bridges), 1)

    def test_a_higher_hinge_ratio_demands_a_longer_facing_span(self):
        plan = self.braced(hinge_ratio=50.0)
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(plan.joins_short_of_support, 1)

    def test_a_second_bridge_does_not_change_connectivity(self):
        self.assertEqual(self.braced().components_after, 1)


class TestTheMassRule(BridgeTestCase):
    """How many bridges a join gets is decided by what it holds up.

    Cut one edge of the spanning tree and the map falls in two; the lighter
    half is the branch that edge carries, and its printed area is the mass on
    that join.  The point of asking the *graph* is that the answer is not in
    the pair: the same two islets are a trinket on one map and the hinge of an
    archipelago on another, and a rule that looks only at the two shores in
    front of it cannot tell those apart.

    Areas here are print-mm², so a 10 mm square is 100 mm² of branch.
    """

    def bar(self, x_mm, y_mm, w_mm, h_mm):
        return box(x_mm * self.MM, y_mm * self.MM,
                   (x_mm + w_mm) * self.MM, (y_mm + h_mm) * self.MM)

    def facing(self, mass_per_bridge_mm2, west_mm=40.0, east_mm=40.0):
        """Two bars 2 mm apart, long enough to take a second bridge."""
        islands = [self.bar(0, 0, west_mm, 4), self.bar(0, 6, east_mm, 4)]
        return plan_bridges(islands, BridgeParams(
            mass_per_bridge_mm2=mass_per_bridge_mm2))

    def test_a_light_branch_gets_one_bridge(self):
        # 160 mm² of bar against 400 mm² a bridge: one is enough.
        self.assertEqual(len(self.facing(400.0).bridges), 1)

    def test_a_heavy_branch_gets_another(self):
        self.assertEqual(len(self.facing(150.0).bridges), 2)

    def test_and_the_threshold_is_where_it_turns(self):
        # 160 mm² of branch: one bridge at 160, two at just under.
        self.assertEqual(len(self.facing(160.0).bridges), 1)
        self.assertEqual(len(self.facing(159.0).bridges), 2)

    def test_every_bridge_of_a_join_carries_the_same_figure(self):
        plan = self.facing(150.0)
        self.assertEqual(len({round(b.holds_mm2, 6) for b in plan.bridges}), 1)
        self.assertAlmostEqual(plan.bridges[0].holds_mm2, 160.0, places=3)

    def test_the_branch_is_the_lighter_side_not_the_pair(self):
        # A 4 mm islet hung off a 40 mm mainland. The join holds the islet;
        # a rule that weighed the pair, or the mainland, would see 1,616 mm².
        islands = [self.bar(0, 0, 40, 40), self.bar(42, 18, 4, 4)]
        plan = plan_bridges(islands, BridgeParams())
        self.assertAlmostEqual(plan.bridges[0].holds_mm2, 16.0, places=3)

    def test_a_join_in_the_middle_of_a_chain_holds_more_than_one_at_the_end(self):
        # Four islands in a row: the end join holds one island, the middle
        # join holds two. Same shapes, same gaps, different loads.
        islands = [self.bar(i * 6.0, 0, 4, 4) for i in range(4)]
        plan = plan_bridges(islands, BridgeParams())
        holds = sorted(round(b.holds_mm2, 3) for b in plan.bridges)
        self.assertEqual(holds, [16.0, 16.0, 32.0])

    def test_islands_that_already_touch_are_one_load(self):
        # The branch is held on by whatever holds it on, so a pair that touches
        # counts as a single lump on the bridge beyond it -- 32 mm², not 16.
        islands = [self.bar(0, 0, 40, 40),
                   self.bar(42, 18, 4, 4), self.bar(46, 18, 4, 4)]
        plan = plan_bridges(islands, BridgeParams())
        self.assertEqual(len(plan.bridges), 1)      # the touching pair is one
        self.assertAlmostEqual(plan.bridges[0].holds_mm2, 32.0, places=3)

    def test_the_cap_still_caps_it(self):
        plan = self.facing(1.0)                     # 160 bridges' worth of mass
        self.assertEqual(len(plan.bridges), 2)      # max_per_pair is 2

    def test_a_deeper_cap_lets_the_mass_ask_for_more(self):
        islands = [self.bar(0, 0, 40, 4), self.bar(0, 6, 40, 4)]
        plan = plan_bridges(islands, BridgeParams(mass_per_bridge_mm2=60.0,
                                                  max_per_pair=3))
        self.assertEqual(len(plan.bridges), 3)

    def test_switching_the_rule_off_puts_every_join_back_on_one(self):
        self.assertEqual(len(self.facing(0.0).bridges), 1)

    def test_the_heaviest_branch_is_reported(self):
        islands = [self.bar(i * 6.0, 0, 4, 4) for i in range(4)]
        plan = plan_bridges(islands, BridgeParams())
        self.assertAlmostEqual(plan.heaviest_mm2, 32.0, places=3)

    def test_mass_is_printed_area_so_it_moves_with_the_scale(self):
        # The same ground at half the reduction is four times the printed area,
        # so the same branch asks for a second bridge.
        islands = [self.bar(0, 0, 40, 4), self.bar(0, 6, 40, 4)]
        near = BridgeParams(scale=4_000_000, mass_per_bridge_mm2=400.0)
        far = BridgeParams(scale=8_000_000, mass_per_bridge_mm2=400.0)
        self.assertEqual(len(plan_bridges(islands, far).bridges), 1)
        self.assertEqual(len(plan_bridges(islands, near).bridges), 2)


class TestSlenderness(BridgeTestCase):
    """A matchstick is reported, not silently doubled or silently dropped."""

    def pair(self):
        return [self.square(0.0, 0.0), self.square(16.0, 0.0)]   # 12 mm apart

    def test_a_long_thin_bridge_is_flagged(self):
        # 12 mm over a 1 mm bridge: the aspect is span over width.
        params = BridgeParams(max_span_mm=20.0, max_aspect=10.0, width_mm=1.0)
        bridge = plan_bridges(self.pair(), params).bridges[0]
        self.assertTrue(bridge.slender)
        self.assertAlmostEqual(bridge.aspect, 12.0, places=4)

    def test_flagged_is_still_placed_by_default(self):
        plan = plan_bridges(self.pair(),
                            BridgeParams(max_span_mm=20.0, max_aspect=10.0))
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(len(plan.slender), 1)
        self.assertEqual(plan.components_after, 1)

    def test_dropping_it_disconnects_the_island(self):
        plan = plan_bridges(self.pair(), BridgeParams(
            max_span_mm=20.0, max_aspect=10.0, drop_slender=True))
        self.assertEqual(plan.bridges, [])
        self.assertEqual(plan.components_after, 2)
        self.assertEqual(plan.pairs_without_landing, 1)

    def test_a_wider_bridge_is_the_fix(self):
        # Same 12 mm crossing, 2 mm wide: aspect 6, under the limit.
        plan = plan_bridges(self.pair(), BridgeParams(
            max_span_mm=20.0, max_aspect=10.0, width_mm=2.0))
        self.assertFalse(plan.bridges[0].slender)


class TestApplying(BridgeTestCase):
    """Unioning the slabs on, and what happens to the water they enclose."""

    def c_shape(self):
        """An island with two arms reaching towards a bar 2 mm away."""
        arms = Polygon([(0, 0), (10 * self.MM, 0), (10 * self.MM, 2 * self.MM),
                        (2 * self.MM, 2 * self.MM), (2 * self.MM, 8 * self.MM),
                        (10 * self.MM, 8 * self.MM), (10 * self.MM, 10 * self.MM),
                        (0, 10 * self.MM)])
        return arms, box(12 * self.MM, 0, 16 * self.MM, 10 * self.MM)

    def test_a_strait_closed_by_one_bridge_is_filled(self):
        # One bridge across the mouth of the C traps the bay behind it; that
        # water is an accident of the bridge and is filled.
        arms, far = self.c_shape()
        geom, plan = bridge_islands([arms, far], BridgeParams(max_per_pair=1))
        self.assertEqual(len(plan.bridges), 1)
        for part in _polygons(geom):
            self.assertEqual(len(part.interiors), 0)

    def test_two_arms_are_each_held(self):
        # Welding only one arm of a C leaves the other free to pivot, so a join
        # owed two bridges puts one on each arm, and the water between them is
        # a strait that was there all along. The bar is 40 mm², so this asks
        # for the second outright rather than leaning on the default.
        arms, far = self.c_shape()
        geom, plan = bridge_islands([arms, far],
                                    BridgeParams(mass_per_bridge_mm2=20.0))
        self.assertEqual(len(plan.bridges), 2)
        ys = sorted(b.line.coords[0][1] / self.MM for b in plan.bridges)
        self.assertLess(ys[0], 2.0)        # the lower arm
        self.assertGreater(ys[1], 8.0)     # the upper one
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 1)

    def test_the_lagoon_between_two_bridges_is_kept(self):
        # Two 160 mm² bars, told that 100 mm² is a bridge's worth, so the join
        # is owed two and the water they enclose is a real lagoon.
        a = box(0, 0, 40 * self.MM, 4 * self.MM)
        b = box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM)
        geom, plan = bridge_islands([a, b],
                                    BridgeParams(mass_per_bridge_mm2=100.0))
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 1)

    def test_the_lagoon_can_be_filled_on_request(self):
        # The same pair, the same two bridges, the lagoon filled instead.
        a = box(0, 0, 40 * self.MM, 4 * self.MM)
        b = box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM)
        plan = plan_bridges([a, b], BridgeParams(mass_per_bridge_mm2=100.0))
        geom = apply_bridges([a, b], plan, keep_lagoons=False)
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 0)

    def test_a_real_lake_survives_bridging(self):
        lake = Polygon(box(1 * self.MM, 1 * self.MM,
                           3 * self.MM, 3 * self.MM).exterior)
        holed = Polygon(box(0, 0, 4 * self.MM, 4 * self.MM).exterior,
                        [lake.exterior])
        plan = plan_bridges([holed, self.square(6.0, 0.0)])
        geom = apply_bridges([holed, self.square(6.0, 0.0)], plan)
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 1)

    def test_an_empty_plan_still_returns_the_islands(self):
        islands = [self.square(0.0, 0.0), self.square(100.0, 0.0)]
        geom = apply_bridges(islands, [])
        self.assertEqual(len(_polygons(geom)), 2)

    def test_apply_accepts_a_plan_or_a_list(self):
        islands = [self.square(0.0, 0.0), self.square(6.0, 0.0)]
        plan = plan_bridges(islands)
        from_plan = apply_bridges(islands, plan)
        from_list = apply_bridges(islands, plan.bridges)
        self.assertAlmostEqual(from_plan.area, from_list.area, places=6)

    def test_nothing_to_bridge_returns_none(self):
        self.assertIsNone(apply_bridges([], []))

    def test_the_input_islands_are_not_modified(self):
        islands = [self.square(0.0, 0.0), self.square(6.0, 0.0)]
        before = [g.wkt for g in islands]
        bridge_islands(islands)
        self.assertEqual([g.wkt for g in islands], before)


class TestParams(BridgeTestCase):
    """The print-mm to metres conversion every parameter goes through."""

    def test_metres_per_print_mm(self):
        self.assertAlmostEqual(BridgeParams(scale=8_000_000).mpm, 8000.0)
        self.assertAlmostEqual(BridgeParams(scale=4_000_000).mpm, 4000.0)

    def test_lengths_convert_through_the_scale(self):
        params = BridgeParams(scale=8_000_000, width_mm=1.0,
                              max_span_mm=5.0, sample_mm=0.25)
        self.assertAlmostEqual(params.width_m, 8000.0)
        self.assertAlmostEqual(params.max_span_m, 40000.0)
        self.assertAlmostEqual(params.sample_m, 2000.0)

    def test_the_cap_is_half_the_width_and_knows_its_own_area(self):
        params = BridgeParams(scale=8_000_000, width_mm=1.0)
        self.assertAlmostEqual(params.cap_m, 4000.0)
        # Shapely's own half-disc, not pi r^2 / 2: a landing that buries its
        # cap whole has to be able to score exactly 1.0.
        self.assertLess(params.cap_area_m2, math.pi * 4000.0 ** 2 / 2.0)
        self.assertAlmostEqual(params.cap_area_m2 / (math.pi * 4000.0 ** 2 / 2.0),
                               1.0, places=2)

    def test_the_same_bridge_at_half_the_scale_is_half_the_ground_width(self):
        islands = [self.square(0.0, 0.0), self.square(6.0, 0.0)]
        wide = plan_bridges(islands, BridgeParams(scale=8_000_000)).bridges[0]
        # At 1:4M the same islands are 4 mm apart on paper, so ask for the span
        # in ground metres instead: it must be unchanged.
        narrow = plan_bridges(islands, BridgeParams(scale=4_000_000)).bridges[0]
        self.assertAlmostEqual(wide.span_mm * 8000.0,
                               narrow.span_mm * 4000.0, places=3)

    def test_defaults_are_the_shipped_settings(self):
        # The knobs are project truths, not literals hidden in the module --
        # and built directly, the block reads no file.
        from lib.LIB_project_config import shipped
        params = BridgeParams()
        self.assertEqual(params.width_mm, shipped("bridges.width_mm"))
        self.assertGreater(params.width_mm, 0.0)
        self.assertGreater(params.max_span_mm, params.width_mm)
        self.assertGreaterEqual(params.max_per_pair, 1)

    def test_a_bare_bridge_has_sane_derived_numbers(self):
        self.assertEqual(Bridge().aspect, 0.0)
        self.assertEqual(Bridge(span_mm=6.0, width_mm=2.0).aspect, 3.0)
        self.assertEqual(Bridge(span_mm=6.0, width_mm=2.0).area_mm2, 12.0)


if __name__ == "__main__":
    unittest.main()
