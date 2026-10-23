"""
test_island_editor_actions.py

The island editor's edits that had no test: **Merge**, **Remove small**,
**Buffer from gap**, importing and saving a GeoJSON, and the two workers that
run off the UI thread.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_keys.py.

Run with:  python -m pytest tests/test_island_editor_actions.py -v
"""

import geopandas as gpd
import pytest
from shapely.geometry import box
from shapely.ops import unary_union

import UI_island_editor as M
from lib.LIB_island_bridges import BridgeParams
from lib.LIB_stress_points import StressParams

pytestmark = pytest.mark.usefixtures("qapp")

KM = 1000.0
#: Two islands 15 km apart, a third far off, and an islet: at 1:8,000,000 a
#: printed millimetre is 8 km, so the pair bridges at about a millimetre.
NEAR_A = box(0, 0, 40 * KM, 40 * KM)
NEAR_B = box(55 * KM, 0, 95 * KM, 40 * KM)
FAR = box(400 * KM, 0, 440 * KM, 40 * KM)
ISLET = box(200 * KM, 200 * KM, 202 * KM, 202 * KM)
ISLANDS = [NEAR_A, NEAR_B, FAR, ISLET]


@pytest.fixture
def editor(qtbot, monkeypatch):
    """A shown editor holding the four islands, nothing selected, with any
    modal dialog recorded rather than shown."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    window.dialogs = []
    for kind in ("critical", "warning"):
        monkeypatch.setattr(M.QMessageBox, kind,
                            lambda *a, kind=kind: window.dialogs.append(
                                (kind, a[1])))
    window._set_state(M._as_multi(unary_union(ISLANDS)), [],
                      preserve_view=False)
    window.undo_stack.clear()
    yield window
    window.undo_stack.clear()


def _index(editor, island):
    """Where ``island`` sits in the editor's parts."""
    return next(i for i, part in enumerate(editor.parts)
                if part.equals(island))


def _select(editor, *islands):
    editor.selected = [_index(editor, island) for island in islands]


class TestMerge:

    def test_two_near_islands_become_one(self, editor):
        _select(editor, NEAR_A, NEAR_B)
        editor.buffer_spin.setValue(1.5)
        editor.merge_selected()
        assert len(editor.parts) == 3
        joined = max(editor.parts, key=lambda p: p.area)
        assert joined.covers(NEAR_A) and joined.covers(NEAR_B)
        assert editor.undo_stack.count() == 1

    def test_undo_brings_them_back(self, editor):
        _select(editor, NEAR_A, NEAR_B)
        editor.buffer_spin.setValue(1.5)
        editor.merge_selected()
        editor.undo_stack.undo()
        assert len(editor.parts) == 4

    def test_one_island_is_nothing_to_merge(self, editor):
        _select(editor, NEAR_A)
        editor.merge_selected()
        assert len(editor.parts) == 4 and editor.undo_stack.count() == 0

    def test_a_failure_is_reported_and_changes_nothing(self, editor,
                                                       monkeypatch):
        def boom(*args):
            raise RuntimeError("no merge today")
        monkeypatch.setattr(M, "merge_islands", boom)
        _select(editor, NEAR_A, NEAR_B)
        editor.merge_selected()
        assert editor.dialogs == [("critical", "Merge failed")]
        assert len(editor.parts) == 4


class TestRemoveSmall:

    def test_the_islet_goes(self, editor):
        # The islet is 4 km², 0.06 mm² printed; the others are 25 mm².
        editor.area_spin.setValue(1.0)
        editor.remove_small()
        assert len(editor.parts) == 3
        assert not any(part.equals(ISLET) for part in editor.parts)
        assert editor.undo_stack.count() == 1

    def test_nothing_below_the_threshold_is_no_edit(self, editor):
        editor.area_spin.setValue(0.01)
        editor.remove_small()
        assert len(editor.parts) == 4 and editor.undo_stack.count() == 0
        assert editor.status.text().startswith("Nothing below")


class TestBufferFromGap:

    def test_it_sets_a_buffer_that_bridges_the_pair(self, editor):
        _select(editor, NEAR_A, NEAR_B)
        editor.buffer_spin.setValue(0.0)
        editor.buffer_from_gap()
        found = editor.buffer_spin.value()
        assert found > 0.0
        # What it found does join them, and a hair less does not.
        assert len(M._polygons(M.merge_islands(
            [NEAR_A, NEAR_B], editor._to_km(found)))) == 1
        assert len(M._polygons(M.merge_islands(
            [NEAR_A, NEAR_B], editor._to_km(found * 0.8)))) == 2

    def test_touching_islands_need_no_buffer(self, editor):
        # Islands sharing an edge would be one part; two meeting at a corner
        # stay two, with no gap between them.
        corner_a, corner_b = box(0, 0, 10, 10), box(10, 10, 20, 20)
        editor._set_state(M._as_multi(unary_union([corner_a, corner_b])), [],
                          preserve_view=False)
        editor.selected = list(range(len(editor.parts)))
        before = editor.buffer_spin.value()
        editor.buffer_from_gap()
        assert editor.buffer_spin.value() == before
        assert editor.status.text().startswith("Already touching")

    def test_too_far_for_the_spin_says_so(self, editor):
        editor.buffer_spin.setMaximum(0.5)
        _select(editor, NEAR_A, FAR)
        before = editor.buffer_spin.value()
        editor.buffer_from_gap()
        assert editor.buffer_spin.value() == before


