"""
test_failure_paths.py

What the libraries promise when things go wrong, held to it.

Each lib documents a failure contract -- an edit never destroys the
document, a measurement that could not be made says so rather than reporting
a clean province, bad input is refused with a message -- and until these
tests none of those branches ran.  The geometry that breaks GEOS for real is a
self-intersecting "bowtie" ring; where nothing real fails, the inner step is
made to throw.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_failure_paths.py -v
"""

import json

import geopandas as gpd
import numpy as np
import pytest
import shapely
import trimesh
from shapely.geometry import LineString, Polygon, box

from lib import LIB_border_plan as BP
from lib import LIB_border_split as BS
from lib import LIB_island_bridges as IB
from lib import LIB_island_merging as IM
from lib import LIB_stl_reconcile as SR
from lib import LIB_stress_points as SP
from lib import LIB_terrain_stl as TS
from lib import LIB_thin_features as TF
from lib.LIB_border_connectors import ConnectorParams
from lib.LIB_island_bridging import draw_bridge
from lib.LIB_island_erasing import erase_stroke

KM = 1000.0
#: Crosses itself at its centre: every GEOS boolean on it throws.
BOWTIE = Polygon([(0, 0), (10 * KM, 10 * KM), (10 * KM, 0), (0, 10 * KM)])
OTHER = box(12 * KM, 0, 20 * KM, 8 * KM)


def _boom(*args, **kwargs):
    raise RuntimeError("made to fail")


class TestInvalidIslandsAreRepaired:
    """An invalid island is repaired, not thrown on: the fallback union
    threw too, so merging one used to raise out of the lib."""

    def test_the_bowtie_really_breaks_a_union(self):
        assert not BOWTIE.is_valid
        with pytest.raises(shapely.errors.GEOSException):
            shapely.unary_union([BOWTIE, OTHER])

    def test_merge_islands(self):
        merged = IM.merge_islands([BOWTIE, OTHER], 5.0)
        assert merged.is_valid
        assert merged.covers(OTHER)
        assert merged.area >= shapely.make_valid(BOWTIE).area + OTHER.area

    def test_apply_bridges(self):
        slab = box(9 * KM, 4 * KM, 13 * KM, 6 * KM)
        merged = IB.apply_bridges([BOWTIE, OTHER], [IB.Bridge(polygon=slab)])
        assert merged.is_valid
        assert merged.covers(OTHER) and merged.covers(slab)

    def test_a_valid_island_is_left_as_it_is(self):
        assert IM._valid(OTHER) is OTHER

    def test_a_repair_keeps_only_land(self):
        fixed = IM._valid(BOWTIE)
        assert fixed.geom_type == "MultiPolygon" and fixed.is_valid


class TestEditsNeverDestroyTheDocument:

    def test_draw_on_a_broken_document_returns_it(self):
        assert draw_bridge(BOWTIE, [(5 * KM, 5 * KM), (15 * KM, 5 * KM)],
                           1.0) is BOWTIE

    def test_erase_on_a_broken_document_returns_it(self):
        assert erase_stroke(BOWTIE, [(2 * KM, 5 * KM), (8 * KM, 5 * KM)],
                            1.0) is BOWTIE

    @pytest.mark.parametrize("failure", [_boom, lambda *a: Polygon()],
                             ids=["throws", "comes-back-empty"])
    def test_merge_falls_back_to_the_plain_union(self, monkeypatch, failure):
        a, b = box(0, 0, 10 * KM, 10 * KM), box(11 * KM, 0, 20 * KM, 10 * KM)
        monkeypatch.setattr(IM, "_fill_new_holes", failure)
        merged = IM.merge_islands([a, b], 5.0)
        assert merged.equals(shapely.unary_union([a, b]))

    @pytest.mark.parametrize("failure", [_boom, lambda *a: Polygon()],
                             ids=["throws", "comes-back-empty"])
    def test_bridges_fall_back_to_the_plain_union(self, monkeypatch, failure):
        a, b = box(0, 0, 10 * KM, 10 * KM), box(11 * KM, 0, 20 * KM, 10 * KM)
        monkeypatch.setattr(IB, "_fill_new_holes", failure)
        slab = IB.Bridge(polygon=box(9 * KM, 4 * KM, 12 * KM, 6 * KM))
        merged = IB.apply_bridges([a, b], [slab])
        assert merged.equals(shapely.unary_union([a, b]))


class TestMeasurementsSayWhenTheyCannot:
    """A province that could not be measured must not come back clean."""

    PROVINCE = box(0, 0, 400 * KM, 300 * KM)

    def test_thin_features_gives_up_after_a_repair(self, monkeypatch):
        monkeypatch.setattr(TF, "_opened", _boom)
        report = TF.thin_features(self.PROVINCE, TF.ThinParams())
        assert report.measured is False

    def test_thin_features_gives_up_when_it_cannot_repair(self, monkeypatch):
        monkeypatch.setattr(TF, "_opened", _boom)
        monkeypatch.setattr(TF.shapely, "make_valid", _boom)
        report = TF.thin_features(self.PROVINCE, TF.ThinParams())
        assert report.measured is False

    @pytest.mark.parametrize("repair", ["fails-again", "cannot-repair"])
    def test_printable_hands_the_land_back_untouched(self, monkeypatch,
                                                     repair):
        monkeypatch.setattr(TF, "_opened", _boom)
        if repair == "cannot-repair":
            monkeypatch.setattr(TF.shapely, "make_valid", _boom)
        # The same land: the province itself, or its repaired copy.
        kept = TF.printable(self.PROVINCE, TF.ThinParams())
        assert kept.equals(self.PROVINCE)

    def test_stress_points_cannot_repair(self, monkeypatch):
        monkeypatch.setattr(SP.shapely, "make_valid", _boom)
        assert SP.stress_points(BOWTIE, SP.StressParams()).measured is False

    def test_stress_points_repair_leaves_nothing(self, monkeypatch):
        monkeypatch.setattr(SP.shapely, "make_valid", lambda g: Polygon())
        assert SP.stress_points(BOWTIE, SP.StressParams()).measured is False

    def test_stress_points_cannot_rasterise(self, monkeypatch):
        monkeypatch.setattr(SP, "_grid_for", _boom)
        report = SP.stress_points(self.PROVINCE, SP.StressParams())
        assert report.measured is False

    def test_stress_points_cannot_rasterise_or_repair(self, monkeypatch):
        monkeypatch.setattr(SP, "_grid_for", _boom)
        monkeypatch.setattr(SP.shapely, "make_valid", _boom)
        report = SP.stress_points(self.PROVINCE, SP.StressParams())
        assert report.measured is False


