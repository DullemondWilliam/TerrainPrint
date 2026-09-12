"""
test_screen_ruler_config.py

Where ScreenRuler.py keeps its calibrations: one per monitor, and the migration
that rescues the single machine-wide figure older versions wrote.

Run with:  python -m pytest tests/test_screen_ruler_config.py -v

This layer is worth testing because it is the only part of the program holding
something the user cannot regenerate for free -- a calibration is minutes of
holding a card against the screen. Losing one to a bad merge, a stale legacy
key, or an unreadable file is the failure that matters here; being off by a
pixel is not.
"""

import json

import pytest

import ScreenRuler as R

MONITOR = R.Monitor("DELA0C9@2560x1440", "Dell U2515H", 2560, 1440)
OTHER = R.Monitor("SAM0F91@1920x1080", "Samsung S24", 1920, 1080)
LEGACY_PPI = 81.54726735598227


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the module at a throwaway config file, and hand back its path."""
    path = tmp_path / "ruler.json"
    monkeypatch.setattr(R, "CONFIG_PATH", str(path))
    return path


def _write(path, payload):
    path.write_text(json.dumps(payload))


class TestReadingTheConfig:
    """Nothing on disk is trusted to be there, or to be sane."""

    def test_no_file_at_all(self, config):
        assert R.load_config() == {}

    def test_a_file_that_is_not_json(self, config):
        config.write_text("{ not json at all")
        assert R.load_config() == {}

    def test_a_file_that_is_json_but_not_a_config(self, config):
        _write(config, ["a", "list"])
        assert R.load_config() == {}

    def test_a_per_monitor_file_reads_back(self, config):
        _write(config, {"version": 2,
                        "monitors": {MONITOR.key: {"ppi": 108.8}}})
        assert R.load_config() == {MONITOR.key: {"ppi": 108.8}}


class TestTheLegacySingleCalibration:
    """A v1 file holds one number for the whole machine. It is not thrown away."""

    def test_it_is_offered_under_the_sentinel_key(self, config):
        _write(config, {"ppi_x": LEGACY_PPI})
        assert R.load_config() == {R.UNKNOWN_MONITOR_KEY: {"ppi": LEGACY_PPI}}

    @pytest.mark.parametrize("payload", [
        {"ppi_x": 0}, {"ppi_x": -5}, {"ppi_x": "wide"}, {"ppi_x": None}, {},
    ])
    def test_a_useless_legacy_value_is_not_adopted(self, config, payload):
        _write(config, payload)
        assert R.load_config() == {}

    def test_saving_retires_the_sentinel(self, config):
        """
        Once the figure has a monitor's name on it the machine-wide copy must
        go, or it would be re-adopted by the *next* monitor the window lands on
        and quietly claim that panel is the same size as this one.
        """
        _write(config, {"ppi_x": LEGACY_PPI})
        R.save_ppi(MONITOR, LEGACY_PPI)
        stored = R.load_config()
        assert R.UNKNOWN_MONITOR_KEY not in stored
        assert stored[MONITOR.key]["ppi"] == LEGACY_PPI


class TestWritingCalibrations:
    """Each monitor's figure is its own, and survives the others being written."""

    def test_a_calibration_round_trips(self, config):
        R.save_ppi(MONITOR, 108.8)
        assert R.load_saved_ppi(MONITOR) == pytest.approx(108.8)

    def test_it_records_what_the_monitor_was(self, config):
        R.save_ppi(MONITOR, 108.8)
        entry = R.load_config()[MONITOR.key]
        assert entry["label"] == "Dell U2515H"
        assert entry["size_px"] == [2560, 1440]

    def test_it_is_written_in_the_current_format(self, config):
        R.save_ppi(MONITOR, 108.8)
        assert json.loads(config.read_text())["version"] == R.CONFIG_VERSION

    def test_saving_one_monitor_leaves_the_others_alone(self, config):
        R.save_ppi(MONITOR, 108.8)
        R.save_ppi(OTHER, 96.0)
        R.save_ppi(MONITOR, 110.0)                 # recalibrate just the first
        assert R.load_saved_ppi(MONITOR) == pytest.approx(110.0)
        assert R.load_saved_ppi(OTHER) == pytest.approx(96.0)

    def test_two_monitors_coexist(self, config):
        R.save_ppi(MONITOR, 108.8)
        R.save_ppi(OTHER, 96.0)
        assert set(R.load_config()) == {MONITOR.key, OTHER.key}

    def test_an_unwritable_config_is_not_fatal(self, tmp_path, monkeypatch):
        """A read-only home should cost you the save, not the running ruler."""
        monkeypatch.setattr(R, "CONFIG_PATH", str(tmp_path))   # a directory
        R.save_ppi(MONITOR, 108.8)                             # must not raise


