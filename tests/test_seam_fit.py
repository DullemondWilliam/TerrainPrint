"""
test_seam_fit.py

lib/LIB_seam_fit.py: where two printed provinces will not mesh, and the brush
that moves their border.

Run with:  python -m pytest tests/test_seam_fit.py -v
"""

import pytest
from shapely.geometry import Polygon, box

from lib.LIB_seam_fit import FitParams, fit_report, push_border, solve_fit

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


class TestSolveFit:
    """The auto-solver: repairs by handing land over, never by opening gaps."""

    @staticmethod
    def _partition_kept(a0, b0, res):
        before = a0.union(b0)
        after = res.a.union(res.b)
        assert before.symmetric_difference(after).area / MM ** 2 < 1e-6
        assert res.a.intersection(res.b).area / MM ** 2 < 1e-6

    def test_frees_a_locking_mushroom_by_filling_its_undercuts(self):
        a, b = _pair(MUSHROOM)
        res = solve_fit(a, b, _params(), max_move_mm=3.0)
        assert res.problems_before == 1 and res.problems_after == 0
        assert [(f.kind, f.action, f.grows) for f in res.fixed] == \
            [("lock", "fill", "a")]
        # The two undercuts beside the 2 mm neck: 2 x 2 and 2 x 6 mm.
        assert res.fixed[0].area_mm2 == pytest.approx(16.0, rel=0.1)
        assert fit_report(res.a, res.b, params=_params()).locking == []
        self._partition_kept(a, b, res)

    def test_cuts_a_thin_finger_off(self):
        a, b = _pair(FINGER)
        res = solve_fit(a, b, _params(), max_move_mm=3.0)
        assert res.problems_after == 0
        assert res.fixed and res.fixed[0].kind == "finger"
        self._partition_kept(a, b, res)

    def test_a_clean_seam_is_handed_back_untouched(self):
        a, b = _pair(STRAIGHT)
        res = solve_fit(a, b, _params())
        assert res.a is a and res.b is b
        assert not res.changed
        assert "meshes cleanly" in res.summary()

    def test_never_moves_the_border_further_than_allowed(self):
        a, b = _pair(MUSHROOM)
        res = solve_fit(a, b, _params(), max_move_mm=0.5)
        assert not res.changed
        assert res.a is a and res.b is b
        assert len(res.skipped) == 1
        assert "mm or more" in res.skipped[0].reason
        assert "left for a hand" in res.summary()

    def test_every_move_made_is_within_the_cap(self):
        a, b = _pair(MUSHROOM)
        res = solve_fit(a, b, _params(), max_move_mm=1.2)
        assert all(f.move_mm <= 1.2 for f in res.fixed)

    def test_reports_progress(self):
        a, b = _pair(MUSHROOM)
        calls = []
        solve_fit(a, b, _params(), max_move_mm=3.0,
                  progress=lambda *args: calls.append(args))
        assert calls and calls[0] == (1, 0, 1)

    def test_move_is_measured_along_edges_not_only_at_vertices(self):
        """A triangle with both base corners on the seam and its apex 2 mm out
        is a 2 mm move -- and a long thin one with no vertex out there at all
        must still read as one."""
        from shapely.geometry import LineString
        from lib.LIB_seam_fit import _move_mm
        seam = LineString([(O, O), (O + 10 * MM, O)])
        # A box hanging off the seam, 2 mm deep, drawn with its far side as a
        # single edge between two corners.
        moved = box(O + 2 * MM, O, O + 8 * MM, O + 2 * MM)
        assert _move_mm(moved, seam, MM) == pytest.approx(2.0, abs=0.01)
        # The same depth reached only in the middle of an edge.
        bow = Polygon([(O, O), (O + 10 * MM, O), (O + 10 * MM, O + 1e-3),
                       (O, O + 4 * MM)])
        assert _move_mm(bow, seam, MM) == pytest.approx(4.0, abs=0.01)


