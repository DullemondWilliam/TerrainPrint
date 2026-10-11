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
