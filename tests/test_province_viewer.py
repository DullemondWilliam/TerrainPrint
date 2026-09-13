"""
test_province_viewer.py

UI_province_viewer.py: many provinces on one map, at the size they will print,
with one layer per stage of the pipeline.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_keys.py.

Run with:  python -m pytest tests/test_province_viewer.py -v

Three things carry the weight here. The viewer is a **bulk** tool, so one bad
file among forty must not cost the other thirty-nine -- the loading tests are
mostly about that. Its printed extent is the figure it exists to give, so that
is checked against the arithmetic rather than merely displayed. And comparing
two stages of the pipeline is a *blink* comparison, which is only a comparison
at all if the two frames are registered: most of TestBlinkingBetweenLayers is
about the three things that must not move when the layer does -- the view, each
province's colour, and the selection.
"""

import geopandas as gpd
import pytest
from matplotlib.colors import to_hex
from shapely.geometry import box

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut

import UI_province_viewer as V
from lib.LIB_screen_calibration import Monitor

pytestmark = pytest.mark.usefixtures("qapp")

#: Three provinces in a row, each 100 km wide, the first two sharing an edge and
#: the third set apart. In EPSG:3347 metres so no reprojection muddies the sums.
TOUCHING_A = box(0, 0, 100_000, 100_000)
TOUCHING_B = box(100_000, 0, 200_000, 100_000)
APART = box(400_000, 0, 500_000, 100_000)

#: The same Alpha after a stage of the pipeline has been at it: a 10 km bite out
#: of one corner. Small enough that only a blink would show it up.
TRIMMED_A = box(0, 0, 100_000, 90_000)

FAKE_PX_PER_MM = 4.116034147873318
FAKE_MONITOR = Monitor("TEST0001@2560x1440", "Test panel", 2560, 1440)


def _write(folder, name, geometry):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.geojson"
    gpd.GeoDataFrame({"name": [name]}, geometry=[geometry],
                     crs=V.EPSG_METRIC).to_file(path, driver="GeoJSON")
    return path


def _fire(window, sequence) -> bool:
    """
    Press a key the way the window binds it, not the way the test wishes it did.

    Calling ``next_layer`` directly would pass just as well with Space bound to
    something else entirely -- and the whole point of Space is that it is the
    key under the thumb.
    """
    wanted = QKeySequence(sequence)
    for shortcut in window.canvas.findChildren(QShortcut):
        if shortcut.key() == wanted:
            shortcut.activated.emit()
            return True
    return False


def _drawn(window) -> list:
    """
    The colour each province was actually given on the canvas, in draw order.

    Read back off the axes rather than out of ``_colours``, because the map can
    be coloured perfectly and then drawn from something else entirely. One
    ``PatchCollection`` goes on per province; the scale bars are lines and do
    not appear here.
    """
    return [to_hex(c.get_facecolor()[0]).upper() for c in window.ax.collections]


class _Click:
    """Enough of a matplotlib mouse event for the press/release pair to read."""

    def __init__(self, ax, x, y):
        self.inaxes, self.xdata, self.ydata = ax, x, y
        self.button, self.x, self.y = 1, 0, 0


def _click(window, x, y) -> None:
    """A press and a release in the same spot: a click, not a drag."""
    event = _Click(window.ax, x, y)
    window._on_press(event)
    window._on_release(event)


@pytest.fixture
def files(tmp_path):
    """Three province GeoJSON files on disk, already in the working CRS."""
    folder = tmp_path / "loose"
    return [_write(folder, name, geometry)
            for name, geometry in (("Alpha", TOUCHING_A), ("Beta", TOUCHING_B),
                                   ("Gamma", APART))]


