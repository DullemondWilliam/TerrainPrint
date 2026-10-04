"""
test_butterfly_keys.py

LIB_butterfly_keys: slot two neighbouring province STLs and build the
double-dovetail keys that join them.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_butterfly_keys.py -v

The test that carries the weight is the assembly one: every key, laid in place,
must fill its slots and collide with neither piece.  A slot cut in the wrong
frame -- a stale origin, a missed Y-flip -- still leaves two watertight pieces
and a pile of keys, and only putting them together shows it.
"""

import json

import numpy as np
import pytest
import trimesh
from shapely.affinity import affine_transform, translate
from shapely.geometry import box

import geopandas as gpd

from lib import LIB_butterfly_keys as K
from lib.LIB_border_connectors import _extrude_man, _man_to_trimesh, \
    _trimesh_to_man

KM = 1000.0
P = K.KeyParams()
MPM = P.mpm
# Two 50 mm (400 km) provinces meeting along x = 7400 km.
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
HEIGHT_MM = 3.0


def _prism(geom, flip=False):
    """A flat piece in its own print-mm frame (origin at its min corner)."""
    x0, y0, _, y1 = geom.bounds
    e = -1.0 / MPM if flip else 1.0 / MPM
    yoff = (y1 / MPM) if flip else (-y0 / MPM)
    mm = affine_transform(geom, [1 / MPM, 0, 0, e, -x0 / MPM, yoff])
    return _man_to_trimesh(_extrude_man(mm, 0.0, HEIGHT_MM)), (x0, y0)


def _write_piece(tmp_path, name, geom, sidecar=True, flip=False):
    mesh, origin = _prism(geom, flip)
    stl = tmp_path / f"{name}.stl"
    mesh.export(str(stl))
    if sidecar:
        stl.with_suffix(".frame.json").write_text(
            json.dumps({"origin_m": list(origin)}), encoding="utf-8")
    gj = tmp_path / f"{name}.geojson"
    gpd.GeoDataFrame(geometry=[geom], crs=K.EPSG_METRIC).to_crs(
        "EPSG:4326").to_file(gj, driver="GeoJSON")
    return stl, gj


@pytest.fixture
def pieces(tmp_path):
    return (_write_piece(tmp_path, "A", A), _write_piece(tmp_path, "B", B))


def _volume(man):
    mesh = _man_to_trimesh(man)
    return 0.0 if mesh is None else float(mesh.volume)


def _assert_keys_seat(result):
    """Each key fills its slot in both pieces and collides with neither."""
    parts = dict(K.assembly(result)[:2])
    a, b = (_trimesh_to_man(parts[result.a.name]),
            _trimesh_to_man(parts[result.b.name]))
    for label, key in K.assembly(result)[2:]:
        k = _trimesh_to_man(key)
        assert _volume(k ^ a) == pytest.approx(0.0, abs=1e-3), label
        assert _volume(k ^ b) == pytest.approx(0.0, abs=1e-3), label


