"""
test_terrain_editor_ui_guide.py

UI_terrain_editor.py keeping to docs/UI_GUIDE.md: controls in titled groups,
a key on every button named in its tooltip, no key claimed twice, undo that a
focused spin box cannot swallow, arrow keys that pan, and no text colour that
only one theme can read.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_terrain_editor_ui_guide.py -v
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QLabel, QPushButton

import UI_terrain_editor as U

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def window(qtbot):
    w = U.TerrainEditorTester()
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w


def _keys(widget) -> str:
    return widget.shortcut().toString(QKeySequence.SequenceFormat.NativeText)


class TestGroups:
    """Each control sits in the group whose title names what it acts on."""

    @pytest.mark.parametrize("group, members", [
        ("grp_regions", ["btn_add_targets", "btn_add_surrounding",
                         "btn_remove"]),
        ("grp_simplify", ["coast_spin", "internal_spin"]),
        ("grp_seams", ["min_seg_spin", "min_angle_spin"]),
        ("grp_borders", ["btn_define", "btn_save_borders"]),
        ("grp_shape", ["lobe_height_spin", "lobe_depth_spin",
                       "lobe_width_spin", "lobe_ratio_spin"]),
        ("grp_fit", ["lobe_clear_spin", "lobe_height_clear_spin"]),
        ("grp_by_hand", ["btn_edit_lobes", "btn_lobe_undo", "btn_lobe_redo"]),
        ("grp_selected", ["sel_angle_spin", "sel_depth_spin",
                          "btn_sel_reset", "btn_sel_delete"]),
        ("grp_warp", ["crs_combo", "res_spin", "resample_combo",
                      "cache_spin"]),
        ("grp_cut", ["buf_spin", "chk_touched", "chk_crop", "btn_run",
                     "btn_save"]),
        ("grp_solid", ["exag_spin", "base_spin", "floor_spin", "btn_build",
                       "btn_save_stl"]),
        ("grp_bar", ["btn_build_bar", "btn_save_bar"]),
    ])
    def test_each_group_holds_its_own(self, window, group, members):
        box = getattr(window, group)
        for name in members:
            assert box.isAncestorOf(getattr(window, name)), name

    def test_every_group_is_titled(self, window):
        assert all(group.title() for group in window._groups())

    def test_busy_locks_every_group(self, window):
        """Read off each group's own flag: the Borders tab, disabled until
        provinces are added, greys its groups through their parent."""
        def locked(group):
            return group.testAttribute(Qt.WidgetAttribute.WA_ForceDisabled)

        window._set_busy(True)
        assert all(locked(group) for group in window._groups())
        window._set_busy(False)
        assert not any(locked(group) for group in window._groups())


class TestTheFootRow:

    def test_the_minimum_width_fits_a_1080p_desktop(self, window):
        assert window.minimumSizeHint().width() <= 1920

    def test_the_view_tools_share_the_status_row(self, window):
        root = window.centralWidget().layout()
        rows = [root.itemAt(i).layout() for i in range(root.count())]
        foot = next(r for r in rows
                    if r is not None and r.indexOf(window.status) >= 0)
        assert foot.indexOf(window.mpl_toolbar) > foot.indexOf(window.status)
        assert foot.indexOf(window.show_combo) >= 0

    def test_the_toolbar_has_no_save_or_subplots(self, window):
        names = {a.text() for a in window.mpl_toolbar.actions() if a.text()}
        assert not names & {"Save", "Subplots", "Customize"}


class TestKeys:

    def test_every_button_carries_a_key(self, window):
        """Undo and Redo excepted: they answer to the window's bindings."""
        for button in window.findChildren(QPushButton):
            if button in (window.btn_lobe_undo, window.btn_lobe_redo):
                assert button.shortcut().isEmpty()
                continue
            assert not button.shortcut().isEmpty(), button.text()

    def test_the_tooltip_says_so(self, window):
        for button in window.findChildren(QPushButton):
            if button.shortcut().isEmpty():
                continue
            assert button.toolTip().endswith("Shortcut: " + _keys(button))

    def test_no_key_is_claimed_twice(self, window):
        claimed = [_keys(b) for b in window.findChildren(QPushButton)
                   if not b.shortcut().isEmpty()]
        claimed += [_keys(a) for a in window.mpl_toolbar.actions()
                    if not a.shortcut().isEmpty()]
        claimed += [s.key().toString(QKeySequence.SequenceFormat.NativeText)
                    for s in window.findChildren(QShortcut)
                    if s.parent() is window]
        assert len(claimed) == len(set(claimed)), sorted(claimed)

    def test_redo_answers_to_ctrl_shift_z_as_well(self, window):
        bound = {s.key().toString() for s in window.findChildren(QShortcut)
                 if s.parent() is window}
        assert {"Ctrl+Z", "Ctrl+Y", "Ctrl+Shift+Z"} <= bound

    @pytest.mark.parametrize("spin", ["coast_spin", "lobe_width_spin",
                                      "res_spin", "exag_spin"])
    def test_undo_still_fires_from_a_spin_box(self, window, qtbot, spin):
        undo = next(s for s in window.findChildren(QShortcut)
                    if s.parent() is window and s.key().toString() == "Ctrl+Z")
        fired = []
        undo.activated.connect(lambda: fired.append(True))
        window.tabs.setTabEnabled(1, True)
        entry = getattr(window, spin)
        window.tabs.setCurrentIndex(
            next(i for i in range(window.tabs.count())
                 if window.tabs.widget(i).isAncestorOf(entry)))
        entry = getattr(window, spin)
        entry.setFocus()
        qtbot.keyClick(entry, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert fired

    def test_clicking_a_button_hands_the_keyboard_back(self, window, qtbot):
        window.coast_spin.setFocus()
        window.btn_remove.click()
        assert window.canvas.hasFocus()

    def test_the_arrow_keys_pan(self, window, qtbot):
        window.canvas.setFocus()
        before = window.ax.get_xlim()
        qtbot.keyClick(window.canvas, Qt.Key.Key_Right)
        after = window.ax.get_xlim()
        span = before[1] - before[0]
        assert after[0] - before[0] == pytest.approx(span * U.PAN_STEP)


class TestText:

    def test_no_label_sets_its_own_colour(self, window):
        for label in window.findChildren(QLabel):
            assert "color" not in label.styleSheet(), label.text()

    def test_the_status_line_is_larger_than_the_chrome(self, window):
        assert (window.status.font().pointSizeF()
                > window.border_report.font().pointSizeF())

    def test_the_title_names_the_dem(self, window):
        assert window.windowTitle() == "Terrain Editor — (no file)"
