"""
test_border_bar.py

``lib.LIB_border_bar``: splits planned across the corridor, and the bar
built in the pieces they make -- tested without the editor.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_border_bar.py -v
"""

from dataclasses import replace

import numpy as np
import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from lib import LIB_border_bar as BB
from lib import LIB_border_plan as BP
from lib import LIB_connector_rules as CR
from lib.LIB_border_connectors import MIN_PART_AREA_MM2

KM = 1000.0
MPM = BP.BorderSettings().mpm
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
C = box(7_000 * KM, 1_400 * KM, 7_800 * KM, 1_800 * KM)
SEAM_MID = (7_400 * KM, 1_200 * KM)
FRONTIER_WEST = (7_200 * KM, 1_408 * KM)
FRONTIER_EAST = (7_600 * KM, 1_408 * KM)


@pytest.fixture(scope="module")
def defined():
    return BP.define_borders({"A": A, "B": B}, {"C": C}, BP.BorderSettings())


def _copy(defined, **changes):
    changes.setdefault("lobes", list(defined.lobes))
    return replace(defined, **changes)


def _split_near(d, x, y, step_km=4.0, tries=40):
    """A split at or beside (x, y), where no connector is in the way."""
    for k in range(tries):
        for sign in (1, -1):
            for dx, dy in ((sign * k * step_km * KM, 0),
                           (0, sign * k * step_km * KM)):
                cut, _ = BB.split_at(d, x + dx, y + dy)
                if cut is not None:
                    return cut, (x + dx, y + dy)
    raise AssertionError("no free spot to split near there")


class TestSplitAt:

    def test_straight_across_the_corridor(self, defined):
        cut, _ = _split_near(defined, *SEAM_MID)
        width = defined.settings.width_mm * MPM
        assert cut.length == pytest.approx(width, rel=0.02)
        (_, y0), (_, y1) = cut.coords
        assert abs(y1 - y0) < 0.01 * width

    def test_refused_through_a_connector(self, defined):
        x, y, _, _ = CR.connector_pose(defined, 0)
        cut, why = BB.split_at(defined, x, y)
        assert cut is None and "connector" in why

    def test_refused_where_it_leaves_a_crumb(self, defined):
        cut, (x, y) = _split_near(defined, *SEAM_MID)
        alone = [(x, y + f * MPM) for f in (0.1, -0.1)
                 if BB.split_at(defined, x, y + f * MPM)[0] is not None]
        assert alone
        crumb, why = BB.split_at(_copy(defined, splits=[cut]), *alone[0])
        assert crumb is None and "too small to build" in why

    def test_close_splits_are_fine(self, defined):
        cut, (x, y) = _split_near(defined, *SEAM_MID)
        d = _copy(defined, splits=[cut])
        near = [BB.split_at(d, x, y + f * MPM)[0] for f in (2, -2, 3, -3)]
        assert any(c is not None and c.distance(cut) < 5 * MPM
                   for c in near)

    def test_a_speck_elsewhere_does_not_stop_it(self, defined):
        """A corridor speck under the sliver floor -- where it rings a tiny
        island -- is no crumb this split made."""
        speck = box(6_000 * KM, 2_000 * KM, 6_000 * KM + 0.5 * MPM,
                    2_000 * KM + 0.5 * MPM)
        d = _copy(defined, border=unary_union([defined.border, speck]))
        assert any(p.area < MIN_PART_AREA_MM2 * MPM ** 2 for p in d.pieces())
        _, at = _split_near(d, *SEAM_MID)
        assert BB.split_at(d, *at)[1] == ""

    def test_no_corridor_no_split(self):
        lonely = BP.define_borders({"A": A}, {})
        assert BB.split_at(lonely, *SEAM_MID)[0] is None


