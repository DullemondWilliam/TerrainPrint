"""
test_terrain_editor_settings.py

UI_terrain_editor.py remembering its Borders settings: each control opens on
what the config files say and is written back to the local file as it is
turned, as UI_island_editor's boxes are.

Both config files are sandboxed for every test (see conftest.py), so turning a
box here never touches the real config.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_terrain_editor_settings.py -v
"""

import json

import pytest
from PySide6.QtWidgets import QCheckBox

from lib import LIB_project_config as config

import UI_terrain_editor as U
from lib import LIB_border_plan as BP

pytestmark = pytest.mark.usefixtures("qapp")

#: A value for each control, away from its shipped one and inside its range.
TURNED = {
    "borders.coast_tol_mm": 0.7,
    "borders.internal_tol_mm": 1.1,
    "seam.min_segment_mm": 0.9,
    "seam.min_angle_deg": 35.0,
    "borders.width_mm": 3.0,
    "borders.enclave_mm2": 2.5,
    "borders.bridge_gap_ratio": 0.8,
    "borders.connectors": False,
    "borders.lobe_spacing_mm": 20.0,
    "print.dovetail_mm": 0.8,
    "borders.lobe_depth_mm": 4.5,
    "borders.lobe_width_mm": 2.2,
    "borders.lobe_ratio": 1.6,
    "borders.clearance_mm": 0.35,
    "borders.height_clearance_mm": 0.15,
}


@pytest.fixture
def window(qtbot):
    w = U.TerrainEditorTester()
    qtbot.addWidget(w)
    return w


def _local(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _set(widget, value):
    if isinstance(widget, QCheckBox):
        widget.setChecked(value)
    else:
        widget.setValue(value)


def _value(widget):
    return widget.isChecked() if isinstance(widget, QCheckBox)         else widget.value()


def test_the_table_covers_every_border_setting():
    # The scale and the printer have no control on the Borders tab: they are
    # read from the config files, never turned there.
    printer = {BP._BORDER_SETTINGS_KEYS[f] for f in BP.PRINTER_FIELDS}
    keyed = set(BP._BORDER_SETTINGS_KEYS.values()) - {"print.scale"} - printer
    assert keyed == {key for key, _ in U.SAVED_BORDER_SETTINGS}
    assert set(TURNED) == keyed


def test_the_window_opens_on_the_shipped_values(window):
    for key, control in U.SAVED_BORDER_SETTINGS:
        assert _value(getattr(window, control)) ==             pytest.approx(config.shipped(key))


def test_a_fresh_window_writes_nothing(window, _fixed_config):
    window._write_settings()
    assert _local(_fixed_config) == {}


@pytest.mark.parametrize("key, control", U.SAVED_BORDER_SETTINGS)
def test_turning_it_is_what_the_next_launch_reads(qtbot, window, key, control):
    _set(getattr(window, control), TURNED[key])
    window._write_settings()
    assert config.get(key) == pytest.approx(TURNED[key])
    again = U.TerrainEditorTester()
    qtbot.addWidget(again)
    assert _value(getattr(again, control)) == pytest.approx(TURNED[key])
    assert getattr(again.border_settings(),
                   _field(key)) == pytest.approx(TURNED[key])


def _field(key):
    return next(f for f, k in BP._BORDER_SETTINGS_KEYS.items() if k == key)


def test_the_tick_is_written_as_a_bool(window, _fixed_config):
    window.chk_connectors.setChecked(False)
    window._write_settings()
    assert _local(_fixed_config)["borders"]["connectors"] is False


def test_a_gesture_is_one_pending_write(window, _fixed_config):
    for value in (2.5, 3.0, 3.5):
        window.width_spin.setValue(value)
    assert window._settings_timer.isActive()
    assert _local(_fixed_config) == {}
    window._write_settings()
    assert _local(_fixed_config) == {"borders": {"width_mm": 3.5}}


def test_it_writes_by_itself_after_the_pause(qtbot, window, _fixed_config):
    window.coast_spin.setValue(0.6)
    qtbot.waitUntil(lambda: _local(_fixed_config) != {},
                    timeout=U.SETTINGS_WRITE_MS * 4)
    assert config.get("borders.coast_tol_mm") == pytest.approx(0.6)


def test_closing_writes_what_the_timer_has_not(window):
    window.spacing_spin.setValue(18.0)
    window.close()
    assert config.get("borders.lobe_spacing_mm") == pytest.approx(18.0)


def test_it_writes_the_local_file_not_the_tracked_one(window):
    before = open(config.CONFIG_PATH, encoding="utf-8").read()
    window.width_spin.setValue(2.5)
    window._write_settings()
    assert open(config.CONFIG_PATH, encoding="utf-8").read() == before


def test_the_triangle_limit_is_kept(window, _fixed_config):
    window.tri_spin.setValue(4_000_000)
    window._write_settings()
    assert _local(_fixed_config)["terrain"]["max_triangles"] == 4_000_000
    assert isinstance(_local(_fixed_config)["terrain"]["max_triangles"], int)
    reopened = U.TerrainEditorTester()
    assert reopened.tri_spin.value() == 4_000_000