@pytest.fixture
def stages(tmp_path):
    """
    Two folders standing for two stages of the pipeline.

    ``cleaned`` holds three provinces under their plain names; ``bordered``
    holds two of them, trimmed, under the ``_simplified`` names the border step
    writes. Which is exactly the pair the viewer has to recognise as the same
    two provinces.
    """
    cleaned = tmp_path / "cleaned"
    for name, geometry in (("CAN_Alpha", TOUCHING_A), ("CAN_Beta", TOUCHING_B),
                           ("CAN_Gamma", APART)):
        _write(cleaned, name, geometry)
    bordered = tmp_path / "bordered"
    for name, geometry in (("CAN_Alpha_simplified", TRIMMED_A),
                           ("CAN_Beta_simplified", TOUCHING_B)):
        _write(bordered, name, geometry)
    return cleaned, bordered


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


@pytest.fixture
def layered(viewer, stages):
    """Both stages loaded, sitting on the second one."""
    for folder in stages:
        viewer._load_layer(folder, V.geojson_in(folder))
    return viewer


@pytest.fixture
def deep_stack(viewer, tmp_path):
    """
    Three stages rather than two.

    With two layers, "next" and "previous" land on the same place and the last
    layer is also the one on show -- so a stack of two cannot tell a direction
    from its opposite, nor ``pop(active)`` from ``pop()``. Three can.
    """
    for name in ("one", "two", "three"):
        folder = tmp_path / name
        _write(folder, "Alpha", TOUCHING_A)
        _write(folder, "Beta", TOUCHING_B)
        viewer._load_layer(folder, V.geojson_in(folder))
    return viewer


class TestStageSuffix:
    """Which part of a filename is the province and which part is the stage."""

    def test_a_suffix_the_whole_folder_shares_is_the_stage(self):
        assert V.stage_suffix(["CAN_Alpha_simplified",
                               "CAN_Beta_simplified"]) == "_simplified"

    def test_a_folder_that_disagrees_has_no_stage_suffix(self):
        assert V.stage_suffix(["CAN_Alpha", "CAN_Beta", "CAN_Gamma"]) == ""

    def test_one_file_never_has_one(self):
        """``CAN_Alberta`` alone is not the province ``CAN`` at stage Alberta."""
        assert V.stage_suffix(["CAN_Alberta"]) == ""

    def test_a_suffix_only_some_files_carry_is_part_of_their_name(self):
        assert V.stage_suffix(["CAN_Alpha_simplified", "CAN_Beta"]) == ""

    def test_it_will_not_eat_a_whole_name(self):
        """Stripping would leave nothing to call the province."""
        assert V.stage_suffix(["_x", "_x"]) == ""

    def test_names_without_a_separator_are_left_alone(self):
        assert V.stage_suffix(["Alpha", "Beta"]) == ""