class TestPlacement:

    def test_auto_place_spaces_keys_and_keeps_clear_of_the_ends(self):
        seam = K.find_seam(A, B)
        placed = K.auto_place(seam, P)
        length = seam.geoms[0].length
        assert len(placed) == int((length - 2 * P.margin_mm * MPM)
                                  // (P.spacing_mm * MPM)) + 1
        s = sorted(p.s_m for p in placed)
        assert np.allclose(np.diff(s), P.spacing_mm * MPM)
        assert s[0] >= P.margin_mm * MPM and s[-1] <= length - P.margin_mm * MPM
        # Centred: the same slack at each end.
        assert s[0] == pytest.approx(length - s[-1])

    def test_a_short_seam_gets_one_key_or_none(self):
        stub = box(7_400 * KM, 1_000 * KM, 7_500 * KM, 1_000 * KM + 4 * MPM)
        seam = K.find_seam(A, stub)
        assert len(K.auto_place(seam, P)) == 1
        sliver = box(7_400 * KM, 1_000 * KM, 7_500 * KM, 1_000 * KM + 2 * MPM)
        assert K.auto_place(K.find_seam(A, sliver), P) == []

    def test_snap_finds_the_nearest_seam_point(self):
        seam = K.find_seam(A, B)
        placement = K.snap(seam, 7_410 * KM, 1_123 * KM)
        point = seam.geoms[0].interpolate(placement.s_m)
        assert point.x == pytest.approx(7_400 * KM)
        assert point.y == pytest.approx(1_123 * KM)

    def test_seam_found_across_a_small_gap(self):
        """Pieces trimmed separately may not share an exact edge."""
        apart = translate(B, xoff=0.05 * MPM)
        assert K.find_seam(A, apart, P) is not None
        assert K.find_seam(A, translate(B, xoff=1.0 * MPM), P) is None


class TestKeyShape:

    def test_the_waist_sits_on_the_seam_and_both_halves_match(self):
        seam = K.find_seam(A, B)
        key = K.key_polygon(seam, K.Placement(0, seam.geoms[0].length / 2), P)
        in_a, in_b = key.intersection(A).area, key.intersection(B).area
        assert in_a == pytest.approx(in_b, rel=1e-9)
        assert in_a + in_b == pytest.approx(key.area, rel=1e-9)
        waist = key.intersection(seam.geoms[0].buffer(1.0))
        assert waist.bounds[3] - waist.bounds[1] == pytest.approx(
            P.neck_mm * MPM, rel=1e-3)
        assert key.bounds[2] - key.bounds[0] == pytest.approx(
            2 * P.reach_mm * MPM, rel=1e-9)

    def test_it_locks_the_ends_are_wider_than_the_waist(self):
        with pytest.raises(ValueError, match="neck_mm < mouth_mm"):
            K.KeyParams(neck_mm=3.0, mouth_mm=2.0).validate()

    def test_the_slot_is_the_key_grown_by_the_clearance(self):
        seam = K.find_seam(A, B)
        place = K.Placement(0, 20 * MPM)
        key = K.key_polygon(seam, place, P)
        slot = K.key_polygon(seam, place, P, clearance=True)
        assert slot.contains(key)
        assert slot.exterior.distance(key) == pytest.approx(
            P.clearance_mm * MPM, rel=1e-6)

    def test_the_printed_key_is_the_placed_key(self):
        solid = K.key_solid(P)
        seam = K.find_seam(A, B)
        key = K.key_polygon(seam, K.Placement(0, 20 * MPM), P)
        assert solid.is_watertight
        assert solid.volume == pytest.approx(
            key.area / MPM ** 2 * P.key_height_mm, rel=1e-6)
        assert solid.extents[2] == pytest.approx(P.depth_mm
                                                 - P.height_clearance_mm)


class TestChecks:

    def test_a_key_off_the_coast_is_refused(self):
        seam = K.find_seam(A, B)
        checks = K.check_keys(seam, [K.Placement(0, 0.0)], A.union(B), P)
        assert not checks[0].ok and "off the land" in checks[0].reason

    def test_colliding_keys_are_refused(self):
        seam = K.find_seam(A, B)
        mid = seam.geoms[0].length / 2
        checks = K.check_keys(seam, [K.Placement(0, mid),
                                     K.Placement(0, mid + 1.0 * MPM)],
                              A.union(B), P)
        assert [c.ok for c in checks] == [True, False]
        assert checks[1].overlaps


class TestFitKeys:

    def test_outputs_are_watertight(self, pieces):
        (sa, ga), (sb, gb) = pieces
        res = K.fit_keys(sa, ga, sb, gb)
        for mesh in (res.keyed_a, res.keyed_b, res.keys_mesh):
            assert mesh.is_watertight and mesh.volume > 0
        assert res.a.method == res.b.method == "sidecar"

    def test_each_piece_loses_its_half_of_every_slot(self, pieces):
        (sa, ga), (sb, gb) = pieces
        res = K.fit_keys(sa, ga, sb, gb)
        half = sum(c.slot.intersection(A).area for c in res.keys) / MPM ** 2
        removed = res.a.mesh.volume - res.keyed_a.volume
        assert removed == pytest.approx(half * P.depth_mm, rel=1e-3)

    def test_keys_seat_in_their_slots(self, pieces):
        (sa, ga), (sb, gb) = pieces
        _assert_keys_seat(K.fit_keys(sa, ga, sb, gb))

    def test_a_reconciled_flipped_stl_still_seats(self, tmp_path):
        # Notched in one corner: a plain square is the same shape flipped, so
        # nothing could tell the reconciler which way up it is.
        notched = A.difference(box(7_000 * KM, 1_300 * KM, 7_100 * KM,
                                   1_400 * KM))
        sa, ga = _write_piece(tmp_path, "A", notched, sidecar=False, flip=True)
        sb, gb = _write_piece(tmp_path, "B", B)
        res = K.fit_keys(sa, ga, sb, gb)
        assert res.a.method == "reconciled"
        assert res.a.matrix[3] < 0                  # the flip was found
        _assert_keys_seat(res)

    def test_a_slot_in_the_wrong_place_is_caught(self, pieces, monkeypatch):
        """The seating test must fail if a frame is off by a millimetre."""
        (sa, ga), (sb, gb) = pieces
        res = K.fit_keys(sa, ga, sb, gb)
        res.a.matrix = list(res.a.matrix)
        res.a.matrix[4] += 1.0                       # shift A's slots 1 mm
        res.keyed_a = K.cut_slots(res.a, [c.slot for c in res.keys], P)
        res.a.matrix[4] -= 1.0                       # but assemble truthfully
        with pytest.raises(AssertionError):
            _assert_keys_seat(res)

    def test_manual_placements_are_used(self, pieces):
        (sa, ga), (sb, gb) = pieces
        res = K.fit_keys(sa, ga, sb, gb,
                         placements=[K.Placement(0, 25 * MPM)])
        assert len(res.keys) == 1 and K.key_layout(1, P) is not None

    def test_refused_keys_are_reported_not_cut(self, pieces):
        (sa, ga), (sb, gb) = pieces
        res = K.fit_keys(sa, ga, sb, gb, placements=[
            K.Placement(0, 0.0), K.Placement(0, 25 * MPM)])
        assert len(res.keys) == 1 and len(res.skipped) == 1

    def test_nothing_cuttable_is_an_error(self, pieces):
        (sa, ga), (sb, gb) = pieces
        with pytest.raises(ValueError, match="No key can be cut"):
            K.fit_keys(sa, ga, sb, gb, placements=[K.Placement(0, 0.0)])

    def test_wrong_pairing_is_refused(self, tmp_path):
        sa, _ = _write_piece(tmp_path, "A", A)
        _, gb = _write_piece(tmp_path, "B", B)
        with pytest.raises(ValueError, match="does not put it over"):
            K.load_province(sa, gb)

    def test_non_neighbours_are_refused(self, tmp_path):
        far = translate(B, xoff=100 * KM)
        (sa, ga), (sb, gb) = (_write_piece(tmp_path, "A", A),
                              _write_piece(tmp_path, "F", far))
        with pytest.raises(ValueError, match="share no seam"):
            K.fit_keys(sa, ga, sb, gb)

    def test_export_writes_three_stls(self, pieces, tmp_path):
        (sa, ga), (sb, gb) = pieces
        out = tmp_path / "out"
        K.fit_keys(sa, ga, sb, gb, out_dir=out)
        for name in ("A_keyed.stl", "B_keyed.stl", "butterfly_keys.stl"):
            assert trimesh.load(str(out / name)).is_watertight


class TestKeyLayout:

    def test_every_key_is_a_separate_body_on_the_bed(self):
        layout = K.key_layout(40, P)
        bodies = layout.split(only_watertight=False)
        assert len(bodies) == 40
        usable = 256.0 - 2 * 10.0
        assert layout.extents[0] <= usable

    def test_no_keys_no_mesh(self):
        assert K.key_layout(0, P) is None
