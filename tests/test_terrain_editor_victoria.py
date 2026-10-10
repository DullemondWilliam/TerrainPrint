"""
test_terrain_editor_victoria.py

Victoria Island through ``UI_terrain_editor.define_borders``: the island is
split between the Northwest Territories and Nunavut by a boundary with **two
legs** -- one east along the south of the island, and one
north-east up the middle of it -- and each leg needs a corridor.

It used to get one.  The seam turns 92 degrees where the legs meet, and
simplification dropped that vertex as a "reversal": the shortcut crossed the
NWT's coast, the repaired outline no longer shared the north-east leg, and
only the south leg was carved (78% of it; 14% of the north-east leg).  See
``REVERSAL_CORNER_DEG`` in ``LIB_border_simplification``.

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
        south, _ = legs
        assert _covered(south, defined) >= MIN_COVER

    def test_the_north_east_leg_gets_a_corridor(self, defined, legs):
        _, north_east = legs
        assert _covered(north_east, defined) >= MIN_COVER

    def test_the_whole_seam_gets_a_corridor(self, defined, legs):
        south, north_east = legs
        assert _covered(south.union(north_east), defined) >= MIN_COVER

    def test_the_turn_between_the_legs_survives_simplification(self, defined,
                                                               legs):
        """The vertex the two legs meet at is what was lost."""
        south, north_east = legs
        turn = south.intersection(north_east)
        s = defined.settings
        assert any(turn.distance(shapely.Point(xy)) < s.internal_tol_mm * s.mpm
                   for seg in defined.internal_segs for xy in seg)

    def test_the_simplified_outlines_are_valid(self, regions):
        """Valid before any repair: the repair is what broke the seam."""
        import geopandas as gpd
        from lib.LIB_border_simplification import topo_simplify_two_tolerances
        s = U.BorderSettings()
        frame = gpd.GeoDataFrame({"name": list(regions)},
                                 geometry=list(regions.values()),
                                 crs=U.EPSG_WORKING)
        simp, _, _ = topo_simplify_two_tolerances(
            frame, s.coast_tol_mm * s.mpm, s.internal_tol_mm * s.mpm,
            min_segment_m=s.min_segment_mm * s.mpm,
            min_angle_deg=s.min_angle_deg)
        assert all(geom.is_valid for geom in simp.geometry)


# -- the connectors --------------------------------------------------------------

#: Where the two legs of the seam meet, in EPSG:3347 metres: the inside of the
#: bend is on the NWT side.
TURN = (5_534_157.0, 3_811_208.0)


def _border_line(definition):
    """The NWT/Nunavut border line the corridor is carved along: the shared
    (internal) arcs as simplified."""
    return shapely.union_all([shapely.LineString(seg)
                              for seg in definition.internal_segs
                              if len(seg) >= 2])


def _crossing(definition) -> list:
    """The connectors that cross the border line, by index."""
    line = _border_line(definition)
    return [i for i, lobe in enumerate(definition.lobes)
            if lobe.intersects(line)]


class TestVictoriaIslandConnectors:
    """A connector keys one territory into the bar, from its own side of the
    border line.

    On Victoria Island one did not: in the inside corner of the bend, on the
    NWT side, Define hung a connector from the north-east leg's edge that
    swung its root over the south leg's line.  None of the rules every
    connector is held to saw it -- it met the corridor in one piece (the two
    legs' corridors join at the corner), and its far end was all in the NWT.
    Define now refuses a connector that crosses the line.  By hand the same
    graze stays allowed: a turned connector's root crosses it by design.
    """

    def _corner_connector(self, defined):
        """The connector Define would have placed in the corner: of those it
        generates that cross the line, the one at the bend.  (The other, at
        the south leg's west end, breaks a rule of its own first.)"""
        from lib.LIB_border_connectors import generate_lobes
        p = defined.settings.connector_params()
        line = _border_line(defined)
        candidates = [lobe for lobe in generate_lobes(defined.border,
                                                      defined.land(), p)
                      if lobe.intersects(line)]
        return min(candidates, key=shapely.Point(TURN).distance)

    def test_connectors_are_placed_on_both_sides(self, defined):
        owners = {name for lobe in defined.lobes
                  for name, geom in defined.targets.items()
                  if lobe.difference(defined.border).intersects(geom)}
        assert owners == {"NWT", "NU"}

    def test_define_places_no_connector_across_the_border_line(self,
                                                               defined):
        assert _crossing(defined) == []

    def test_the_corner_connector_is_the_one_refused(self, defined):
        """So the test above cannot pass for want of a candidate: the one
        crossing the line is in the corner of the bend, on the NWT side, and
        the report counts it."""
        lobe = self._corner_connector(defined)
        assert lobe.distance(shapely.Point(TURN)) < 4 * defined.settings.mpm
        assert lobe.difference(defined.border).intersects(
            defined.targets["NWT"])
        assert defined.drop_reasons.get("crosses_line") == 1

    def test_put_there_by_hand_it_is_allowed(self, defined):
        """The graze is the same one a turned connector makes by design."""
        x, y, angle, scale = U.lobe_pose(self._corner_connector(defined),
                                         defined.border,
                                         defined.settings.connector_params())
        lobe, why = U.place_connector(defined, x, y, angle_deg=angle,
                                      scale=scale, allow_faults=True)
        assert lobe is not None and why == ""
