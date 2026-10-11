"""
test_connector_rules.py

``lib.LIB_connector_rules``: the rules a dovetail connector is held to, and
the geometry of moving one -- tested without the editor.

The map is the one the terrain-editor tests use: two printed provinces side by
side (A, B) under a surrounding region (C), at 1:8,000,000, where one print-mm
is 8 km.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_connector_rules.py -v
"""

import pytest
from shapely.geometry import box

from lib import LIB_border_plan as BP
from lib import LIB_connector_rules as CR
from lib.LIB_border_connectors import lobe_at

KM = 1000.0
MPM = BP.BorderSettings().mpm
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
C = box(7_000 * KM, 1_400 * KM, 7_800 * KM, 1_800 * KM)
#: Beside the A/B corridor, in A, half way up; at the coast; facing C.
FREE_A = (A.bounds[2] - 2 * MPM, A.bounds[1] + 25 * MPM)
AT_COAST = (A.bounds[2] - 2 * MPM, A.bounds[1] + 0.3 * MPM)
FACING_C = (7_300 * KM, 1_430 * KM)


@pytest.fixture(scope="module")
def defined():
    return BP.define_borders({"A": A, "B": B}, {"C": C}, BP.BorderSettings())


def _copy(defined):
    from dataclasses import replace
    return replace(defined, lobes=list(defined.lobes))


class TestTheWords:

    def test_every_fault_is_said_three_ways(self):
        """A refused placement, a standing connector and Define's report
        each have a phrase for every fault code."""
        codes = set(CR.FAULTS)
        assert set(CR.STANDING_FAULTS) <= codes | {"crosses_line"}
        assert codes <= set(CR.FAULT_COUNTS)


class TestPlaceConnector:

    def test_a_free_spot_takes_one(self, defined):
        lobe, why = CR.place_connector(_copy(defined), *FREE_A)
        assert lobe is not None and why == ""

    def test_facing_a_surrounding_region_is_refused(self, defined):
        lobe, why = CR.place_connector(defined, *FACING_C)
        assert lobe is None and why == CR.FAULTS["no_province"]

    def test_by_hand_it_comes_back_with_its_fault(self, defined):
        """allow_faults: the connector and why it is red."""
        lobe, why = CR.place_connector(defined, *FACING_C, allow_faults=True)
        assert lobe is not None and why == CR.FAULTS["no_province"]

    def test_across_the_coast_it_escapes(self, defined):
        _, why = CR.place_connector(defined, *AT_COAST)
        assert why == CR.FAULTS["escapes"]

    def test_on_another_it_crowds(self, defined):
        x, y, _, _ = CR.connector_pose(defined, 0)
        _, why = CR.place_connector(defined, x, y)
        assert why == CR.FAULTS["crowded"]

    def test_the_one_being_moved_may_land_where_it_was(self, defined):
        x, y, angle, scale = CR.connector_pose(defined, 0)
        lobe, why = CR.place_connector(defined, x, y, ignore=0,
                                       angle_deg=angle, scale=scale)
        assert why == "" and lobe.equals_exact(defined.lobes[0], 1e-3)


class TestStandingLobes:

    def test_each_rule_is_counted_by_its_own_reason(self, defined):
        p = defined.settings.connector_params()
        free = lobe_at(defined.border, *FREE_A, p)
        coast = lobe_at(defined.border, *AT_COAST, p)
        kept, reasons = CR._standing_lobes([free, free, coast], defined, p)
        assert kept == [free]
        assert reasons == {"crowded": 1, "escapes": 1}


class TestMovingOne:

    def test_its_pose_rebuilds_it(self, defined):
        p = defined.settings.connector_params()
        for i in range(len(defined.lobes)):
            x, y, angle, scale = CR.connector_pose(defined, i)
            again = lobe_at(defined.border, x, y, p, angle, scale)
            assert again.equals_exact(defined.lobes[i], 1e-3)

    def test_the_handle_is_the_middle_of_the_far_end(self, defined):
        c = defined.lobes[0].exterior.coords
        hx, hy = CR.handle_at(defined, 0)
        assert (hx, hy) == pytest.approx(((c[1][0] + c[2][0]) / 2,
                                          (c[1][1] + c[2][1]) / 2))

    def test_pointing_at_its_own_handle_keeps_its_angle(self, defined):
        hx, hy = CR.handle_at(defined, 0)
        assert CR.angle_toward(defined, 0, hx, hy) == pytest.approx(
            CR.connector_pose(defined, 0)[2], abs=1e-6)

    def test_pointing_elsewhere_turns_it_that_far(self, defined):
        import math
        x, y, angle, _ = CR.connector_pose(defined, 0)
        hx, hy = CR.handle_at(defined, 0)
        r = math.hypot(hx - x, hy - y)
        here = math.atan2(hy - y, hx - x)
        turned = here + math.radians(20.0)
        target = (x + r * math.cos(turned), y + r * math.sin(turned))
        assert CR.angle_toward(defined, 0, *target) == pytest.approx(
            angle + 20.0, abs=1e-6)


class TestReshape:

    def test_every_connector_is_kept_where_it_stood(self, defined):
        wider = BP.BorderSettings(lobe_width_mm=2.0, lobe_ratio=1.5)
        lobes, dropped = CR.reshape_connectors(defined, wider)
        assert dropped == 0 and len(lobes) == len(defined.lobes)

    def test_a_shape_that_collides_is_kept_for_the_user_to_fix(self, defined):
        huge = BP.BorderSettings(lobe_width_mm=10.0, lobe_ratio=5.0)
        lobes, dropped = CR.reshape_connectors(defined, huge)
        assert dropped == 0 and len(lobes) == len(defined.lobes)