class TestImportAndSave:

    def _province(self, tmp_path, crs="EPSG:4326"):
        frame = gpd.GeoDataFrame({"name": ["Testland"], "code": ["TL"]},
                                 geometry=[unary_union(ISLANDS)],
                                 crs="EPSG:3347")
        if crs is not None:
            frame = frame.to_crs(crs)
        path = tmp_path / "Testland.geojson"
        frame.to_file(path, driver="GeoJSON")
        return path

    def _open(self, editor, monkeypatch, path):
        monkeypatch.setattr(M.QFileDialog, "getOpenFileName",
                            lambda *a, **k: (str(path), ""))
        editor.import_geojson()

    def test_a_round_trip_keeps_the_crs_the_fields_and_the_land(
            self, editor, monkeypatch, tmp_path):
        self._open(editor, monkeypatch, self._province(tmp_path))
        assert len(editor.parts) == 4
        assert editor.original_doc.equals(editor.doc)
        out = tmp_path / "saved.geojson"
        monkeypatch.setattr(M.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(out), ""))
        editor.save_geojson()
        back = gpd.read_file(out)
        assert back.crs.to_epsg() == 4326
        assert back.iloc[0]["name"] == "Testland"
        assert back.iloc[0]["code"] == "TL"
        area = back.to_crs("EPSG:3347").geometry.iloc[0].area
        assert area == pytest.approx(unary_union(ISLANDS).area, rel=1e-6)
        assert editor.undo_stack.isClean()

    def test_cancelling_the_dialog_changes_nothing(self, editor,
                                                   monkeypatch):
        before = editor.doc
        monkeypatch.setattr(M.QFileDialog, "getOpenFileName",
                            lambda *a, **k: ("", ""))
        editor.import_geojson()
        assert editor.doc is before

    def test_an_unreadable_file_is_reported(self, editor, monkeypatch,
                                            tmp_path):
        bad = tmp_path / "bad.geojson"
        bad.write_text("not json at all", encoding="utf-8")
        self._open(editor, monkeypatch, bad)
        assert editor.dialogs[0][:2] == ("critical", "Import failed")

    def test_an_empty_file_is_reported(self, editor, monkeypatch, tmp_path):
        empty = tmp_path / "empty.geojson"
        empty.write_text('{"type": "FeatureCollection", "features": []}',
                         encoding="utf-8")
        self._open(editor, monkeypatch, empty)
        assert editor.dialogs == [("warning", "Nothing to show")]

    def test_a_bare_geojson_is_read_as_wgs84(self, editor, monkeypatch,
                                             tmp_path):
        """GDAL reads a GeoJSON with no ``crs`` member as WGS 84, as the
        spec says, so a bare file of longitudes and latitudes opens like any
        other -- and the editor's own no-CRS warning never fires for one."""
        import json
        path = self._province(tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("crs", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        self._open(editor, monkeypatch, path)
        assert editor.dialogs == []
        assert editor.source_crs.to_epsg() == 4326
        assert len(editor.parts) == 4

    def test_saving_with_nothing_loaded_does_nothing(self, qtbot,
                                                     monkeypatch):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        asked = []
        monkeypatch.setattr(M.QFileDialog, "getSaveFileName",
                            lambda *a, **k: asked.append(1) or ("", ""))
        window.save_geojson()
        assert asked == []


class TestWorkers:
    """The workers run on a thread in the window; here they are run
    directly, which is the same code with the thread taken away."""

    @staticmethod
    def _run(worker):
        got = {}
        worker.done.connect(lambda *a: got.setdefault("done", a))
        worker.failed.connect(lambda msg: got.setdefault("failed", msg))
        worker.run()
        return got

    def test_bridge_worker_plans_and_applies(self):
        got = self._run(M.BridgeWorker([NEAR_A, NEAR_B], [NEAR_A, NEAR_B],
                                       None, BridgeParams(max_span_mm=5.0)))
        plan, geom = got["done"]
        assert plan.bridges
        assert len(M._polygons(geom)) == 1

    def test_bridge_worker_reports_a_failure(self):
        got = self._run(M.BridgeWorker([NEAR_A, NEAR_B], [NEAR_A, NEAR_B],
                                       None, "not params"))
        assert "failed" in got and "done" not in got

    def test_stress_worker_measures_then_remeasures(self):
        first = self._run(M.StressWorker(unary_union(ISLANDS),
                                         StressParams()))
        report, seconds = first["done"]
        assert report.measured and seconds >= 0.0
        again = self._run(M.StressWorker(unary_union(ISLANDS[:3]),
                                         StressParams(), report.grid))
        assert again["done"][0].measured

    def test_stress_worker_reports_a_failure(self):
        got = self._run(M.StressWorker(unary_union(ISLANDS), "not params"))
        assert "failed" in got and "done" not in got
