"""
test_print_all.py

``lib.LIB_print_all``: every province's solid and the border bar's pieces in
one run, into one folder -- on the two-province map the border tests use,
over a small DEM written for it.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_print_all.py -v
"""

from dataclasses import replace

import numpy as np
import pytest
import rasterio
import trimesh
from affine import Affine
from shapely.geometry import box

from lib import LIB_border_plan as BP
from lib import LIB_print_all as PA
from lib.LIB_terrain_stl import TerrainParams

KM = 1000.0
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
C = box(7_000 * KM, 1_400 * KM, 7_800 * KM, 1_800 * KM)
RES = 3200.0
#: The solid's settings: a scale a sloping DEM reads at, and the Base deep
#: enough for the connectors' pockets.
TERRAIN = TerrainParams(expect_crs=None, z_exaggeration=50.0,
                        base_height_mm=3.0)


@pytest.fixture(scope="module")
def dem(tmp_path_factory):
    """A sloping DEM in the print CRS over both provinces, with a margin."""
    path = tmp_path_factory.mktemp("dem") / "dem.tif"
    x0, y0 = 6_960 * KM, 1_440 * KM
    cols, rows = int(880 * KM / RES), int(480 * KM / RES)
    data = np.tile(np.linspace(50, 900, cols, dtype="float32"), (rows, 1))
    with rasterio.open(path, "w", driver="GTiff", width=cols, height=rows,
                       count=1, dtype="float32", crs="EPSG:3347",
                       nodata=-32767.0, transform=Affine.translation(x0, y0)
                       * Affine.scale(RES, -RES)) as ds:
        ds.write(data, 1)
    return path


@pytest.fixture(scope="module")
def defined():
    return BP.define_borders({"A": A, "B": B}, {"C": C}, BP.BorderSettings())


def _run(dem, tmp_path, definition, **kw):
    provinces = {n: definition.targets[n] for n in definition.targets} \
        if definition is not None else {"A": A, "B": B}
    kw.setdefault("res_m", RES)
    kw.setdefault("terrain", TERRAIN)
    return PA.print_all(dem, tmp_path / "out", provinces, tmp_path / "work",
                        definition=definition, **kw)


class TestEverything:

    def test_every_province_and_the_bar(self, dem, tmp_path, defined):
        report = _run(dem, tmp_path, defined)
        out = tmp_path / "out"
        assert not report.failed and not report.bar_error
        for name in ("A", "B"):
            stl = out / f"{name}.stl"
            assert stl.exists() and (out / f"{name}.frame.json").exists()
            assert trimesh.load(stl).is_watertight
        assert report.bar_paths
        assert all(trimesh.load(p).is_watertight for p in report.bar_paths)
        assert [p.name for p in report.bar_paths][0] == "border_001.stl"

    def test_each_is_pocketed_for_its_connectors(self, dem, tmp_path,
                                                 defined):
        report = _run(dem, tmp_path, defined)
        for done in report.provinces:
            assert done.pockets == len(defined.province_lobes(done.name))
            assert done.pockets > 0

    def test_without_borders_plain_solids_and_no_bar(self, dem, tmp_path):
        report = _run(dem, tmp_path, None)
        assert [p.pockets for p in report.provinces] == [0, 0]
        assert report.bar_paths == [] and not report.failed

    def test_auto_resolution_fits_each_province(self, dem, tmp_path,
                                                defined):
        limited = replace(TERRAIN, max_triangles=20_000)
        report = _run(dem, tmp_path, defined, res_m=None, terrain=limited)
        for done in report.provinces:
            assert done.res_m > RES and not done.error
            assert done.triangles <= 20_000 * 2      # cut solid, plus pockets


class TestWhatGoesWrong:

    def test_one_province_failing_does_not_stop_the_rest(self, dem,
                                                         tmp_path):
        far = box(1_000 * KM, 1_000 * KM, 1_100 * KM, 1_100 * KM)   # no DEM
        report = PA.print_all(dem, tmp_path / "out", {"A": A, "far": far},
                              tmp_path / "work", res_m=RES, terrain=TERRAIN)
        assert [p.name for p in report.failed] == ["far"]
        assert (tmp_path / "out" / "A.stl").exists()

    def test_it_can_be_stopped(self, dem, tmp_path, defined):
        steps = []
        report = _run(dem, tmp_path, defined,
                      progress=lambda step, total, text: steps.append(step),
                      cancelled=lambda: len(steps) >= 1)
        assert report.cancelled
        assert len(report.provinces) == 1 and report.bar_paths == []

    def test_progress_counts_every_piece(self, dem, tmp_path, defined):
        seen = []
        _run(dem, tmp_path, defined,
             progress=lambda step, total, text: seen.append((step, total)))
        assert seen[0] == (0, 3) and seen[-1] == (3, 3)


class TestTheBar:

    def test_stale_pieces_are_cleared(self, dem, tmp_path, defined):
        out = tmp_path / "out"
        out.mkdir()
        (out / "border_009.stl").write_text("old")
        report = _run(dem, tmp_path, defined)
        assert not (out / "border_009.stl").exists()
        assert len(list(out.glob("border_*.stl"))) == len(report.bar_paths)

    def test_removed_pieces_are_not_written(self, dem, tmp_path, defined):
        """Split the A/B arm off, take it out: one piece fewer is written."""
        from shapely.geometry import LineString
        cut = LineString([(7_391.9 * KM, 1_200 * KM),
                          (7_408.1 * KM, 1_200 * KM)])
        split = replace(defined, lobes=list(defined.lobes), splits=[cut])
        whole = _run(dem, tmp_path / "1", split)
        assert len(whole.bar_paths) == 2
        arm = split.pieces()[[p.bounds[1] for p in split.pieces()].index(
            min(p.bounds[1] for p in split.pieces()))]
        gone = replace(split, removed=[arm.representative_point()])
        report = _run(dem, tmp_path / "2", gone)
        assert len(report.bar_paths) == 1 and not report.bar_error

    def test_with_every_piece_removed_it_says_so(self, dem, tmp_path,
                                                 defined):
        gone = replace(defined, lobes=list(defined.lobes),
                       removed=[defined.pieces()[0].representative_point()])
        report = _run(dem, tmp_path, gone)
        assert report.bar_paths == []
        assert "removed" in report.bar_error


class TestOutputs:

    def test_it_lists_what_would_be_written_over(self, tmp_path):
        (tmp_path / "A.stl").write_text("x")
        (tmp_path / "A.frame.json").write_text("{}")
        (tmp_path / "border_002.stl").write_text("x")
        (tmp_path / "other.stl").write_text("x")
        names = {p.name for p in PA.outputs(tmp_path, ["A", "B"])}
        assert names == {"A.stl", "A.frame.json", "border_002.stl"}
