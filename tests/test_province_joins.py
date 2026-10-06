"""
test_province_joins.py

LIB_province_joins: butterfly keys across every border of a map.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_province_joins.py -v

The fixture is three flat slabs meeting at a triple junction -- West, and East
split into North and South -- because that is where the map-wide rules live:
one province on two borders, keys on different borders meeting at a corner,
and a slot that must not reach into the third piece.  The pairwise geometry is
covered by test_butterfly_keys.py.
"""

import json

import pytest
from shapely.affinity import translate
from shapely.geometry import Polygon, box

from lib import LIB_butterfly_keys as K
from lib import LIB_province_joins as J
from lib.LIB_border_connectors import _trimesh_to_man
from lib.LIB_seam_fit import FitParams, FitReport, NarrowPatch
from lib.LIB_terrain_stl import settings_id
from test_butterfly_keys import MPM, _assert_keys_seat, _write_piece

KM = 1000.0
P = K.KeyParams()


def _west(east_edge_x):
    """A 50 mm square with a vertex where its neighbours meet: the files go
    through EPSG:4326 and back, and only shared vertices come back shared."""
    return Polygon([(7_000 * KM, 1_000 * KM), (east_edge_x, 1_000 * KM),
                    (east_edge_x, 1_200 * KM), (east_edge_x, 1_400 * KM),
                    (7_000 * KM, 1_400 * KM)])


# West; East is the same size, split at the junction height.
WEST = _west(7_400 * KM)
NORTH = box(7_400 * KM, 1_200 * KM, 7_800 * KM, 1_400 * KM)
SOUTH = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_200 * KM)
SHAPES = {"CAN_West": WEST, "CAN_North": NORTH, "CAN_South": SOUTH}
JUNCTION = (7_400 * KM, 1_200 * KM)


def _folder(tmp_path, shapes=SHAPES, geojson_shapes=None):
    """STLs cut from ``shapes``; GeoJSONs from ``geojson_shapes`` if given."""
    folder = tmp_path / "pieces"
    folder.mkdir(exist_ok=True)
    for name, geom in shapes.items():
        _write_piece(folder, name, geom)
    if geojson_shapes is not None:
        for name, geom in geojson_shapes.items():
            stl_dir = tmp_path / "other"
            stl_dir.mkdir(exist_ok=True)
            _, gj = _write_piece(stl_dir, name, geom)
            (folder / f"{name}.geojson").write_bytes(gj.read_bytes())
    return folder


def _session(tmp_path, **kwargs):
    folder = _folder(tmp_path, **kwargs)
    pairs, unpaired = J.pair_folders(folder, folder)
    assert not unpaired
    pieces, problems = J.load_pieces(pairs, P)
    assert not problems
    return pieces, J.find_joins(pieces, P)


@pytest.fixture
def session(tmp_path):
    return _session(tmp_path)


def _join(joins, a, b):
    return next(j for j in joins if j.involves(a) and j.involves(b))


def _pair_result(result, pieces, join):
    return K.KeyResult(
        a=pieces[join.a].province, b=pieces[join.b].province,
        keyed_a=result.keyed[join.a], keyed_b=result.keyed[join.b],
        keys=[c for c in result.checks[join.key] if c.ok], params=P)


