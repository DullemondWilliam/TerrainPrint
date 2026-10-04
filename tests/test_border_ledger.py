"""
test_border_ledger.py

lib/LIB_border_ledger.py: which provinces share a border, and which of those
borders are cleaned.

Run with:  python -m pytest tests/test_border_ledger.py -v
"""

import pytest
from shapely.geometry import box

from lib.LIB_border_ledger import (LEDGER_NAME, Border, BorderLedger,
                                   border_key, find_borders)
from lib.LIB_shared_border import SeamParams, simplify_shared_border

MM = 8_000.0                           # one print-mm at 1:8,000,000, in metres
O = 7_000_000.0


def _row():
    """Three provinces in a row, plus one that only touches a corner."""
    return {
        "West": box(O, O, O + 20 * MM, O + 20 * MM),
        "Middle": box(O + 20 * MM, O, O + 40 * MM, O + 20 * MM),
        "East": box(O + 40 * MM, O, O + 60 * MM, O + 20 * MM),
        "Corner": box(O + 60 * MM, O + 20 * MM, O + 80 * MM, O + 40 * MM),
        "Island": box(O, O + 100 * MM, O + 5 * MM, O + 105 * MM),
    }


class TestFindBorders:

    def test_finds_every_shared_seam_and_nothing_else(self):
        keys = [b.key for b in find_borders(_row())]
        assert keys == ["East|Middle", "Middle|West"]

    def test_a_corner_touch_is_not_a_border(self):
        assert all(not b.involves("Corner") for b in find_borders(_row()))

    def test_key_is_the_same_either_way_round(self):
        assert border_key("b", "a") == border_key("a", "b") == "a|b"

    def test_border_counts_its_seam_vertices(self):
        border = find_borders(_row())[0]
        assert border.vertices == 2


class TestLedger:

    def _cleaned(self, tmp_path):
        provinces = _row()
        border = next(b for b in find_borders(provinces) if b.key == "Middle|West")
        result = simplify_shared_border(provinces["Middle"], provinces["West"],
                                        SeamParams())
        ledger = BorderLedger(tmp_path)
        ledger.record(border, result, SeamParams())
        return ledger, border, result

    def test_unrecorded_border_is_not_cleaned(self, tmp_path):
        border = find_borders(_row())[0]
        assert BorderLedger(tmp_path).status(border) == "not cleaned"

    def test_recorded_border_is_cleaned(self, tmp_path):
        ledger, border, _ = self._cleaned(tmp_path)
        assert ledger.is_cleaned(border)

    def test_survives_a_save_and_load(self, tmp_path):
        ledger, border, _ = self._cleaned(tmp_path)
        ledger.save()
        assert (tmp_path / LEDGER_NAME).exists()
        again = BorderLedger.load(tmp_path)
        assert again.is_cleaned(border)
        assert again.entry(border)["params"]["tolerance_mm"] == \
            SeamParams().tolerance_mm

    def test_a_moved_seam_reads_as_changed(self, tmp_path):
        """The files moved on: the flag alone would go on saying 'cleaned'."""
        ledger, border, _ = self._cleaned(tmp_path)
        provinces = _row()
        provinces["West"] = box(O, O, O + 21 * MM, O + 20 * MM)
        provinces["Middle"] = box(O + 21 * MM, O, O + 40 * MM, O + 20 * MM)
        moved = next(b for b in find_borders(provinces) if b.key == border.key)
        assert ledger.status(moved) == "changed"
        assert not ledger.is_cleaned(moved)

    def test_sub_metre_drift_still_counts_as_the_same_seam(self, tmp_path):
        """The round trip through EPSG:4326 must not un-clean a border."""
        ledger, border, _ = self._cleaned(tmp_path)
        nudged = Border(border.a, border.b,
                        type(border.seam)([[(x + 0.01, y) for x, y in part.coords]
                                           for part in border.seam.geoms]))
        assert ledger.is_cleaned(nudged)

    def test_unreadable_ledger_is_empty_not_fatal(self, tmp_path):
        (tmp_path / LEDGER_NAME).write_text("{ half written", encoding="utf-8")
        assert BorderLedger.load(tmp_path).entries == {}

    def test_records_the_before_and_after(self, tmp_path):
        ledger, border, result = self._cleaned(tmp_path)
        entry = ledger.entry(border)
        assert entry["vertices_before"] == result.vertices_before
        assert entry["vertices_after"] == result.vertices_after
        assert entry["max_deviation_mm"] == pytest.approx(
            result.max_deviation_mm, abs=1e-4)