class TestLoadRegion:
    """A region file is refused with its name, or read as the spec says."""

    def _write(self, tmp_path, geometry, crs="EPSG:4326"):
        path = tmp_path / "region.geojson"
        gpd.GeoDataFrame({"name": ["r"] * len(geometry)},
                         geometry=geometry, crs=crs).to_file(path,
                                                             driver="GeoJSON")
        return path

    def test_no_features(self, tmp_path):
        path = tmp_path / "empty.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection",
                                    "features": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="empty.geojson has no features"):
            BP.load_region(path)

    def test_no_polygon(self, tmp_path):
        path = self._write(tmp_path, [LineString([(-80, 50), (-79, 51)])])
        with pytest.raises(ValueError, match="no polygon"):
            BP.load_region(path)

    def test_no_crs_is_read_as_wgs84(self, tmp_path):
        """Bare GeoJSON is WGS 84 by spec, so the same file with and
        without its CRS lands in the same place."""
        square = box(-80, 50, -79, 51)
        with_crs, _ = BP.load_region(self._write(tmp_path, [square]))
        path = tmp_path / "bare.geojson"
        data = json.loads((tmp_path / "region.geojson").read_text())
        data.pop("crs", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        bare, source_crs = BP.load_region(path)
        assert source_crs.to_epsg() == 4326
        assert bare.symmetric_difference(with_crs).area < 1.0


class TestTerrainEdges:

    def test_a_missing_or_broken_sidecar_is_none(self, tmp_path):
        stl = tmp_path / "piece.stl"
        assert TS.read_frame(stl) is None
        stl.with_suffix(".frame.json").write_text("{not json",
                                                  encoding="utf-8")
        assert TS.read_frame(stl) is None

    def test_simplify_that_fails_keeps_the_mesh(self, monkeypatch):
        mesh = trimesh.creation.box((10, 10, 2))
        monkeypatch.setattr(TS, "_to_manifold", _boom)
        assert TS.simplify_solid(mesh, 0.1) is mesh

    def test_simplify_that_comes_back_unprintable_keeps_the_mesh(
            self, monkeypatch):
        mesh = trimesh.creation.box((10, 10, 2))
        monkeypatch.setattr(TS, "_man_to_trimesh", lambda man: None)
        assert TS.simplify_solid(mesh, 0.1) is mesh


class TestSplitInputs:

    BAR = trimesh.creation.box((300.0, 20.0, 2.0))

    def test_a_bed_structure_supplies_the_bed(self):
        pieces = BS.split_to_bed(self.BAR, bed_mm=1000.0, margin_mm=0.0,
                                 bed=BS.BedParams(bed_mm=120.0,
                                                  margin_mm=0.0))
        assert len(pieces) > 1

    @pytest.mark.parametrize("bed", [(100.0,), (100.0, 0.0), -5.0])
    def test_a_bad_bed_is_refused(self, bed):
        with pytest.raises(ValueError):
            BS.split_to_bed(self.BAR, bed_mm=bed, margin_mm=0.0)

    def test_a_rectangular_bed(self):
        pieces = BS.split_to_bed(self.BAR, bed_mm=(320.0, 100.0),
                                 margin_mm=0.0)
        assert len(pieces) == 1

    def test_a_mesh_from_a_path(self, tmp_path):
        path = tmp_path / "bar.stl"
        self.BAR.export(path)
        assert len(BS.split_to_bed(path, bed_mm=120.0, margin_mm=0.0)) > 1

    def test_not_a_mesh(self):
        with pytest.raises(TypeError):
            BS.split_to_bed(42, bed_mm=120.0, margin_mm=0.0)

    def test_a_flat_footprint_turns_to_nothing(self):
        assert BS.bed_turn(np.zeros((0, 2)), 100.0) == 0.0


class TestReconcileEdges:

    def test_an_empty_mesh_has_no_footprint(self):
        assert SR.base_footprint(trimesh.Trimesh()) is None

    def test_no_footprint_is_refused(self):
        with pytest.raises(ValueError, match="no usable footprint"):
            SR.reconcile("piece", trimesh.creation.box((10, 10, 2)),
                         box(0, 0, 1, 1), ConnectorParams(), 0.9,
                         footprint_fn=lambda mesh: None)

    def test_a_poor_fit_is_refused(self):
        mesh = trimesh.creation.box((10, 10, 2))
        far_off = box(0, 0, 8_000.0 * 100, 8_000.0)      # the wrong shape
        with pytest.raises(ValueError, match="could not reconcile"):
            SR.reconcile("piece", mesh, far_off, ConnectorParams(), 0.9)

    def test_overlap_of_broken_footprints_is_still_measured(self):
        square = box(0, 0, 10, 10)
        assert SR._iou(BOWTIE, square) >= 0.0
