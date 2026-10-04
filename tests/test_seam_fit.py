"""
test_seam_fit.py

lib/LIB_seam_fit.py: where two printed provinces will not mesh, and the brush
that moves their border.

Run with:  python -m pytest tests/test_seam_fit.py -v
"""

import pytest
from shapely.geometry import Polygon, box

from lib.LIB_seam_fit import (FitParams, fit_report, push_border)

SCALE = 8_000_000.0
MM = SCALE / 1000.0                    # one print-mm, in metres
O = 7_000_000.0


def _params(**kw):
    base = dict(scale=SCALE, min_width_mm=1.0, edge_loss_mm=0.2, band_mm=3.0,
                tab_mm=8.0, lock_ratio=1.05)
    base.update(kw)
    return FitParams(**base)


def _pair(seam_mm):
    """West and East meeting along a seam given in print-mm from O.

    The seam runs bottom (y=0) to top (y=40); West is everything left of it
    in a 40 x 40 mm square.
    """
    seam = [(O + x * MM, O + y * MM) for x, y in seam_mm]
    west = Polygon([(O, O)] + seam + [(O, O + 40 * MM)])
    east = Polygon([(O + 40 * MM, O)] + seam + [(O + 40 * MM, O + 40 * MM)])
    return west.buffer(0), east.buffer(0)


STRAIGHT = [(20, 0), (20, 40)]

#: A mushroom tab of West reaching into East: a 2 mm neck opening into a 6 mm
#: head. It locks.
MUSHROOM = [(20, 0), (20, 17), (22, 17), (22, 15), (28, 15), (28, 25),
            (22, 25), (22, 19), (20, 19), (20, 40)]

#: A plain rectangular tab, 6 mm wide and 4 mm deep: wide at the mouth, so it
#: lifts straight out and does not lock.
BLOCK = [(20, 0), (20, 17), (24, 17), (24, 23), (20, 23), (20, 40)]

#: A finger 0.5 mm wide and 5 mm long: under the 1.4 mm the seam needs.
FINGER = [(20, 0), (20, 19.75), (25, 19.75), (25, 20.25), (20, 20.25),
          (20, 40)]


class TestRequiredWidth:

    def test_needs_twice_the_edge_loss_on_top_of_the_minimum(self):
        assert _params().required_width_mm == pytest.approx(1.4)


class TestTabs:

    def test_a_straight_seam_meshes_cleanly(self):
        report = fit_report(*_pair(STRAIGHT), params=_params())
        assert report.tabs == []
        assert report.narrow == []
        assert report.summary() == "meshes cleanly"

    def test_a_mushroom_tab_locks(self):
        report = fit_report(*_pair(MUSHROOM), params=_params())
        assert len(report.locking) == 1
        tab = report.locking[0]
        assert tab.side == "a"                     # West's land
        assert tab.neck_mm == pytest.approx(2.0, abs=0.05)
        # The closing rounds the inside corners at the neck a little, so the
        # head reads a shade over the 6 mm drawn.
        assert tab.head_mm == pytest.approx(6.0, abs=0.2)
        # Measured from the lid, so the far corner (8 across, 6 up) is 10 away.
        assert tab.depth_mm == pytest.approx(10.0, abs=0.05)
        assert "1 locking tab" in report.summary()

    def test_a_tab_wide_at_the_mouth_does_not_lock(self):
        report = fit_report(*_pair(BLOCK), params=_params())
        block = max(report.tabs, key=lambda t: t.area_mm2)
        assert block.side == "a"
        assert report.locking == []
        assert report.problem_tabs == []

    def test_reflex_corners_give_fillets_that_are_never_problems(self):
        """Closing fills every inside corner a little. Those wedges widen
        towards their lid, so they can neither lock nor snap off."""
        report = fit_report(*_pair(BLOCK), params=_params())
        fillets = [t for t in report.tabs if t.side == "b"]
        assert fillets
        assert not any(t.is_problem for t in fillets)

    def test_a_thin_finger_is_too_narrow(self):
        report = fit_report(*_pair(FINGER), params=_params())
        assert any(t.too_narrow for t in report.tabs)
        assert report.problems >= 1

    def test_the_finger_passes_once_the_printer_is_good_enough(self):
        """The test is a width: drop the requirement below it and it clears."""
        report = fit_report(*_pair(FINGER),
                            params=_params(min_width_mm=0.3, edge_loss_mm=0.0))
        assert not any(t.too_narrow for t in report.tabs)

    def test_a_shallow_wobble_is_not_a_finger(self):
        """Thin because it is shallow, with a whole province behind it."""
        wobble = [(20, 0), (20, 10), (20.05, 20), (20, 30), (20, 40)]
        report = fit_report(*_pair(wobble), params=_params())
        assert report.problems == 0

    def test_tabs_belong_to_the_province_that_holds_them(self):
        east_tab = [(20, 0), (20, 17), (16, 17), (16, 23), (20, 23), (20, 40)]
        report = fit_report(*_pair(east_tab), params=_params())
        assert max(report.tabs, key=lambda t: t.area_mm2).side == "b"


