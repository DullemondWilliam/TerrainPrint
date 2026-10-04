"""
test_terrain_editor_borders.py

UI_terrain_editor.py's Import and Borders stages, and what they hand stage 3:
many regions in, one topology simplified, the corridor carved, the connectors
placed, the province cut to its trimmed shape and pocketed, the border bar
built and split.

Pytest-style for the ``qapp`` fixture, as in test_terrain_editor.py.

The libs are tested on their own (test_border_simplification.py,
test_shared_border.py, test_border_split.py, ...).  What is guarded here is the
hand-off between them -- the order (simplify, carve, *then* cut the terrain),
which side of a frontier gives up the corridor, which lobes a province keys
into, and that a pocket lands where its lobe is -- because each lib being right
on its own does not make the pipeline right.
"""

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import Polygon, box

import geopandas as gpd

import UI_terrain_editor as U
from lib.LIB_project_config import get as cfg

pytestmark = pytest.mark.usefixtures("qapp")

WORK = "EPSG:3347"
KM = 1000.0
SCALE = cfg("print.scale")
MPM = SCALE / 1000.0

# Two printed provinces side by side, a surrounding region across the top of
# both: one target/target seam, two target/surrounding frontiers.
A = box(7_000 * KM, 1_000 * KM, 7_400 * KM, 1_400 * KM)
B = box(7_400 * KM, 1_000 * KM, 7_800 * KM, 1_400 * KM)
C = box(7_000 * KM, 1_400 * KM, 7_800 * KM, 1_800 * KM)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Headless runs have nobody to dismiss a message box."""
    monkeypatch.setattr(U.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(U.QMessageBox, "warning", lambda *a, **k: None)


def _write_region(tmp_path, name, geom, crs=WORK):
    path = tmp_path / f"{name}.geojson"
    gpd.GeoDataFrame(geometry=[geom], crs=WORK).to_crs(crs).to_file(
        path, driver="GeoJSON")
    return path


@pytest.fixture(scope="module")
def defined():
    """One border definition of the fixture map, shared (it is pure)."""
    return U.define_borders({"A": A, "B": B}, {"C": C}, U.BorderSettings())


def _window(tmp_path):
    window = U.TerrainEditorTester()
    window.add_regions([_write_region(tmp_path, "A", A),
                        _write_region(tmp_path, "B", B)], "target")
    window.add_regions([_write_region(tmp_path, "C", C)], "surrounding")
    return window


class TestLoadRegion:

    def test_reprojects_to_the_working_crs_and_keeps_the_source(self, tmp_path):
        geom, source = U.load_region(_write_region(tmp_path, "A", A,
                                                   crs="EPSG:4326"))
        assert CRS.from_user_input(source) == CRS.from_epsg(4326)
        assert geom.equals_exact(A, tolerance=1.0) or \
            abs(geom.area - A.area) / A.area < 1e-6

    def test_repairs_an_invalid_ring(self, tmp_path):
        bowtie = Polygon([(7_000 * KM, 1_000 * KM), (7_100 * KM, 1_100 * KM),
                          (7_100 * KM, 1_000 * KM), (7_000 * KM, 1_100 * KM)])
        assert not bowtie.is_valid
        geom, _ = U.load_region(_write_region(tmp_path, "bow", bowtie))
        assert geom.is_valid and geom.area > 0


class TestDefineBorders:

    def test_seam_cleanup_reaches_the_topology_lib_in_metres(self, monkeypatch):
        seen = {}
        real = U.topo_simplify_two_tolerances

        def spy(gdf, coast, internal, **kw):
            seen.update(coast=coast, internal=internal, **kw)
            return real(gdf, coast, internal, **kw)

        monkeypatch.setattr(U, "topo_simplify_two_tolerances", spy)
        s = U.BorderSettings(coast_tol_mm=0.3, internal_tol_mm=0.5,
                             min_segment_mm=0.4, min_angle_deg=25.0)
        U.define_borders({"A": A, "B": B}, {}, s)
        assert seen["coast"] == pytest.approx(0.3 * MPM)
        assert seen["internal"] == pytest.approx(0.5 * MPM)
        assert seen["min_segment_m"] == pytest.approx(0.4 * MPM)
        assert seen["min_angle_deg"] == 25.0

    def test_targets_are_trimmed_clear_of_the_corridor(self, defined):
        for name in ("A", "B"):
            assert defined.targets[name].intersection(defined.border).area \
                == pytest.approx(0.0, abs=1.0)
        assert defined.targets["A"].area < A.area

    def test_target_target_seam_is_split_evenly(self, defined):
        half = cfg("borders.width_mm") * MPM / 2
        assert defined.targets["A"].bounds[2] == pytest.approx(
            7_400 * KM - half, abs=1.0)
        assert defined.targets["B"].bounds[0] == pytest.approx(
            7_400 * KM + half, abs=1.0)

    def test_surrounding_gives_up_the_whole_corridor(self, defined):
        """FR-3.3: a printed region keeps its full extent at a frontier."""
        assert defined.targets["A"].bounds[3] == pytest.approx(1_400 * KM,
                                                               abs=1.0)

    def test_no_overlap_between_the_simplified_targets(self, defined):
        assert defined.overlap_mm2 == pytest.approx(0.0, abs=1e-9)

    def test_an_overlap_goes_back_to_the_province_that_held_it(self):
        """
        A shared arc straightened across a neighbour's island makes two
        simplified provinces claim the same land; carving needs them disjoint.
        """
        sliver = box(7_390 * KM, 1_100 * KM, 7_400 * KM, 1_200 * KM)
        simplified = {"A": A, "B": B.union(sliver)}     # B overreached into A
        fixed_m2 = U._resolve_overlaps(simplified, ["A", "B"],
                                       {"A": A, "B": B})
        assert fixed_m2 == pytest.approx(sliver.area)
        assert simplified["A"].equals(A)                # A held it, A keeps it
        assert simplified["A"].intersection(simplified["B"]).area             == pytest.approx(0.0, abs=1.0)

    def test_carved_targets_are_disjoint(self, defined):
        a, b = defined.targets["A"], defined.targets["B"]
        assert a.intersection(b).area == pytest.approx(0.0, abs=1.0)

    def test_each_lobe_keys_into_exactly_one_target(self, defined):
        assert defined.lobes
        for lobe in defined.lobes:
            reach = lobe.difference(defined.border)
            owners = [n for n, g in defined.targets.items()
                      if reach.intersection(g).area > 1e-3 * lobe.area]
            assert len(owners) == 1

    def test_no_lobe_reaches_into_a_surrounding_region(self, defined):
        """FR-3.8: there is nothing in a surrounding region to key into."""
        corridor_free_c = C.difference(defined.border)
        for lobe in defined.lobes:
            assert lobe.intersection(corridor_free_c).area < 1e-3 * lobe.area

    def test_province_lobes_partition_the_lobes(self, defined):
        a, b = defined.province_lobes("A"), defined.province_lobes("B")
        assert a and b
        assert len(a) + len(b) == len(defined.lobes)

    def test_connectors_off_places_none(self):
        d = U.define_borders({"A": A, "B": B}, {"C": C},
                             U.BorderSettings(connectors=False))
        assert d.lobes == [] and d.border is not None

    def test_needs_a_target(self):
        with pytest.raises(ValueError, match="at least one province"):
            U.define_borders({}, {"C": C})


class TestImportStage:

    def test_regions_land_in_their_roles(self, tmp_path):
        window = _window(tmp_path)
        assert set(window.targets) == {"A", "B"}
        assert set(window.surrounding) == {"C"}
        assert window.region_list.count() == 3
        assert window.tabs.isTabEnabled(1)

    def test_re_adding_moves_a_region_between_roles(self, tmp_path):
        """FR-1.8: a region is printed or context, never both."""
        window = _window(tmp_path)
        window.add_regions([_write_region(tmp_path, "B", B)], "surrounding")
        assert "B" not in window.targets and "B" in window.surrounding

    def test_province_combo_lists_only_targets(self, tmp_path):
        window = _window(tmp_path)
        items = [window.province_combo.itemText(i)
                 for i in range(window.province_combo.count())]
        assert items == ["A", "B"]

    def test_selected_province_becomes_the_boundary(self, tmp_path):
        window = _window(tmp_path)
        window.province_combo.setCurrentText("B")
        assert window.boundary.geometry.iloc[0].equals(window.targets["B"])
        assert window._stem() == "B"

    def test_removing_the_selected_province_clears_the_boundary(self, tmp_path):
        window = _window(tmp_path)
        for name in ("A", "B"):
            window.remove_region(name)
        assert window.boundary is None
        assert not window.tabs.isTabEnabled(1)

    def test_a_bad_file_is_skipped_not_fatal(self, tmp_path):
        window = U.TerrainEditorTester()
        bad = tmp_path / "bad.geojson"
        bad.write_text("not json")
        added = window.add_regions([bad, _write_region(tmp_path, "A", A)],
                                   "target")
        assert added == ["A"]

    def test_outlines_drawn_with_no_dem(self, tmp_path):
        window = _window(tmp_path)
        window._draw_before()
        assert len(window.ax_before.lines) == 3     # three one-ring boxes


class TestBordersStage:

    def test_stage_3_cuts_to_the_trimmed_province(self, tmp_path, defined):
        window = _window(tmp_path)
        window.province_combo.setCurrentText("A")
        window.apply_borders(defined)
        assert window.boundary.geometry.iloc[0].equals(defined.targets["A"])

    def test_buffer_is_locked_at_zero_once_borders_exist(self, tmp_path,
                                                          defined):
        window = _window(tmp_path)
        window.buf_spin.setValue(500.0)
        window.apply_borders(defined)
        assert window.buf_spin.value() == 0.0
        assert not window.buf_spin.isEnabled()
        window._set_busy(True)
        window._set_busy(False)
        assert not window.buf_spin.isEnabled()

    def test_a_setting_change_reverts_stage_3_to_the_imported_province(
            self, tmp_path, defined):
        window = _window(tmp_path)
        window.province_combo.setCurrentText("A")
        window.apply_borders(defined)
        window.width_spin.setValue(window.width_spin.value() + 0.5)
        assert window.borders is None
        assert window.boundary.geometry.iloc[0].equals(window.targets["A"])
        assert window.buf_spin.isEnabled()

    def test_adding_a_region_stales_the_definition(self, tmp_path, defined):
        window = _window(tmp_path)
        window.apply_borders(defined)
        window.add_regions([_write_region(tmp_path, "D", box(
            6_600 * KM, 1_000 * KM, 7_000 * KM, 1_400 * KM))], "target")
        assert window.borders is None
        assert not window.btn_save_borders.isEnabled()

    def test_settings_come_from_the_spins(self, tmp_path):
        window = _window(tmp_path)
        window.coast_spin.setValue(0.6)
        window.chk_connectors.setChecked(False)
        s = window.border_settings()
        assert s.coast_tol_mm == pytest.approx(0.6)
        assert s.connectors is False

    def test_report_names_the_numbers(self, tmp_path, defined):
        window = _window(tmp_path)
        window.apply_borders(defined)
        text = window.border_report.text()
        assert "No overlap." in text
        assert f"{len(defined.lobes)} connector(s)" in text

    def test_borders_tab_draws_both_panels(self, tmp_path, defined):
        window = _window(tmp_path)
        window.apply_borders(defined)
        window.tabs.setCurrentIndex(1)
        assert window.ax_before.patches and window.ax_after.patches
        assert window.ax_after.get_title().startswith("Defined")
        # Leaving it must not carry the borders' limits into the raster view.
        assert window._before_drawn is False
        window.tabs.setCurrentIndex(2)
        assert window.ax_after.get_title() == "Not yet run"

    def test_saved_folder_is_what_the_dovetail_tools_read(self, tmp_path,
                                                         defined):
        from lib.LIB_border_connectors import build_border_from_dir
        window = _window(tmp_path)
        window.apply_borders(defined)
        folder = tmp_path / "borders"
        written = window.write_borders(folder)
        assert (folder / "borders.geojson") in written
        assert (folder / "name_A.geojson").exists()
        mesh, n_lobes = build_border_from_dir(
            folder, tmp_path / "bar.stl",
            U.BorderSettings().connector_params())
        assert mesh is not None and n_lobes > 0


@pytest.fixture(scope="module")
def bar(defined):
    return U.build_bar(defined)


class TestBorderBar:

    def test_every_piece_is_watertight_and_fits_the_bed(self, bar):
        usable = cfg("print.bed_mm") - 2 * cfg("print.bed_margin_mm")
        assert bar.pieces
        for piece in bar.pieces:
            assert piece.is_watertight and piece.volume > 0
            w, d, _ = piece.extents
            assert w <= usable + 1e-6 and d <= usable + 1e-6

    def test_bar_carries_the_lobe_layer(self, bar, defined):
        assert bar.lobes == len(defined.lobes)
        assert bar.mesh.extents[2] == pytest.approx(
            cfg("print.border_thickness_mm"))

    def test_origin_is_the_bars_print_mm_zero(self, bar):
        assert bar.mesh.bounds[0][:2] == pytest.approx((0.0, 0.0), abs=1e-6)

    def test_cut_connectors_are_counted(self, defined):
        """A tiny bed forces cuts through lobes; they must be counted."""
        bar = U.build_bar(defined, bed_mm=40.0, margin_mm=0.0)
        assert len(bar.pieces) > 1
        assert bar.lobes_cut > 0

    def test_window_shows_the_bar_and_offers_the_save(self, tmp_path, defined,
                                                      bar):
        window = _window(tmp_path)
        window.apply_borders(defined)
        window.apply_bar(bar)
        assert window.btn_save_bar.isEnabled()
        assert window.show_combo.currentText() == "Border bar"
        assert len(window._solid_meshes) == 1

    def test_new_borders_stale_the_bar(self, tmp_path, defined, bar):
        window = _window(tmp_path)
        window.apply_borders(defined)
        window.apply_bar(bar)
        window.apply_borders(defined)
        assert window.bar is None and not window.btn_save_bar.isEnabled()

    def test_no_corridor_is_refused(self):
        lonely = U.define_borders({"A": A}, {})
        with pytest.raises(ValueError, match="No corridor"):
            U.build_bar(lonely)


def _write_dem(tmp_path, crs=WORK, res=3200.0):
    """A sloping DEM in the print CRS covering A with a margin."""
    x0, y0 = 6_980 * KM, 1_420 * KM
    cols = rows = int(440 * KM / res)
    data = np.tile(np.linspace(50, 500, cols, dtype="float32"), (rows, 1))
    path = tmp_path / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", width=cols, height=rows,
                       count=1, dtype="float32", crs=crs, nodata=-32767.0,
                       transform=Affine.translation(x0, y0)
                       * Affine.scale(res, -res)) as ds:
        ds.write(data, 1)
    return path


class TestPockets:

    def _params(self):
        return U.TerrainParams(scale=SCALE, expect_crs=None)

    def test_pockets_remove_material_and_stay_watertight(self, tmp_path,
                                                         defined):
        dem = _write_dem(tmp_path)
        boundary = defined.targets["A"]
        plain = U.build_terrain(dem, boundary, None, self._params())
        lobes = defined.province_lobes("A")
        keyed = U.build_terrain(dem, boundary, tmp_path / "a.stl",
                                self._params(), lobes,
                                defined.settings.connector_params())
        assert keyed.watertight and keyed.volume_mm3 > 0
        assert keyed.volume_mm3 < plain.volume_mm3
        assert (tmp_path / "a.stl").exists()

    def test_pockets_are_cut_under_the_lobes(self, tmp_path, defined):
        """
        The material removed is exactly the lobes' footprint inside the
        province, a dovetail deep.

        A pocket carved in the wrong frame -- a stale origin, a CRS slip --
        lands partly or wholly off the solid and removes less, so this fails
        on placement, not merely on "something was cut".
        """
        from shapely.ops import unary_union
        dem = _write_dem(tmp_path)
        province = defined.targets["A"]
        lobes = defined.province_lobes("A")
        params = defined.settings.connector_params()
        plain = U.build_terrain(dem, province, None, self._params())
        keyed = U.build_terrain(dem, province, None, self._params(), lobes,
                                params)
        pocket_m2 = unary_union(
            [lobe.buffer(params.clearance_mm * MPM) for lobe in lobes]
        ).intersection(province).area
        expected = pocket_m2 / MPM ** 2 * params.dovetail_mm
        removed = plain.volume_mm3 - keyed.volume_mm3
        assert removed == pytest.approx(expected, rel=0.02)

    def test_pockets_refuse_a_crop_in_another_crs(self, tmp_path, defined):
        dem = _write_dem(tmp_path, crs="EPSG:3979")
        with pytest.raises(ValueError, match="Pockets need"):
            U.build_terrain(dem, None, None, self._params(),
                            defined.province_lobes("A"))

    def test_window_hands_only_its_provinces_lobes(self, tmp_path, defined):
        window = _window(tmp_path)
        window.province_combo.setCurrentText("B")
        window.apply_borders(defined)
        assert window.province_lobes() == defined.province_lobes("B")
        window.chk_pockets.setChecked(False)
        assert window.province_lobes() == []


class TestAssembly:

    def test_assembly_draws_solid_and_bar_in_one_frame(self, tmp_path,
                                                      defined):
        window = _window(tmp_path)
        window.province_combo.setCurrentText("A")
        window.apply_borders(defined)
        window.apply_bar(U.build_bar(defined))
        window.stl_result = U.build_terrain(
            _write_dem(tmp_path), defined.targets["A"], None,
            U.TerrainParams(scale=SCALE, expect_crs=None))
        window.show_combo.setCurrentText("Assembly")
        assert len(window._solid_meshes) == 2
        assert window.solid_title.text().startswith("Assembly")

    def test_solid_lands_against_the_corridor(self, tmp_path, defined, bar):
        """In the bar's frame, A's east edge meets the corridor's A side."""
        window = _window(tmp_path)
        window.province_combo.setCurrentText("A")
        window.apply_borders(defined)
        window.apply_bar(bar)
        window.stl_result = U.build_terrain(
            _write_dem(tmp_path), defined.targets["A"], None,
            U.TerrainParams(scale=SCALE, expect_crs=None))
        _, meshes = window._assembly()
        solid = meshes[1][0]
        seam_x = (defined.targets["A"].bounds[2] - bar.origin_m[0]) / MPM
        assert solid.bounds[1][0] == pytest.approx(seam_x, abs=0.05)

    def test_assembly_without_both_says_so(self, tmp_path):
        window = _window(tmp_path)
        window.show_combo.setCurrentText("Assembly")
        assert window._solid_meshes == []
        assert "needs both" in window.solid_title.text()
