"""
test_province_viewer.py

UI_province_viewer.py: many provinces on one map, at the size they will print.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_keys.py.

Run with:  python -m pytest tests/test_province_viewer.py -v

Two things carry the weight here. The viewer is a **bulk** tool, so one bad file
among forty must not cost the other thirty-nine -- the loading tests are mostly
about that. And its whole reason to exist is the printed extent, so that figure
is checked against the arithmetic rather than merely displayed.
"""

import geopandas as gpd
import pytest
from shapely.geometry import box

import UI_province_viewer as V
from lib.LIB_screen_calibration import Monitor

pytestmark = pytest.mark.usefixtures("qapp")

#: Three provinces in a row, each 100 km wide, the first two sharing an edge and
#: the third set apart. In EPSG:3347 metres so no reprojection muddies the sums.
TOUCHING_A = box(0, 0, 100_000, 100_000)
TOUCHING_B = box(100_000, 0, 200_000, 100_000)
APART = box(400_000, 0, 500_000, 100_000)

FAKE_PX_PER_MM = 4.116034147873318
FAKE_MONITOR = Monitor("TEST0001@2560x1440", "Test panel", 2560, 1440)


@pytest.fixture
def files(tmp_path):
    """Three province GeoJSON files on disk, already in the working CRS."""
    written = []
    for name, geometry in (("Alpha", TOUCHING_A), ("Beta", TOUCHING_B),
                           ("Gamma", APART)):
        path = tmp_path / f"{name}.geojson"
        gpd.GeoDataFrame({"name": [name]}, geometry=[geometry],
                         crs=V.EPSG_METRIC).to_file(path, driver="GeoJSON")
        written.append(path)
    return written


@pytest.fixture
def viewer(qtbot):
    window = V.ProvinceViewer()
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    qtbot.waitExposed(window)
    return window


@pytest.fixture
def loaded(viewer, files):
    viewer._load(files)
    return viewer


class TestLoadingManyAtOnce:
    """The point of the tool: any number of files, together, in one go."""

    def test_every_file_becomes_a_province(self, loaded):
        assert len(loaded.provinces) == 3
        assert [p.name for p in loaded.provinces] == ["Alpha", "Beta", "Gamma"]

    def test_they_keep_their_true_positions(self, loaded):
        """Loaded together in a metre CRS, so they sit as the ground does."""
        minx, _, maxx, _ = loaded._extent()
        assert maxx - minx == pytest.approx(500_000.0)

    def test_loading_again_adds_rather_than_replaces(self, loaded, tmp_path):
        extra = tmp_path / "Delta.geojson"
        gpd.GeoDataFrame({"name": ["Delta"]},
                         geometry=[box(600_000, 0, 700_000, 100_000)],
                         crs=V.EPSG_METRIC).to_file(extra, driver="GeoJSON")
        loaded._load([extra])
        assert len(loaded.provinces) == 4

    def test_the_same_file_twice_is_loaded_once(self, loaded, files):
        loaded._load(files)
        assert len(loaded.provinces) == 3

    def test_one_bad_file_does_not_cost_the_others(self, viewer, files,
                                                   tmp_path):
        """
        A bulk tool must survive a dud. Forty provinces is exactly when you
        cannot afford a dialog that stops the run on the third one.
        """
        broken = tmp_path / "Broken.geojson"
        broken.write_text("this is not geojson", encoding="utf-8")
        viewer._load([files[0], broken, files[1]])
        assert len(viewer.provinces) == 2
        assert "failed" in viewer.status.text()

    def test_a_file_with_no_land_is_skipped_quietly(self, viewer, tmp_path):
        empty = tmp_path / "Empty.geojson"
        empty.write_text('{"type":"FeatureCollection","features":[]}',
                         encoding="utf-8")
        viewer._load([empty])
        assert viewer.provinces == []

    def test_clear_unloads_everything(self, loaded):
        loaded.clear_all()
        assert loaded.provinces == [] and loaded._colours == []
        assert not loaded.btn_real.isEnabled()


class TestTheMapColouring:
    """No two provinces that touch may be given the same colour."""

    def test_neighbours_differ(self, loaded):
        colours = loaded._colours
        assert colours[0] != colours[1]          # Alpha and Beta share an edge

    def test_provinces_that_do_not_touch_may_repeat_a_colour(self, loaded):
        """Six colours for any number of provinces only works if they recur."""
        assert len(set(loaded._colours)) <= len(V.PROVINCE_COLOURS)

    def test_every_province_gets_one(self, loaded):
        assert len(loaded._colours) == len(loaded.provinces)
        assert all(c in V.PROVINCE_COLOURS for c in loaded._colours)

    def test_a_lone_province_is_still_coloured(self, viewer, files):
        viewer._load([files[0]])
        assert viewer._colours == [V.PROVINCE_COLOURS[0]]


