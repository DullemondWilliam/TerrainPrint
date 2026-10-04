"""
test_island_editor_settings.py

UI_island_editor.py remembering what it was set to.

Every box in the window is a setting rather than a property of the file open at
the time — what the printer can hold, how wide a causeway you build, what
reduction you work at — and each is written back to the local config as it is
turned, so the next launch starts where the last one left off.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_layout.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_settings.py -v

The local config is redirected to a throwaway copy for every test in the suite
(see conftest.py), which is what makes it safe for a test to turn a box at all.
``test_the_sandbox_is_in_place`` is the canary on that: if the redirection ever
stops working, this suite starts rewriting the config of whoever ran it.
"""

import json

import pytest

from lib import LIB_project_config as config

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def editor(qtbot):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    return window


def saved(key):
    """What a fresh launch would read for ``key``."""
    return config.get(key)


class TestTheSandboxIsInPlace:
    """The guard that lets every other test here turn a box."""

    def test_the_local_config_is_a_copy(self, _local_config):
        assert config.LOCAL_CONFIG_PATH == str(_local_config)
        assert "Temp" in config.LOCAL_CONFIG_PATH or "tmp" in config.LOCAL_CONFIG_PATH

    def test_it_still_reads_what_the_real_one_says(self, _local_config):
        """A copy, not an empty file: the suite must measure with the same
        numbers as the windows it is testing."""
        assert saved("stress.min_lobe_mm2") == config.get("stress.min_lobe_mm2")


class TestEachBoxIsKept:

    @pytest.mark.parametrize("key, control, value", [
        ("island_editor.buffer_mm", "buffer_spin", 0.4),
        ("island_editor.brush_mm", "brush_spin", 0.2),
        ("island_editor.min_area_mm2", "area_spin", 0.05),
        ("thin.width_mm", "width_spin", 1.5),
        ("bridges.width_mm", "bridge_width_spin", 2.0),
        ("bridges.max_span_mm", "bridge_span_spin", 8.0),
        ("stress.report_above", "stress_spin", 1.25),
        ("print.scale", "scale_spin", 4_000_000),
        ("island_editor.original_opacity", "opacity_slider", 75),
    ])
    def test_turning_it_is_what_the_next_launch_reads(self, editor, key,
                                                      control, value):
        getattr(editor, control).setValue(value)
        editor._write_settings()
        assert saved(key) == value

    def test_the_table_covers_every_entry_in_the_window(self, editor):
        """A control added to the window and left out of SAVED_SETTINGS would
        quietly forget itself, which is the bug this file exists about."""
        from PySide6.QtWidgets import QAbstractSpinBox, QSlider
        named = {id(v): k for k, v in vars(editor).items()}
        entries = {named[id(w)]
                   for w in (editor.findChildren(QAbstractSpinBox)
                             + editor.findChildren(QSlider))
                   if id(w) in named}
        assert entries == {control for _, control in M.SAVED_SETTINGS}

    def test_a_percentage_stays_a_whole_number(self, editor):
        """A QSlider refuses a float outright, so 40.0 from the config would
        be a crash on the next launch rather than a lost preference."""
        editor.opacity_slider.setValue(75)
        editor._write_settings()
        assert isinstance(saved("island_editor.original_opacity"), int)

    def test_the_print_scale_stays_a_whole_number(self, editor):
        """It is 1:N, and a JSON float would read back as 1:8000000.0."""
        editor.scale_spin.setValue(4_000_000)
        editor._write_settings()
        assert isinstance(saved("print.scale"), int)

    def test_a_fresh_window_writes_nothing(self, editor, _local_config):
        """Building the window sets every box to its default; none of that is
        a value anybody chose."""
        before = _local_config.read_text(encoding="utf-8")
        other = M.IslandEditor()
        other._write_settings()
        assert _local_config.read_text(encoding="utf-8") == before


class TestWhatItWritesInto:

    def test_it_keeps_what_else_is_in_the_file(self, editor):
        """The screen calibration lives in the same file, and another window
        may have saved its own settings into it."""
        config.update_local("screen.monitors.TEST@1x1.ppi", 96.0)
        editor.buffer_spin.setValue(0.4)
        editor._write_settings()
        assert saved("screen.monitors.TEST@1x1.ppi") == 96.0
        assert saved("island_editor.buffer_mm") == 0.4

    def test_it_writes_the_local_file_not_the_tracked_one(self, editor,
                                                          _local_config):
        """The tracked file is the project's shipped numbers and a reviewable
        diff; this is what one desk works at."""
        editor.buffer_spin.setValue(0.4)
        editor._write_settings()
        assert "island_editor" in json.loads(
            _local_config.read_text(encoding="utf-8"))
        tracked = json.loads(
            open(config.CONFIG_PATH, encoding="utf-8").read())
        assert tracked["island_editor"]["buffer_mm"] != 0.4


class TestWhenItWrites:

    def test_a_gesture_is_one_write_not_one_per_value(self, editor,
                                                      monkeypatch):
        """Holding an arrow key down walks through a dozen values."""
        writes = []
        monkeypatch.setattr(M, "update_local",
                            lambda key, value: writes.append((key, value)))
        for step in range(10):
            editor.buffer_spin.setValue(0.5 + step * 0.05)
        assert writes == [], "nothing should be written until the pause"
        editor._write_settings()
        assert writes == [("island_editor.buffer_mm", 0.95)]

    def test_the_clock_restarts_on_every_turn(self, editor):
        editor.buffer_spin.setValue(0.4)
        assert editor._settings_timer.isActive()
        editor._write_settings()

    def test_it_writes_by_itself_after_the_pause(self, editor, qtbot):
        """The timer, not the close: a window left open all day has saved."""
        editor.buffer_spin.setValue(0.4)
        qtbot.waitUntil(lambda: not editor._pending_settings,
                        timeout=M.SETTINGS_WRITE_MS * 4)
        assert saved("island_editor.buffer_mm") == 0.4

    def test_closing_writes_what_the_timer_has_not(self, editor):
        """A box turned in the last half second, and the window is going."""
        editor.brush_spin.setValue(0.333)
        assert editor._pending_settings
        editor.close()
        assert not editor._pending_settings
        assert saved("island_editor.brush_mm") == 0.333


class TestTheKeysAreTheOnesTheWindowStartsFrom:
    """Writing a key nothing reads back would be a setting that still forgets
    itself. Each entry's default is built from the key it saves to."""

    @pytest.mark.parametrize("key, constant, position", [
        ("island_editor.original_opacity", "ORIGINAL_OPACITY", 2),
        ("island_editor.buffer_mm", "BUFFER_MM", 2),
        ("island_editor.brush_mm", "BRUSH_MM", 2),
        ("island_editor.min_area_mm2", "AREA_MM2", 2),
        ("thin.width_mm", "MIN_WIDTH_MM", 2),
        ("bridges.width_mm", "BRIDGE_WIDTH_MM", 2),
        ("bridges.max_span_mm", "BRIDGE_SPAN_MM", 2),
        ("stress.report_above", "STRESS_SEVERITY", 2),
    ])
    def test_the_default_comes_from_the_key_it_saves_to(self, key, constant,
                                                        position):
        assert getattr(M, constant)[position] == saved(key)

    def test_the_print_scale_too(self):
        assert M.DEFAULT_PRINT_SCALE == saved("print.scale")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
