"""
test_terrain_editor_labrador.py

Enclaves along the Quebec / Newfoundland and Labrador border, through
``UI_terrain_editor.define_borders``.

Carving the corridor can cut pieces off a province's land.  Where the border
zig-zags, two arms of the corridor all but meet and leave crumbs of province
in holes in the bar; where it meets the coast, the bar cuts slivers off
between itself and the sea.  Printed, each is a loose crumb with no land to
hang on to, so below ``borders.enclave_mm2`` it is added to the border bar.

The geometry is real: ``labrador_border_qc.wkt`` and ``labrador_border_nl.wkt``
were cut from the tracked ``Data/JSon-Provinces-Orig`` files (see their
headers), 30 km around the border.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_terrain_editor_labrador.py -v
"""

from pathlib import Path

import pytest
import shapely
from shapely.geometry import Polygon

import UI_terrain_editor as U
from lib import LIB_border_connectors as BC
from lib import LIB_border_plan as BP

HERE = Path(__file__).parent


def _load(name):
    text = (HERE / f"labrador_border_{name}.wkt").read_text(encoding="utf-8")
    return shapely.from_wkt("\n".join(
        line for line in text.splitlines() if not line.startswith("#")))


@pytest.fixture(scope="module")
def regions():
    return {"Quebec": _load("qc"), "Labrador": _load("nl")}


@pytest.fixture(scope="module")
def defined(regions):
    return BP.define_borders(regions, {}, BP.BorderSettings())


@pytest.fixture(scope="module")
def kept_apart(regions):
    """The same border with the merge turned off: what carving leaves."""
    return BP.define_borders(regions, {}, BP.BorderSettings(enclave_mm2=0.0))


def _cut_off(definition) -> list:
    """The pieces the carving cut off a province: of each simplified part,
    every piece left but the largest."""
    pieces = []
    for name, trimmed in definition.targets.items():
        for part in BC._polys(definition.simplified[name]):
            inside = [piece for piece in BC._polys(trimmed)
                      if part.covers(piece.representative_point())]
            inside.sort(key=lambda g: g.area, reverse=True)
            pieces += inside[1:]
    return pieces


def _mm2(definition, geom) -> float:
    return geom.area / definition.settings.mpm ** 2


def _holes(definition) -> list:
    return [Polygon(ring) for poly in BC._polys(definition.border)
            for ring in poly.interiors]


class TestWhatCarvingLeaves:
    """With the merge off, so the tests below cannot pass for want of a
    case: this border really does leave both kinds of enclave."""

    def test_it_cuts_pieces_off(self, kept_apart):
        small = [p for p in _cut_off(kept_apart)
                 if _mm2(kept_apart, p) < BP.BorderSettings().enclave_mm2]
        assert len(small) >= 3

    def test_some_sit_in_holes_in_the_bar(self, kept_apart):
        """Between two arms of the zig-zag, wholly surrounded by corridor."""
        holes = _holes(kept_apart)
        assert any(hole.covers(piece) for piece in _cut_off(kept_apart)
                   for hole in holes)

    def test_some_lie_between_the_bar_and_the_sea(self, kept_apart):
        holes = _holes(kept_apart)
        assert any(not any(hole.covers(piece) for hole in holes)
                   for piece in _cut_off(kept_apart))


class TestEnclavesJoinTheBar:

    def test_no_small_piece_is_left_cut_off(self, defined):
        limit = defined.settings.enclave_mm2
        assert all(_mm2(defined, p) >= limit for p in _cut_off(defined))

    def test_the_bar_has_no_holes_left(self, defined):
        assert _holes(defined) == []

    def test_each_one_is_now_part_of_the_bar(self, defined, kept_apart):
        limit = defined.settings.enclave_mm2
        for piece in _cut_off(kept_apart):
            if _mm2(kept_apart, piece) < limit:
                assert defined.border.buffer(1.0).covers(piece)

    def test_no_land_is_lost_or_doubled(self, defined, kept_apart):
        """What leaves the provinces is what the bar gains."""
        moved = sum(_mm2(kept_apart, p) for p in _cut_off(kept_apart)
                    if _mm2(kept_apart, p) < defined.settings.enclave_mm2)
        assert defined.enclaves_mm2 == pytest.approx(moved, rel=1e-6)
        land = lambda d: sum(_mm2(d, g) for g in d.targets.values())
        assert land(kept_apart) - land(defined) == pytest.approx(moved,
                                                                 rel=1e-3)
        assert not defined.targets["Quebec"].intersects(
            defined.border.buffer(-1.0))

    def test_islands_are_never_touched(self, defined, kept_apart):
        """A piece that was its own part before carving stays, however
        small: only what the carving cut off is merged."""
        before = [p for name, g in kept_apart.targets.items()
                  for p in BC._polys(g) if p not in _cut_off(kept_apart)]
        after = [p for g in defined.targets.values() for p in BC._polys(g)]
        lost = [p for p in before
                if not any(q.covers(p.representative_point()) for q in after)]
        assert lost == []

    def test_the_limit_is_honoured(self, regions):
        tight = BP.define_borders(regions, {}, BP.BorderSettings(
            enclave_mm2=0.2))
        assert tight.enclaves < BP.define_borders(
            regions, {}, BP.BorderSettings()).enclaves
        assert all(_mm2(tight, p) >= 0.2 for p in _cut_off(tight))

    def test_the_count_and_area_are_reported(self, defined):
        assert defined.enclaves >= 3 and defined.enclaves_mm2 > 0


class TestPinholes:
    """Specks of hole where two outlines meet by rounding are filled
    whatever the enclave setting: none of them holds any province."""

    def test_no_empty_speck_is_left(self, kept_apart):
        for hole in _holes(kept_apart):
            assert any(hole.intersection(g).area > 0.5 * hole.area
                       for g in kept_apart.targets.values())


class TestTheWindow:

    def test_the_report_says_how_many(self, qtbot, defined):
        window = U.TerrainEditorTester()
        qtbot.addWidget(window)
        window.apply_borders(defined)
        assert f"{defined.enclaves} enclave(s)" in window.border_report.text()

    def test_the_setting_reaches_define(self, qtbot):
        window = U.TerrainEditorTester()
        qtbot.addWidget(window)
        window.enclave_spin.setValue(1.5)
        assert window.border_settings().enclave_mm2 == 1.5
