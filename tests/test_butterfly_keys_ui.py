"""
test_butterfly_keys_ui.py

harnesses/UI_butterfly_keys.py: place butterfly keys on the seam between two
province STLs, cut the slots, and see them assembled.

Pytest-style for pytest-qt's ``qapp`` fixture.  The geometry is covered by
test_butterfly_keys.py; this guards the harness: that a click does what the
status line says it does, that the keys drawn are the keys cut, and that every
change stales the cut it would have changed.
"""

import matplotlib
import pytest

from harnesses import UI_butterfly_keys as U
from lib import LIB_butterfly_keys as K
from test_butterfly_keys import A, B, MPM, _write_piece

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "warning", lambda *a, **k: None)


@pytest.fixture
def window(tmp_path):
    """Both provinces in state, loaded synchronously (no dialogs, no thread)."""
    w = U.ButterflyKeysTester()
    for side, name, geom in (("a", "A", A), ("b", "B", B)):
        stl, gj = _write_piece(tmp_path, name, geom)
        w.set_province(side, K.load_province(stl, gj, w.params()))
    return w


def _click(window, x, y, button=1):
    event = matplotlib.backend_bases.MouseEvent(
        "button_press_event", window.canvas, x=0, y=0, button=button)
    event.inaxes = window.ax_before
    event.xdata, event.ydata = x, y
    window._on_click(event)


SEAM_X = 7_400_000.0


class TestLoading:

    def test_one_province_is_not_a_seam(self, tmp_path):
        w = U.ButterflyKeysTester()
        stl, gj = _write_piece(tmp_path, "A", A)
        w.set_province("a", K.load_province(stl, gj))
        assert w.seam is None and not w.btn_cut.isEnabled()
        assert "Import both" in w.status.text()

    def test_a_pair_gets_a_seam_and_auto_placed_keys(self, window):
        assert window.seam is not None
        assert window.placements == K.auto_place(window.seam, window.params())
        assert all(c.ok for c in window.checks)
        assert window.btn_cut.isEnabled()
        assert "sidecar" in window.status.text()

    def test_the_view_frames_the_seam(self, window):
        x0, x1 = window.ax_before.get_xlim()
        assert x0 < SEAM_X < x1


class TestPlacingKeys:

    def test_a_click_near_the_seam_adds_a_key_there(self, window):
        window.clear_keys()
        _click(window, SEAM_X + 100.0, 1_200_000.0)
        assert len(window.placements) == 1
        point = window.seam.geoms[0].interpolate(window.placements[0].s_m)
        assert point.y == pytest.approx(1_200_000.0)

    def test_a_click_far_from_the_seam_does_nothing(self, window):
        window.clear_keys()
        _click(window, SEAM_X - 150_000.0, 1_200_000.0)
        assert window.placements == []

    def test_a_click_on_a_key_removes_it(self, window):
        before = len(window.placements)
        target = window.seam.geoms[0].interpolate(window.placements[1].s_m)
        _click(window, target.x, target.y)
        assert len(window.placements) == before - 1

    def test_clicks_while_panning_are_ignored(self, window):
        before = list(window.placements)
        window.mpl_toolbar.pan()                      # pan owns the mouse now
        _click(window, SEAM_X, 1_200_000.0)
        assert window.placements == before

    def test_a_key_off_the_land_is_drawn_refused_and_not_cut(self, window):
        window.clear_keys()
        _click(window, SEAM_X, 1_000_000.0)          # the seam's very end
        assert len(window.checks) == 1 and not window.checks[0].ok
        assert not window.btn_cut.isEnabled()
        assert "refused" in window.status.text()

    def test_auto_place_follows_the_spacing(self, window):
        window.spacing_spin.setValue(25.0)
        window.auto_place()
        assert len(window.placements) == len(K.auto_place(
            window.seam, window.params()))
        assert window.params().spacing_mm == 25.0

    def test_a_key_that_cannot_lock_is_refused_with_the_reason(self, window):
        window.neck_spin.setValue(5.0)                # wider than the mouth
        assert not window.btn_cut.isEnabled()
        assert "neck_mm < mouth_mm" in window.status.text()


class TestCutting:

    def _cut(self, window):
        window.apply_result(K.fit_loaded(
            window.provinces["a"], window.provinces["b"], window.params(),
            list(window.placements)))

    def test_cutting_offers_the_save_and_shows_the_assembly(self, window):
        self._cut(window)
        assert window.btn_save.isEnabled()
        # Two pieces plus one visual per key.
        assert len(window._solid_meshes) == 2 + len(window.result.keys)
        assert window.solid_title.text().startswith("Assembly")

    def test_the_keys_cut_are_the_keys_drawn(self, window):
        self._cut(window)
        drawn = [c.placement for c in window.checks if c.ok]
        assert [c.placement for c in window.result.keys] == drawn

    def test_each_view_draws_its_meshes(self, window):
        self._cut(window)
        for view, count in (("Province A", 1), ("Province B", 1), ("Keys", 1)):
            window.view_combo.setCurrentText(view)
            assert len(window._solid_meshes) == count, view

    def test_moving_a_key_stales_the_cut(self, window):
        self._cut(window)
        _click(window, SEAM_X, 1_250_000.0)
        assert window.result is None and not window.btn_save.isEnabled()
        assert window._solid_meshes == []

    def test_a_shape_change_stales_the_cut(self, window):
        self._cut(window)
        window.clear_spin.setValue(0.3)
        assert window.result is None

    def test_save_writes_the_three_stls(self, window, tmp_path, monkeypatch):
        self._cut(window)
        out = tmp_path / "saved"
        out.mkdir()
        monkeypatch.setattr(U.QFileDialog, "getExistingDirectory",
                            lambda *a, **k: str(out))
        window.save()
        assert sorted(p.name for p in out.iterdir()) == [
            "A_keyed.stl", "B_keyed.stl", "butterfly_keys.stl"]

    def test_busy_locks_and_restores(self, window):
        window._set_busy(True)
        assert not window.btn_cut.isEnabled() and not window.btn_a.isEnabled()
        window._set_busy(False)
        assert window.btn_cut.isEnabled() and window.btn_a.isEnabled()
        assert not window.btn_save.isEnabled()