class TestPairing:

    def test_suffixes_are_stripped(self):
        assert J.province_name("CAN_Alberta_simplified") == "CAN_Alberta"
        assert J.province_name("CAN_Alberta_keyed") == "CAN_Alberta"
        assert J.province_name("CAN_Alberta") == "CAN_Alberta"

    def test_pairs_across_naming_and_reports_the_rest(self, tmp_path):
        gj = [tmp_path / "CAN_Alberta_simplified.geojson",
              tmp_path / "CAN_Yukon.geojson"]
        stl = [tmp_path / "CAN_Alberta.stl", tmp_path / "CAN_Manitoba.stl"]
        pairs, unpaired = J.pair_files(gj, stl)
        assert [p[0] for p in pairs] == ["CAN_Alberta"]
        assert any("CAN_Yukon.geojson" in u for u in unpaired)
        assert any("CAN_Manitoba.stl" in u for u in unpaired)

    def test_two_files_for_one_province_is_a_clash(self, tmp_path):
        gj = [tmp_path / "CAN_A.geojson", tmp_path / "CAN_A_simplified.geojson"]
        pairs, unpaired = J.pair_files(gj, [tmp_path / "CAN_A.stl"])
        assert any("two GeoJSON" in u for u in unpaired)

    def test_ledgers_plans_and_sidecars_are_not_provinces(self, tmp_path):
        folder = tmp_path / "in"
        folder.mkdir()
        for name in ("border_ledger.json", "join_plan.json", "X.frame.json",
                     "CAN_A.geojson", "CAN_A.stl", "butterfly_keys_01.stl"):
            (folder / name).write_text("{}")
        assert [p.name for p in J.geojson_files(folder)] == ["CAN_A.geojson"]
        assert [p.name for p in J.stl_files(folder)] == ["CAN_A.stl"]


class TestLoading:

    def test_a_piece_built_under_other_settings_is_refused(self, tmp_path):
        folder = _folder(tmp_path)
        frame = folder / "CAN_West.frame.json"
        data = json.loads(frame.read_text())
        data["settings_id"] = "not-this-map"
        frame.write_text(json.dumps(data))
        pieces, problems = J.load_pieces(J.pair_folders(folder, folder)[0], P)
        assert "CAN_West" not in pieces and len(pieces) == 2
        assert "different terrain settings" in problems[0]

    def test_a_piece_built_under_these_settings_is_accepted(self, tmp_path):
        folder = _folder(tmp_path)
        frame = folder / "CAN_West.frame.json"
        data = json.loads(frame.read_text())
        data["settings_id"] = settings_id()
        frame.write_text(json.dumps(data))
        pieces, problems = J.load_pieces(J.pair_folders(folder, folder)[0], P)
        assert "CAN_West" in pieces and not problems


class TestJoins:

    def test_every_border_is_found_and_agrees_with_its_stls(self, session):
        _, joins = session
        assert {j.key for j in joins} == {
            "CAN_North|CAN_South", "CAN_North|CAN_West", "CAN_South|CAN_West"}
        for join in joins:
            assert join.agreement_mm < 0.01 and not join.stale

    def test_a_province_recleaned_since_its_stl_is_caught(self, tmp_path):
        """GeoJSONs whose shared edge moved 1 mm; STLs from before."""
        moved = {"CAN_West": _west(7_400 * KM + MPM),
                 "CAN_North": box(7_400 * KM + MPM, 1_200 * KM, 7_800 * KM,
                                  1_400 * KM),
                 "CAN_South": box(7_400 * KM + MPM, 1_000 * KM, 7_800 * KM,
                                  1_200 * KM)}
        pieces, joins = _session(tmp_path, geojson_shapes=moved)
        west_north = _join(joins, "CAN_West", "CAN_North")
        assert west_north.stale
        assert west_north.agreement_mm == pytest.approx(1.0, abs=0.05)
        plan = J.auto_plan(joins, P)
        checks = J.check_plan(plan, pieces, joins, P)
        assert checks[west_north.key]
        assert all(not c.ok and "out of date" in c.reason
                   for c in checks[west_north.key])
        # The north/south border did not move: still agrees.
        assert not _join(joins, "CAN_North", "CAN_South").stale


