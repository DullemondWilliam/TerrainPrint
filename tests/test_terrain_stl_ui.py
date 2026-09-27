"""
test_terrain_stl_ui.py

harnesses/UI_terrain_stl.py: build a printable terrain solid from a prepared DEM.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp``
fixture, as in test_province_viewer.py.

Run with:  python -m pytest tests/test_terrain_stl_ui.py -v

The meshing itself is covered by test_terrain_stl.py; nothing here re-tests the
geometry. What matters here is that the harness tells the truth *before* you
commit to a build — the triangle estimate must equal what the lib produces, and
the bed warning must fire on a piece that will not fit — and that a solid which
failed the watertight gate can never reach the Save button.

The 3-D panel is a vispy scene, so it is exercised headlessly by feeding a mesh
into the window's state and calling the draw method, as `BUILDING_UI_HARNESSES`
§11.D describes. No test reads ``Canada120m.tif``.
"""

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box

import geopandas as gpd

from harnesses import UI_terrain_stl as U

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Stub the modal dialogs for every test in this module.

    ``QMessageBox.critical`` blocks on a button press, and headlessly there is
    nobody to press it — a test that reaches an error path would hang the whole
    suite rather than fail. Stubbing them is what makes the error paths testable
    at all.
    """
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "warning", lambda *a, **k: None)

PRINT_CRS = CRS.from_epsg(3347)
OTHER_CRS = CRS.from_epsg(3979)
SCALE = 8_000_000.0
MPM = SCALE / 1000.0
RES = 8000.0                           # 1 print-mm per pixel
ORIGIN = (7_000_000.0, 2_000_000.0)
COLS, ROWS = 21, 11                    # -> a 20 x 10 mm footprint
NODATA = -32767.0


def _write_dem(tmp_path, name="dem.tif", cols=COLS, rows=ROWS, crs=PRINT_CRS,
               res=RES):
    data = np.tile(np.arange(cols, dtype="float32") * 100.0, (rows, 1))
    path = tmp_path / name
    with rasterio.open(path, "w", driver="GTiff", height=rows, width=cols,
                       count=1, dtype="float32", crs=crs, nodata=NODATA,
                       transform=Affine.translation(*ORIGIN) * Affine.scale(res, -res)) as ds:
        ds.write(data, 1)
    return path


def _metric_box(col0, row0, col1, row1):
    west, north = ORIGIN
    return box(west + col0 * RES, north - row1 * RES,
               west + col1 * RES, north - row0 * RES)


def _loaded(tmp_path, boundary=True, **dem_kw):
    """A window with a DEM (and optionally a boundary) already in state."""
    window = U.TerrainStlTester()
    window._load_raster(_write_dem(tmp_path, **dem_kw))
    window.scale_spin.setValue(SCALE)
    window.exag_spin.setValue(10.0)
    window.base_spin.setValue(2.0)
    if boundary:
        window.boundary = gpd.GeoDataFrame(geometry=[_metric_box(5, 2, 15, 8)],
                                           crs=PRINT_CRS)
        window.boundary_path = tmp_path / "boundary.geojson"
    window._invalidate()
    return window


def _run_build(window):
    """Run the build synchronously, mirroring ``BuildWorker.run`` step for step."""
    from lib.LIB_terrain_stl import terrain_to_stl
    result = terrain_to_stl(str(window.raster_path), window.cut_boundary(),
                            str(window._tempdir / "out.stl"), window._params())
    window._on_built(result)
    return result


class TestCostPreview:
    """The estimate must equal what the lib actually makes."""

    def test_triangle_estimate_matches_the_build(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        predicted = window.status.text()
        result = _run_build(window)
        assert f"{result.triangles:,} triangles" in predicted
        assert result.triangles == 920

    def test_cut_estimate_is_quoted_as_an_upper_bound(self, tmp_path):
        """The grid count is what gets meshed, not what survives the cut.

        On Newfoundland the coastline removes 65% of it, so quoting the grid
        count as the answer would overstate the piece threefold.
        """
        window = _loaded(tmp_path)                 # with a boundary
        text = window.status.text()
        assert "up to" in text and "before the coastline cut" in text

        result = _run_build(window)
        quoted = int(text.split("up to ")[1].split(" triangles")[0].replace(",", ""))
        assert quoted == 920                       # the ceiling is the full grid
        assert result.triangles < quoted           # and the cut really reduces it

    def test_slab_estimate_is_exact_not_a_bound(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        text = window.status.text()
        assert "up to" not in text
        result = _run_build(window)
        assert f"{result.triangles:,} triangles" in text

    def test_printed_size_estimate_matches_the_build(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        predicted = window.status.text()
        result = _run_build(window)
        assert "20 x 10 mm" in predicted
        assert result.extent_mm[0] == pytest.approx(20.0)
        assert result.extent_mm[1] == pytest.approx(10.0)

    def test_preview_tracks_the_scale(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window.scale_spin.setValue(SCALE / 2)        # twice as big on the bed
        assert "40 x 20 mm" in window.status.text()

    def test_preview_names_the_cut_or_the_slab(self, tmp_path):
        window = _loaded(tmp_path)
        assert "cut to the boundary" in window.status.text()
        window.chk_cut.setChecked(False)
        assert "rectangular slab" in window.status.text()

    def test_preview_warns_when_it_will_not_fit_the_bed(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window.scale_spin.setValue(SCALE / 100)      # 2000 x 1000 mm
        assert "DOES NOT FIT THE BED" in window.status.text()

    def test_preview_refuses_an_over_budget_grid(self, tmp_path):
        window = _loaded(tmp_path, boundary=False, cols=900, rows=900)
        text = window.status.text()
        assert "over the" in text and "cap" in text
        assert "UI_terrain_editor" in text        # names the way out

    def test_preview_reports_spacing_not_a_resolution_control(self, tmp_path):
        """Resolution is upstream; this harness only reports what it inherited."""
        window = _loaded(tmp_path, boundary=False)
        assert "1.00 mm spacing" in window.status.text()
        assert not hasattr(window, "res_spin")


class TestCut:

    def test_ticking_off_the_cut_drops_the_boundary(self, tmp_path):
        window = _loaded(tmp_path)
        assert window.cut_boundary() is not None
        window.chk_cut.setChecked(False)
        assert window.cut_boundary() is None

    def test_cut_solid_is_smaller_than_the_slab(self, tmp_path):
        window = _loaded(tmp_path)
        cut = _run_build(window)
        window.chk_cut.setChecked(False)
        slab = _run_build(window)
        assert cut.volume_mm3 < slab.volume_mm3
        assert cut.cut_to_boundary is True and slab.cut_to_boundary is False

    def test_no_boundary_still_builds_a_slab(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        assert window.cut_boundary() is None
        result = _run_build(window)
        assert result.watertight is True

    def test_wgs84_boundary_is_reprojected_for_the_cut(self, tmp_path):
        window = _loaded(tmp_path)
        native = window.cut_boundary().bounds
        window.boundary = window.boundary.to_crs(4326)
        window._invalidate()
        assert window.cut_boundary().bounds == pytest.approx(native, abs=1.0)


class TestParameters:

    def test_exaggeration_reaches_the_lib(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window.exag_spin.setValue(20.0)
        assert window._params().z_exaggeration == pytest.approx(20.0)
        result = _run_build(window)
        assert result.extent_mm[2] == pytest.approx(2000.0 * 20 / MPM + 2.0)

    def test_floor_reaches_the_lib(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window.floor_spin.setValue(1000.0)
        assert window._params().floor_m == pytest.approx(1000.0)

    def test_base_reaches_the_lib(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window.base_spin.setValue(3.0)
        result = _run_build(window)
        assert result.base_height_mm == pytest.approx(3.0)
        assert result.mesh.bounds[0][2] == pytest.approx(0.0)

    def test_crs_guard_is_on_by_default(self, tmp_path):
        window = _loaded(tmp_path, boundary=False, name="wrong.tif",
                         crs=OTHER_CRS)
        assert window._params().expect_crs is not None
        with pytest.raises(ValueError, match="print pipeline builds in"):
            _run_build(window)

    def test_parameter_change_invalidates_the_solid(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        _run_build(window)
        assert window.btn_save.isEnabled() and window.result is not None
        window.exag_spin.setValue(25.0)
        assert window.result is None
        assert not window.btn_save.isEnabled()

    def test_cut_toggle_invalidates_the_solid(self, tmp_path):
        window = _loaded(tmp_path)
        _run_build(window)
        window.chk_cut.setChecked(False)
        assert window.result is None
        assert not window.btn_save.isEnabled()


class TestBusyState:

    def test_build_disabled_until_a_dem_is_loaded(self, tmp_path):
        window = U.TerrainStlTester()
        assert not window.btn_build.isEnabled()
        window._load_raster(_write_dem(tmp_path))
        window._invalidate()
        assert window.btn_build.isEnabled()      # a boundary is optional

    def test_busy_locks_and_restores_preconditions(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window._set_busy(True)
        assert window.progress.minimum() == 0 and window.progress.maximum() == 0
        assert not window.btn_build.isEnabled()

        window._set_busy(False)
        assert window.progress.maximum() == 1
        assert window.btn_build.isEnabled()
        # Save must stay disabled: the blanket enable would otherwise offer a
        # save with no solid behind it.
        assert not window.btn_save.isEnabled()

    def test_failed_build_leaves_save_disabled(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window._on_failed("boom")
        assert window.result is None
        assert not window.btn_save.isEnabled()
        assert "boom" in window.status.text()


class TestPanels:

    def test_dem_panel_draws_the_raster_and_the_outline(self, tmp_path):
        window = _loaded(tmp_path)
        window._draw_dem()
        assert len(window.ax_dem.images) == 1     # the DEM overview
        assert len(window.ax_dem.lines) == 1      # one ring for a box

    def test_dem_panel_without_a_boundary_has_no_outline(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        window._draw_dem()
        assert len(window.ax_dem.lines) == 0

    def test_solid_panel_holds_one_visual_per_build(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        _run_build(window)
        assert len(window._solid_meshes) == 1
        _run_build(window)                        # rebuild detaches the old one
        assert len(window._solid_meshes) == 1

    def test_invalidating_clears_the_solid_panel(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        _run_build(window)
        window.exag_spin.setValue(15.0)
        assert window._solid_meshes == []
        assert window.solid_title.text() == "Not yet built"

    def test_height_colours_span_the_mesh(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        result = _run_build(window)
        colours = U._height_colors(result.mesh)
        assert colours.shape == (len(result.mesh.vertices), 4)
        assert colours.dtype == np.float32

    def test_height_colours_survive_a_flat_mesh(self, tmp_path):
        """A constant-height solid would divide by zero on a naive ramp."""
        import trimesh
        flat = trimesh.creation.box(extents=(1, 1, 1))
        flat.vertices[:, 2] = 0.0
        colours = U._height_colors(flat)
        assert np.isfinite(colours).all()


class TestSave:

    def test_build_writes_stl_and_sidecar(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        result = _run_build(window)
        from pathlib import Path
        assert Path(result.path).exists()
        assert Path(result.frame_path).exists()
        assert Path(result.frame_path).name.endswith(".frame.json")

    def test_default_name_comes_from_the_boundary(self, tmp_path):
        window = _loaded(tmp_path)
        window.boundary_path = tmp_path / "CAN_Newfoundland and Labrador.geojson"
        assert window._stem() == "CAN_Newfoundland and Labrador"

    def test_default_name_falls_back_to_the_raster(self, tmp_path):
        window = _loaded(tmp_path, boundary=False)
        assert window._stem() == "dem"