class TestReadingOneMonitorsCalibration:

    def test_a_monitor_never_seen_before(self, config):
        R.save_ppi(MONITOR, 108.8)
        assert R.load_saved_ppi(OTHER) is None

    @pytest.mark.parametrize("stored", [
        {"ppi": 0}, {"ppi": -1}, {"ppi": "wide"}, {"ppi": None}, {}, None,
    ])
    def test_a_damaged_entry_reads_as_uncalibrated(self, config, stored):
        _write(config, {"version": 2, "monitors": {MONITOR.key: stored}})
        assert R.load_saved_ppi(MONITOR) is None

    def test_a_supplied_config_is_not_re_read(self, config):
        """The app loads once and asks about several monitors from that copy."""
        snapshot = {MONITOR.key: {"ppi": 108.8}}
        config.unlink(missing_ok=True)
        assert R.load_saved_ppi(MONITOR, snapshot) == pytest.approx(108.8)


class TestMonitorIdentity:
    """The key has to separate what differs and join what does not."""

    def test_resolution_is_part_of_the_key(self, config):
        """Changing resolution changes how many pixels an inch holds."""
        R.save_ppi(MONITOR, 108.8)
        same_panel_new_mode = R.Monitor("DELA0C9@1920x1080", "Dell U2515H",
                                        1920, 1080)
        assert R.load_saved_ppi(same_panel_new_mode) is None

    def test_identical_panels_share_one_calibration(self, config):
        """Same model, same mode, same physical pixel size — one figure serves."""
        R.save_ppi(MONITOR, 108.8)
        twin = R.Monitor(MONITOR.key, "Dell U2515H", 2560, 1440)
        assert R.load_saved_ppi(twin) == pytest.approx(108.8)


@pytest.fixture(scope="module")
def tk_root():
    """
    One Tk root for the whole module.

    Deliberately not one per test: creating and destroying a root repeatedly in
    a single process is unreliable on this Python's Tcl install -- about one
    init in four cannot find tk.tcl -- which turned these into tests that
    sometimes silently skipped instead of running. Making the root once and
    rebuilding the app on it is steady.
    """
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except tk.TclError as exc:                     # no display to talk to
        pytest.skip(f"tkinter unavailable: {exc}")
    window.geometry("900x430+80+80")
    window.update()
    yield window
    window.destroy()


@pytest.fixture
def new_ruler(tk_root, config):
    """Builds a ScreenRulerApp, and clears it off the shared root afterwards."""
    built = []

    def build():
        app = R.ScreenRulerApp(tk_root)
        built.append(app)
        # Let Tk lay the canvas out before handing it over: both views bail out
        # of drawing into a canvas that has no size yet, which is correct of
        # them and would otherwise make every drawing assertion vacuous.
        tk_root.update()
        app.redraw()
        return app

    yield build
    for app in built:
        app.stop_watching()      # or its poll returns to a destroyed canvas
        app.canvas.destroy()


class TestTheAppAdoptsWhatIsStored:
    """The migration as the running program performs it."""

    def test_a_legacy_figure_is_claimed_for_the_monitor_in_front_of_you(
            self, new_ruler, config):
        _write(config, {"ppi_x": LEGACY_PPI})
        ruler = new_ruler()
        assert ruler.ppi == pytest.approx(LEGACY_PPI)
        assert ruler.mode == "ruler"               # not sent back to calibrate
        assert R.load_saved_ppi(ruler.monitor) == pytest.approx(LEGACY_PPI)

    def test_an_uncalibrated_monitor_asks_for_one(self, new_ruler, config):
        ruler = new_ruler()
        assert ruler.mode == "calibrate"
        assert ruler.saved_ppi is None

    def test_accepting_files_it_under_this_monitor(self, new_ruler, config):
        ruler = new_ruler()
        ruler.ppi = 101.5
        ruler.accept_calibration()
        assert ruler.mode == "ruler"
        assert R.load_saved_ppi(ruler.monitor) == pytest.approx(101.5)

    def test_staying_put_changes_nothing(self, new_ruler, config):
        ruler = new_ruler()
        ruler.ppi = 101.5
        ruler.accept_calibration()
        before = (ruler.ppi, ruler.mode, ruler.monitor.key)
        ruler.check_monitor()
        assert (ruler.ppi, ruler.mode, ruler.monitor.key) == before

    def test_both_views_draw_without_complaint(self, new_ruler, config):
        """A ruler nobody can look at is no use, however well it stores things."""
        ruler = new_ruler()
        for mode in ("calibrate", "ruler"):
            ruler.mode = mode
            ruler.redraw()
            tk_items = ruler.canvas.find_all()
            assert tk_items, f"{mode} mode drew nothing"

    def test_the_caption_can_tell_two_generic_monitors_apart(self, new_ruler,
                                                            config):
        """
        Windows names any panel without a vendor driver "Generic PnP Monitor",
        so the caption has to carry the model code as well or a two-monitor desk
        reads the same on both.
        """
        ruler = new_ruler()
        ruler.monitor = MONITOR._replace(label="Generic PnP Monitor")
        assert "DELA0C9" in ruler._monitor_caption()
