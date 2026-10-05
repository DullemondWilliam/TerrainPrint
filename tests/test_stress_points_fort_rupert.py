"""
test_stress_points_fort_rupert.py

One real coastline: the north end of Vancouver Island, around Fort Rupert.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_stress_points_fort_rupert.py -v

Every other test of ``LIB_stress_points`` is built out of squares and bars,
because a fixture you can work out in your head is the only kind that can tell
you *why* something broke. This one is the exception, and it earns it: the
isthmus at Rupert Inlet is a neck that was reported as missing, and a synthetic
stand-in for it would have been a guess about what makes it hard.

What makes it hard is that it is **not** a dumbbell. Rupert Inlet comes in from
the west and very nearly meets Hardy Bay from the east, so the land that holds
the island's northern tip on is a bent, ragged corridor rather than a bar, and
the narrowest cross-section of it is nowhere near the middle of anything.

Three things are pinned here.

**It is found at all.** A 1.15 mm throat holding some tens of square millimetres
is the sort of thing the measurement can lose at either end -- too thin to
survive the ``min_lobe_mm2`` floor on one side, too wide to be a neck on the
other -- and losing it is what prompted the fixture.

**It is found in the right place**, within a few millimetres of Fort Rupert,
rather than somewhere else on a 371 mm² coast.

**It is sound, and it is on the map anyway.** Its severity is about a quarter,
well under the 1.0 anchor. ``UI_province_viewer``'s stress layer used to default
to drawing only necks at 1.0 and worse, which hid this one completely -- found
every single time, never drawn, and so indistinguishable from a measurement
failure. The floor now starts at zero and is shared with the harness, and
``TestItIsOnTheMap`` holds it there.

The load is deliberately asserted as a **range**. The cut that weighs a branch
does not part this one (``severed`` is False), so the figure reported is the
mass standing above the throat, which is an undercount: the descent's own upper
bound for the same branch is about 34 mm² against the 18 mm² reported, and a
disc cut across the isthmus severs about 34 mm² of real coast. Pinning 18 would
freeze an answer known to be low, and pinning 34 would fail today, so the test
brackets both and will survive the undercount being fixed.
"""

import math
import unittest
from pathlib import Path

import shapely

from lib.LIB_stress_points import StressParams, stress_points

#: One print-mm, in metres, at the project's 1:8,000,000.
MM = 8000.0

#: The settings this coast was pinned under. The neck holds about 18 mm², so
#: the smallest lobe worth holding up has to be below that or the neck is not
#: reported at all -- exactly the "too thin to survive the floor" loss the
#: docstring describes. These tests were written on a desk whose saved floor
#: was 15 mm², and passed only by inheriting it; the shipped floor is 25. They
#: now say the number they need instead of borrowing it.
PARAMS = StressParams(report_above=0.0, min_lobe_mm2=15.0)

#: Fort Rupert in the working CRS (EPSG:3347), from 127.41 W 50.695 N.
FORT_RUPERT = (3832122.0, 2296279.0)

COAST = Path(__file__).with_name("fort_rupert_coast.wkt")


def north_vancouver_island():
    """The clipped coast, as it was cut from the cleaned province."""
    text = "\n".join(line for line in COAST.read_text(encoding="utf-8").splitlines()
                     if not line.startswith("#"))
    return shapely.from_wkt(text.strip())


class FortRupertTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.coast = north_vancouver_island()
        # report_above=0 so the measurement is on trial rather than the filter.
        cls.report = stress_points(cls.coast, PARAMS)

    def distance_mm(self, point):
        return math.hypot(point.x - FORT_RUPERT[0],
                          point.y - FORT_RUPERT[1]) / MM


class TestTheFixture(FortRupertTestCase):

    def test_it_is_the_coast_it_claims_to_be(self):
        """A guard on the fixture itself: if this drifts, nothing below means
        what it says."""
        self.assertTrue(self.coast.is_valid)
        self.assertAlmostEqual(self.coast.area / MM ** 2, 369.0, delta=5.0)
        minx, miny, maxx, maxy = self.coast.bounds
        self.assertLess((maxx - minx) / MM, 61.0)
        self.assertLess((maxy - miny) / MM, 61.0)

    def test_fort_rupert_is_on_it(self):
        from shapely.geometry import Point
        self.assertLess(self.coast.distance(Point(*FORT_RUPERT)) / MM, 1.0)


class TestTheNeckIsFound(FortRupertTestCase):
    """The regression this file exists for."""

    def test_the_measurement_succeeded(self):
        self.assertTrue(self.report.measured)

    def test_a_neck_is_reported(self):
        self.assertTrue(self.report.necks,
                        "the Rupert Inlet isthmus went missing again")

    def test_it_is_the_one_at_rupert_inlet(self):
        """Within a few millimetres of Fort Rupert on a 60 mm square of coast --
        so this is the isthmus and not some other narrow place."""
        neck = self.report.necks[0]
        self.assertLess(self.distance_mm(neck.point), 6.0)

    def test_the_throat_is_the_width_of_the_corridor(self):
        """About 1.15 mm. Bracketed rather than pinned: the corridor is bent, so
        a tenth of a millimetre either way is the raster choosing a different
        cell across the same waist, not a different answer."""
        neck = self.report.necks[0]
        self.assertGreater(neck.throat_mm, 0.8)
        self.assertLess(neck.throat_mm, 2.6)

    def test_it_holds_a_real_piece_of_the_island(self):
        """Between the mass standing above the throat and the branch a cut
        across the isthmus actually severs -- see the module docstring on why
        this is a range and not a number."""
        neck = self.report.necks[0]
        self.assertGreater(neck.load_mm2, 15.0)
        self.assertLess(neck.load_mm2, 40.0)
        self.assertGreater(neck.other_mm2, neck.load_mm2,
                           "the island is the heavier side of its own tip")

    def test_the_bar_and_the_blob_sit_on_the_neck(self):
        neck = self.report.necks[0]
        self.assertAlmostEqual(neck.throat.length / MM, neck.throat_mm, places=6)
        self.assertIsNotNone(neck.region)
        self.assertLess(self.distance_mm(neck.region.centroid), 8.0)


class TestItIsOnTheMap(FortRupertTestCase):
    """Sound, and drawn anyway.

    Worth a test of its own because this neck is exactly the case that went
    wrong: "the overlay does not show it" and "the measurement does not find
    it" look identical from the map and have entirely different fixes. The
    viewer used to default its severity floor to 1.0, which hid every sound
    neck -- so a 0.26 that was found every single time read as a miss. The floor
    now starts at zero and is shared with the harness.
    """

    def test_the_neck_is_sound(self):
        """It is not at risk, and that is the right answer: 18 mm² of island on
        a 1.15 mm corridor is comfortable. Being sound is not a reason to leave
        it off the map."""
        neck = self.report.necks[0]
        self.assertLess(neck.severity, 1.0)
        self.assertFalse(neck.at_risk)

    def test_the_viewers_floor_draws_it(self):
        import UI_province_viewer as viewer
        floor = viewer.STRESS_SEVERITY[2]
        self.assertGreaterEqual(self.report.necks[0].severity, floor,
                                "the province viewer would not draw the neck "
                                "this whole file was written about")

    def test_the_floor_is_shared_so_it_cannot_drift_back(self):
        """The harness saves the floor along with the measurement, so the two
        windows cannot disagree about which part of the answer to show."""
        from harnesses import UI_stress_points as harness
        assert any(key == "stress.report_above"
                   for key, _ in harness.SAVED_SETTINGS)


if __name__ == "__main__":
    unittest.main()
