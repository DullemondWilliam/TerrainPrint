"""Tests for ``lib.LIB_border_split.split_to_bed``.

Subdivides a border solid into watertight pieces that each fit a print bed.
Fixtures are simple axis-aligned bars authored directly in print-mm, so bed
fit, piece count, and volume conservation can be checked exactly.
"""

import numpy as np
import pytest
import trimesh
from shapely.geometry import box

from lib.LIB_border_split import (MIN_PART_AREA_MM2, DEFAULT_BED_MM,
                                   split_to_bed)


def a_bar(w, d, h=2.0, x0=0.0, y0=0.0):
    """A solid rectangular bar (w x d x h) in print-mm, base at z=0."""
    bar = trimesh.creation.box((w, d, h))
    bar.apply_translation((x0 + w / 2.0, y0 + d / 2.0, h / 2.0))
    return bar


def _footprint(piece):
    (xmin, ymin, _), (xmax, ymax, _) = piece.bounds
    return xmax - xmin, ymax - ymin


class TestSplitToBed:

    def test_fitting_bar_returned_whole(self):
        bar = a_bar(200.0, 2.0)
        pieces = split_to_bed(bar, bed_mm=256.0)
        assert len(pieces) == 1
        assert pieces[0].volume == pytest.approx(bar.volume, rel=1e-6)

    def test_long_bar_is_split_to_fit(self):
        bar = a_bar(300.0, 2.0)                 # longer than the bed
        pieces = split_to_bed(bar, bed_mm=256.0)
        assert len(pieces) >= 2
        for piece in pieces:
            fw, fd = _footprint(piece)
            assert fw <= 256.0 + 1e-6
            assert fd <= 256.0 + 1e-6

    def test_volume_is_conserved(self):
        bar = a_bar(300.0, 2.0)
        pieces = split_to_bed(bar, bed_mm=256.0)
        total = sum(p.volume for p in pieces)
        assert total == pytest.approx(bar.volume, rel=1e-6)

    def test_every_piece_watertight(self):
        pieces = split_to_bed(a_bar(700.0, 2.0), bed_mm=256.0)
        assert len(pieces) >= 3
        assert all(p.is_watertight and p.volume > 0 for p in pieces)

    def test_splits_both_axes_for_wide_shape(self):
        plate = a_bar(300.0, 300.0)             # overflows both axes
        pieces = split_to_bed(plate, bed_mm=256.0)
        assert len(pieces) >= 4                 # a 2x2 grid at minimum
        for piece in pieces:
            fw, fd = _footprint(piece)
            assert fw <= 256.0 + 1e-6 and fd <= 256.0 + 1e-6
        assert sum(p.volume for p in pieces) == pytest.approx(plate.volume, rel=1e-6)

    def test_rectangular_bed_respected_per_axis(self):
        plate = a_bar(300.0, 200.0)
        pieces = split_to_bed(plate, bed_mm=(256.0, 150.0))
        for piece in pieces:
            fw, fd = _footprint(piece)
            assert fw <= 256.0 + 1e-6
            assert fd <= 150.0 + 1e-6

    def test_margin_shrinks_usable_area(self):
        bar = a_bar(240.0, 2.0)                 # fits 256 raw, not 256-2*10
        whole = split_to_bed(bar, bed_mm=256.0, margin_mm=0.0)
        assert len(whole) == 1
        margined = split_to_bed(bar, bed_mm=256.0, margin_mm=10.0)
        assert len(margined) >= 2
        for piece in margined:
            fw, _ = _footprint(piece)
            assert fw <= 236.0 + 1e-6

    def test_disjoint_bars_not_cut_together(self):
        # Two separate fitting bars far apart: their combined bbox is huge, but
        # each bar fits, so decomposing first must return them uncut.
        left = a_bar(100.0, 2.0, x0=0.0)
        right = a_bar(100.0, 2.0, x0=1000.0)
        both = trimesh.util.concatenate([left, right])
        pieces = split_to_bed(both, bed_mm=256.0)
        assert len(pieces) == 2
        assert sum(p.volume for p in pieces) == pytest.approx(both.volume, rel=1e-6)

    def test_exports_numbered_stls(self, tmp_path):
        pieces = split_to_bed(a_bar(300.0, 2.0), bed_mm=256.0,
                              out_dir=tmp_path, out_stem="border")
        written = sorted(tmp_path.glob("border_*.stl"))
        assert len(written) == len(pieces)
        assert all(p.stat().st_size > 0 for p in written)

    def test_loads_from_stl_path(self, tmp_path):
        src = tmp_path / "bar.stl"
        a_bar(300.0, 2.0).export(str(src))
        pieces = split_to_bed(src, bed_mm=256.0)
        assert len(pieces) >= 2
        assert all(p.is_watertight for p in pieces)

    def test_non_positive_bed_raises(self):
        with pytest.raises(ValueError):
            split_to_bed(a_bar(100.0, 2.0), bed_mm=0.0)

    def test_margin_larger_than_bed_raises(self):
        with pytest.raises(ValueError):
            split_to_bed(a_bar(100.0, 2.0), bed_mm=256.0, margin_mm=200.0)

    def test_empty_mesh_raises(self):
        with pytest.raises(ValueError):
            split_to_bed(trimesh.Trimesh(), bed_mm=256.0)

    def test_default_bed_constant(self):
        assert DEFAULT_BED_MM == 256.0



class TestBedTurn:
    """Fitting the bed at any turn, as a slicer places a part."""

    def test_a_part_that_fits_square_needs_no_turn(self):
        from lib.LIB_border_split import bed_turn
        assert bed_turn(box(0, 0, 50, 20), 60.0) == 0.0

    def test_a_long_thin_part_fits_corner_to_corner(self):
        from lib.LIB_border_split import bed_turn
        angle = bed_turn(box(0, 0, 78, 4), 60.0)
        assert angle is not None and 30.0 < angle < 60.0

    def test_too_big_at_every_turn_is_none(self):
        from lib.LIB_border_split import bed_turn
        assert bed_turn(box(0, 0, 61, 61), 60.0) is None
        assert bed_turn(box(0, 0, 90, 4), 60.0) is None

    def test_it_takes_points_as_well(self):
        import numpy as np
        from lib.LIB_border_split import bed_turn
        corners = np.array([[0, 0, 1], [78, 0, 1], [78, 4, 1], [0, 4, 1]])
        assert bed_turn(corners, 60.0) is not None
