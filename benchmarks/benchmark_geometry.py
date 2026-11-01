"""Compare geometry performance and outputs with a baseline checkout.

Run from the optimized checkout with its Python environment::

    python benchmarks/benchmark_geometry.py --baseline-dir ../TerrainPrint-baseline

Timing results are medians of three runs, after one output-equivalence check.
The optional real Nunavut merge can take several minutes in the baseline.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from affine import Affine
from pyproj import Transformer
from shapely.geometry import Polygon, box, shape
from shapely.ops import transform

from lib import LIB_island_merging as new_merge
from lib import LIB_stress_points as new_stress


def load_baseline(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def measure(label, before, after, check, repeats):
    check(before(), after())
    timings = {"before": [], "after": []}
    for _ in range(repeats):
        for name, function in (("before", before), ("after", after)):
            start = time.perf_counter()
            result = function()
            timings[name].append(time.perf_counter() - start)
            del result
    medians = {name: statistics.median(values) for name, values in timings.items()}
    print(json.dumps({"case": label, "seconds": medians, "repeats": repeats,
                      "speedup": medians["before"] / medians["after"],
                      "outputs_equal": True}), flush=True)


def same_geometry(a, b):
    assert a.equals_exact(b, 0), "geometry changed"


def same_field(a, b):
    np.testing.assert_array_equal(a[0], b[0])
    assert a[1] == b[1], "transform changed"


def same_summary(a, b):
    assert a.lineages == b.lineages
    for field in ("profile", "reps", "border", "border_lineage"):
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    assert a.edges == b.edges
    assert a.lobe_edges == b.lobe_edges


def same_report(a, b):
    assert a.measured and b.measured
    assert len(a.necks) == len(b.necks)
    for na, nb in zip(a.necks, b.necks):
        for key in na.__dataclass_fields__:
            va, vb = getattr(na, key), getattr(nb, key)
            if hasattr(va, "equals_exact"):
                assert va.equals_exact(vb, 0), key
            else:
                assert va == vb, (key, va, vb)
    np.testing.assert_array_equal(a.grid.land, b.grid.land)
    np.testing.assert_array_equal(a.grid.level, b.grid.level)
    assert a.grid.tiles.keys() == b.grid.tiles.keys()
    for key in a.grid.tiles:
        same_summary(a.grid.tiles[key], b.grid.tiles[key])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--include-nunavut-merge", action="store_true",
                        help="Also benchmark the slow real-coastline merge")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    baseline = args.baseline_dir.resolve() / "lib"
    old_merge = load_baseline("baseline_merging", baseline / "LIB_island_merging.py")
    old_stress = load_baseline("baseline_stress", baseline / "LIB_stress_points.py")

    for count in (20, 200):
        islands = [box(x + offset, 0, x + offset + 1000, 1000)
                   for x in range(0, count * 10000, 10000)
                   for offset in (0, 1400)]
        measure(f"merge {2 * count} islands",
                lambda: old_merge.merge_islands(islands, 0.3),
                lambda: new_merge.merge_islands(islands, 0.3), same_geometry, args.repeats)

    grid = new_stress.StressGrid(Affine(1, 0, 0, 0, -1, 3330), (3330, 3200),
                                None, None, 0.1, 3, 0.025, 128)
    regions = [box(i * 80, i * 80, i * 80 + 160, i * 80 + 160) for i in range(20)]
    report = new_stress.StressReport(
        necks=[new_stress.Neck(None, None, region=region, severity=(i + 1) / 7)
               for i, region in enumerate(regions)], grid=grid)
    measure("severity field: 10.66M pixels, 20 overlapping regions",
            lambda: old_stress.severity_field(report),
            lambda: new_stress.severity_field(report), same_field, args.repeats)
    for name, module in (("before", old_stress), ("after", new_stress)):
        tracemalloc.start()
        module.severity_field(report)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(json.dumps({"case": "severity traced Python/NumPy peak allocation",
                          "version": name, "peak_MiB": peak / 1024 ** 2}), flush=True)

    report.necks += [new_stress.Neck(None, None, region=box(-5, -5, 8, 8),
                                   severity=float("nan")),
                     new_stress.Neck(None, None, region=box(5, 5, 10, 10),
                                   severity=float("inf")),
                     new_stress.Neck(None, None, region=Polygon(
                         box(0, 0, 500, 500).exterior.coords,
                         [box(100, 100, 400, 400).exterior.coords]), severity=-10)]
    same_field(old_stress.severity_field(report), new_stress.severity_field(report))
    report.necks.reverse()
    same_field(old_stress.severity_field(report), new_stress.severity_field(report))

    rng = np.random.default_rng(20261031)
    for _ in range(100):
        land = rng.random((20, 30)) > 0.2
        level = rng.choice([0, 1, 8, 11, 17, 22, 29, 30], (20, 30)).astype(np.uint16)
        level[~land] = 0
        lobes, _ = old_stress.ndimage.label(land & (level >= 30))
        crop = (slice(3, 17), slice(4, 22))
        inputs = (land, level, lobes, crop, land.shape, 30, 0.1)
        same_summary(old_stress._summarise_tile(*inputs),
                     new_stress._summarise_tile(*inputs))
    print(json.dumps({"case": "overlay edge cases and 100 random sparse-level tiles",
                      "outputs_equal": True}), flush=True)

    project = Transformer.from_crs("EPSG:4326", "EPSG:3347", always_xy=True).transform
    for province in ("Nova Scotia", "Nunavut"):
        path = ROOT / "Data" / "JSon-Provinces-Orig" / f"CAN_{province}.geojson"
        data = json.loads(path.read_text(encoding="utf-8"))
        geom = transform(project, shape(data["features"][0]["geometry"]))
        measure(f"stress: real {province}", lambda: old_stress.stress_points(geom),
                lambda: new_stress.stress_points(geom), same_report, args.repeats)
        if args.include_nunavut_merge and province == "Nunavut":
            islands = list(geom.geoms)
            measure(f"merge: real {province}, {len(islands)} islands, 2 km",
                    lambda: old_merge.merge_islands(islands, 2),
                    lambda: new_merge.merge_islands(islands, 2),
                    same_geometry, args.repeats)


if __name__ == "__main__":
    main()
