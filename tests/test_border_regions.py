"""
test_border_regions.py

``lib.LIB_border_regions``: the corridor carved between regions -- here, the
bridging of gaps between a core (a printed province) and a surrounding
region that come from different datasets and never quite meet, as Quebec
and the US states do.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_border_regions.py -v
"""

import pytest
from shapely.geometry import box

from lib.LIB_border_regions import bridge_gaps, build_border_regions

KM = 1000.0
#: A core, and a surrounding region north of it a ``gap`` away.
CORE = box(0, 0, 400 * KM, 400 * KM)


def _north(gap_km):
    return box(0, 400 * KM + gap_km * KM, 400 * KM, 800 * KM)


def _touching(regions, core=CORE):
    """How much of the core's north edge the corridor runs against."""
    north_edge = core.boundary.intersection(
        box(-1, 400 * KM - 1, 400 * KM + 1, 400 * KM + 1))
    if regions.border is None:
        return 0.0
    return north_edge.intersection(regions.border.buffer(1.0)).length


class TestBridgeGaps:

    def test_a_narrow_gap_is_filled(self):
        bridge = bridge_gaps(CORE, _north(3), 8 * KM)
        assert bridge.area == pytest.approx(400 * KM * 3 * KM, rel=1e-3)

    def test_a_gap_wider_than_the_limit_is_left(self):
        """Not even the band in its middle that is within the limit of both
        sides: it would float, attached to neither."""
        assert bridge_gaps(CORE, _north(12), 8 * KM) is None

    def test_open_coast_is_never_bridged(self):
        """The core's other sides face nothing: no bridge runs along them."""
        bridge = bridge_gaps(CORE, _north(3), 8 * KM)
        assert bridge.bounds[1] >= 400 * KM - 1

    def test_zero_bridges_nothing(self):
        assert bridge_gaps(CORE, _north(3), 0.0) is None


class TestTheCorridor:

    def test_without_bridging_it_stops_at_the_surrounding_edge(self):
        regions = build_border_regions([CORE], [_north(3)],
                                       border_width_km=16.0)
        assert _touching(regions) == 0.0

    def test_bridged_it_reaches_the_core(self):
        regions = build_border_regions([CORE], [_north(3)],
                                       border_width_km=16.0,
                                       bridge_gap_km=8.0)
        assert _touching(regions) == pytest.approx(400 * KM, rel=1e-3)

    def test_still_as_wide_as_asked(self):
        """Measured from the core: the bridge is part of the corridor's
        width, not added to it."""
        regions = build_border_regions([CORE], [_north(3)],
                                       border_width_km=16.0,
                                       bridge_gap_km=8.0)
        assert regions.border.bounds[3] == pytest.approx(416 * KM, abs=1.0)

    def test_the_core_keeps_its_full_extent(self):
        regions = build_border_regions([CORE], [_north(3)],
                                       border_width_km=16.0,
                                       bridge_gap_km=8.0)
        assert regions.core[0].equals(CORE)

    def test_the_surrounding_gives_up_only_the_rest(self):
        regions = build_border_regions([CORE], [_north(3)],
                                       border_width_km=16.0,
                                       bridge_gap_km=8.0)
        assert regions.surrounding[0].bounds[1] == pytest.approx(416 * KM,
                                                                 abs=1.0)

    def test_an_overlap_is_still_taken_from_the_surrounding(self):
        regions = build_border_regions([CORE], [_north(-2)],
                                       border_width_km=16.0,
                                       bridge_gap_km=8.0)
        assert _touching(regions) == pytest.approx(400 * KM, rel=1e-3)
        assert not regions.surrounding[0].intersects(CORE.buffer(-1.0))
