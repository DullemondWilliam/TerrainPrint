"""
test_join_editor.py

UI_join_editor.py: place butterfly keys on every border of a map, set the
key's size, cut every piece and write the lot.

pytest-style for pytest-qt's ``qapp`` fixture.  The geometry is covered by
test_province_joins.py and test_butterfly_keys.py; this guards the window:
that a click does what it says, that the key size set here is the one judged
and cut, that edits undo, and that what is written reopens.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_join_editor.py -v
"""

import json

import matplotlib.backend_bases
import pytest
from shapely.geometry import box

import UI_join_editor as U
from lib import LIB_project_config as config
from lib.LIB_province_joins import PLAN_NAME
from lib.LIB_seam_fit import FitParams
from test_butterfly_keys import MPM
from test_province_joins import JUNCTION, KM, _folder, _west

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    for name in ("critical", "warning", "information"):
        monkeypatch.setattr(U.QMessageBox, name, lambda *a, **k: None)


def _window(tmp_path, folder=None):
    folder = folder or _folder(tmp_path)
    w = U.JoinEditor()
    w.resize(1500, 920)
    w.geojson_dir = w.stl_dir = folder
    w.save_dir = tmp_path / "keyed"
    w.apply_session(U.load_session(folder, folder, w.params(), None,
                                   FitParams()))
    return w


@pytest.fixture
def window(tmp_path):
    return _window(tmp_path)


def _key(w, a, b):
    return next(j.key for j in w.joins if j.involves(a) and j.involves(b))


#: A point on the West/North seam, 6 mm above the junction.
ON_WEST_NORTH = (JUNCTION[0], JUNCTION[1] + 6 * MPM)


class TestLoading:

    def test_lists_every_border_with_no_keys(self, window):
        assert window.border_list.count() == 3
        texts = [window.border_list.item(i).text() for i in range(3)]
        assert all("no keys" in t for t in texts)
        assert "Pick a border" in window.selection_label.text()

    def test_unpaired_files_are_listed_as_problems(self, tmp_path):
        folder = _folder(tmp_path)
        (folder / "CAN_Lonely.geojson").write_bytes(
            (folder / "CAN_West.geojson").read_bytes())
        w = _window(tmp_path, folder)
        assert "CAN_Lonely.geojson: no STL" in w.problems_label.text()


class TestPlacing:

    def test_auto_place_all_puts_keys_on_every_border(self, window):
        window.auto_place_all()
        assert window.key_count() > 3
        assert all(window.state_of(j) == "ok" for j in window.joins)

    def test_clicking_near_a_seam_selects_its_border(self, window):
        window.click(ON_WEST_NORTH[0] + 0.1 * MPM, ON_WEST_NORTH[1])
        assert window.selected == _key(window, "CAN_West", "CAN_North")
        assert window.plan.count == 0      # selecting is not placing

    def test_a_click_on_the_selected_seam_adds_a_key_on_it(self, window):
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.click(ON_WEST_NORTH[0] + 0.1 * MPM, ON_WEST_NORTH[1])
        (x, y), = window.plan.points(window.selected)
        assert x == pytest.approx(JUNCTION[0])        # snapped onto the seam
        assert window.checks[window.selected][0].ok

    def test_a_click_on_a_key_removes_it(self, window):
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.click(*ON_WEST_NORTH)
        window.click(ON_WEST_NORTH[0] + 0.5 * MPM, ON_WEST_NORTH[1])
        assert window.plan.points(window.selected) == []

    def test_dragging_a_key_slides_it_along_the_seam(self, window):
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.click(*ON_WEST_NORTH)
        window.move_key(0, JUNCTION[0] + 3 * MPM, JUNCTION[1] + 15 * MPM)
        (x, y), = window.plan.points(window.selected)
        assert (x, y) == pytest.approx((JUNCTION[0], JUNCTION[1] + 15 * MPM))

    def test_a_mouse_drag_on_the_map_moves_the_key(self, window):
        """Through matplotlib's own events: press on a key, move, release."""
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.add_key(*ON_WEST_NORTH)
        window.zoom_to_border()
        window.canvas.draw()
        to = (JUNCTION[0], JUNCTION[1] + 12 * MPM)
        for name, (x, y) in (("button_press_event", ON_WEST_NORTH),
                             ("motion_notify_event", to),
                             ("button_release_event", to)):
            px, py = window.ax.transData.transform((x, y))
            event = matplotlib.backend_bases.MouseEvent(
                name, window.canvas, px, py, button=1)
            {"button_press_event": window._on_press,
             "motion_notify_event": window._on_motion,
             "button_release_event": window._on_release}[name](event)
        (x, y), = window.plan.points(window.selected)
        assert y == pytest.approx(to[1], abs=0.1 * MPM)
        assert len(window._undo) == 2          # the add, then the move

    def test_a_refused_key_says_why(self, window):
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.add_key(*JUNCTION)          # reaches into South
        assert window.state_of(window.current_join()) == "refused"
        assert "off the land" in window.selection_label.text()

    def test_undo_and_redo_step_through_placements(self, window):
        window.select(_key(window, "CAN_West", "CAN_North"))
        window.click(*ON_WEST_NORTH)
        window.auto_place_selected()
        placed = window.plan.points(window.selected)
        window.undo()
        assert len(window.plan.points(window.selected)) == 1
        window.undo()
        assert window.plan.count == 0 and not window.btn_undo.isEnabled()
        window.redo()
        window.redo()
        assert window.plan.points(window.selected) == placed