class TestLoadingFoldersAsLayers:
    """The unit of loading is a folder, because the unit of work is a stage."""

    def test_a_folder_becomes_one_layer(self, viewer, stages):
        cleaned, _ = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        assert len(viewer.layers) == 1
        assert viewer.layer.name == "cleaned"
        assert len(viewer.provinces) == 3

    def test_a_second_folder_is_a_second_layer_not_more_provinces(self, layered):
        assert len(layered.layers) == 2
        assert [len(layer) for layer in layered.layers] == [3, 2]
        assert len(layered.provinces) == 2      # only the layer on show is drawn

    def test_the_stage_suffix_is_taken_off_the_keys(self, layered):
        assert sorted(p.key for p in layered.layers[1].provinces) == [
            "CAN_Alpha", "CAN_Beta"]

    def test_the_name_on_screen_keeps_the_suffix(self, layered):
        """The key is for matching; the name is what the file is called."""
        assert sorted(p.name for p in layered.layers[1].provinces) == [
            "CAN_Alpha_simplified", "CAN_Beta_simplified"]

    def test_the_status_says_what_it_matched_on(self, layered):
        assert "_simplified" in layered.status.text()

    def test_a_new_layer_is_the_one_on_show(self, layered):
        assert layered.active == 1

    def test_layers_sharing_a_folder_name_are_told_apart(self, viewer, tmp_path):
        """``v1/provinces`` and ``v2/provinces`` is the obvious way to keep two."""
        for version in ("v1", "v2"):
            folder = tmp_path / version / "provinces"
            _write(folder, "Alpha", TOUCHING_A)
            viewer._load_layer(folder, V.geojson_in(folder))
        assert [layer.name for layer in viewer.layers] == [
            "provinces", "v2/provinces"]

    def test_removing_a_layer_leaves_the_others(self, layered):
        layered.remove_layer()
        assert len(layered.layers) == 1
        assert layered.layer.name == "cleaned"
        assert layered.active == 0

    def test_removing_takes_the_layer_on_show(self, deep_stack):
        """Not simply the last one — with three, those are different layers."""
        deep_stack.show_layer(0)
        deep_stack.remove_layer()
        assert [layer.name for layer in deep_stack.layers] == ["two", "three"]
        assert deep_stack.layer.name == "two"

    def test_removing_the_top_of_the_stack_steps_back(self, deep_stack):
        deep_stack.show_layer(2)
        deep_stack.remove_layer()
        assert deep_stack.active == 1 and deep_stack.layer.name == "two"

    def test_removing_the_last_layer_empties_the_window(self, viewer, stages):
        cleaned, _ = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        viewer.remove_layer()
        assert viewer.layers == [] and viewer.provinces == []
        assert not viewer.btn_drop_layer.isEnabled()

    def test_one_bad_file_does_not_cost_the_others(self, viewer, tmp_path):
        """
        A bulk tool must survive a dud. Forty provinces is exactly when you
        cannot afford a dialog that stops the run on the third one.
        """
        folder = tmp_path / "stage"
        _write(folder, "Alpha", TOUCHING_A)
        (folder / "Broken.geojson").write_text("this is not geojson",
                                               encoding="utf-8")
        _write(folder, "Gamma", APART)
        viewer._load_layer(folder, V.geojson_in(folder))
        assert len(viewer.provinces) == 2
        assert "failed" in viewer.status.text()


