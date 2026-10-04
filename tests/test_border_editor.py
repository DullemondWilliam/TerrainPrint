"""
test_border_editor.py

UI_border_editor.py: the list of borders, and the seam editor it opens.

pytest-style for pytest-qt's ``qapp`` fixture, as in test_shared_border_ui.py.
The seam geometry is covered by test_shared_border.py and the ledger by
test_border_ledger.py; what this file guards is the two screens agreeing with
the files -- a border listed as cleaned must be one whose files say so.

Run with:  python -m pytest tests/test_border_editor.py -v
"""

import numpy as np
import pytest
from shapely.geometry import Polygon, box

import geopandas as gpd

import UI_border_editor as U
from lib.LIB_border_ledger import LEDGER_NAME
from lib.LIB_shared_border import simplify_shared_border

pytestmark = pytest.mark.usefixtures("qapp")

MM = 8_000.0
O = 7_000_000.0


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "information", lambda *a, **k: None)


def _zigzag_pair():
    """West and Middle share a sub-nozzle zigzag; East is a plain neighbour."""
    ys = np.arange(0.0, 20.1, 0.1)
    seam = [(O + (20 + (0.05 if i % 2 else -0.05)) * MM, O + y * MM)
            for i, y in enumerate(ys)]
    west = Polygon([(O, O)] + seam + [(O, O + 20 * MM)])
    middle = Polygon([(O + 40 * MM, O)] + seam + [(O + 40 * MM, O + 20 * MM)])
    east = box(O + 40 * MM, O, O + 60 * MM, O + 20 * MM)
    far = box(O, O + 100 * MM, O + 5 * MM, O + 105 * MM)
    return {"CAN_West": west, "CAN_Middle": middle, "CAN_East": east,
            "CAN_Far": far}


