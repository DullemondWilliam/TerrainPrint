#!/usr/bin/env python3
"""
LIB_print_all.py

Every printed piece of a map in one run: each province's solid, pocketed for
its connectors, and the border bar in its planned pieces -- written to one
folder, ready for the slicer.

``print_all`` does for every province what the terrain editor's STL tab does
for one -- plan the crop (``LIB_raster_plan``), warp and clip it, mesh it and
carve its pockets (``build_terrain``) -- and then builds the bar
(``LIB_border_bar``), draped over the same DEM so it stands as tall as the
solids either side.  A province that fails is reported and the rest go on.

Files written, into ``out_dir``:

* ``<province>.stl`` + ``<province>.frame.json`` per province -- the frame
  sidecar each solid carries, so a later tool can recover its metres frame;
* ``border_<province>_01.stl`` ... -- the bar's pieces, each named for the
  province with the largest share of its edge and numbered within it;
  removed pieces, and pieces with no connector, left out.

Pure (no Qt): the editor runs it off the UI thread, and tests call it.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling

from lib.LIB_border_bar import BORDER_STEM, build_bar, build_draped_bar
from lib.LIB_border_connectors import ConnectorParams, notch_province
from lib.LIB_border_plan import EPSG_WORKING, _safe_name
from lib.LIB_geotiff_clip import clip_geotiff
from lib.LIB_project_config import shipped as _shipped
from lib.LIB_raster_plan import finest_res, needs_warp, plan_run
from lib.LIB_raster_reproject import ReprojectParams, reproject_geotiff
from lib.LIB_terrain_stl import (TerrainParams, _finish, _validate, _write,
                                 terrain_to_stl)

def prepare_dem(raster, dst_crs, boundary, warp, clip, warp_path, crop_path,
                keep_array: bool = False):
    """
    The crop a solid is built from: ``raster`` warped to ``dst_crs`` (into
    ``warp_path``) when ``warp`` is given, then clipped to ``boundary`` (into
    ``crop_path``).  Returns the ``ClipResult``.

    ``warp`` and ``clip`` are ``LIB_raster_plan.plan_run``'s.  ``boundary``
    is a geometry already in the CRS the clip reads -- ``dst_crs`` when
    warping, the raster's own when not -- so neither lib re-projects it.
    The editor's STL tab and ``print_all`` both crop through this, so a
    preview and a full run cut the same raster.
    """
    source = str(raster)
    if warp is not None:
        reproject_geotiff(source, dst_crs, str(warp_path), warp)
        source = str(warp_path)
    return clip_geotiff(source, boundary, str(crop_path), clip,
                        keep_array=keep_array)


def build_terrain(raster, boundary, out_path, params, lobes=None,
                  connector=None, config=None):
    """
    ``terrain_to_stl``, then the dovetail pockets when ``lobes`` are given.

    The solid's ``origin_m`` *is* its metres -> print-mm frame, so the pockets
    go straight in with ``notch_province`` -- no reconciliation.  That only
    holds when the solid was built in the CRS the lobes were (EPSG:3347).

    ``config`` is the merged configuration the piece is built under
    (``LIB_project_config.load_files``), which its sidecar fingerprints; a
    window passes this desk's.  Without one, and without ``connector``, the
    shipped settings.
    """
    if not lobes:
        return terrain_to_stl(raster, boundary, out_path, params, config)
    result = terrain_to_stl(raster, boundary, None, params, config)
    if CRS.from_user_input(result.crs) != CRS.from_user_input(EPSG_WORKING):
        raise ValueError(
            f"Pockets need the terrain built in {EPSG_WORKING}, where the "
            f"connectors are; this crop is in {result.crs}. Set CRS to "
            f"{EPSG_WORKING} and re-run the crop.")
    connector = connector or ConnectorParams(scale=params.scale)
    if connector.pocket_depth_mm >= params.base_height_mm:
        raise ValueError(
            f"The pockets are {connector.pocket_depth_mm:g} mm deep (Height "
            f"plus Height clear.), which cuts through the "
            f"{params.base_height_mm:g} mm Base. Raise the Base or make the "
            "connectors shallower.")
    mesh = notch_province(result.mesh, lobes, *result.origin_m, connector)
    _finish(mesh, result, params)
    _validate(result, raster)          # FR-4.7 again: the boolean can break it
    if out_path is not None:
        _write(mesh, result, out_path)
    return result


@dataclass
class ProvincePrint:
    """What became of one province."""
    name: str
    path: Path = None              #: the STL written, or None if it failed
    res_m: float = 0.0             #: the resolution it was cropped at (0: native)
    triangles: int = 0
    pockets: int = 0
    bed_fits: bool = False
    error: str = ""


@dataclass
class PrintAllReport:
    """What ``print_all`` wrote, and what it could not."""
    provinces: list = field(default_factory=list)   #: ProvincePrint each
    bar_paths: list = field(default_factory=list)   #: border_*.stl written
    bar_too_big: int = 0                            #: pieces over the bed
    bar_res_m: float = 0.0                          #: its top's pitch, draped
    bar_error: str = ""
    cancelled: bool = False
    seconds: float = 0.0

    @property
    def failed(self) -> list:
        return [p for p in self.provinces if p.error]

    @property
    def written(self) -> list:
        return [p.path for p in self.provinces if p.path is not None] \
            + list(self.bar_paths)


def outputs(out_dir, names) -> list:
    """The files ``print_all`` would write over in ``out_dir``: each
    province's STL and frame, and every ``border_*.stl`` there."""
    out_dir = Path(out_dir)
    found = []
    for name in names:
        for suffix in (".stl", ".frame.json"):
            path = out_dir / f"{_safe_name(name)}{suffix}"
            if path.exists():
                found.append(path)
    found += sorted(out_dir.glob(f"{BORDER_STEM}_*.stl"))
    return found