class TestPlan:

    def test_auto_plan_places_keys_on_every_border(self, session):
        pieces, joins = session
        plan = J.auto_plan(joins, P)
        assert all(plan.points(j.key) for j in joins)
        checks = J.check_plan(plan, pieces, joins, P)
        assert all(c.ok for cs in checks.values() for c in cs)

    def test_only_the_named_borders_are_replaced(self, session):
        _, joins = session
        first = joins[0]
        plan = J.auto_plan(joins, P, only={first.key})
        assert list(plan.keys) == [first.key]

    def test_keys_on_two_borders_may_not_collide_at_a_corner(self, session):
        pieces, joins = session
        """Both reach into North from its two borders, 2.5 mm off the corner:
        clear of West and South, but not of each other."""
        plan = J.JoinPlan()
        off = 2.5 * MPM
        west_north = _join(joins, "CAN_West", "CAN_North")
        north_south = _join(joins, "CAN_North", "CAN_South")
        plan.set_points(west_north, [(JUNCTION[0], JUNCTION[1] + off)])
        plan.set_points(north_south, [(JUNCTION[0] + off, JUNCTION[1])])
        alone = J.JoinPlan()
        alone.set_points(north_south, plan.points(north_south.key))
        assert J.check_plan(alone, pieces, joins, P)[north_south.key][0].ok
        checks = J.check_plan(plan, pieces, joins, P)
        verdicts = [checks[west_north.key][0], checks[north_south.key][0]]
        assert sum(c.ok for c in verdicts) == 1
        refused = next(c for c in verdicts if not c.ok)
        assert refused.reason.startswith("overlaps a key on")

    def test_a_slot_into_the_third_piece_is_refused(self, session):
        """At the junction a West/North slot reaches into South."""
        pieces, joins = session
        plan = J.JoinPlan()
        west_north = _join(joins, "CAN_West", "CAN_North")
        plan.set_points(west_north, [JUNCTION])
        check = J.check_plan(plan, pieces, joins, P)[west_north.key][0]
        assert not check.ok and "off the land" in check.reason

    def test_keys_on_narrow_land_are_refused(self, session):
        pieces, joins = session
        join = _join(joins, "CAN_West", "CAN_North")
        plan = J.auto_plan(joins, P, only={join.key})
        x, y = plan.points(join.key)[0]
        report = FitReport(FitParams(),
                           narrow=[NarrowPatch(box(x - MPM, y - MPM, x + MPM,
                                                   y + MPM), "a", 4.0)])
        checks = J.check_plan(plan, pieces, joins, P, fits={join.key: report})
        assert checks[join.key][0].reason == "on land too narrow at the seam"
        assert all(c.ok for c in checks[join.key][1:])

    def test_save_and_load_round_trip(self, session, tmp_path):
        _, joins = session
        plan = J.auto_plan(joins, P)
        loaded = J.JoinPlan.load(plan.save(tmp_path / J.PLAN_NAME, P))
        assert loaded.keys == plan.keys and loaded.seams == plan.seams
        assert not any(loaded.seam_moved(j) for j in joins)

    def test_a_missing_or_broken_plan_loads_empty(self, tmp_path):
        assert J.JoinPlan.load(tmp_path / "nope.json").count == 0
        (tmp_path / "bad.json").write_text("{ not json")
        assert J.JoinPlan.load(tmp_path / "bad.json").count == 0

    def test_a_moved_seam_is_flagged_and_keys_snap_to_the_new_one(self, session):
        _, joins = session
        join = joins[0]
        plan = J.auto_plan(joins, P, only={join.key})
        plan.seams[join.key] = translate(join.seam, xoff=MPM).wkt
        assert plan.seam_moved(join)
        for placement in plan.placements(join):
            point = join.seam.geoms[placement.part].interpolate(placement.s_m)
            assert join.seam.distance(point) < 1e-6