class TestNarrowLand:

    def test_a_thin_slot_is_narrow_land_on_the_other_side(self):
        """A 0.5 mm finger of West is a 0.5 mm slot in East: both reported."""
        report = fit_report(*_pair(FINGER), params=_params())
        assert {p.side for p in report.narrow} == {"a"}
        assert report.narrow_area_mm2 > 0

    def test_narrow_land_away_from_the_seam_is_not_reported(self):
        west, east = _pair(STRAIGHT)
        # A thin spit on West's far coast, 15 mm from the seam.
        spit = box(O - 5 * MM, O + 20 * MM, O + 0.1 * MM, O + 20.4 * MM)
        report = fit_report(west.union(spit), east, params=_params())
        assert report.narrow == []

    def test_worst_prefers_the_locking_tab(self):
        report = fit_report(*_pair(MUSHROOM), params=_params())
        assert report.worst().equals(report.locking[0].geometry)


class TestNoSeam:

    def test_provinces_that_do_not_touch_get_an_empty_report(self):
        a = box(O, O, O + MM, O + MM)
        b = box(O + 10 * MM, O, O + 11 * MM, O + MM)
        report = fit_report(a, b, params=_params())
        assert report.problems == 0 and report.tabs == []


class TestPushBorder:

    def test_moves_land_across_the_seam(self):
        west, east = _pair(STRAIGHT)
        stroke = [(O + 20 * MM, O + 10 * MM), (O + 20 * MM, O + 30 * MM)]
        grown, shrunk = push_border(west, east, stroke, 2.0, SCALE)
        moved = (grown.area - west.area) / MM ** 2
        # A 2 mm brush centred on the seam takes a 1 mm strip, 20 mm long,
        # plus half-disc caps.
        assert moved == pytest.approx(20.0 + 3.14159 / 2, rel=0.02)

    def test_the_pair_still_partitions_the_ground(self):
        west, east = _pair(STRAIGHT)
        stroke = [(O + 20 * MM, O + 10 * MM), (O + 22 * MM, O + 30 * MM)]
        grown, shrunk = push_border(west, east, stroke, 3.0, SCALE)
        assert grown.intersection(shrunk).area / MM ** 2 < 1e-6
        before = west.union(east)
        after = grown.union(shrunk)
        assert before.symmetric_difference(after).area / MM ** 2 < 1e-6

    def test_a_stroke_away_from_the_border_moves_nothing(self):
        """Painting inside East would make an enclave of West: refused."""
        west, east = _pair(STRAIGHT)
        stroke = [(O + 30 * MM, O + 10 * MM), (O + 30 * MM, O + 30 * MM)]
        grown, shrunk = push_border(west, east, stroke, 2.0, SCALE)
        assert grown is west and shrunk is east

    def test_a_stroke_inside_the_growing_side_is_a_no_op(self):
        west, east = _pair(STRAIGHT)
        stroke = [(O + 10 * MM, O + 10 * MM), (O + 10 * MM, O + 30 * MM)]
        assert push_border(west, east, stroke, 2.0, SCALE) == (west, east)

    def test_pushing_away_a_locking_tab_clears_it(self):
        """The by-hand fix for what the fit check finds."""
        west, east = _pair(MUSHROOM)
        stroke = [(O + 21 * MM, O + 20 * MM), (O + 28 * MM, O + 20 * MM)]
        grown_east, shrunk_west = push_border(east, west, stroke, 12.0, SCALE)
        report = fit_report(shrunk_west, grown_east, params=_params())
        assert report.locking == []