def print_all(raster_path, out_dir, provinces: dict, work_dir, *,
              definition=None, dst_crs=EPSG_WORKING, res_m=None,
              crop: bool = True, all_touched: bool = False,
              resampling: str = "max", cache_mb: int = 0,
              terrain: TerrainParams = None, cut: bool = True,
              pockets: bool = True, bar: bool = True, bar_res_m=None,
              config=None, progress=None,
              cancelled=None) -> PrintAllReport:
    """
    Write every province's solid and the border bar's pieces to ``out_dir``.

    ``provinces`` maps name -> geometry in the working CRS: what each solid
    is cut to (the trimmed province, once borders are defined).
    ``definition`` -- a ``BorderDefinition`` -- supplies the connectors each
    province is pocketed for and the bar; without one the solids are built
    plain and no bar is made.

    ``res_m`` is the crop's metres per pixel for every province (0 for the
    DEM's own), or None for each province's own finest under
    ``terrain.max_triangles`` (``LIB_raster_plan.finest_res``).
    ``bar_res_m`` is the bar's own -- the pitch its draped top is read at
    (0 for the DEM's own) -- or None for the shipped ``print.border_res_m``.
    ``config`` is the merged configuration each solid's sidecar records
    (see :func:`build_terrain`).  The other
    settings are the editor's STL tab's.  ``progress(step, total, text)`` is
    told before each step; ``cancelled()`` is asked between them, and a run
    stopped early reports what it had written.
    """
    started = time.perf_counter()
    out_dir, work_dir = Path(out_dir), Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    terrain = terrain or TerrainParams()
    dst = CRS.from_user_input(dst_crs)
    report = PrintAllReport()
    names = list(provinces)
    make_bar = bool(bar and definition is not None
                    and definition.border is not None)
    total = len(names) + (1 if make_bar else 0)
    tell = progress or (lambda step, total, text: None)
    stop = cancelled or (lambda: False)

    with rasterio.open(str(raster_path)) as ds:
        raster_crs, native = ds.crs, float(ds.res[0])
    frame = dst if needs_warp(raster_crs, dst) else raster_crs
    used = []

    for step, name in enumerate(names):
        if stop():
            report.cancelled = True
            break
        tell(step, total, f"{name}: cropping and meshing")
        done = ProvincePrint(name=name)
        report.provinces.append(done)
        geom = provinces[name]

        def boundary(crs, geom=geom):
            if crs is None or CRS.from_user_input(crs) == \
                    CRS.from_user_input(EPSG_WORKING):
                return geom
            return gpd.GeoSeries([geom], crs=EPSG_WORKING).to_crs(
                crs).iloc[0]

        try:
            res = res_m if res_m is not None else finest_res(
                raster_path, raster_crs, native, dst, boundary, crop,
                terrain.max_triangles)
            done.res_m = res
            warp, clip = plan_run(raster_crs, native, dst, res, boundary,
                                  crop=crop, all_touched=all_touched,
                                  resampling=resampling, cache_mb=cache_mb)
            stem = _safe_name(name)
            crop_path = work_dir / f"{stem}_crop.tif"
            prepare_dem(raster_path, dst, boundary(frame), warp, clip,
                        work_dir / f"{stem}_warp.tif", crop_path)
            lobes = []
            if definition is not None and pockets \
                    and definition.settings.connectors:
                lobes = definition.province_lobes(name)
            solid = build_terrain(
                str(crop_path), boundary(frame) if cut else None,
                str(out_dir / f"{stem}.stl"), terrain, lobes,
                definition.settings.connector_params() if lobes else None,
                config)
            done.path = out_dir / f"{stem}.stl"
            done.triangles = solid.triangles
            done.pockets = len(lobes)
            done.bed_fits = solid.bed_fits
            used.append(res)
        except Exception as exc:          # one province's failure is reported
            done.error = str(exc) or type(exc).__name__

    if make_bar and not report.cancelled and not stop():
        tell(len(names), total, "the border bar")
        try:
            if used:
                # Its own resolution: the bar's surface is read once over
                # the whole corridor, where a province's could be a
                # whole-country raster.
                bar_res = (_shipped("print.border_res_m") if bar_res_m is None
                           else bar_res_m) or native
                report.bar_res_m = bar_res
                result = build_draped_bar(
                    definition, str(raster_path),
                    ReprojectParams(dst_res_m=bar_res,
                                    resampling=Resampling[resampling],
                                    gdal_cache_mb=cache_mb),
                    terrain, str(work_dir / "border_surface.tif"))
            else:
                result = build_bar(definition)
            report.bar_too_big = result.too_big
            for piece, name in zip(result.pieces, result.file_names()):
                path = out_dir / name
                piece.export(str(path))
                report.bar_paths.append(path)
            # A run with fewer pieces than the last must not leave its extra
            # pieces lying in the folder to be printed by mistake.
            for stale in sorted(out_dir.glob(f"{BORDER_STEM}_*.stl")):
                if stale not in report.bar_paths:
                    stale.unlink()
        except Exception as exc:
            report.bar_error = str(exc) or type(exc).__name__
    elif report.cancelled or stop():
        report.cancelled = True

    tell(total, total, "done")
    report.seconds = time.perf_counter() - started
    return report


__all__ = ["BORDER_STEM", "PrintAllReport", "ProvincePrint", "build_terrain",
           "outputs", "print_all"]