class TestCutting:

    def test_every_key_seats_on_every_border(self, session):
        pieces, joins = session
        result = J.cut_all(J.auto_plan(joins, P), pieces, joins, P)
        assert not result.failed and set(result.keyed) == set(pieces)
        for join in joins:
            pair = _pair_result(result, pieces, join)
            assert pair.keys
            _assert_keys_seat(pair)

    def test_a_province_on_two_borders_gets_both_borders_slots(self, session):
        pieces, joins = session
        result = J.cut_all(J.auto_plan(joins, P), pieces, joins, P)
        west = pieces["CAN_West"]
        removed = west.province.mesh.volume - result.keyed["CAN_West"].volume
        keys = sum(len([c for c in result.checks[j.key] if c.ok])
                   for j in joins if j.involves("CAN_West"))
        half_slot = J.check_plan(J.auto_plan(joins, P), pieces, joins, P)
        slot = next(c.slot for cs in half_slot.values() for c in cs)
        expected = keys * slot.area / 2 / MPM ** 2 * P.depth_mm
        assert removed == pytest.approx(expected, rel=0.02)

    def test_every_output_is_watertight_and_plates_carry_the_spares(
            self, session):
        pieces, joins = session
        result = J.cut_all(J.auto_plan(joins, P), pieces, joins, P)
        for mesh in list(result.keyed.values()) + result.plates:
            assert mesh.is_watertight and mesh.volume > 0
        bodies = sum(len(p.split(only_watertight=False))
                     for p in result.plates)
        assert bodies == result.key_count + P.spares

    def test_an_unchanged_province_is_not_cut_again(self, session):
        pieces, joins = session
        plan = J.auto_plan(joins, P)
        first = J.cut_all(plan, pieces, joins, P)
        north_south = _join(joins, "CAN_North", "CAN_South")
        fewer = plan.copy()
        fewer.set_points(north_south, plan.points(north_south.key)[:1])
        second = J.cut_all(fewer, pieces, joins, P, previous=first)
        assert second.keyed["CAN_West"] is first.keyed["CAN_West"]
        assert second.keyed["CAN_North"] is not first.keyed["CAN_North"]

    def test_a_slot_deeper_than_the_base_is_refused(self, session):
        pieces, joins = session
        with pytest.raises(ValueError, match="cut through"):
            J.cut_all(J.auto_plan(joins, P), pieces, joins, P,
                      base_mm=P.depth_mm)

    def test_no_keys_no_plates_and_pieces_pass_through(self, session):
        pieces, joins = session
        result = J.cut_all(J.JoinPlan(), pieces, joins, P)
        assert result.plates == [] and result.key_count == 0
        assert result.keyed["CAN_West"] is pieces["CAN_West"].province.mesh

    def test_the_assembly_is_cropped_to_the_border(self, session):
        pieces, joins = session
        result = J.cut_all(J.auto_plan(joins, P), pieces, joins, P)
        join = _join(joins, "CAN_North", "CAN_South")
        parts = J.join_assembly(result, pieces, join)
        assert len(parts) == 2 + len([c for c in result.checks[join.key]
                                      if c.ok])
        _, piece = parts[0]
        assert piece.extents[1] < 4 * P.reach_mm * 2 + 1.0
        # The keys still sit in the cropped pieces' slots, not in their solid.
        key = _trimesh_to_man(parts[2][1])
        assert key.volume() > 0


class TestExport:

    def test_writes_pieces_frames_plates_and_plan(self, session, tmp_path):
        pieces, joins = session
        plan = J.auto_plan(joins, P)
        result = J.cut_all(plan, pieces, joins, P)
        out = tmp_path / "keyed"
        out.mkdir()
        (out / "butterfly_keys_07.stl").write_text("stale")
        paths = {p.name for p in J.export(result, pieces, out, plan)}
        assert {"CAN_West_keyed.stl", "CAN_North_keyed.stl",
                "CAN_South_keyed.stl", "butterfly_keys_01.stl",
                J.PLAN_NAME} <= paths
        assert not (out / "butterfly_keys_07.stl").exists()
        frame = json.loads((out / "CAN_West_keyed.frame.json").read_text())
        assert frame == pieces["CAN_West"].frame
        assert J.JoinPlan.load(out / J.PLAN_NAME).keys == plan.keys

    def test_the_keyed_folder_pairs_again(self, session, tmp_path):
        """Output re-imports: `_keyed` reduces to the province's name."""
        pieces, joins = session
        out = tmp_path / "keyed"
        J.export(J.cut_all(J.auto_plan(joins, P), pieces, joins, P), pieces,
                 out)
        folder = next(iter(pieces.values())).geojson.parent
        pairs, unpaired = J.pair_folders(folder, out)
        assert len(pairs) == 3 and not unpaired
