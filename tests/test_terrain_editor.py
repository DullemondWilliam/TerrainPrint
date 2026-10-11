"""
test_terrain_editor.py

UI_terrain_editor.py: reproject, downsample and crop a DEM to a boundary,
with a live cost preview.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp``
fixture.

Run with:  python -m pytest tests/test_terrain_editor.py -v

Two things carry the weight here. The harness exists to tell you what a clip
*will* cost before you commit to one, so the preview agreeing with the run it
predicts is the invariant worth guarding — a preview that drifts from the lib is
worse than no preview. And because a real clip can be a 400 MB array shown in a
600-pixel panel, the tests check that the harness says so rather than quietly
displaying something coarser than it saved.

The libs themselves are covered by test_geotiff_clip.py and
test_raster_reproject.py; nothing here re-tests the
geometry, and nothing here opens ``Canada120m.tif``.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box

import geopandas as gpd

import UI_terrain_editor as U
from lib.LIB_project_config import update_local

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Stub the modal dialogs for every test in this module.

    ``QMessageBox.critical`` blocks on a button press, and headlessly there is
    nobody to press it -- a test reaching an error path would hang the suite
    rather than fail.
    """
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "warning", lambda *a, **k: None)

DEM_CRS = CRS.from_epsg(3979)      # the Canada120m DEM's CRS
PRINT_CRS = CRS.from_epsg(3347)    # the project's print CRS
RES = 100.0
ORIGIN = (1_000_000.0, 2_000_000.0)
WIDTH, HEIGHT = 200, 150
NODATA = -32767.0


def _write_dem(tmp_path, name="dem.tif"):
    """A DEM whose elevation is its column index (as in test_geotiff_clip)."""
    data = np.tile(np.arange(WIDTH, dtype="float32"), (HEIGHT, 1))
    path = tmp_path / name
    with rasterio.open(path, "w", driver="GTiff", height=HEIGHT, width=WIDTH,
                       count=1, dtype="float32", crs=DEM_CRS,
                       transform=Affine.translation(*ORIGIN) * Affine.scale(RES, -RES),
                       nodata=NODATA) as ds:
        ds.write(data, 1)
    return path


def _metric_box(col0, row0, col1, row1):
    west, north = ORIGIN
    return box(west + col0 * RES, north - row1 * RES,
               west + col1 * RES, north - row0 * RES)


def _loaded(tmp_path, boundary=None, crs=DEM_CRS, target_crs=DEM_CRS):
    """
    A window with a DEM and boundary already in state (no file dialogs).

    Two defaults are forced, both so assertions cannot hold for the wrong reason:

    *Resolution is native.*  The harness defaults to 3200 m/px, right for the
    120 m Canada DEM at print scale but enough to decimate this 100 m fixture to
    a 3x3 grid.

    *Target CRS is the raster's own*, so the crop path is exercised without a
    warp.  ``target_crs=PRINT_CRS`` opts into the reprojecting path; see
    ``TestReprojection``.
    """
    window = U.TerrainEditorTester()
    window._load_raster(_write_dem(tmp_path))
    window.crs_combo.setCurrentText(str(target_crs))
    window.res_spin.setValue(0.0)
    geom = boundary if boundary is not None else _metric_box(50, 30, 150, 100)
    frame = gpd.GeoDataFrame(geometry=[geom], crs=DEM_CRS)
    if crs != DEM_CRS:
        frame = frame.to_crs(crs)
    window.boundary = frame
    window.boundary_path = tmp_path / "boundary.geojson"
    window._invalidate()
    return window


def _run_clip(window):
    """
    Run the warp+crop synchronously, bypassing the worker thread.

    Mirrors ``RunWorker.run`` step for step; if the two drift, these tests stop
    describing the harness.
    """
    from lib.LIB_geotiff_clip import clip_geotiff
    from lib.LIB_raster_reproject import reproject_geotiff

    warp, clip = window._plan()
    source = str(window.raster_path)
    if warp is not None:
        reproject_geotiff(source, window.target_crs(),
                          str(window._tempdir / "warp.tif"), warp)
        source = str(window._tempdir / "warp.tif")
    result = clip_geotiff(source, window._output_geom(),
                          str(window._tempdir / "out.tif"), clip,
                          keep_array=True)
    window._on_done(result)
    return result


