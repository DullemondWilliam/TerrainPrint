"""Compare raster operations with an unmodified TerrainPrint checkout.

Run with the project's Python environment. Timings exclude fixture creation;
tracemalloc measures Python/NumPy allocations, not native GDAL memory or RSS.
"""

import argparse
import gc
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import LIB_geotiff_clip as clip
from lib import LIB_raster_reproject as warp


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def same_raster(before, after):
    np.testing.assert_array_equal(before.data, after.data)
    for field in ("width", "height", "transform", "valid_pixels", "total_pixels",
                  "nodata", "zmin", "zmax"):
        assert getattr(before, field) == getattr(after, field), field


def measure(label, before, after, check, repeats):
    check(before(), after())
    functions = {"before": before, "after": after}
    times = {name: [] for name in functions}
    for repeat in range(repeats):
        order = ("before", "after") if repeat % 2 == 0 else ("after", "before")
        for name in order:
            gc.collect()
            start = time.perf_counter()
            result = functions[name]()
            times[name].append(time.perf_counter() - start)
            del result
    peaks = {}
    for name, function in functions.items():
        gc.collect()
        tracemalloc.start()
        result = function()
        peaks[name] = tracemalloc.get_traced_memory()[1] / (1024 ** 2)
        tracemalloc.stop()
        del result
    return {"case": label,
            "median_seconds": {name: statistics.median(values)
                               for name, values in times.items()},
            "peak_traced_mib": peaks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    old_clip = load("baseline_clip", args.baseline_dir / "lib/LIB_geotiff_clip.py")
    old_warp = load("baseline_warp", args.baseline_dir / "lib/LIB_raster_reproject.py")
    results = []
    with tempfile.TemporaryDirectory(prefix="terrainprint-benchmark-") as tmp:
        path = Path(tmp) / "dem.tif"
        side = 4096
        transform = Affine.translation(1_000_000, 2_000_000) * Affine.scale(100, -100)
        data = np.broadcast_to(np.arange(side, dtype=np.float32), (side, side)).copy()
        data[100:300, 500:900] = -32767
        with rasterio.open(path, "w", driver="GTiff", width=side, height=side,
                           count=1, dtype="float32", crs="EPSG:3979", nodata=-32767,
                           transform=transform) as dataset:
            dataset.write(data, 1)
        del data
        geom = Polygon([transform * point for point in
                        ((0, 0), (side, 0), (side, side), (0, side // 2))])
        params = clip.ClipParams(band_bytes=1 << 20)
        results.append(measure(
            "clip 4096x4096, 1 MiB bands, retain array",
            lambda: old_clip.clip_geotiff(path, geom, params=params),
            lambda: clip.clip_geotiff(path, geom, params=params), same_raster, args.repeats))
        params = warp.ReprojectParams(dst_res_m=100, band_bytes=1 << 20)
        results.append(measure(
            "warp 4096x4096 in same CRS, 1 MiB bands, retain array",
            lambda: old_warp.reproject_geotiff(path, "EPSG:3979", params=params),
            lambda: warp.reproject_geotiff(path, "EPSG:3979", params=params),
            same_raster, args.repeats))
        with rasterio.open(path) as dataset:
            params = warp.ReprojectParams(dst_res_m=3200)

            def grids(module):
                return [module.output_grid(dataset, "EPSG:3347", params)
                        for _ in range(40)]

            def same_grids(before, after):
                assert [vars(grid) for grid in before] == [vars(grid) for grid in after]

            results.append(measure(
                "40 explicit-resolution grid estimates",
                lambda: grids(old_warp), lambda: grids(warp), same_grids, args.repeats))

    for path in sorted((ROOT / "Data/JSon-Provinces-Orig").glob("CAN_*.geojson")):
        frame = gpd.read_file(path).to_crs("EPSG:3347")
        np.testing.assert_array_equal(old_warp.bounds_for(frame, "EPSG:3347"),
                                      warp.bounds_for(frame, "EPSG:3347"))
        if path.stem == "CAN_Nunavut":
            results.append(measure(
                "Nunavut bounds, preloaded in print CRS",
                lambda: old_warp.bounds_for(frame, "EPSG:3347"),
                lambda: warp.bounds_for(frame, "EPSG:3347"),
                np.testing.assert_array_equal, args.repeats))
    print(json.dumps({"python": sys.version, "numpy": np.__version__,
                      "rasterio": rasterio.__version__, "repeats": args.repeats,
                      "equality": "Raster pixels and metadata, grids, all 13 province bounds",
                      "measurements": results}, indent=2))


if __name__ == "__main__":
    main()