class TestKeySize:

    def test_the_spin_boxes_are_the_key_that_is_judged(self, window):
        window.auto_place_all()
        window.key_spins["mouth_mm"].setValue(5.0)
        assert window.params().mouth_mm == 5.0
        key = window.checks[window.joins[0].key][0].polygon
        x0, y0, x1, y1 = key.bounds
        # The seams are straight: the key is mouth along it, two reaches across.
        assert sorted((x1 - x0, y1 - y0)) == pytest.approx(
            [5.0 * MPM, 2 * window.params().reach_mm * MPM])

    def test_settings_are_saved_to_the_local_config(self, window):
        window.key_spins["neck_mm"].setValue(2.4)
        window.spares_spin.setValue(9)
        merged = config.load_files(*config.settings_files())
        assert config.get("keys.neck_mm", config=merged) == 2.4
        assert config.get("keys.spares", config=merged) == 9
        assert U.JoinEditor().key_spins["neck_mm"].value() == 2.4

    def test_a_key_that_cannot_lock_blocks_cutting(self, window):
        window.key_spins["neck_mm"].setValue(window.params().mouth_mm + 1.0)
        assert window.params_error and not window.btn_cut.isEnabled()
        assert "neck_mm < mouth_mm" in window.key_label.text()

    def test_a_slot_through_the_base_blocks_cutting(self, window):
        window.key_spins["depth_mm"].setValue(window.base_mm())
        assert "base" in window.key_label.text()
        assert not window.btn_cut.isEnabled()

    def test_the_key_size_is_reported(self, window):
        assert "Each key 3.2 × 6 × 0.9 mm" in window.key_label.text()


class TestStale:

    def test_a_border_off_its_stls_is_red_and_its_keys_refused(self, tmp_path):
        """GeoJSONs whose shared edge moved 1 mm; STLs from before."""
        moved = {"CAN_West": _west(7_400 * KM + MPM),
                 "CAN_North": box(7_400 * KM + MPM, 1_200 * KM, 7_800 * KM,
                                  1_400 * KM),
                 "CAN_South": box(7_400 * KM + MPM, 1_000 * KM, 7_800 * KM,
                                  1_200 * KM)}
        w = _window(tmp_path, _folder(tmp_path, geojson_shapes=moved))
        stale = [j for j in w.joins if j.stale]
        assert stale
        w.auto_place_all()
        assert all(w.state_of(j) == "stale" for j in stale)
        w.select(stale[0].key)
        assert "rebuild" in w.selection_label.text()


class TestCutting:

    def _cut(self, w):
        params = w.params()
        w.apply_cut(U.cut_and_export(w.plan.copy(), w.pieces, w.joins, params,
                                     w.fits, w.base_mm(), w.result,
                                     w.save_dir))

    def test_cut_and_save_writes_everything(self, window):
        window.auto_place_all()
        self._cut(window)
        out = window.save_dir
        names = {p.name for p in out.iterdir()}
        assert {"CAN_West_keyed.stl", "CAN_North_keyed.stl",
                "CAN_South_keyed.stl", "butterfly_keys_01.stl",
                PLAN_NAME} <= names
        assert "Wrote 3 keyed piece(s)" in window.status.text()
        assert window.view_combo.count() == 1 + len(window.result.plates)

    def test_the_cut_uses_the_key_set_here(self, window):
        window.key_spins["mouth_mm"].setValue(4.0)
        window.auto_place_all()
        self._cut(window)
        saved = json.loads((window.save_dir / PLAN_NAME).read_text())
        assert saved["params"]["mouth_mm"] == 4.0
        assert window.result.params.mouth_mm == 4.0

    def test_the_plan_reopens_on_the_next_load(self, window, tmp_path):
        window.auto_place_all()
        self._cut(window)
        again = _window(tmp_path, window.geojson_dir)
        assert again.plan.keys == window.plan.keys
        assert again.key_count() == window.key_count()
        assert "reopened" in again.status.text()

    def test_3d_shows_the_selected_border_and_the_plates(self, window):
        window.auto_place_all()
        self._cut(window)
        window.select(_key(window, "CAN_North", "CAN_South"))
        window.show_3d()
        keys = window.result.checks[window.selected]
        assert len(window._solid_meshes) == 2 + sum(c.ok for c in keys)
        window.view_combo.setCurrentIndex(1)
        window.show_3d()
        assert len(window._solid_meshes) == 1
        assert "Key plate 1" in window.solid_title.text()

    def test_load_and_cut_run_on_threads(self, tmp_path, qtbot):
        w = U.JoinEditor()
        w.geojson_dir = w.stl_dir = _folder(tmp_path)
        w.save_dir = tmp_path / "keyed"
        w.start_load()
        assert w._busy and not w.btn_cut.isEnabled()
        qtbot.waitUntil(lambda: not w._busy, timeout=60_000)
        assert len(w.joins) == 3
        w.auto_place_all()
        w.cut_and_save()
        qtbot.waitUntil(lambda: not w._busy, timeout=60_000)
        assert (w.save_dir / "butterfly_keys_01.stl").exists()
        assert not w.progress.isVisible()

    def test_an_edit_after_cutting_is_flagged_in_3d(self, window):
        window.auto_place_all()
        self._cut(window)
        window.select(_key(window, "CAN_North", "CAN_South"))
        window.clear_selected()
        window.show_3d()
        assert "changed since" in window.solid_title.text()