class TestBlinkingBetweenLayers:
    """
    The reason layers exist: swap fast enough and a change shows itself.

    A blink is only a comparison if the two frames are registered, so the three
    things that identify what you are looking at -- where the camera is, what
    colour each province is, and which ones are selected -- must survive a swap
    untouched. Each of those has its own test below because each was its own
    decision in the code.
    """

    def test_space_steps_to_the_next_layer(self, layered):
        layered.show_layer(0)
        assert _fire(layered, Qt.Key.Key_Space.value)
        assert layered.active == 1

    def test_space_wraps_so_two_layers_blink_on_one_key(self, layered):
        layered.show_layer(0)
        for expected in (1, 0, 1, 0):
            _fire(layered, Qt.Key.Key_Space.value)
            assert layered.active == expected

    def test_shift_space_steps_back(self, layered):
        layered.show_layer(0)
        assert _fire(layered,
                     Qt.Modifier.SHIFT.value | Qt.Key.Key_Space.value)
        assert layered.active == 1                  # wrapped the other way

    def test_space_and_shift_space_go_opposite_ways(self, deep_stack):
        """
        Which needs three layers to say at all.

        Over a stack of two, forward and back land on the same layer, so Space
        wired to ``previous_layer`` would look perfectly correct.
        """
        deep_stack.show_layer(0)
        _fire(deep_stack, Qt.Key.Key_Space.value)
        assert deep_stack.active == 1
        deep_stack.show_layer(0)
        _fire(deep_stack, Qt.Modifier.SHIFT.value | Qt.Key.Key_Space.value)
        assert deep_stack.active == 2               # wrapped the other way

    def test_the_number_keys_jump_straight_to_a_layer(self, deep_stack):
        for key, expected in ((Qt.Key.Key_1, 0), (Qt.Key.Key_3, 2),
                              (Qt.Key.Key_2, 1)):
            assert _fire(deep_stack, key.value)
            assert deep_stack.active == expected

    def test_a_number_key_past_the_last_layer_does_nothing(self, layered):
        _fire(layered, Qt.Key.Key_9.value)
        assert layered.active == 1

    def test_the_view_does_not_move_on_a_swap(self, layered):
        """
        The whole comparison rests on this. Re-fitting the view would show every
        province shift at once and say nothing about which ones really did.
        """
        layered.canvas.draw()
        layered.ax.set_xlim(0, 50_000)
        layered.ax.set_ylim(0, 50_000)
        before = layered.ax.get_xlim(), layered.ax.get_ylim()
        layered.show_layer(0)
        assert (layered.ax.get_xlim(), layered.ax.get_ylim()) == before

    def test_adding_a_layer_does_not_move_the_view_either(self, viewer, stages):
        """You add the stage you meant to compare; you keep the framing."""
        cleaned, bordered = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        viewer.canvas.draw()
        viewer.ax.set_xlim(0, 50_000)
        viewer.ax.set_ylim(0, 50_000)
        before = viewer.ax.get_xlim(), viewer.ax.get_ylim()
        viewer._load_layer(bordered, V.geojson_in(bordered))
        assert (viewer.ax.get_xlim(), viewer.ax.get_ylim()) == before

    def test_the_first_layer_is_framed(self, viewer, stages):
        """Nothing to stay registered with yet, so fit it."""
        cleaned, _ = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        viewer.canvas.draw()
        x0, x1 = viewer.ax.get_xlim()
        assert x0 <= 0 and x1 >= 500_000

    def test_a_province_keeps_its_colour_across_the_layers(self, layered):
        """
        Otherwise a blink reports that all of Canada changed.

        The greedy colouring runs in adjacency order, and two stages hold
        different numbers of files, so colouring per layer really would deal
        Alpha a different colour in each.
        """
        for layer in layered.layers:
            by_key = {p.key: layered._colours[p.key] for p in layer.provinces}
            assert by_key["CAN_Alpha"] == layered._colours["CAN_Alpha"]
        assert len({p.key for layer in layered.layers
                    for p in layer.provinces}) == 3

    def test_neighbours_still_differ(self, layered):
        """Alpha and Beta share an edge, in both stages."""
        assert layered._colours["CAN_Alpha"] != layered._colours["CAN_Beta"]

    def test_the_canvas_uses_the_colours_that_were_worked_out(self, layered):
        """
        The colour map can be right and go unread.

        Everything above this checks ``_colours``; this is the only thing that
        checks the map was drawn from it.
        """
        layered.show_layer(0)
        layered.canvas.draw()
        assert _drawn(layered) == [layered._colours[p.key]
                                   for p in layered.provinces]

    def test_a_province_is_drawn_the_same_colour_in_both_layers(self, layered):
        """What the blink actually depends on, measured off the canvas."""
        painted = []
        for index in (0, 1):
            layered.show_layer(index)
            layered.canvas.draw()
            painted.append(dict(zip((p.key for p in layered.provinces),
                                    _drawn(layered))))
        cleaned, bordered = painted
        assert cleaned["CAN_Alpha"] == bordered["CAN_Alpha"]
        assert cleaned["CAN_Beta"] == bordered["CAN_Beta"]
        assert cleaned["CAN_Alpha"] != cleaned["CAN_Beta"]

    def test_a_selected_province_is_drawn_as_selected(self, layered):
        layered.show_layer(0)
        layered.selected = {"CAN_Beta"}
        layered._after_selection_change()
        layered.canvas.draw()
        painted = dict(zip((p.key for p in layered.provinces), _drawn(layered)))
        assert painted["CAN_Beta"] == V.SELECT_COLOUR.upper()
        assert painted["CAN_Alpha"] == layered._colours["CAN_Alpha"]

    def test_the_selection_follows_the_province_not_the_index(self, layered):
        """
        Index 2 is Gamma in one layer and does not exist in the other. Selecting
        by index would have put the highlight on a different province -- or off
        the end of the list -- the moment the layer changed.
        """
        layered.show_layer(0)
        layered.selected = {"CAN_Gamma"}
        layered._after_selection_change()
        layered.show_layer(1)
        assert layered.picked == []                 # bordered has no Gamma
        layered.show_layer(0)
        assert [layered.provinces[i].name for i in layered.picked] == ["CAN_Gamma"]

    def test_a_province_in_both_layers_stays_selected(self, layered):
        layered.selected = {"CAN_Alpha"}
        layered.show_layer(0)
        assert [layered.provinces[i].key for i in layered.picked] == ["CAN_Alpha"]

    def test_the_caption_names_the_layer_on_show(self, layered):
        layered.show_layer(0)
        assert "cleaned" in layered.caption.text()
        layered.show_layer(1)
        assert "bordered" in layered.caption.text()

    def test_the_title_and_status_place_you_in_the_stack(self, layered):
        layered.show_layer(0)
        assert "1 of 2" in layered.windowTitle()
        assert "layer 1 of 2" in layered.status.text()

    def test_the_layer_list_follows_the_keyboard(self, layered):
        _fire(layered, Qt.Key.Key_1.value)
        assert layered.layer_box.currentIndex() == 0

    def test_choosing_from_the_list_swaps_the_layer(self, layered):
        layered.layer_box.setCurrentIndex(0)
        assert layered.active == 0

    def test_the_arrows_are_dead_with_one_layer(self, viewer, stages):
        cleaned, _ = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        assert not viewer.btn_next.isEnabled()
        assert not viewer.btn_prev.isEnabled()

    def test_the_printed_size_is_the_layer_on_show(self, layered):
        """Each stage has its own extent -- that difference is worth seeing."""
        layered.scale_spin.setValue(8_000_000)
        layered.show_layer(0)
        cleaned = layered._printed_extent_text()
        layered.show_layer(1)
        assert cleaned == "prints 62 x 12 mm"       # out to Gamma at 500 km
        assert layered._printed_extent_text() == "prints 25 x 12 mm"