def _source(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    for name, geom in _zigzag_pair().items():
        gpd.GeoDataFrame(geometry=[geom], crs="EPSG:3347").to_crs(
            "EPSG:4326").to_file(folder / f"{name}.geojson", driver="GeoJSON")
    return folder


def _window(tmp_path):
    window = U.BorderEditor()
    window.set_save_dir(tmp_path / "saved")
    window.load(U.geojson_in(_source(tmp_path)))
    return window


def _clean(window, a, b):
    """Select, open, simplify synchronously, apply."""
    window.toggle_province(a)
    window.toggle_province(b)
    window.open_editor()
    page = window.seam_page
    page._on_done(simplify_shared_border(page.geom_a, page.geom_b,
                                         page._params()))
    page.btn_save.click()


class TestBordersPage:

    def test_lists_every_border_as_not_cleaned_at_first(self, tmp_path):
        window = _window(tmp_path)
        assert [b.key for b in window.borders] == \
            ["CAN_East|CAN_Middle", "CAN_Middle|CAN_West"]
        assert window.list_todo.count() == 2
        assert window.list_done.count() == 0
        assert window.box_todo.title() == "Not cleaned (2)"

    def test_names_drop_the_country_prefix(self, tmp_path):
        window = _window(tmp_path)
        assert window.list_todo.item(0).text().startswith("East — Middle")

    def test_edit_needs_two_neighbours(self, tmp_path):
        window = _window(tmp_path)
        assert not window.btn_edit.isEnabled()
        window.toggle_province("CAN_West")
        assert not window.btn_edit.isEnabled()
        window.toggle_province("CAN_Far")
        assert not window.btn_edit.isEnabled()
        assert "do not share a border" in window.selection_label.text()
        window.toggle_province("CAN_Middle")       # replaces the older pick
        assert window.selected == ["CAN_Far", "CAN_Middle"]
        window.toggle_province("CAN_Far")
        window.toggle_province("CAN_West")
        assert window.btn_edit.isEnabled()

    def test_clicking_a_listed_border_selects_its_pair(self, tmp_path):
        window = _window(tmp_path)
        window._on_item_clicked(window.list_todo.item(1))
        assert sorted(window.selected) == ["CAN_Middle", "CAN_West"]
        assert window.list_todo.item(1).isSelected()

    def test_clicking_the_map_picks_the_province_under_the_pointer(self, tmp_path):
        window = _window(tmp_path)
        assert window.province_at(O + 5 * MM, O + 5 * MM) == "CAN_West"
        assert window.province_at(O - 5 * MM, O) is None

    def test_ledger_file_is_not_imported_as_a_province(self, tmp_path):
        folder = tmp_path / "saved"        # tmp_path holds conftest's config
        folder.mkdir()
        (folder / LEDGER_NAME).write_text("{}", encoding="utf-8")
        (folder / "a.geojson").write_text("{}", encoding="utf-8")
        assert [p.name for p in U.geojson_in(folder)] == ["a.geojson"]


class TestEditPage:

    def test_opens_the_pair_on_the_second_screen(self, tmp_path):
        window = _window(tmp_path)
        window.select_border(window.borders[1])
        window.open_editor()
        assert window.stack.currentIndex() == U.PAGE_EDIT
        assert window.seam_page.seam is not None
        assert window.seam_page.btn_run.isEnabled()
        assert "Middle ⇄ West" in window.seam_page.heading.text()

    def test_back_returns_without_saving(self, tmp_path):
        window = _window(tmp_path)
        window.select_border(window.borders[1])
        window.open_editor()
        window.seam_page.btn_back.click()
        assert window.stack.currentIndex() == U.PAGE_BORDERS
        assert not (tmp_path / "saved").exists()

    def test_back_is_locked_while_a_run_is_in_flight(self, tmp_path):
        window = _window(tmp_path)
        window.select_border(window.borders[1])
        window.open_editor()
        window.seam_page._set_busy(True)
        assert not window.seam_page.btn_back.isEnabled()
        window.seam_page._set_busy(False)
        assert window.seam_page.btn_back.isEnabled()


class TestApply:

    def test_apply_saves_both_and_moves_the_border_to_cleaned(self, tmp_path):
        window = _window(tmp_path)
        _clean(window, "CAN_Middle", "CAN_West")
        saved = tmp_path / "saved"
        assert sorted(p.name for p in saved.iterdir()) == \
            ["CAN_Middle.geojson", "CAN_West.geojson", LEDGER_NAME]
        assert window.stack.currentIndex() == U.PAGE_BORDERS
        assert [b.key for b in window.cleaned()] == ["CAN_Middle|CAN_West"]
        assert window.list_done.count() == 1
        assert window.list_todo.count() == 1

    def test_saved_in_the_crs_the_file_arrived_in(self, tmp_path):
        window = _window(tmp_path)
        _clean(window, "CAN_Middle", "CAN_West")
        frame = gpd.read_file(tmp_path / "saved" / "CAN_West.geojson")
        assert frame.crs.to_epsg() == 4326

    def test_cleaning_one_border_leaves_the_neighbour_alone(self, tmp_path):
        window = _window(tmp_path)
        before = next(b for b in window.borders if b.key == "CAN_East|CAN_Middle")
        _clean(window, "CAN_Middle", "CAN_West")
        after = next(b for b in window.borders if b.key == "CAN_East|CAN_Middle")
        assert after.seam.hausdorff_distance(before.seam) < 1.0

    def test_cleaned_survives_a_new_session(self, tmp_path):
        """Reopening the originals picks up the saved copies, and the ledger."""
        window = _window(tmp_path)
        _clean(window, "CAN_Middle", "CAN_West")
        again = U.BorderEditor()
        again.set_save_dir(tmp_path / "saved")
        again.load(U.geojson_in(tmp_path / "source"))
        assert [b.key for b in again.cleaned()] == ["CAN_Middle|CAN_West"]
        assert again.provinces["CAN_West"].resumed
        assert not again.provinces["CAN_East"].resumed

    def test_a_province_changed_since_drops_back_to_not_cleaned(self, tmp_path):
        window = _window(tmp_path)
        _clean(window, "CAN_Middle", "CAN_West")
        # Re-export both from elsewhere, with the seam somewhere else.
        saved = tmp_path / "saved"
        for name, geom in (("CAN_West", box(O, O, O + 21 * MM, O + 20 * MM)),
                           ("CAN_Middle",
                            box(O + 21 * MM, O, O + 40 * MM, O + 20 * MM))):
            (saved / f"{name}.geojson").unlink()
            gpd.GeoDataFrame(geometry=[geom], crs="EPSG:3347").to_file(
                saved / f"{name}.geojson", driver="GeoJSON")
        again = U.BorderEditor()
        again.set_save_dir(saved)
        again.load(U.geojson_in(tmp_path / "source"))
        border = again.border_between("CAN_Middle", "CAN_West")
        assert again.status_of(border) == "changed"
        assert again.list_done.count() == 0
        assert any("changed since cleaned" in again.list_todo.item(i).text()
                   for i in range(again.list_todo.count()))


# ---------------------------------------------------------------------------
# Fit check and push brush
# ---------------------------------------------------------------------------

def _mushroom_window(tmp_path):
    """West has a locking mushroom tab reaching into East."""
    seam = [(20, 0), (20, 17), (22, 17), (22, 15), (28, 15), (28, 25),
            (22, 25), (22, 19), (20, 19), (20, 40)]
    seam = [(O + x * MM, O + y * MM) for x, y in seam]
    west = Polygon([(O, O)] + seam + [(O, O + 40 * MM)])
    east = Polygon([(O + 40 * MM, O)] + seam + [(O + 40 * MM, O + 40 * MM)])
    folder = tmp_path / "mushroom"
    folder.mkdir()
    for name, geom in (("CAN_West", west), ("CAN_East", east)):
        gpd.GeoDataFrame(geometry=[geom], crs="EPSG:3347").to_file(
            folder / f"{name}.geojson", driver="GeoJSON")
    window = U.BorderEditor()
    window.set_save_dir(tmp_path / "saved")
    window.load(U.geojson_in(folder))
    window.select_border(window.borders[0])
    window.open_editor()
    return window


def _fills(ax, colour):
    from matplotlib.colors import to_rgba
    want = tuple(round(c, 3) for c in to_rgba(colour)[:3])
    return [p for p in ax.patches
            if tuple(round(c, 3) for c in p.get_facecolor()[:3]) == want]


def _grow(page, name):
    """The push mode that grows the province called ``name``."""
    return U.GROW_A if page.province_a.name == name else U.GROW_B


ACROSS_THE_TAB = [(O + 21 * MM, O + 20 * MM), (O + 28 * MM, O + 20 * MM)]


class TestFitOnTheList:

    def test_each_border_says_whether_it_meshes(self, tmp_path):
        window = _window(tmp_path)
        texts = [window.list_todo.item(i).text()
                 for i in range(window.list_todo.count())]
        assert all(" · " in t for t in texts)
        assert any("meshes cleanly" in t for t in texts)

    def test_a_locking_border_says_so_and_is_coloured(self, tmp_path):
        window = _mushroom_window(tmp_path)
        window.show_borders()
        item = window.list_todo.item(0)
        assert "1 locking tab" in item.text()
        assert item.foreground().color().name().lower() == \
            U.LOCK_COLOUR.lower()

    def test_fit_is_not_remeasured_while_nothing_changes(self, tmp_path,
                                                         monkeypatch):
        window = _window(tmp_path)
        calls = []
        real = U.fit_report
        monkeypatch.setattr(U, "fit_report",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        window._refresh_lists()
        assert calls == []


class TestFitOnTheEditPage:

    def test_marks_the_locking_tab(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        assert len(page.fit_before.locking) == 1
        assert _fills(page.ax_before, U.LOCK_COLOUR)
        assert "1 locking tab" in page.fit_label.text()
        assert page.btn_worst.isEnabled()

    def test_show_fit_off_clears_the_marks(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.fit_check.setChecked(False)
        assert page.fit_before is None
        assert not _fills(page.ax_before, U.LOCK_COLOUR)

    def test_the_simplified_pair_is_checked_too(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page._on_done(simplify_shared_border(page.geom_a, page.geom_b,
                                             page._params()))
        assert page.fit_after is not None
        assert "simplified:" in page.fit_label.text()

    def test_settings_are_saved_for_the_list_to_use(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.fit_spins["seam.fit_tab_mm"].setValue(1.0)
        assert U.cfg("seam.fit_tab_mm") == pytest.approx(1.0)
        # Too small a search to see a 6 mm head: the lock is gone, on the
        # page and, once back, on the list.
        assert page.fit_before.locking == []
        window.show_borders()
        assert "locking" not in window.list_todo.item(0).text()

    def test_zoom_to_worst_frames_the_tab(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.zoom_to_worst_fit()
        x0, x1 = page.ax_before.get_xlim()
        y0, y1 = page.ax_before.get_ylim()
        assert x0 < O + 24 * MM < x1 and y0 < O + 20 * MM < y1
        assert (x1 - x0) < 40 * MM            # closer in than the whole pair


class TestPushBrush:

    def test_buttons_are_named_after_the_provinces(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        assert {page.btn_grow_a.text(), page.btn_grow_b.text()} == \
            {"Grow East", "Grow West"}

    def test_the_two_directions_are_exclusive(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.btn_grow_a.click()
        page.btn_grow_b.click()
        assert page._push_mode == U.GROW_B
        assert not page.btn_grow_a.isChecked()
        page._escape()
        assert page._push_mode is None

    def test_pushing_the_tab_back_clears_the_lock(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.set_push_mode(_grow(page, "CAN_East"))
        page.brush_spin.setValue(12.0)
        before = page.geom_a.union(page.geom_b)
        assert page.push(ACROSS_THE_TAB)
        assert page.fit_before.locking == []
        assert len(page._undo) == 1
        assert "1 push" in page.heading.text()
        assert page.btn_undo.isEnabled()
        after = page.geom_a.union(page.geom_b)
        assert before.symmetric_difference(after).area / MM ** 2 < 1e-6

    def test_undo_puts_it_back(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        original = (page.geom_a, page.geom_b)
        page.set_push_mode(_grow(page, "CAN_East"))
        page.brush_spin.setValue(12.0)
        page.push(ACROSS_THE_TAB)
        page.undo_push()
        assert (page.geom_a, page.geom_b) == original
        assert len(page.fit_before.locking) == 1
        assert not page.btn_undo.isEnabled()

    def test_a_stroke_that_moves_nothing_is_not_an_edit(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.set_push_mode(_grow(page, "CAN_West"))
        deep_in_west = [(O + 1 * MM, O + 1 * MM), (O + 2 * MM, O + 2 * MM)]
        assert not page.push(deep_in_west)
        assert page._undo == []
        assert "Nothing moved" in page.push_label.text()

    def test_a_push_after_simplifying_runs_it_again(self, tmp_path,
                                                    monkeypatch):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page._on_done(simplify_shared_border(page.geom_a, page.geom_b,
                                             page._params()))
        runs = []
        monkeypatch.setattr(page, "run", lambda: runs.append(1))
        page.set_push_mode(_grow(page, "CAN_East"))
        page.brush_spin.setValue(12.0)
        page.push(ACROSS_THE_TAB)
        assert runs == [1]
        assert page.result is None             # stale until the run lands

    def test_no_pushing_while_a_run_is_in_flight(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.set_push_mode(U.GROW_A)
        page._set_busy(True)
        assert page._push_mode is None
        assert not page.btn_grow_a.isEnabled()
        page._set_busy(False)
        assert page.btn_grow_a.isEnabled()

    def test_apply_saves_the_pushed_border(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.set_push_mode(_grow(page, "CAN_East"))
        page.brush_spin.setValue(12.0)
        page.push(ACROSS_THE_TAB)
        east = page.province_a if page.province_a.name == "CAN_East" \
            else page.province_b
        pushed = (page.geom_a if east is page.province_a else page.geom_b).area
        page._on_done(simplify_shared_border(page.geom_a, page.geom_b,
                                             page._params()))
        page.btn_save.click()
        saved = U.read_province(tmp_path / "saved" / "CAN_East.geojson")
        assert saved.geometry.area == pytest.approx(pushed, rel=0.01)
        assert window.list_done.count() == 1

    def test_a_new_pair_starts_with_no_pushes(self, tmp_path):
        window = _mushroom_window(tmp_path)
        page = window.seam_page
        page.set_push_mode(_grow(page, "CAN_East"))
        page.brush_spin.setValue(12.0)
        page.push(ACROSS_THE_TAB)
        page.btn_back.click()
        window.open_editor()
        assert page._undo == []
        assert page._push_mode is None