class TestBuildBar:

    def test_nothing_is_split_for_you(self, defined):
        bar = BB.build_bar(defined, bed_mm=40.0, margin_mm=0.0)
        assert len(bar.pieces) == 1 and bar.too_big == 1
        assert bar.piece_too_big == [True]

    def test_built_in_the_planned_pieces(self, defined):
        whole = BB.build_bar(defined)
        d = _copy(defined, splits=[_split_near(defined, *SEAM_MID)[0],
                                   _split_near(defined, *FRONTIER_WEST)[0]])
        bar = BB.build_bar(d)
        assert len(bar.pieces) == len(d.pieces()) == 3
        assert all(p.is_watertight and p.volume > 0 for p in bar.pieces)
        assert sum(p.volume for p in bar.pieces) == pytest.approx(
            whole.mesh.volume, rel=1e-3)
        assert bar.mesh.bounds == pytest.approx(whole.mesh.bounds, abs=1e-6)
        assert bar.lobes_cut == 0

    def test_the_origin_is_print_mm_zero(self, defined):
        bar = BB.build_bar(defined)
        assert bar.mesh.bounds[0][:2] == pytest.approx((0.0, 0.0), abs=1e-6)

    def test_no_corridor_is_refused(self):
        with pytest.raises(ValueError, match="No corridor"):
            BB.build_bar(BP.define_borders({"A": A}, {}))


class TestColours:

    FIT, BIG = (0.5, 0.5, 0.5, 1.0), (0.8, 0.2, 0.2, 1.0)

    def test_none_when_every_piece_fits(self, defined):
        assert BB.bar_vertex_colours(BB.build_bar(defined), self.FIT,
                                     self.BIG) is None
        assert BB.bar_vertex_colours(None, self.FIT, self.BIG) is None

    def test_each_piece_in_its_colour(self, defined):
        d = _copy(defined, splits=[_split_near(defined, *FRONTIER_WEST)[0]])
        bar = BB.build_bar(d, bed_mm=60.0, margin_mm=0.0)
        colours = BB.bar_vertex_colours(bar, self.FIT, self.BIG)
        assert len(colours) == len(bar.mesh.vertices)
        big = np.all(np.isclose(colours, self.BIG), axis=1)
        fit = np.all(np.isclose(colours, self.FIT), axis=1)
        assert big.any() and fit.any() and (big | fit).all()
        start = 0
        for piece, too_big in zip(bar.pieces, bar.piece_too_big):
            n = len(piece.vertices)
            assert (big[start:start + n] if too_big else fit[start:start + n]).all()
            start += n

    def test_a_mesh_that_is_not_its_pieces_gets_no_colours(self, defined):
        d = _copy(defined, splits=[_split_near(defined, *FRONTIER_WEST)[0]])
        bar = BB.build_bar(d, bed_mm=60.0, margin_mm=0.0)
        bar.mesh = bar.pieces[0]
        assert BB.bar_vertex_colours(bar, self.FIT, self.BIG) is None