class TestSelection:
    """As in the island editor, minus anything that would act on it."""

    def test_clicking_semantics_by_index(self, loaded):
        loaded.selected = [0, 2]
        loaded.invert_selection()
        assert sorted(loaded.selected) == [1]

    def test_all_and_none(self, loaded):
        loaded.select_all()
        assert len(loaded.selected) == 3
        loaded.clear_selection()
        assert loaded.selected == []

    def test_invert_is_live_at_both_extremes(self, loaded):
        loaded.clear_selection()
        assert loaded.btn_invert.isEnabled()
        loaded.select_all()
        assert loaded.btn_invert.isEnabled()

    def test_the_count_names_the_whole(self, loaded):
        loaded.selected = [1]
        loaded._after_selection_change()
        assert "1 of 3 selected" in loaded.lbl_count.text()

    def test_the_status_names_what_is_selected(self, loaded):
        loaded.selected = [0]
        loaded._after_selection_change()
        assert "Alpha" in loaded.status.text()

    def test_a_click_lands_on_the_province_under_it(self, loaded):
        assert loaded._province_at(50_000, 50_000) == 0
        assert loaded._province_at(150_000, 50_000) == 1
        assert loaded._province_at(300_000, 50_000) is None      # open water


class TestPrintedExtent:
    """The figure the tool exists to give: how big is all of this on paper."""

    def test_it_follows_the_arithmetic(self, loaded):
        """500 x 100 km at 1:8 000 000 is 62.5 x 12.5 mm."""
        loaded.scale_spin.setValue(8_000_000)
        assert "prints 62 x 12 mm" in loaded.status.text()

    def test_a_finer_reduction_prints_bigger(self, loaded):
        loaded.scale_spin.setValue(8_000_000)
        coarse = loaded.status.text()
        loaded.scale_spin.setValue(4_000_000)
        assert "prints 125 x 25 mm" in loaded.status.text()
        assert coarse != loaded.status.text()

    def test_there_is_nothing_to_measure_with_nothing_loaded(self, viewer):
        assert viewer._extent() is None
        assert viewer._printed_extent_text() == ""


class TestTheScaleControls:
    """The same print-scale machinery the editor carries."""

    def test_the_readout_tracks_the_scale(self, loaded):
        loaded.scale_spin.setValue(4_000_000)
        assert loaded.perkm_box.text() == "4 km/mm"

    def test_the_bars_appear_once_something_is_loaded(self, loaded):
        loaded.canvas.draw()
        assert all(artist.get_visible()
                   for pair in loaded._scale_artists for artist in pair)

    def test_the_bars_are_hidden_with_nothing_loaded(self, viewer):
        viewer.canvas.draw()
        assert all(not artist.get_visible()
                   for pair in viewer._scale_artists for artist in pair)

    def test_the_bars_settle(self, loaded):
        loaded.canvas.draw()
        assert loaded._update_scales() is False

    def test_real_size_puts_a_printed_millimetre_on_a_screen_millimetre(
            self, loaded, monkeypatch):
        monkeypatch.setattr(loaded, "_screen_calibration",
                            lambda: (FAKE_PX_PER_MM, FAKE_MONITOR))
        for denom in (4_000_000, 8_000_000):
            loaded.scale_spin.setValue(denom)
            loaded.canvas.draw()
            loaded.zoom_to_real_size()
            loaded.canvas.draw()
            ground_per_screen_mm = FAKE_PX_PER_MM / loaded._px_per_metre()
            assert ground_per_screen_mm == pytest.approx(denom / 1000.0)

    def test_real_size_declines_without_a_measurement(self, loaded, monkeypatch):
        monkeypatch.setattr(loaded, "_screen_calibration",
                            lambda: (None, FAKE_MONITOR))
        loaded.canvas.draw()
        before = loaded.ax.get_xlim()
        loaded.zoom_to_real_size()
        assert loaded.ax.get_xlim() == before
        assert "ScreenRuler" in loaded.status.text()


class TestItChangesNothing:
    """A viewer. There is no save, no undo, and no edit to need either."""

    def test_there_is_no_way_to_write(self, viewer):
        assert not hasattr(viewer, "save_geojson")
        assert not hasattr(viewer, "undo_stack")

    def test_the_files_are_left_alone(self, loaded, files):
        before = [p.read_bytes() for p in files]
        loaded.select_all()
        loaded.invert_selection()
        loaded.scale_spin.setValue(2_000_000)
        assert [p.read_bytes() for p in files] == before