class TestTransfer:

    def test_moves_only_land_touching_the_growing_side(self):
        from lib.LIB_seam_fit import transfer
        west, east = _pair(STRAIGHT)
        region = box(O + 30 * MM, O + 10 * MM, O + 32 * MM, O + 12 * MM)
        grown, shrunk, moved = transfer(region, west, east, SCALE)
        assert grown is west and shrunk is east and moved is None

    def test_slivers_thinner_than_a_tenth_of_a_mm_are_not_tabs(self):
        """A 0.02 mm sliver neither prints nor stops the pieces meeting."""
        sliver = [(20, 0), (20, 10), (24, 10), (24, 10.02), (20, 10.02),
                  (20, 40)]
        report = fit_report(*_pair(sliver), params=_params())
        assert not any(t.head_mm < 0.1 for t in report.tabs)


class TestMoveSeamVertex:
    """Dragging one vertex: the sliver between old and new path changes hands."""

    @staticmethod
    def _index_at(seam, y_mm, part=0):
        coords = list(seam.geoms[part].coords)
        return next(i for i, c in enumerate(coords)
                    if abs(c[1] - (O + y_mm * MM)) < 1.0)

    def test_moves_exactly_the_triangle_between_the_paths(self):
        from lib.LIB_shared_border import _shared_seam
        from lib.LIB_seam_fit import move_seam_vertex
        a, b = _pair([(20, 0), (20, 20), (20, 40)])
        seam = _shared_seam(a, b)
        new_a, new_b = move_seam_vertex(a, b, seam, 0, self._index_at(seam, 20),
                                        (O + 25 * MM, O + 20 * MM), SCALE)
        # Half of 40 mm by 5 mm, from East to West.
        assert (new_a.area - a.area) / MM ** 2 == pytest.approx(100.0)
        assert a.union(b).symmetric_difference(
            new_a.union(new_b)).area / MM ** 2 < 1e-6
        assert new_a.intersection(new_b).area / MM ** 2 < 1e-6
        moved = _shared_seam(new_a, new_b)
        assert (O + 25 * MM, O + 20 * MM) in list(moved.geoms[0].coords)

    def test_moving_back_restores_the_pair(self):
        from lib.LIB_shared_border import _shared_seam
        from lib.LIB_seam_fit import move_seam_vertex
        a, b = _pair([(20, 0), (20, 20), (20, 40)])
        seam = _shared_seam(a, b)
        i = self._index_at(seam, 20)
        a2, b2 = move_seam_vertex(a, b, seam, 0, i, (O + 23 * MM, O + 20 * MM))
        seam2 = _shared_seam(a2, b2)
        a3, b3 = move_seam_vertex(a2, b2, seam2, 0, self._index_at(seam2, 20),
                                  (O + 20 * MM, O + 20 * MM))
        assert a3.symmetric_difference(a).area / MM ** 2 < 1e-6

    @pytest.mark.parametrize("end", ["first", "last"])
    def test_the_ends_are_pinned(self, end):
        from lib.LIB_shared_border import _shared_seam
        from lib.LIB_seam_fit import move_seam_vertex
        a, b = _pair([(20, 0), (20, 20), (20, 40)])
        seam = _shared_seam(a, b)
        index = 0 if end == "first" else len(seam.geoms[0].coords) - 1
        with pytest.raises(ValueError, match="pinned"):
            move_seam_vertex(a, b, seam, 0, index, (O + 21 * MM, O + 1 * MM))

    def test_a_vertex_cannot_go_to_sea(self):
        from lib.LIB_shared_border import _shared_seam
        from lib.LIB_seam_fit import move_seam_vertex
        a, b = _pair([(20, 0), (20, 20), (20, 40)])
        seam = _shared_seam(a, b)
        with pytest.raises(ValueError, match="sea"):
            move_seam_vertex(a, b, seam, 0, self._index_at(seam, 20),
                             (O + 50 * MM, O + 20 * MM))

    def test_the_border_cannot_cross_itself(self):
        from lib.LIB_shared_border import _shared_seam
        from lib.LIB_seam_fit import move_seam_vertex
        a, b = _pair([(20, 0), (20, 10), (24, 15), (20, 20), (20, 40)])
        seam = _shared_seam(a, b)
        with pytest.raises(ValueError, match="cross"):
            move_seam_vertex(a, b, seam, 0, self._index_at(seam, 20),
                             (O + 24 * MM, O + 5 * MM))
