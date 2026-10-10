"""
test_terrain_editor_victoria.py

Victoria Island through ``UI_terrain_editor.define_borders``: the island is
split between the Northwest Territories and Nunavut by a boundary with **two
legs** -- one east along the south of the island, and one
north-east up the middle of it -- and each leg needs a corridor.

It gets one.  Only the south leg comes out of simplification still shared by
both provinces, so only it is carved -- and not all the way to the turn: 78%
of it is covered, 14% of the north-east leg, and 51% of the 642 km seam.  The
north-east leg is drawn as an internal arc with no corridor along it.

The two tests that say so are marked ``xfail(strict=True)``: the suite stays
green while the bug stands, and turns red the moment it is fixed, so the marks
come off with the fix.

The geometry is real: ``victoria_island_nwt.wkt`` and ``victoria_island_nu.wkt``
were cut from the tracked ``Data/JSon-Provinces-Orig`` files (see their
headers), so the test needs nothing from the untracked ``Data/`` folders.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_terrain_editor_victoria.py -v
"""

from pathlib import Path

import pytest
import shapely
from shapely.geometry import box
from shapely.ops import linemerge

import UI_terrain_editor as U

HERE = Path(__file__).parent

#: How much of each leg the corridor must run along.  Not all of it: the
#: corridor is carved along the *simplified* seam, and the two ends meet the
#: coast, where the coastal simplification can shorten them.
MIN_COVER = 0.9


def _load(name):
    text = (HERE / f"victoria_island_{name}.wkt").read_text(encoding="utf-8")
    return shapely.from_wkt("\n".join(
        line for line in text.splitlines() if not line.startswith("#")))


@pytest.fixture(scope="module")
def regions():
    return {"NWT": _load("nwt"), "NU": _load("nu")}


@pytest.fixture(scope="module")
def defined(regions):
    return U.define_borders(regions, {}, U.BorderSettings())


@pytest.fixture(scope="module")
def legs(regions):
    """The seam as given, split at its southernmost vertex into its two legs:
    ``(south, north_east)``."""
    seam = linemerge(regions["NWT"].boundary.intersection(
        regions["NU"].boundary))
    turn_x = min(seam.coords, key=lambda c: c[1])[0]
    x0, y0, x1, y1 = seam.bounds
    south = seam.intersection(box(x0 - 1, y0 - 1, turn_x, y1 + 1))
    north_east = seam.intersection(box(turn_x, y0 - 1, x1 + 1, y1 + 1))
    return south, north_east


def _covered(leg, definition) -> float:
    """The share of ``leg`` the corridor runs along, allowing for the seam
    having moved by up to the internal tolerance in simplification."""
    s = definition.settings
    near = definition.border.buffer(s.internal_tol_mm * s.mpm)
    return leg.intersection(near).length / leg.length


class TestTheSeam:
    """What the fixture says, so the test below cannot pass for want of it."""

    def test_the_provinces_meet_along_one_642_km_seam(self, regions):
        seam = linemerge(regions["NWT"].boundary.intersection(
            regions["NU"].boundary))
        assert seam.geom_type == "LineString"
        assert seam.length == pytest.approx(642e3, rel=0.01)

    def test_it_has_two_long_legs(self, legs):
        south, north_east = legs
        assert south.length > 200e3 and north_east.length > 200e3


class TestVictoriaIslandBorders:

    def test_the_south_leg_gets_a_corridor(self, defined, legs):
        """The one border it does get (most of it: see the module docstring)."""
        south, _ = legs
        assert _covered(south, defined) >= 0.5

    @pytest.mark.xfail(strict=True, reason=(
        "Victoria Island gets one border, not two: simplification leaves the "
        "north-east leg of the NWT/Nunavut seam unshared, so no corridor is "
        "carved along it"))
    def test_the_north_east_leg_gets_a_corridor(self, defined, legs):
        _, north_east = legs
        assert _covered(north_east, defined) >= MIN_COVER

    @pytest.mark.xfail(strict=True, reason="as above")
    def test_the_whole_seam_gets_a_corridor(self, defined, legs):
        south, north_east = legs
        assert _covered(south.union(north_east), defined) >= MIN_COVER