class TestLoadingLooseFiles:
    """Files still load one at a time, into whichever layer you are on."""

    def test_every_file_becomes_a_province(self, loaded):
        assert len(loaded.provinces) == 3
        assert [p.name for p in loaded.provinces] == ["Alpha", "Beta", "Gamma"]

    def test_they_land_in_a_layer_of_their_own(self, loaded):
        assert len(loaded.layers) == 1
        assert loaded.layer.name == V.LOOSE_LAYER

    def test_they_join_the_layer_you_are_on(self, viewer, stages, files):
        cleaned, _ = stages
        viewer._load_layer(cleaned, V.geojson_in(cleaned))
        viewer._load(files)
        assert len(viewer.layers) == 1
        assert len(viewer.provinces) == 6

    def test_they_keep_their_true_positions(self, loaded):
        """Loaded together in a metre CRS, so they sit as the ground does."""
        minx, _, maxx, _ = loaded._extent()
        assert maxx - minx == pytest.approx(500_000.0)

    def test_loading_again_adds_rather_than_replaces(self, loaded, tmp_path):
        extra = _write(tmp_path / "extra", "Delta",
                       box(600_000, 0, 700_000, 100_000))
        loaded._load([extra])
        assert len(loaded.provinces) == 4

    def test_the_same_file_twice_is_loaded_once(self, loaded, files):
        loaded._load(files)
        assert len(loaded.provinces) == 3

    def test_one_bad_file_does_not_cost_the_others(self, viewer, files,
                                                   tmp_path):
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

    def test_clear_unloads_everything(self, layered):
        layered.clear_all()
        assert layered.layers == [] and layered.provinces == []
        assert layered._colours == {} and layered.selected == set()
        assert not layered.btn_real.isEnabled()


