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
