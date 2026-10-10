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
from shapely.geometry import Point, Polygon, box

import geopandas as gpd

import UI_terrain_editor as U
from lib.LIB_border_connectors import lobe_at
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
        window._draw_map()
        assert len(window.ax.lines) == 3     # three one-ring boxes


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

    def test_borders_tab_shows_the_latest_state(self, tmp_path, defined):
        window = _window(tmp_path)
        window.tabs.setCurrentIndex(1)
        assert window.ax.get_title().startswith("As imported")
        window.apply_borders(defined)
        assert window.ax.patches
        assert window.ax.get_title().startswith("Defined")
        window.tabs.setCurrentIndex(2)
        assert window.ax.get_title().startswith("Regions")

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


#: Steep enough that a flat bar would sit millimetres below the solids.
DRAPE_PARAMS = dict(scale=SCALE, expect_crs=None, z_exaggeration=100.0)


@pytest.fixture(scope="module")
def draped(defined, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("draped")
    return U.build_draped_bar(defined, _write_dem(tmp),
                              U.ReprojectParams(dst_res_m=3200.0),
                              U.TerrainParams(**DRAPE_PARAMS),
                              tmp / "surface.tif")


class TestDrapedBar:
    """The bar's top stands as tall as the solids either side of it."""

    def test_pieces_stay_printable(self, draped):
        assert draped.draped and draped.pieces
        for piece in draped.pieces:
            assert piece.is_watertight and piece.volume > 0

    def test_rises_above_the_flat_bar(self, draped, bar):
        assert not bar.draped
        assert draped.mesh.extents[2] > bar.mesh.extents[2] + 1.0
        assert draped.origin_m == bar.origin_m

    def test_meets_the_solid_at_its_height(self, tmp_path, defined, draped):
        """Along A's east edge the bar's top is the solid's top."""
        solid = U.build_terrain(_write_dem(tmp_path), defined.targets["A"],
                                None, U.TerrainParams(**DRAPE_PARAMS)).mesh
        # The DEM rises only eastward, so each edge is one height along it.
        east = solid.vertices[solid.vertices[:, 0] > solid.bounds[1][0] - 1e-3]
        seam_x = (defined.targets["A"].bounds[2] - draped.origin_m[0]) / MPM
        v = draped.mesh.vertices
        bar_edge = v[np.abs(v[:, 0] - seam_x) < 1e-3]
        assert len(bar_edge)
        assert bar_edge[:, 2].max() == pytest.approx(east[:, 2].max(),
                                                     abs=0.1)

    def test_terrain_settings_stale_a_draped_bar(self, tmp_path, defined,
                                                 draped, bar):
        window = _window(tmp_path)
        window.apply_borders(defined)
        window.apply_bar(bar)
        window.exag_spin.setValue(window.exag_spin.value() + 5.0)
        assert window.bar is bar           # flat: the terrain is not under it
        window.apply_bar(draped)
        assert "draped" in window.status.text()
        window.exag_spin.setValue(window.exag_spin.value() + 5.0)
        assert window.bar is None and not window.btn_save_bar.isEnabled()


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

    def test_clearances_size_the_pocket(self, tmp_path, defined):
        """Wider by Clearance per side, deeper by Height clear."""
        from dataclasses import replace
        from shapely.ops import unary_union
        dem = _write_dem(tmp_path)
        province = defined.targets["A"]
        lobes = defined.province_lobes("A")
        params = replace(defined.settings, clearance_mm=0.4,
                         height_clearance_mm=0.3).connector_params()
        assert (params.clearance_mm, params.height_clearance_mm) == (0.4, 0.3)
        plain = U.build_terrain(dem, province, None, self._params())
        keyed = U.build_terrain(dem, province, None, self._params(), lobes,
                                params)
        pocket_m2 = unary_union(
            [lobe.buffer(0.4 * MPM) for lobe in lobes]
        ).intersection(province).area
        expected = pocket_m2 / MPM ** 2 * (params.dovetail_mm + 0.3)
        removed = plain.volume_mm3 - keyed.volume_mm3
        assert removed == pytest.approx(expected, rel=0.02)

    def test_a_pocket_through_the_base_is_refused(self, tmp_path, defined):
        from dataclasses import replace
        params = replace(defined.settings,
                         height_clearance_mm=1.5).connector_params()
        with pytest.raises(ValueError, match="cuts through"):
            U.build_terrain(_write_dem(tmp_path), defined.targets["A"], None,
                            self._params(), defined.province_lobes("A"),
                            params)

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


# -- where Define puts connectors ---------------------------------------------

def _pocket(lobe, d):
    p = d.settings.connector_params()
    return lobe.buffer(p.clearance_mm * MPM).difference(d.border)


def _assert_standing(d):
    """Every connector's pocket inside one province; no two touching."""
    p = d.settings.connector_params()
    targets = list(d.targets.values())
    for lobe in d.lobes:
        pocket = _pocket(lobe, d)
        owner = max(targets, key=lambda g: pocket.intersection(g).area)
        assert pocket.difference(owner).area < 1e-3 * lobe.area
    for lobe in d.lobes:
        assert U._pieces(lobe.intersection(d.border), lobe) == 1
        assert U._pieces(lobe.difference(d.border), lobe) == 1
    for i, a in enumerate(d.lobes):
        for b in d.lobes[i + 1:]:
            assert not a.intersects(b)
            assert a.difference(d.border).distance(
                b.difference(d.border)) >= p.clearance_mm * MPM - 1e-6


class TestAutoPlacement:

    def test_every_connector_stays_in_its_province(self, defined):
        assert defined.lobes
        _assert_standing(defined)

    def test_crowded_connectors_are_dropped_not_overlapped(self):
        """Spaced closer than they are wide, every other one must go."""
        d = U.define_borders({"A": A, "B": B}, {"C": C},
                             U.BorderSettings(lobe_spacing_mm=2.0))
        assert d.lobes
        assert d.drop_reasons.get("crowded", 0) > 0
        assert d.lobes_dropped == sum(d.drop_reasons.values())
        _assert_standing(d)

    def test_a_pocket_off_the_coast_is_refused(self, defined):
        """The A/B seam's south end meets open sea: a connector there would
        put half its pocket in the water."""
        lobe, why = U.place_connector(defined, A.bounds[2] - 2 * MPM,
                                      A.bounds[1] + 0.3 * MPM)
        assert lobe is None and why == U.FAULTS["escapes"]

    def test_a_connector_meeting_the_corridor_twice_is_refused(self,
                                                              defined):
        """Just under the T-junction, a connector on the A/B seam swings its
        mouth up into the C corridor: the bar twice, not once."""
        lobe, why = U.place_connector(defined, A.bounds[2] - 2 * MPM,
                                      A.bounds[3] - 1.0 * MPM)
        assert lobe is None and why == U.FAULTS["recrosses"]

    def test_auto_placement_drops_a_connector_meeting_it_twice(self,
                                                               defined):
        p = defined.settings.connector_params()
        free = lobe_at(defined.border, A.bounds[2] - 2 * MPM,
                       A.bounds[1] + 25 * MPM, p)
        junction = lobe_at(defined.border, A.bounds[2] - 2 * MPM,
                           A.bounds[3] - 1.0 * MPM, p)
        assert U._pieces(junction.intersection(defined.border), junction) == 2
        kept, reasons = U._standing_lobes([free, junction], defined, p)
        assert kept == [free]
        assert reasons == {"recrosses": 1}

    def test_define_refuses_a_connector_across_the_border_line(self,
                                                               defined):
        """Turned 45 degrees, a connector's root corner reaches past the A/B
        line: Define drops it, but by hand it may stand."""
        p = defined.settings.connector_params()
        turned = lobe_at(defined.border, A.bounds[2] - 2 * MPM,
                         A.bounds[1] + 25 * MPM, p, 45.0)
        assert turned.intersects(defined.border_line())
        kept, reasons = U._standing_lobes([turned], defined, p)
        assert kept == [] and reasons == {"crosses_line": 1}
        x, y, angle, scale = U.lobe_pose(turned, defined.border, p)
        lobe, why = U.place_connector(defined, x, y, angle_deg=angle,
                                      scale=scale, allow_faults=True)
        assert lobe is not None and why == ""

    def test_the_border_line_leaves_out_the_surrounding_frontier(self,
                                                                 defined):
        """Every connector's root crosses the A/C frontier by design: that
        corridor is wholly on C's side."""
        line = defined.border_line()
        assert line.distance(Point(7_200 * KM, 1_400 * KM)) > 50 * KM
        assert line.distance(Point(A.bounds[2], 1_200 * KM)) < MPM

    def test_each_rule_is_counted_by_its_own_reason(self, defined):
        p = defined.settings.connector_params()
        free = lobe_at(defined.border, A.bounds[2] - 2 * MPM,
                       A.bounds[1] + 25 * MPM, p)
        coast = lobe_at(defined.border, A.bounds[2] - 2 * MPM,
                        A.bounds[1] + 0.3 * MPM, p)
        kept, reasons = U._standing_lobes([free, free, coast], defined, p)
        assert kept == [free]
        assert reasons == {"crowded": 1, "escapes": 1}

    def test_the_report_says_why(self, tmp_path):
        d = U.define_borders({"A": A, "B": B}, {"C": C},
                             U.BorderSettings(lobe_spacing_mm=2.0))
        window = _window(tmp_path)
        window.apply_borders(d)
        assert "ran into another connector" in window.border_report.text()


# -- editing connectors by hand ----------------------------------------------
# On the fixture the A/B seam's corridor runs x 7392-7408 km; A's lobes sit
# along its west edge at y ~1040, 1152, 1264, 1376 km and B's along its east
# edge at y ~1136, 1248, 1360 km.  So y 1210 km is free on both sides.
FREE_A = (7_380 * KM, 1_210 * KM)       # beside the corridor, in A
FREE_B = (7_420 * KM, 1_050 * KM)       # beside the corridor, in B
ON_FIRST = (7_380 * KM, 1_040 * KM)     # where A's first lobe already is
FACING_C = (7_300 * KM, 1_430 * KM)     # across the A/C frontier, in C


def _fresh(defined):
    """A copy whose lobe list the window may change: ``defined`` is shared."""
    from dataclasses import replace
    return replace(defined, lobes=list(defined.lobes))


def _edit_window(tmp_path, defined):
    window = _window(tmp_path)
    window.apply_borders(_fresh(defined))
    window.tabs.setCurrentIndex(1)
    window.btn_edit_lobes.setChecked(True)
    return window


def _owner(definition, lobe):
    reach = lobe.difference(definition.border)
    return [n for n, g in definition.targets.items()
            if reach.intersection(g).area > 1e-3 * lobe.area]


class TestCrossingTheBoundary:
    """A connector must not cross its province's boundary line -- the coast,
    or a frontier -- or its pocket opens in the side of the printed piece.

    The rule ("escapes") measures how much *area* of the pocket lies outside
    the province.  These tests hold it to a different question, asked of the
    line itself: does the pocket touch the province's boundary anywhere but
    along the corridor it hangs from?
    """

    #: Three print-mm up the A/B seam from the coast at its south end: a
    #: connector here is valid square to the corridor, and swung 40 degrees
    #: towards the coast its mouth crosses it.
    NEAR_COAST = (A.bounds[2] - 2 * MPM, A.bounds[1] + 3 * MPM)

    @staticmethod
    def _crosses_the_line(lobe, d) -> bool:
        p = d.settings.connector_params()
        pocket = lobe.buffer(p.clearance_mm * p.mpm).difference(d.border)
        owner = max(d.targets.values(),
                    key=lambda geom: pocket.intersection(geom).area)
        # The province's outline, less the corridor edge every pocket meets.
        line = owner.boundary.difference(d.border.buffer(0.01 * p.mpm))
        return pocket.intersects(line)

    def test_the_rule_agrees_with_the_line_everywhere(self, defined):
        """Every corridor edge, every half-millimetre, three ways turned."""
        from lib.LIB_border_connectors import _rings
        d, p = defined, defined.settings.connector_params()
        crossed = inside = 0
        for ring in _rings(d.border):
            for s in np.arange(0.0, ring.length, 0.5 * MPM):
                at = ring.interpolate(s)
                for angle in (-40.0, 0.0, 40.0):
                    lobe = lobe_at(d.border, at.x, at.y, p, angle)
                    if lobe is None:
                        continue
                    code = U._own_fault(lobe, d, p, d.land())
                    if code not in ("", "escapes"):
                        continue      # broke an earlier rule: not this one's
                    crosses = self._crosses_the_line(lobe, d)
                    assert crosses == (code == "escapes"), (at, angle)
                    crossed += crosses
                    inside += not crosses
        assert crossed and inside             # both answers were exercised

    def test_no_defined_connector_crosses_the_line(self, defined):
        assert defined.lobes
        for lobe in defined.lobes:
            assert not self._crosses_the_line(lobe, defined)

    def test_one_placed_across_the_coast_is_red(self, tmp_path, defined):
        """At the very end of the seam, where it meets the open sea."""
        window = _edit_window(tmp_path, defined)
        assert window.add_connector(A.bounds[2] - 2 * MPM,
                                    A.bounds[1] + 0.3 * MPM)
        lobe = window.borders.lobes[-1]
        assert self._crosses_the_line(lobe, window.borders)
        assert window.borders.faults()[-1] == "escapes"
        assert U.STANDING_FAULTS["escapes"] in window.status.text()

    def test_turning_one_across_the_coast_turns_it_red(self, tmp_path,
                                                       defined):
        window = _edit_window(tmp_path, defined)
        spot = Point(self.NEAR_COAST)
        for i in reversed(range(len(window.borders.lobes))):
            if window.borders.lobes[i].distance(spot) < 10 * MPM:
                window.delete_connector(i)        # room to work in
        assert window.add_connector(*self.NEAR_COAST)
        index = len(window.borders.lobes) - 1
        assert not any(window.borders.faults())
        window.select_connector(index)

        window.pose_selected(40.0, 1.0, "Turned a connector.")
        lobe = window.borders.lobes[index]
        assert self._crosses_the_line(lobe, window.borders)
        assert window.borders.faults()[index] == "escapes"

        window.pose_selected(0.0, 1.0, "Turned a connector.")
        lobe = window.borders.lobes[index]
        assert not self._crosses_the_line(lobe, window.borders)
        assert not any(window.borders.faults())


class TestPlaceConnector:

    def test_lands_on_the_side_clicked(self, defined):
        lobe_a, why = U.place_connector(defined, *FREE_A)
        assert lobe_a is not None, why
        assert _owner(defined, lobe_a) == ["A"]
        lobe_b, why = U.place_connector(defined, *FREE_B)
        assert lobe_b is not None, why
        assert _owner(defined, lobe_b) == ["B"]

    def test_has_the_generated_profile(self, defined):
        lobe, _ = U.place_connector(defined, *FREE_A)
        assert lobe.area == pytest.approx(defined.lobes[0].area, rel=1e-6)

    def test_its_root_sinks_into_the_corridor(self, defined):
        lobe, _ = U.place_connector(defined, *FREE_A)
        assert lobe.intersection(defined.border).area > 0

    def test_refused_facing_a_surrounding_region(self, defined):
        lobe, why = U.place_connector(defined, *FACING_C)
        assert lobe is None and "no province" in why

    def test_refused_on_top_of_another_connector(self, defined):
        lobe, why = U.place_connector(defined, *ON_FIRST)
        assert lobe is None and "another connector" in why

    def test_a_moved_connector_may_land_where_it_was(self, defined):
        lobe, why = U.place_connector(defined, *ON_FIRST, ignore=0)
        assert lobe is not None, why

    def test_refused_with_no_corridor(self, defined):
        from dataclasses import replace
        lobe, why = U.place_connector(replace(defined, border=None), *FREE_A)
        assert lobe is None and "no corridor" in why


class TestEditingConnectors:

    def test_tool_needs_connectors_and_a_corridor(self, tmp_path, defined):
        window = _window(tmp_path)
        assert not window.btn_edit_lobes.isEnabled()
        window.apply_borders(_fresh(defined))
        assert window.btn_edit_lobes.isEnabled()
        window.btn_edit_lobes.setChecked(True)
        window.chk_connectors.setChecked(False)     # stales the definition
        assert not window.btn_edit_lobes.isEnabled()
        assert not window.btn_edit_lobes.isChecked()

    def test_add_delete_move(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        n = len(defined.lobes)
        assert window.add_connector(*FREE_A)
        assert len(window.borders.lobes) == n + 1
        window.delete_connector(n)
        assert len(window.borders.lobes) == n
        first = window.borders.lobes[0]
        assert window.move_connector(0, *FREE_A)
        assert len(window.borders.lobes) == n
        assert not window.borders.lobes[0].equals(first)
        assert _owner(window.borders, window.borders.lobes[0]) == ["A"]

    def test_an_invalid_add_is_kept_red_and_says_why(self, tmp_path,
                                                     defined):
        """Facing a surrounding region it has nothing to key into -- but a
        connector put by hand stays, red, until it is fixed."""
        window = _edit_window(tmp_path, defined)
        assert window.add_connector(*FACING_C)
        assert len(window.borders.lobes) == len(defined.lobes) + 1
        assert window.borders.faults()[-1] == "no_province"
        assert "1 invalid (red)" in window.status.text()
        assert U.STANDING_FAULTS["no_province"] in window.status.text()
        assert "1 invalid (red)" in window.border_report.text()
        window.undo_connectors()
        assert window.borders.lobes == list(defined.lobes)

    def test_moving_onto_another_is_kept_and_both_go_red(self, tmp_path,
                                                         defined):
        window = _edit_window(tmp_path, defined)
        assert window.move_connector(1, *ON_FIRST)
        faults = window.borders.faults()
        assert faults[0] == faults[1] == "crowded"
        assert sum(1 for code in faults if code) == 2

    def test_a_red_connector_turns_valid_once_fixed(self, tmp_path,
                                                    defined):
        window = _edit_window(tmp_path, defined)
        window.add_connector(*FACING_C)
        bad = len(window.borders.lobes) - 1
        assert window.move_connector(bad, *FREE_A)
        assert not any(window.borders.faults())
        assert "invalid" not in window.status.text()

    def test_a_red_connector_is_drawn_red(self, tmp_path, defined):
        from matplotlib.collections import PolyCollection
        from matplotlib.colors import to_rgba
        window = _edit_window(tmp_path, defined)
        window.add_connector(*FACING_C)
        (layer,) = [a for a in window.ax.collections
                    if isinstance(a, PolyCollection)]
        faces = [tuple(f) for f in layer.get_facecolors()]
        assert faces[-1] == to_rgba(U.INVALID_COLOUR)
        assert faces[0] == to_rgba(U.RESULT_COLOUR)

    def test_selecting_a_red_connector_says_why(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.add_connector(*FACING_C)
        window.select_connector(None)
        window.select_connector(len(window.borders.lobes) - 1)
        assert "is invalid (red)" in window.status.text()

    def test_nothing_is_built_from_a_red_connector(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.add_connector(*FACING_C)
        window.build_border_bar()
        assert window._thread is None and window.bar is None
        assert "not built" in window.status.text()

    def test_define_places_only_valid_connectors(self, defined):
        assert defined.lobes and not any(defined.faults())

    def test_an_edit_measures_only_what_changed(self, tmp_path, defined,
                                                monkeypatch):
        """On all of Canada measuring every connector is a second a click."""
        window = _edit_window(tmp_path, defined)
        window.borders.faults()                    # every one measured
        measured = []
        real = U._own_fault
        monkeypatch.setattr(U, "_own_fault", lambda lobe, *a: (
            measured.append(lobe), real(lobe, *a))[1])
        window.add_connector(*FREE_A)
        assert measured and all(lobe is window.borders.lobes[-1]
                                for lobe in measured)

    def test_the_shared_definition_is_not_touched(self, tmp_path, defined):
        n = len(defined.lobes)
        window = _edit_window(tmp_path, defined)
        window.delete_connector(0)
        assert len(defined.lobes) == n and not defined.lobes_edited

    def test_undo_and_redo(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        original = list(window.borders.lobes)
        window.add_connector(*FREE_A)
        window.delete_connector(0)
        edited = list(window.borders.lobes)
        window.undo_connectors()
        window.undo_connectors()
        assert window.borders.lobes == original
        assert not window.btn_lobe_undo.isEnabled()
        window.redo_connectors()
        window.redo_connectors()
        assert window.borders.lobes == edited
        assert not window.btn_lobe_redo.isEnabled()

    def test_a_new_edit_drops_the_redo(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.delete_connector(0)
        window.undo_connectors()
        window.add_connector(*FREE_B)
        assert not window.btn_lobe_redo.isEnabled()

    def test_defining_again_discards_the_history(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.delete_connector(0)
        window.apply_borders(_fresh(defined))
        assert not window.btn_lobe_undo.isEnabled()
        assert "edited by hand" not in window.border_report.text()

    def test_report_says_edited(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.delete_connector(0)
        text = window.border_report.text()
        assert f"{len(defined.lobes) - 1} connector(s) (edited by hand)" in text

    def test_an_edit_stales_the_bar(self, tmp_path, defined, bar):
        window = _edit_window(tmp_path, defined)
        window.apply_bar(bar)
        window.delete_connector(0)
        assert window.bar is None
        assert not window.btn_save_bar.isEnabled()

    def test_an_edit_redraws_only_the_connectors(self, tmp_path, defined):
        """The provinces and the corridor stay the artists they were: on all
        of Canada redrawing them was 0.8 s of every edit."""
        window = _edit_window(tmp_path, defined)
        under = [a for a in window.ax.patches]
        assert under
        full = []
        window._draw_borders = lambda *a, **k: full.append(1)
        window.add_connector(*FREE_A)
        assert not full
        assert all(a in window.ax.patches for a in under)
        assert f"{len(defined.lobes) + 1} connector(s)" in \
            window.ax.get_title()

    def test_the_connector_layer_is_replaced_not_stacked(self, tmp_path,
                                                         defined):
        window = _edit_window(tmp_path, defined)
        before = len(window.ax.get_children())
        window.add_connector(*FREE_A)
        window.undo_connectors()
        assert len(window.ax.get_children()) == before

    def test_the_land_is_merged_once(self, defined):
        d = _fresh(defined)
        assert d.land() is d.land()
        d.targets = dict(d.targets, A=A)            # a different target
        assert d.land().equals(U._safe_union(list(d.targets.values())))

    def test_an_edit_keeps_the_view(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.ax.set_xlim(7_300 * KM, 7_500 * KM)
        window.ax.set_ylim(1_100 * KM, 1_300 * KM)
        window.add_connector(*FREE_A)
        assert window.ax.get_xlim() == (7_300 * KM, 7_500 * KM)
        assert window.ax.get_ylim() == (1_100 * KM, 1_300 * KM)

    def test_pockets_follow_the_edit(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.province_combo.setCurrentText("A")
        window.add_connector(*FREE_A)
        assert window.borders.lobes[-1] in window.province_lobes()

    def test_saved_connectors_are_what_the_folder_builds(self, tmp_path,
                                                         defined):
        from lib.LIB_border_connectors import build_border_from_dir
        window = _edit_window(tmp_path, defined)
        window.delete_connector(0)
        window.delete_connector(0)
        folder = tmp_path / "borders"
        assert (folder / "connectors.geojson") in window.write_borders(folder)
        _, n_lobes = build_border_from_dir(
            folder, tmp_path / "bar.stl", U.BorderSettings().connector_params())
        assert n_lobes == len(defined.lobes) - 2

    def test_saving_none_removes_a_stale_file(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        folder = tmp_path / "borders"
        window.write_borders(folder)
        window.borders.lobes = []
        window.write_borders(folder)
        assert not (folder / "connectors.geojson").exists()


class TestConnectorMouse:
    """The tool through matplotlib events, as a person would use it."""

    @staticmethod
    def _event(window, xy, button=1):
        from types import SimpleNamespace
        x, y = window.ax.transData.transform(xy)
        return SimpleNamespace(button=button, inaxes=window.ax,
                               xdata=xy[0], ydata=xy[1], x=x, y=y)

    def _click(self, window, xy):
        event = self._event(window, xy)
        window._on_press(event)
        window._on_release(event)

    def test_click_beside_the_corridor_adds(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        self._click(window, FREE_A)
        assert len(window.borders.lobes) == len(defined.lobes) + 1

    def test_click_on_a_connector_selects_it(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        self._click(window, defined.lobes[0].centroid.coords[0])
        assert window.borders.lobes == defined.lobes      # nothing deleted
        assert window._selected == 0
        assert window.btn_sel_delete.isEnabled()

    def test_drag_moves_and_previews(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window._on_press(self._event(window,
                                     defined.lobes[0].centroid.coords[0]))
        window._on_motion(self._event(window, FREE_A))
        assert window._drag_artist is not None
        window._on_release(self._event(window, FREE_A))
        assert window._drag_artist is None
        assert len(window.borders.lobes) == len(defined.lobes)
        assert not window.borders.lobes[0].equals(defined.lobes[0])

    def test_a_click_far_from_the_corridor_does_nothing(self, tmp_path,
                                                        defined):
        window = _edit_window(tmp_path, defined)
        self._click(window, (7_100 * KM, 1_100 * KM))
        assert window.borders.lobes == defined.lobes
        assert "beside the corridor" in window.status.text()

    def _drag(self, window, xy, dx_px):
        """Press at ``xy``, then drag ``dx_px`` screen pixels to the right."""
        from types import SimpleNamespace
        start = self._event(window, xy)
        end = SimpleNamespace(**{**vars(start), "x": start.x + dx_px})
        window._on_press(start)
        window._on_motion(end)
        window._on_release(end)

    @pytest.mark.parametrize("editing", [True, False])
    def test_a_drag_on_empty_map_pans(self, tmp_path, defined, editing):
        window = _edit_window(tmp_path, defined)
        window.btn_edit_lobes.setChecked(editing)
        x0, x1 = window.ax.get_xlim()
        per_px = (x1 - x0) / window.ax.get_window_extent().width
        self._drag(window, FREE_A, 60)
        assert window.ax.get_xlim() == pytest.approx(
            (x0 - 60 * per_px, x1 - 60 * per_px))
        assert window.borders.lobes == defined.lobes    # a pan, not a click

    def test_a_drag_on_a_connector_moves_it_not_the_map(self, tmp_path,
                                                        defined):
        window = _edit_window(tmp_path, defined)
        xlim = window.ax.get_xlim()
        self._drag(window, defined.lobes[0].centroid.coords[0], 60)
        assert window.ax.get_xlim() == xlim

    def test_nothing_happens_with_the_tool_off(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.btn_edit_lobes.setChecked(False)
        self._click(window, FREE_A)
        assert window.borders.lobes == defined.lobes


# -- the connector's shape ------------------------------------------------------
def _neck_mouth_reach_mm(lobe):
    """A lobe's neck, mouth and root-to-far length, in print-mm."""
    from math import dist
    c = lobe.exterior.coords
    root = ((c[0][0] + c[3][0]) / 2, (c[0][1] + c[3][1]) / 2)
    far = ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2)
    return (dist(c[0], c[3]) / MPM, dist(c[1], c[2]) / MPM,
            dist(root, far) / MPM)


class TestConnectorShape:

    def test_settings_reach_the_connector_params(self):
        p = U.BorderSettings(lobe_height_mm=0.8, lobe_depth_mm=4.0,
                             lobe_width_mm=1.2, lobe_ratio=2.5
                             ).connector_params()
        assert (p.dovetail_mm, p.reach_mm, p.neck_mm) == (0.8, 4.0, 1.2)
        assert p.mouth_mm == pytest.approx(3.0)

    def test_shipped_shape_is_the_lib_default(self):
        p = U.BorderSettings().connector_params()
        assert p.neck_mm == pytest.approx(1.6)
        assert p.mouth_mm == pytest.approx(3.2)
        assert p.dovetail_mm == pytest.approx(cfg("print.dovetail_mm"))

    def test_the_spins_feed_the_next_define(self, tmp_path):
        window = _window(tmp_path)
        window.lobe_width_spin.setValue(1.0)
        window.lobe_ratio_spin.setValue(3.0)
        window.lobe_depth_spin.setValue(5.0)
        window.lobe_height_spin.setValue(0.6)
        s = window.border_settings()
        assert (s.lobe_width_mm, s.lobe_ratio, s.lobe_depth_mm,
                s.lobe_height_mm) == (1.0, 3.0, 5.0, 0.6)

    def test_connectors_have_a_row_of_their_own(self, tmp_path):
        window = _window(tmp_path)
        rows = window.tabs.widget(1).layout()
        first, second, third = (rows.itemAt(i).layout() for i in range(3))

        def holds(row, widget):
            """Whether ``row`` holds ``widget``, inside one of its groups."""
            return any(row.itemAt(i).widget() is not None
                       and row.itemAt(i).widget().isAncestorOf(widget)
                       for i in range(row.count()))

        for widget in (window.lobe_height_spin, window.lobe_depth_spin,
                       window.lobe_width_spin, window.lobe_ratio_spin,
                       window.spacing_spin):
            assert holds(second, widget)
            assert not holds(first, widget)
        assert holds(first, window.btn_define)
        # Editing by hand sits with the connector it selects.
        assert holds(third, window.btn_edit_lobes)
        assert holds(third, window.sel_angle_spin)

    def test_reshaping_keeps_every_connector_where_it_stood(self, defined):
        wider = U.replace(defined.settings, lobe_width_mm=2.0,
                          lobe_ratio=1.5)
        lobes, dropped = U.reshape_connectors(defined, wider)
        assert dropped == 0 and len(lobes) == len(defined.lobes)
        for old, new in zip(defined.lobes, lobes):
            neck, mouth, _ = _neck_mouth_reach_mm(new)
            assert neck == pytest.approx(2.0)
            assert mouth == pytest.approx(3.0)
            assert U.lobe_pose(new, defined.border, wider.connector_params(
            ))[:2] == pytest.approx(U.lobe_pose(
                old, defined.border, defined.settings.connector_params())[:2])

    def test_a_shape_that_collides_is_kept_red(self, tmp_path, defined):
        """Grown into each other, the connectors stay where they stood and
        go red: the shape is undone or changed back, not lost."""
        huge = U.replace(defined.settings, lobe_width_mm=10.0, lobe_ratio=5.0)
        lobes, dropped = U.reshape_connectors(defined, huge)
        assert dropped == 0 and len(lobes) == len(defined.lobes)
        window = _edit_window(tmp_path, defined)
        window.lobe_width_spin.setValue(10.0)
        window.lobe_ratio_spin.setValue(5.0)
        assert len(window.borders.lobes) == len(defined.lobes)
        assert any(window.borders.faults())
        assert "invalid (red)" in window.status.text()

    def test_turning_a_spin_reshapes_the_map(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.lobe_depth_spin.setValue(4.0)
        assert window.borders.settings.lobe_depth_mm == 4.0
        for lobe in window.borders.lobes:
            *_, reach = _neck_mouth_reach_mm(lobe)
            assert reach == pytest.approx(4.0 + window.borders.settings
                                          .connector_params().overlap_mm)
        assert "reshaped" in window.status.text()
        assert defined.settings.lobe_depth_mm != 4.0     # shared: untouched

    def test_hand_edits_survive_a_reshape(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        window.add_connector(*FREE_A)
        window.lobe_width_spin.setValue(1.4)
        assert len(window.borders.lobes) == len(defined.lobes) + 1
        assert "edited by hand" in window.border_report.text()

    def test_a_reshape_is_one_undo_step(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        original = list(window.borders.lobes)
        window.lobe_ratio_spin.setValue(1.5)
        window.undo_connectors()
        assert window.borders.lobes == original
        assert window.lobe_ratio_spin.value() == pytest.approx(2.0)
        assert window.borders.settings.lobe_ratio == pytest.approx(2.0)
        window.redo_connectors()
        assert window.lobe_ratio_spin.value() == pytest.approx(1.5)
        assert window.borders.settings.lobe_ratio == pytest.approx(1.5)

    def test_height_changes_the_bar_not_the_outline(self, tmp_path, defined,
                                                    bar):
        window = _edit_window(tmp_path, defined)
        window.apply_bar(bar)
        before = list(window.borders.lobes)
        window.lobe_height_spin.setValue(0.6)
        assert window.bar is None
        assert all(a.equals(b) for a, b in zip(before, window.borders.lobes))
        assert window.borders.settings.connector_params().dovetail_mm == 0.6

    def test_with_no_borders_a_spin_only_waits_for_define(self, tmp_path):
        window = _window(tmp_path)
        window.lobe_width_spin.setValue(1.2)
        assert window.borders is None
        assert not window.btn_lobe_undo.isEnabled()


# -- turning and sizing one connector -----------------------------------------

def _select(window, index):
    window._on_press(TestConnectorMouse._event(
        window, window.borders.lobes[index].centroid.coords[0], button=3))


def _reach(window):
    return window.borders.settings.connector_params().reach_mm


def _key(window, key):
    from types import SimpleNamespace
    window._on_key(SimpleNamespace(key=key))


class TestSelectedConnector:

    def test_right_click_selects_and_shows_it(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        assert not window.sel_angle_spin.isEnabled()
        _select(window, 1)
        assert window._selected == 1
        assert window.sel_angle_spin.isEnabled()
        assert window.sel_angle_spin.value() == pytest.approx(0.0)
        assert window.sel_depth_spin.value() == pytest.approx(_reach(window))
        assert len(window.borders.lobes) == len(defined.lobes)   # not deleted

    def test_right_click_off_a_connector_deselects(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        window._on_press(TestConnectorMouse._event(window, FREE_A, button=3))
        assert window._selected is None
        assert not window.sel_depth_spin.isEnabled()

    def test_a_click_selects_and_delete_deletes(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        TestConnectorMouse()._click(window,
                                    defined.lobes[0].centroid.coords[0])
        assert len(window.borders.lobes) == len(defined.lobes)
        assert window._selected == 0
        window.btn_sel_delete.click()
        assert len(window.borders.lobes) == len(defined.lobes) - 1
        assert defined.lobes[0] not in window.borders.lobes
        assert window._selected is None
        assert not window.btn_sel_delete.isEnabled()

    def test_the_angle_box_turns_it_where_it_stands(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        window.sel_angle_spin.setValue(30.0)
        assert window.connector_pose(1)[2] == pytest.approx(30.0)
        assert window.connector_pose(1)[:2] == pytest.approx(
            U.lobe_pose(defined.lobes[1], defined.border,
                        defined.settings.connector_params())[:2])

    def test_the_depth_box_grows_it_in_shape(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        window.sel_depth_spin.setValue(_reach(window) * 1.5)
        assert window.connector_pose(1)[3] == pytest.approx(1.5)
        c = window.borders.lobes[1].exterior.coords
        neck = Polygon([c[0], c[3], c[0]]).length / 2 / MPM
        mouth = Polygon([c[1], c[2], c[1]]).length / 2 / MPM
        p = window.borders.settings.connector_params()
        assert neck == pytest.approx(p.neck_mm * 1.5)
        assert mouth / neck == pytest.approx(p.mouth_mm / p.neck_mm)

    def test_too_small_to_print_is_kept_red(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        assert window.pose_selected(0.0, 0.2, "x")
        assert window.borders.faults()[1] == "thin"
        assert U.STANDING_FAULTS["thin"] in window.status.text()
        assert window.sel_depth_spin.value() == pytest.approx(
            0.2 * _reach(window))

    def test_the_angle_is_held_to_45_degrees(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        window.pose_selected(80.0, 1.0, "x")
        assert abs(window.connector_pose(1)[2]) <= U.ANGLE_LIMIT_DEG + 1e-6

    def test_keys_turn_and_resize(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        _key(window, "]")
        _key(window, "}")
        assert window.connector_pose(1)[2] == pytest.approx(
            U.ANGLE_STEP_DEG + U.FINE_ANGLE_STEP_DEG)
        _key(window, "+")
        assert window.connector_pose(1)[3] * _reach(window) == pytest.approx(
            _reach(window) + U.DEPTH_STEP_MM)

    def test_the_delete_key_deletes_the_selected(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _key(window, "delete")                   # nothing selected: nothing
        assert len(window.borders.lobes) == len(defined.lobes)
        _select(window, 1)
        _key(window, "delete")
        assert len(window.borders.lobes) == len(defined.lobes) - 1
        assert defined.lobes[1] not in window.borders.lobes
        assert window._selected is None

    def test_a_held_key_is_one_undo_step(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        for _ in range(3):
            _key(window, "]")
        assert window.connector_pose(1)[2] == pytest.approx(
            3 * U.ANGLE_STEP_DEG)
        window.undo_connectors()
        assert window.borders.lobes[1].equals(defined.lobes[1])

    def test_dragging_the_handle_swings_it(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        x, y, _, _ = window.connector_pose(1)
        hx, hy = window._handle_at(1)
        a = np.radians(30.0)              # 30 degrees anticlockwise
        dx, dy = hx - x, hy - y
        to = (x + dx * np.cos(a) - dy * np.sin(a),
              y + dx * np.sin(a) + dy * np.cos(a))
        ev = TestConnectorMouse._event
        window._on_press(ev(window, (hx, hy)))
        window._on_motion(ev(window, to))
        assert window._drag_artist is not None
        window._on_release(ev(window, to))
        assert window.connector_pose(1)[2] == pytest.approx(30.0, abs=0.1)
        assert len(window.borders.lobes) == len(defined.lobes)

    def test_a_turn_survives_a_move_and_a_reshape(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        assert window.pose_selected(20.0, 1.25, "x")
        assert window.move_connector(1, *FREE_A)
        assert window.connector_pose(1)[2:] == pytest.approx((20.0, 1.25))
        window.lobe_width_spin.setValue(window.lobe_width_spin.value() + 0.2)
        assert window.connector_pose(1)[2:] == pytest.approx((20.0, 1.25))
        assert window._selected == 1

    def test_save_writes_the_turn_and_size(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        assert window.pose_selected(-15.0, 1.2, "x")
        window.write_borders(tmp_path / "out")
        frame = gpd.read_file(tmp_path / "out" / "connectors.geojson")
        assert frame["angle_deg"][1] == pytest.approx(-15.0, abs=1e-3)
        assert frame["scale"][1] == pytest.approx(1.2, abs=1e-4)
        assert frame["angle_deg"][0] == pytest.approx(0.0, abs=1e-3)

    def test_leaving_the_tool_lets_it_go(self, tmp_path, defined):
        window = _edit_window(tmp_path, defined)
        _select(window, 1)
        window.btn_edit_lobes.setChecked(False)
        assert window._selected is None