class TestRemovedPieces:
    """Pieces of bar the user takes out are not built, nor their connectors
    -- and a removal is a point, so it follows the piece through new splits."""

    @staticmethod
    def _split_twice(defined):
        """Three pieces: the A/B arm cut off at mid-seam, the rest cut at the
        west frontier."""
        return _copy(defined, splits=[_split_near(defined, *SEAM_MID)[0],
                                      _split_near(defined, *FRONTIER_WEST)[0]])

    @staticmethod
    def _piece_at(d, x, y):
        from shapely.geometry import Point
        return next(i for i, p in enumerate(d.pieces()) if p.covers(Point(x, y)))

    def test_a_removed_piece_is_not_built(self, defined):
        d = self._split_twice(defined)
        whole = BB.build_bar(d)
        i = self._piece_at(d, 7_400 * KM, 1_100 * KM)    # the seam's south end
        d.removed = [d.pieces()[i].representative_point()]
        bar = BB.build_bar(d)
        assert bar.removed == 1 and len(bar.pieces) == len(whole.pieces) - 1
        assert bar.mesh.volume < whole.mesh.volume
        # The rest still lies where it will join: the frame is the whole bar's.
        assert bar.pieces[0].bounds == pytest.approx(
            [p for p in whole.pieces
             if p.bounds[0][0] == pytest.approx(bar.pieces[0].bounds[0][0])
             ][0].bounds, abs=1e-6)

    def test_its_connectors_go_with_it(self, defined):
        d = self._split_twice(defined)
        i = self._piece_at(d, 7_400 * KM, 1_100 * KM)
        on_it = d.piece_lobes()[i]
        assert on_it
        d.removed = [d.pieces()[i].representative_point()]
        kept = d.kept_lobes()
        assert len(kept) == len(d.lobes) - len(on_it)
        assert not any(lobe in kept for lobe in on_it)
        assert BB.build_bar(d).lobes == len(kept)
        for name in d.targets:
            assert not any(lobe in d.province_lobes(name) for lobe in on_it)

    def test_a_removed_piece_is_never_too_big(self, defined):
        d = _copy(defined)
        assert d.too_big(bed_mm=60.0, margin_mm=0.0) == [0]
        d.removed = [d.pieces()[0].representative_point()]
        assert d.too_big(bed_mm=60.0, margin_mm=0.0) == []

    def test_removing_every_piece_is_refused(self, defined):
        d = _copy(defined)
        d.removed = [p.representative_point() for p in d.pieces()]
        with pytest.raises(ValueError, match="removed"):
            BB.build_bar(d)

    def test_a_removal_follows_its_piece_through_a_new_split(self, defined):
        d = _copy(defined)
        point = d.pieces()[0].representative_point()
        d.removed = [point]
        d.splits = [_split_near(defined, *SEAM_MID)[0]]
        flags = d.removed_flags()
        assert sum(flags) == 1
        assert d.pieces()[flags.index(True)].covers(point)

    def test_saved_and_loaded(self, defined, tmp_path):
        d = self._split_twice(defined)
        d.removed = [d.pieces()[0].representative_point()]
        BP.write_borders(d, tmp_path / "saved")
        back = BP.read_borders(tmp_path / "saved")
        assert len(back.removed) == 1
        assert back.removed_flags() == d.removed_flags()
        d.removed = []
        BP.write_borders(d, tmp_path / "saved")
        assert BP.read_borders(tmp_path / "saved").removed == []



class TestRingsAndIslands:
    """A bar with holes: a small one -- where it rings an island -- is
    crossed by a split; a big one -- a province with corridor on every side,
    as Alberta and Saskatchewan are when defined beside the US -- is not."""

    WIDTH = BP.BorderSettings().width_mm * MPM          # 16 km

    def _definition(self, border):
        return BP.BorderDefinition(settings=BP.BorderSettings(), border=border)

    def _ring(self):
        """A bar running right round a 400 km square region."""
        outer = box(0, 0, 400 * KM + 2 * self.WIDTH, 400 * KM + 2 * self.WIDTH)
        inner = box(self.WIDTH, self.WIDTH, 400 * KM + self.WIDTH,
                    400 * KM + self.WIDTH)
        return self._definition(outer.difference(inner))

    def test_a_ring_is_cut_across_its_strip(self):
        d = self._ring()
        cut, why = BB.split_at(d, 200 * KM, self.WIDTH / 2)   # bottom strip
        assert why == "" and cut.length == pytest.approx(self.WIDTH, rel=0.02)

    def test_a_ring_comes_apart_at_its_second_split(self):
        d = self._ring()
        first, _ = BB.split_at(d, 200 * KM, self.WIDTH / 2)
        d = _copy(d, splits=[first])
        assert len(d.pieces()) == 1
        second, why = BB.split_at(d, 200 * KM, 400 * KM + 1.5 * self.WIDTH)
        assert why == ""
        assert len(_copy(d, splits=[first, second]).pieces()) == 2

    def test_a_small_island_is_crossed(self):
        """A 4 km island in a 40 km wide stretch of bar: a cut that stopped
        at it would leave the bar whole on the island's other side."""
        strip = box(0, 0, 400 * KM, 40 * KM)
        island = box(198 * KM, 18 * KM, 202 * KM, 22 * KM)
        d = self._definition(strip.difference(island))
        cut, why = BB.split_at(d, 200 * KM, 21 * KM)          # by the island
        assert why == ""
        assert len(_copy(d, splits=[cut]).pieces()) == 2
