"""
test_island_bridges.py

Unit tests for LIB_island_bridges.py — straight bridges of a fixed printed
width between islands.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_bridges.py -v

Two things are worth guarding here, and they are different in kind.

**How many bridges** is a graph claim: the minimum spanning tree must spend
exactly one bridge per landmass it absorbs, so a chain of four islands costs
three bridges and not the six a pairwise rule would place.  That is the claim
that makes the module more than a loop over pairs, and the one that breaks
silently — six bridges still *look* right on a map.

**Where one bridge goes** is a scoring claim: the shortest crossing is only the
starting point, and the tests below pin the three cases where the scorer must
overrule it — a landing on a spit, a grazing landing, and the pair that needs
bracing rather than a better single bridge.

**What a bridge is made of** is a third claim, and the quietest: a bridge adds
the water it crosses and not a square millimetre more.  Each end is a semicircle
of half the width, centred on the landing, and it is a lap joint — so it must
stop at the land it laps onto.  An end driven through an island thinner than
that comes out of the far coast, which is land nobody asked for and which no
count in the plan admits to.

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
        params = BridgeParams(sample_mm=0.02)
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


class TestBracingAHinge(BridgeTestCase):
    """A second bridge for a pair, and only for the reason that justifies one."""

    def bars(self):
        """Two 40 mm bars facing each other across 2 mm — a hinge."""
        return (box(0, 0, 40 * self.MM, 4 * self.MM),
                box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM))

    def test_a_long_facing_pair_is_braced(self):
        plan = plan_bridges(list(self.bars()))
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual([b.rank for b in sorted(plan.bridges,
                                                 key=lambda b: b.rank)], [0, 1])

    def test_the_brace_is_placed_for_the_longest_baseline(self):
        plan = plan_bridges(list(self.bars()))
        xs = sorted(b.line.coords[0][0] / self.MM for b in plan.bridges)
        # One near each end of the 40 mm facing span, not two in the middle.
        self.assertLess(xs[0], 5.0)
        self.assertGreater(xs[1], 35.0)

    def test_a_pair_meeting_at_a_point_is_not_braced(self):
        # Facing span 4 mm against a 2 mm crossing is below hinge_ratio.
        plan = plan_bridges([self.square(0.0, 0.0), self.square(6.0, 0.0)])
        self.assertEqual(len(plan.bridges), 1)
        self.assertEqual(plan.bridges[0].rank, 0)

    def test_bracing_can_be_switched_off(self):
        plan = plan_bridges(list(self.bars()), BridgeParams(max_per_pair=1))
        self.assertEqual(len(plan.bridges), 1)

    def test_a_higher_hinge_ratio_demands_a_longer_facing_span(self):
        plan = plan_bridges(list(self.bars()), BridgeParams(hinge_ratio=50.0))
        self.assertEqual(len(plan.bridges), 1)

    def test_a_brace_does_not_change_connectivity(self):
        plan = plan_bridges(list(self.bars()))
        self.assertEqual(plan.components_after, 1)


class TestSlenderness(BridgeTestCase):
    """A matchstick is reported, not silently doubled or silently dropped."""

    def pair(self):
        return [self.square(0.0, 0.0), self.square(16.0, 0.0)]   # 12 mm apart

    def test_a_long_thin_bridge_is_flagged(self):
        params = BridgeParams(max_span_mm=20.0, max_aspect=10.0)
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
        # Left to itself the pair braces: welding only one arm of a C leaves
        # the other free to pivot, so both get a bridge and the water between
        # them is a strait that was there all along.
        arms, far = self.c_shape()
        geom, plan = bridge_islands([arms, far])
        self.assertEqual(len(plan.bridges), 2)
        ys = sorted(b.line.coords[0][1] / self.MM for b in plan.bridges)
        self.assertLess(ys[0], 2.0)        # the lower arm
        self.assertGreater(ys[1], 8.0)     # the upper one
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 1)

    def test_the_lagoon_between_two_bridges_is_kept(self):
        a = box(0, 0, 40 * self.MM, 4 * self.MM)
        b = box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM)
        geom, plan = bridge_islands([a, b])
        self.assertEqual(len(plan.bridges), 2)
        self.assertEqual(sum(len(p.interiors) for p in _polygons(geom)), 1)

    def test_the_lagoon_can_be_filled_on_request(self):
        a = box(0, 0, 40 * self.MM, 4 * self.MM)
        b = box(0, 6 * self.MM, 40 * self.MM, 10 * self.MM)
        plan = plan_bridges([a, b])
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

    def test_defaults_come_from_the_project_config(self):
        # The knobs are project truths, not literals hidden in the module.
        params = BridgeParams()
        self.assertGreater(params.width_mm, 0.0)
        self.assertGreater(params.max_span_mm, params.width_mm)
        self.assertGreaterEqual(params.max_per_pair, 1)

    def test_a_bare_bridge_has_sane_derived_numbers(self):
        self.assertEqual(Bridge().aspect, 0.0)
        self.assertEqual(Bridge(span_mm=6.0, width_mm=2.0).aspect, 3.0)
        self.assertEqual(Bridge(span_mm=6.0, width_mm=2.0).area_mm2, 12.0)


if __name__ == "__main__":
    unittest.main()
