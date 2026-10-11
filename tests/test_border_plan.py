"""
test_border_plan.py

``lib.LIB_border_plan``: the Borders stage as data -- settings, Define, the
definition's answers (faults, pieces, what fits the bed), and Save / Load --
tested without the editor.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_border_plan.py -v
"""

import json
from dataclasses import replace

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from lib import LIB_border_plan as BP
from lib.LIB_border_connectors import lobe_at

KM = 1000.0
MPM = BP.BorderSettings().mpm
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
C = box(7_000 * KM, 1_400 * KM, 7_800 * KM, 1_800 * KM)
FREE_A = (A.bounds[2] - 2 * MPM, A.bounds[1] + 25 * MPM)


@pytest.fixture(scope="module")
def defined():
    return BP.define_borders({"A": A, "B": B}, {"C": C}, BP.BorderSettings())


def _copy(defined, **changes):
    changes.setdefault("lobes", list(defined.lobes))
    return replace(defined, **changes)


def _seam_cut(y_km=1_200):
    """A split straight across the A/B corridor at ``y_km``."""
    half = BP.BorderSettings().width_mm * MPM / 2 + 0.01 * MPM
    return LineString([(7_400 * KM - half, y_km * KM),
                       (7_400 * KM + half, y_km * KM)])


class TestSettings:

    def test_the_shipped_values(self):
        s = BP.BorderSettings()
        assert s.width_mm > 0 and s.enclave_mm2 >= 0
        assert s.mpm == pytest.approx(s.scale / 1000.0)

    def test_files_and_overrides(self, tmp_path):
        local = tmp_path / "local.json"
        local.write_text(json.dumps({"borders": {"width_mm": 3.5}}))
        s = BP.load_border_settings(local, lobe_ratio=1.8)
        assert s.width_mm == 3.5 and s.lobe_ratio == 1.8


class TestDefine:

    def test_a_corridor_and_trimmed_provinces(self, defined):
        assert defined.border is not None and defined.border.is_valid
        assert set(defined.targets) == {"A", "B"}
        assert not defined.targets["A"].intersects(defined.targets["B"])
        for geom in defined.targets.values():
            assert defined.border.intersection(geom).area < 1.0

    def test_every_connector_it_places_is_valid(self, defined):
        assert defined.lobes and not any(defined.faults())

    def test_no_splits_to_start(self, defined):
        assert defined.splits == [] and len(defined.pieces()) == 1

    def test_needs_a_target(self):
        with pytest.raises(ValueError):
            BP.define_borders({}, {"C": C})

    def test_the_border_line_is_the_printed_provinces_shared_edge(self,
                                                                  defined):
        line = defined.border_line()
        assert line.length == pytest.approx(400 * KM, rel=0.01)


class TestAnswers:

    def test_land_is_merged_once(self, defined):
        d = _copy(defined)
        assert d.land() is d.land()

    def test_a_split_cuts_the_bar_in_two(self, defined):
        d = _copy(defined, splits=[_seam_cut()])
        assert len(d.pieces()) == 2
        assert sum(p.area for p in d.pieces()) == pytest.approx(
            d.border.area, rel=1e-6)

    def test_each_connector_goes_with_one_piece(self, defined):
        d = _copy(defined, splits=[_seam_cut()])
        assert sum(map(len, d.piece_lobes())) == len(d.lobes)

    def test_too_big_turns_the_piece_to_fit(self, defined):
        """The T of bar is 100 x 52 mm: too big for 60 at any turn, and fits
        100 square -- the long arm goes along the bed."""
        assert defined.too_big(bed_mm=60.0, margin_mm=0.0) == [0]
        assert defined.too_big(bed_mm=101.0, margin_mm=0.0) == []

    def test_a_connector_on_a_split_is_at_fault(self, defined):
        p = defined.settings.connector_params()
        lobe = lobe_at(defined.border, *FREE_A, p)
        x = lobe.centroid.x
        cut = LineString([(x, FREE_A[1] - 5 * MPM), (x, FREE_A[1] + 5 * MPM)])
        d = _copy(defined, lobes=list(defined.lobes) + [lobe], splits=[cut])
        assert d.faults()[-1] in ("on_split", "crowded")
        assert "on_split" in d.faults()

    def test_the_answers_follow_a_change(self, defined):
        d = _copy(defined)
        assert len(d.pieces()) == 1 and d.too_big(60.0, 0.0) == [0]
        d.splits = [_seam_cut()]
        assert len(d.pieces()) == 2


class TestSaveAndLoad:

    def test_a_round_trip_keeps_everything(self, defined, tmp_path):
        d = _copy(defined, splits=[_seam_cut()],
                  settings=replace(defined.settings, lobe_ratio=1.9))
        written = BP.write_borders(d, tmp_path / "saved")
        names = {path.name for path in written}
        assert {"borders.geojson", "name_A.geojson", "name_B.geojson",
                BP.BORDER_SETTINGS_NAME, BP.SPLITS_NAME} <= names
        back = BP.read_borders(tmp_path / "saved")
        assert back.settings == d.settings
        assert len(back.lobes) == len(d.lobes)
        assert len(back.splits) == 1
        assert back.border.symmetric_difference(d.border).area < 1.0
        assert back.loaded_from == "saved"

    def test_each_province_in_the_crs_it_came_in(self, defined, tmp_path):
        BP.write_borders(defined, tmp_path / "out",
                         {"A": "EPSG:4326", "B": "EPSG:4326"})
        frame = gpd.read_file(tmp_path / "out" / "name_A.geojson")
        assert frame.crs.to_epsg() == 4326

    def test_saving_without_splits_removes_stale_ones(self, defined, tmp_path):
        BP.write_borders(_copy(defined, splits=[_seam_cut()]), tmp_path)
        BP.write_borders(defined, tmp_path)
        assert not (tmp_path / BP.SPLITS_NAME).exists()

    def test_a_folder_without_borders_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            BP.read_borders(tmp_path)


class TestLoadRegion:

    def test_into_the_working_crs_and_repaired(self, tmp_path):
        bowtie = box(0, 0, 1, 1).union(box(1, 1, 2, 2))
        path = tmp_path / "r.geojson"
        gpd.GeoDataFrame(geometry=[bowtie], crs="EPSG:3347").to_file(
            path, driver="GeoJSON")
        geom, source = BP.load_region(path)
        assert geom.is_valid and str(source) == "EPSG:3347"