class TestTheMapColouring:
    """No two provinces that touch may be given the same colour."""

    def test_neighbours_differ(self, loaded):
        assert loaded._colours["Alpha"] != loaded._colours["Beta"]

    def test_provinces_that_do_not_touch_may_repeat_a_colour(self, loaded):
        """Six colours for any number of provinces only works if they recur."""
        assert len(set(loaded._colours.values())) <= len(V.PROVINCE_COLOURS)

    def test_every_province_gets_one(self, loaded):
        assert len(loaded._colours) == len(loaded.provinces)
        assert all(c in V.PROVINCE_COLOURS for c in loaded._colours.values())

    def test_a_lone_province_is_still_coloured(self, viewer, files):
        viewer._load([files[0]])
        assert viewer._colours == {"Alpha": V.PROVINCE_COLOURS[0]}


class TestSelection:
    """As in the island editor, minus anything that would act on it."""

    def test_clicking_semantics_by_key(self, loaded):
        loaded.selected = {"Alpha", "Gamma"}
        loaded.invert_selection()
        assert loaded.selected == {"Beta"}

    def test_all_and_none(self, loaded):
        loaded.select_all()
        assert len(loaded.picked) == 3
        loaded.clear_selection()
        assert loaded.selected == set()

    def test_invert_leaves_the_other_layers_alone(self, layered):
        """
        Swap, invert, swap back and you should have what you started with --
        not the complement of it, and not a selection the other stage erased.
        """
        layered.show_layer(0)
        layered.selected = {"CAN_Gamma"}
        layered.show_layer(1)                       # bordered: Alpha, Beta only
        layered.invert_selection()
        assert layered.selected == {"CAN_Gamma", "CAN_Alpha", "CAN_Beta"}

    def test_invert_is_live_at_both_extremes(self, loaded):
        loaded.clear_selection()
        assert loaded.btn_invert.isEnabled()
        loaded.select_all()
        assert loaded.btn_invert.isEnabled()

    def test_the_count_names_the_whole(self, loaded):
        loaded.selected = {"Beta"}
        loaded._after_selection_change()
        assert "1 of 3 selected" in loaded.lbl_count.text()

    def test_the_count_is_of_the_layer_on_show(self, layered):
        layered.select_all()                        # bordered: two provinces
        layered.show_layer(0)
        assert "2 of 3 selected" in layered.lbl_count.text()

    def test_the_status_names_what_is_selected(self, loaded):
        loaded.selected = {"Alpha"}
        loaded._after_selection_change()
        assert "Alpha" in loaded.status.text()

    def test_a_click_lands_on_the_province_under_it(self, loaded):
        assert loaded._province_at(50_000, 50_000) == 0
        assert loaded._province_at(150_000, 50_000) == 1
        assert loaded._province_at(300_000, 50_000) is None      # open water

    def test_a_click_selects_that_province(self, layered):
        layered.show_layer(0)
        _click(layered, 450_000, 50_000)                         # Gamma
        assert layered.selected == {"CAN_Gamma"}

    def test_a_click_selects_the_province_and_not_its_index(self, layered):
        """
        Index 0 in ``cleaned`` and index 0 in ``bordered`` need not be the same
        province, and here they are not, because ``bordered`` has no Gamma to
        push the others along. A click that recorded 0 would move the highlight
        on the next swap.
        """
        layered.show_layer(0)
        _click(layered, 150_000, 50_000)                         # Beta, index 1
        layered.show_layer(1)
        assert [layered.provinces[i].key for i in layered.picked] == ["CAN_Beta"]

    def test_clicking_a_selected_province_lets_it_go(self, layered):
        layered.show_layer(0)
        _click(layered, 50_000, 50_000)
        _click(layered, 50_000, 50_000)
        assert layered.selected == set()

    def test_clicking_open_water_changes_nothing(self, layered):
        layered.show_layer(0)
        _click(layered, 50_000, 50_000)
        _click(layered, 300_000, 50_000)
        assert layered.selected == {"CAN_Alpha"}


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

    def test_the_files_are_left_alone(self, layered, stages):
        written = [p for folder in stages for p in V.geojson_in(folder)]
        before = [p.read_bytes() for p in written]
        layered.select_all()
        layered.invert_selection()
        layered.next_layer()
        layered.scale_spin.setValue(2_000_000)
        assert [p.read_bytes() for p in written] == before