def _run_mesh(window):
    """
    Run the mesh stage synchronously, mirroring ``MeshWorker.run``.

    It asks the window for ``cut_geom()`` rather than restating the tick rule:
    a restated rule is one these tests would stop checking, which a mutation
    sweep proved by breaking the real method with every test still passing.
    """
    from lib.LIB_terrain_stl import terrain_to_stl
    result = terrain_to_stl(window.result.path, window.cut_geom(),
                            str(window._tempdir / "out.stl"),
                            window._terrain_params())
    window._on_meshed(result)
    return result


class TestMeshStage:
    """The second half of the pipeline, in the same window."""

    def test_defaults_come_from_the_config(self, tmp_path):
        from lib.LIB_project_config import get as cfg
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        assert window.exag_spin.value() == pytest.approx(cfg("terrain.exaggeration"))
        assert window.base_spin.value() == pytest.approx(cfg("terrain.base_mm"))
        assert window.floor_spin.value() == pytest.approx(cfg("terrain.floor_m"))
        assert window.resample_combo.currentText() == cfg("terrain.resampling")

    def test_shipped_defaults_are_exaggeration_10_and_base_2(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        assert window.exag_spin.value() == pytest.approx(10.0)
        assert window.base_spin.value() == pytest.approx(2.0)

    def test_res_is_seeded_from_the_configured_spacing(self):
        from lib.LIB_project_config import get as cfg
        fresh = U.TerrainEditorTester()
        assert fresh.res_spin.value() == pytest.approx(
            cfg("terrain.spacing_mm") * cfg("print.scale") / 1000.0)

    def test_res_seed_follows_a_changed_spacing(self, monkeypatch):
        """The shipped 0.4 mm x 1:8M happens to be 3200 m exactly.

        So asserting "3200" cannot tell a computed seed from a hard-coded one --
        a mutation sweep proved it. Move the configured spacing and the seed has
        to move with it.
        """
        # The window reads the setting when it is built, through the
        # settings files -- so it is the saved setting that has to move.
        update_local("terrain.spacing_mm", 0.8)
        fresh = U.TerrainEditorTester()
        assert fresh.res_spin.value() == pytest.approx(6400.0)

    def test_cut_geom_honours_the_tick(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        assert window.cut_geom() is not None
        window.chk_cut.setChecked(False)
        assert window.cut_geom() is None

    def test_build_disabled_until_a_crop_exists(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        assert window.btn_build.isEnabled() is False
        _run_clip(window)
        assert window.btn_build.isEnabled() is True

    def test_build_makes_a_watertight_solid(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        result = _run_mesh(window)
        assert result.watertight is True
        assert result.volume_mm3 > 0.0
        assert window.btn_save_stl.isEnabled() is True

    def test_mesh_parameters_reach_the_lib(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.exag_spin.setValue(25.0)
        window.base_spin.setValue(3.5)
        window.floor_spin.setValue(100.0)
        params = window._terrain_params()
        assert params.z_exaggeration == pytest.approx(25.0)
        assert params.base_height_mm == pytest.approx(3.5)
        assert params.floor_m == pytest.approx(100.0)

    def test_exaggeration_changes_the_built_height(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        window.exag_spin.setValue(10.0)
        low = _run_mesh(window)
        window.exag_spin.setValue(20.0)
        high = _run_mesh(window)
        base = window.base_spin.value()
        assert high.extent_mm[2] - base == pytest.approx(
            2 * (low.extent_mm[2] - base), rel=1e-6)

    def test_cut_checkbox_controls_the_boundary(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        cut = _run_mesh(window)
        window.chk_cut.setChecked(False)
        slab = _run_mesh(window)
        assert cut.cut_to_boundary is True and slab.cut_to_boundary is False
        assert cut.volume_mm3 < slab.volume_mm3

    def test_crs_guard_is_waived_for_the_cropped_raster(self, tmp_path):
        """The raster stage already chose the CRS; re-guarding would block it."""
        window = _loaded(tmp_path, target_crs=DEM_CRS)   # EPSG:3979, not print
        assert window._terrain_params().expect_crs is None
        _run_clip(window)
        assert _run_mesh(window).watertight is True

    def test_solid_panel_holds_one_visual_per_build(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        _run_mesh(window)
        assert len(window._solid_meshes) == 1
        _run_mesh(window)
        assert len(window._solid_meshes) == 1

    def test_build_writes_stl_and_frame_sidecar(self, tmp_path):
        from pathlib import Path as P
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        result = _run_mesh(window)
        assert P(result.path).exists()
        assert P(result.frame_path).name.endswith(".frame.json")


class TestTwoStageInvalidation:
    """Which stage a control stales is the whole reason there are two rows."""

    def test_mesh_change_keeps_the_crop(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        crop = _run_clip(window)
        _run_mesh(window)
        window.exag_spin.setValue(30.0)
        assert window.stl_result is None            # solid is stale
        assert not window.btn_save_stl.isEnabled()
        assert window.result is crop                # ... but the crop survives
        assert window.btn_save.isEnabled()
        assert window.btn_build.isEnabled()         # ready to re-mesh

    def test_raster_change_stales_both(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        _run_mesh(window)
        window.res_spin.setValue(RES * 4)
        assert window.result is None
        assert window.stl_result is None
        assert not window.btn_save.isEnabled()
        assert not window.btn_save_stl.isEnabled()
        assert not window.btn_build.isEnabled()     # nothing to mesh from

    def test_cut_toggle_stales_only_the_solid(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        crop = _run_clip(window)
        _run_mesh(window)
        window.chk_cut.setChecked(False)
        assert window.stl_result is None
        assert window.result is crop

    def test_a_second_crop_after_a_change_does_not_crash(self, tmp_path):
        """A changed setting redraws the map; the colorbar on it must go
        first, or the next crop's redraw dies inside matplotlib."""
        window = _loaded(tmp_path)
        _run_clip(window)
        window.buf_spin.setValue(100.0)
        _run_clip(window)
        assert window._colorbar is not None
        assert len(window.figure.axes) == 2       # the map + one colorbar

    def test_busy_restores_both_stages_preconditions(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        _run_mesh(window)
        window._set_busy(True)
        assert not window.btn_build.isEnabled()
        assert not window.exag_spin.isEnabled()
        window._set_busy(False)
        assert window.btn_build.isEnabled()
        assert window.btn_save.isEnabled()
        assert window.btn_save_stl.isEnabled()

    def test_busy_restore_offers_no_save_without_results(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window._set_busy(True)
        window._set_busy(False)
        assert not window.btn_build.isEnabled()
        assert not window.btn_save_stl.isEnabled()


class TestCostPreview:
    """The preview must predict exactly what the run produces."""

    def test_preview_matches_the_actual_clip(self, tmp_path):
        window = _loaded(tmp_path)
        predicted = window.status.text()
        result = _run_clip(window)
        assert (result.width, result.height) == (100, 70)   # not a degenerate grid
        assert f"{result.width} x {result.height}" in predicted
        assert f"@ {result.res[0]:.0f} m" in predicted

    def test_preview_matches_at_the_harness_default_resolution(self, tmp_path):
        """The 3200 m default must be previewed as faithfully as any other."""
        window = _loaded(tmp_path)
        window.res_spin.setValue(3200.0)
        predicted = window.status.text()
        result = _run_clip(window)
        assert f"{result.width} x {result.height}" in predicted

    def test_preview_tracks_the_resolution(self, tmp_path):
        window = _loaded(tmp_path)
        window.res_spin.setValue(4 * RES)
        predicted = window.status.text()
        result = _run_clip(window)
        assert (result.width, result.height) == (25, 18)
        assert f"{result.width} x {result.height}" in predicted

    def test_preview_tracks_the_buffer(self, tmp_path):
        window = _loaded(tmp_path)
        before = window.status.text()
        window.buf_spin.setValue(5 * RES)
        assert window.status.text() != before
        result = _run_clip(window)
        assert result.width == 110          # 100 + 5 px either side
        assert f"{result.width} x {result.height}" in window.status.text()

    def test_preview_refuses_a_disjoint_boundary(self, tmp_path):
        west, north = ORIGIN
        far = box(west - 500_000, north - 500_000, west - 400_000, north - 400_000)
        window = _loaded(tmp_path, boundary=far)
        assert "Cannot run" in window.status.text()
        assert "does not overlap" in window.status.text()

    def test_preview_handles_a_wgs84_boundary(self, tmp_path):
        window = _loaded(tmp_path, crs=CRS.from_epsg(4326))
        assert "Would produce" in window.status.text()
        result = _run_clip(window)
        assert result.coverage > 0.95


class TestReprojection:
    """The path this harness was renamed for: warp, then crop."""

    def test_target_crs_drives_whether_a_warp_runs(self, tmp_path):
        same = _loaded(tmp_path, target_crs=DEM_CRS)
        assert same.needs_warp() is False
        assert same._plan()[0] is None

        warped = _loaded(tmp_path, target_crs=PRINT_CRS)
        assert warped.needs_warp() is True
        assert warped._plan()[0] is not None

    def test_warped_output_carries_the_target_crs(self, tmp_path):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        result = _run_clip(window)
        with rasterio.open(result.path) as ds:
            assert ds.crs == PRINT_CRS

    def test_warping_changes_the_result_shape(self, tmp_path):
        """The 5.4 x 5.9 mm disagreement, in miniature: the CRS must matter."""
        plain = _run_clip(_loaded(tmp_path, target_crs=DEM_CRS))
        warped = _run_clip(_loaded(tmp_path, target_crs=PRINT_CRS))
        assert (warped.width, warped.height) != (plain.width, plain.height)
        assert warped.bounds_m[0] != pytest.approx(plain.bounds_m[0], abs=1000)

    def test_preview_matches_the_warped_run(self, tmp_path):
        """The preview promise has to survive the extra step."""
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        predicted = window.status.text()
        result = _run_clip(window)
        assert f"{result.width} x {result.height}" in predicted
        assert "warp" in predicted

    def test_preview_names_the_projection_change(self, tmp_path):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        assert "EPSG:3979" in window.status.text()
        assert "EPSG:3347" in window.status.text()

    def test_resampling_reaches_the_warp_params(self, tmp_path):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        window.resample_combo.setCurrentText("max")
        from rasterio.enums import Resampling
        assert window._plan()[0].resampling is Resampling.max

    def test_resampling_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        _run_clip(window)
        window.resample_combo.setCurrentText("nearest")
        assert window.result is None
        assert not window.btn_save.isEnabled()

    def test_crs_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        _run_clip(window)
        assert window.btn_save.isEnabled()
        window.crs_combo.setCurrentText("EPSG:3347")
        assert window.result is None
        assert not window.btn_save.isEnabled()

    def test_half_typed_crs_does_not_crash_the_preview(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.crs_combo.setCurrentText("EPSG:")
        assert window.target_crs() is None
        assert "not recognised" in window.status.text()

    def test_the_view_is_not_kept_across_a_projection_change(self, tmp_path):
        """Limits in the DEM's CRS would point the warped crop off the map."""
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        window._draw_map(preserve_view=False)
        before_x = window.ax.get_xlim()
        _run_clip(window)
        window._draw_map()
        crop_x = window.ax.get_xlim()
        assert crop_x[0] != pytest.approx(before_x[0], abs=1000)
        left, _, right, _ = window.result.bounds_m
        assert crop_x[0] <= left and right <= crop_x[1]

    def test_the_view_is_kept_when_the_frames_agree(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window._draw_map(preserve_view=False)
        xlim = window.ax.get_xlim()
        # Guard against the trivial pass: an undrawn axes sits at (0, 1),
        # which would satisfy the equality below while showing nothing.
        assert xlim[1] - xlim[0] > 1000.0
        _run_clip(window)
        window._draw_map()
        assert window.ax.get_xlim() == pytest.approx(xlim)

    def test_one_map_shows_the_latest_state(self, tmp_path):
        """No before | after pair: the DEM until the crop, the crop after."""
        window = _loaded(tmp_path)
        window._draw_map()
        assert len(window.figure.axes) == 2       # the map + its colorbar
        assert window.ax.get_title().startswith("DEM overview")
        _run_clip(window)
        window._draw_map()
        assert window.ax.get_title().startswith("Cropped")
        assert len(window.ax.images) == 1         # the crop, not the DEM too


class TestDownsample:
    """Reducing terrain resolution, exposed directly by the Res control.

    The fixture DEM is 100 m/px, so ``res_spin`` above 100 is a downsample. The
    behaviour that matters is *which path it takes*: plain decimation can only
    sample nearest, so every coarsening is routed through the warper to keep the
    Resample choice meaningful.
    """

    def test_native_resolution_uses_the_plain_crop_path(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)      # res_spin == native
        assert window.reduction() == pytest.approx(1.0)
        assert window.needs_resample() is False
        assert window._plan()[0] is None

    def test_coarser_resolution_routes_through_the_warper(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        assert window.reduction() == pytest.approx(4.0)
        assert window.needs_warp() is False      # same CRS ...
        assert window.needs_resample() is True   # ... but still resampling
        assert window._plan()[0] is not None

    def test_downsampled_output_keeps_the_source_crs(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        result = _run_clip(window)
        with rasterio.open(result.path) as ds:
            assert ds.crs == DEM_CRS
            assert ds.res[0] == pytest.approx(RES * 4)

    def test_downsampling_shrinks_the_grid(self, tmp_path):
        native = _run_clip(_loaded(tmp_path, target_crs=DEM_CRS))
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        coarse = _run_clip(window)
        assert coarse.width < native.width and coarse.height < native.height
        assert coarse.res[0] == pytest.approx(RES * 4)

    def test_resampling_choice_bites_without_a_projection_change(self, tmp_path):
        """The bug this routing fixes: Resample used to be inert at same CRS."""
        def zmax(method):
            window = _loaded(tmp_path, target_crs=DEM_CRS)
            window.res_spin.setValue(RES * 5)
            window.resample_combo.setCurrentText(method)
            assert window.needs_warp() is False
            return _run_clip(window).zmax

        # Elevation == column index, so a block's max exceeds its interpolation.
        assert zmax("max") > zmax("bilinear")

    def test_resampling_reaches_the_warp_params_on_a_pure_downsample(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        window.resample_combo.setCurrentText("max")
        from rasterio.enums import Resampling
        assert window._plan()[0].resampling is Resampling.max

    def test_preview_names_the_downsample_and_its_factor(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        text = window.status.text()
        assert "downsample" in text
        assert "4x" in text
        assert "100 m" in text and "400 m" in text

    def test_preview_matches_the_downsampled_run(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        predicted = window.status.text()
        result = _run_clip(window)
        assert f"{result.width} x {result.height}" in predicted

    def test_preview_reports_both_when_warping_and_coarsening(self, tmp_path):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        window.res_spin.setValue(RES * 4)
        text = window.status.text()
        assert "warp" in text and "coarser" in text

    def test_reduction_never_reports_upsampling(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES / 4)
        assert window.reduction() == pytest.approx(1.0)
        assert window.needs_resample() is False

    def test_resolution_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window.res_spin.setValue(RES * 4)
        _run_clip(window)
        assert window.btn_save.isEnabled()
        window.res_spin.setValue(RES * 8)
        assert window.result is None
        assert not window.btn_save.isEnabled()


class TestInvalidation:
    """A stale result must never stay saveable."""

    def test_clip_disabled_until_both_inputs_are_present(self, tmp_path):
        window = U.TerrainEditorTester()
        assert not window.btn_run.isEnabled()
        window._load_raster(_write_dem(tmp_path))
        window._invalidate()
        assert not window.btn_run.isEnabled()      # raster alone is not enough
        window.boundary = gpd.GeoDataFrame(geometry=[_metric_box(50, 30, 150, 100)],
                                           crs=DEM_CRS)
        window._invalidate()
        assert window.btn_run.isEnabled()

    def test_parameter_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path)
        _run_clip(window)
        assert window.btn_save.isEnabled() and window.result is not None
        window.res_spin.setValue(2 * RES)
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert window.ax.get_title().startswith("DEM overview")

    def test_checkbox_change_invalidates_the_result(self, tmp_path):
        window = _loaded(tmp_path)
        _run_clip(window)
        window.chk_touched.setChecked(True)
        assert window.result is None
        assert not window.btn_save.isEnabled()


class TestBusyState:
    """Locking the controls must not leave them wrongly enabled afterwards."""

    def test_busy_locks_and_restores_preconditions(self, tmp_path):
        window = _loaded(tmp_path)
        window._set_busy(True)
        assert window.progress.minimum() == 0 and window.progress.maximum() == 0
        for widget in (window.btn_run, window.btn_save, window.res_spin):
            assert not widget.isEnabled()

        window._set_busy(False)
        assert window.progress.maximum() == 1
        assert window.btn_run.isEnabled()          # both inputs present
        # Save must stay disabled: _set_busy's blanket enable would otherwise
        # offer a save with no result behind it.
        assert not window.btn_save.isEnabled()

    def test_busy_restore_enables_save_once_a_result_exists(self, tmp_path):
        window = _loaded(tmp_path)
        _run_clip(window)
        window._set_busy(True)
        window._set_busy(False)
        assert window.btn_save.isEnabled()


class TestDisplay:

    def test_small_clip_is_shown_whole(self, tmp_path):
        window = _loaded(tmp_path)
        result = _run_clip(window)
        data, step = window._for_display(result.data)
        assert step == 1
        assert data.shape == result.data.shape
        window._draw_map()
        assert "shown 1:" not in window.ax.get_title()

    def test_large_clip_is_strided_and_says_so(self):
        window = U.TerrainEditorTester()
        big = np.zeros((3000, 3000), dtype="float32")   # 9 Mpx > MAX_DISPLAY_PX
        data, step = window._for_display(big)
        assert step > 1
        assert data.size <= U.MAX_DISPLAY_PX
        assert data.shape[0] < big.shape[0]

    def test_colour_limits_follow_the_clip_once_there_is_one(self, tmp_path):
        window = _loaded(tmp_path)
        overview_limits = window._colour_limits()
        result = _run_clip(window)
        assert window._colour_limits() == (result.zmin, result.zmax)
        assert window._colour_limits() != overview_limits

    def test_colour_limits_fall_back_to_overview_percentiles(self, tmp_path):
        window = U.TerrainEditorTester()
        window._load_raster(_write_dem(tmp_path))
        vmin, vmax = window._colour_limits()
        # Elevation == column index, so the 2nd/98th percentiles bracket 0..199.
        assert 0 <= vmin < vmax <= WIDTH - 1

    def test_first_draw_ignores_preserve_view(self, tmp_path):
        """A never-drawn panel has no view to keep, only misleading defaults."""
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window._view_crs = None                   # nothing drawn yet
        window._draw_map(preserve_view=True)
        xlim = window.ax.get_xlim()
        # Must be the DEM's metric extent, not matplotlib's default (0, 1).
        assert xlim[1] - xlim[0] > 1000.0
        assert window._view_crs is not None

    def test_later_draws_do_keep_the_view(self, tmp_path):
        window = _loaded(tmp_path, target_crs=DEM_CRS)
        window._draw_map()
        window.ax.set_xlim(ORIGIN[0], ORIGIN[0] + 500.0)
        window._draw_map(preserve_view=True)
        assert window.ax.get_xlim() == pytest.approx(
            (ORIGIN[0], ORIGIN[0] + 500.0))

    def test_outline_drawn_without_geopandas_plotting(self, tmp_path):
        window = _loaded(tmp_path)
        window._draw_map()
        # One ring for a box; drawn as a Line2D, not a geopandas patch.
        assert len(window.ax.lines) == 1
        assert len(window.ax.images) == 1      # the DEM overview

    def test_outline_handles_a_multipolygon(self, tmp_path):
        from shapely.geometry import MultiPolygon
        pair = MultiPolygon([_metric_box(10, 10, 40, 40),
                             _metric_box(60, 60, 90, 90)])
        window = _loaded(tmp_path, boundary=pair)
        window._draw_map()
        assert len(window.ax.lines) == 2


class TestAutoResolution:
    """Res on Auto: the finest resolution the province can be built at --
    the finest whose STL fits ``max_triangles`` -- never finer than the DEM."""

    @staticmethod
    def _triangles_at(window, res):
        """What Build STL would count for a crop at ``res``, from the same
        grid helpers the run uses."""
        window.res_spin.setValue(res)              # (turns Auto off)
        warp, clip = window._plan()
        with rasterio.open(str(window.raster_path)) as ds:
            if warp is not None:
                grid = U._warp_grid(ds, window.target_crs(), warp)
            else:
                grid = U._clip_grid(ds, U._read_window(
                    ds, window._boundary_geom(), clip.crop), clip)
        return U._triangle_count(grid.width, grid.height)

    @staticmethod
    def _budget(window, monkeypatch, triangles):
        params = U.replace(window._terrain_params(), max_triangles=triangles)
        monkeypatch.setattr(window, "_terrain_params", lambda: params)

    def test_a_fresh_window_is_on_auto_with_a_6_gb_cache(self):
        fresh = U.TerrainEditorTester()
        assert fresh.chk_res_auto.isChecked()
        assert fresh.cache_spin.value() == 6144

    def test_it_is_the_finest_that_fits(self, tmp_path, monkeypatch):
        """The 100x70 px boundary at native is ~28,000 triangles: against a
        limit of 5,000 Auto must coarsen, and only as far as it has to."""
        window = _loaded(tmp_path)
        self._budget(window, monkeypatch, 5_000)
        window.chk_res_auto.setChecked(True)
        res = window.res_spin.value()
        assert res > RES
        assert self._triangles_at(window, res) <= 5_000
        assert self._triangles_at(window, res - 1) > 5_000

    def test_a_province_that_fits_at_native_gets_native(self, tmp_path,
                                                        monkeypatch):
        window = _loaded(tmp_path)
        self._budget(window, monkeypatch, 10_000_000)
        window.chk_res_auto.setChecked(True)
        assert window.res_spin.value() == 0.0
        assert window.res_spin.text() == "native"

    def test_it_follows_the_province(self, tmp_path, monkeypatch):
        window = _loaded(tmp_path)
        self._budget(window, monkeypatch, 5_000)
        window.chk_res_auto.setChecked(True)
        big = window.res_spin.value()
        window.boundary = gpd.GeoDataFrame(
            geometry=[_metric_box(50, 30, 90, 60)], crs=DEM_CRS)
        window._invalidate()
        assert window.chk_res_auto.isChecked()
        assert window.res_spin.value() < big

    def test_it_holds_when_the_crop_is_warped(self, tmp_path, monkeypatch):
        window = _loaded(tmp_path, target_crs=PRINT_CRS)
        self._budget(window, monkeypatch, 5_000)
        window.chk_res_auto.setChecked(True)
        res = window.res_spin.value()
        assert res >= RES
        assert self._triangles_at(window, res) <= 5_000

    def test_choosing_a_res_turns_auto_off(self, tmp_path):
        window = _loaded(tmp_path)
        window.chk_res_auto.setChecked(True)
        window.res_spin.setValue(500.0)
        assert not window.chk_res_auto.isChecked()
        assert window.res_spin.value() == 500.0


class TestSave:

    def test_save_copies_the_libs_own_file(self, tmp_path):
        window = _loaded(tmp_path)
        result = _run_clip(window)
        target = tmp_path / "saved.tif"
        shutil.copyfile(result.path, target)
        assert target.read_bytes() == Path(result.path).read_bytes()
        with rasterio.open(target) as ds:
            assert ds.crs == DEM_CRS
            assert (ds.width, ds.height) == (result.width, result.height)

    def test_every_save_starts_in_data_stl(self, tmp_path, monkeypatch):
        from lib.LIB_project_config import get as cfg
        stl_dir = Path(U.__file__).resolve().parent / cfg("paths.stl")
        assert Path(U.TerrainEditorTester._save_dir()) == stl_dir
        asked = []
        monkeypatch.setattr(U.QFileDialog, "getSaveFileName",
                            lambda parent, title, start, *a: (
                                asked.append(start), ("", ""))[1])
        monkeypatch.setattr(U.QFileDialog, "getExistingDirectory",
                            lambda parent, title, start, *a: (
                                asked.append(start), "")[1])
        window = _loaded(tmp_path)
        _run_clip(window)
        window.save_geotiff()
        window.stl_result = SimpleNamespace(path=tmp_path / "x.stl")
        window.save_stl()
        window.borders = SimpleNamespace()
        window.save_borders()
        window.bar = SimpleNamespace(pieces=[])
        window.save_border_pieces()
        assert len(asked) == 4
        for start in asked:
            assert Path(start) == stl_dir or Path(start).parent == stl_dir

    def test_default_save_name_comes_from_the_boundary(self, tmp_path):
        window = _loaded(tmp_path)
        window.boundary_path = tmp_path / "CAN_Newfoundland and Labrador.geojson"
        assert window._stem() == "CAN_Newfoundland and Labrador"
