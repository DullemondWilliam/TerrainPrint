"""Compare slab construction with an unmodified TerrainPrint checkout.

    python benchmarks/benchmark_mesh.py --baseline-dir ../TerrainPrint-baseline

Only the four slab-construction functions are loaded from each source using
the AST, so both versions use the same NumPy/trimesh environment without
importing either checkout's other libraries. This measures mesh construction,
not raster reads, coastline booleans, simplification, validation, or STL writes.
Memory is peak tracemalloc allocation during construction, excluding input
elevations and package imports; it is not whole-process peak working set.
"""

import argparse
import ast
import gc
import json
import logging
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
import tracemalloc
from types import SimpleNamespace

import numpy as np
import trimesh
from affine import Affine

ROOT = Path(__file__).resolve().parents[1]


def load_builder(root):
    path = root / "lib" / "LIB_terrain_stl.py"
    names = {"_build_slab", "_faces", "_surface_mm", "_triangle_count"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in tree.body} != names:
        raise ValueError(f"{path} does not contain the expected slab functions.")
    namespace = {"np": np, "trimesh": trimesh, "log": logging.getLogger(__name__)}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["_build_slab"]


def benchmark(baseline_dir, repeats):
    old, new = load_builder(baseline_dir), load_builder(ROOT)
    params = SimpleNamespace(mpm=8000.0, floor_m=0.0, base_height_mm=2.0,
                             z_exaggeration=10.0, clamp_below_floor=True)
    transform = Affine.translation(7_000_000, 2_000_000) * Affine.scale(8000, -8000)
    rng = np.random.default_rng(2718)

    for shape in [(2, 2), (2, 17), (19, 2), (13, 23), (61, 41)]:
        elev = rng.uniform(-100.0, 2000.0, shape)
        elev[0, 0] = np.nan
        elev[-1, -1] = -32767.0
        old_result, new_result = SimpleNamespace(), SimpleNamespace()
        before = old(elev, transform, -32767.0, params, old_result)
        after = new(elev, transform, -32767.0, params, new_result)
        np.testing.assert_array_equal(before.vertices, after.vertices)
        np.testing.assert_array_equal(before.faces, after.faces)
        if vars(old_result) != vars(new_result):
            raise AssertionError(f"Slab metadata differs on grid {shape}.")

    measurements = []
    for rows, cols in [(256, 256), (512, 768), (866, 866)]:
        elev = rng.uniform(-100.0, 2000.0, (rows, cols))
        times = {"before": [], "after": []}
        functions = {"before": old, "after": new}
        for func in functions.values():
            func(elev, transform, -32767.0, params, SimpleNamespace())
        for repeat in range(repeats):
            order = ("before", "after") if repeat % 2 == 0 else ("after", "before")
            for name in order:
                gc.collect()
                start = perf_counter()
                mesh = functions[name](elev, transform, -32767.0, params, SimpleNamespace())
                times[name].append(perf_counter() - start)
                del mesh
        peaks = {}
        for name, func in functions.items():
            gc.collect()
            tracemalloc.start()
            try:
                mesh = func(elev, transform, -32767.0, params, SimpleNamespace())
                peaks[name] = tracemalloc.get_traced_memory()[1] / (1024 ** 2)
            finally:
                tracemalloc.stop()
            del mesh
        measurements.append({"rows": rows, "cols": cols,
                             "triangles": 4 * (rows * cols - 1),
                             "median_seconds": {name: median(values)
                                                for name, values in times.items()},
                             "samples_seconds": times,
                             "peak_traced_mib": peaks})

    return {"scope": "Slab construction only; excludes input allocation and imports",
            "baseline_dir": str(baseline_dir), "current_dir": str(ROOT),
            "repeats": repeats,
            "equality": "Exact vertices, faces and metadata on five grids",
            "python": sys.version, "numpy": np.__version__,
            "trimesh": trimesh.__version__, "measurements": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path,
                        help="Path to an unmodified TerrainPrint checkout")
    parser.add_argument("--repeats", type=int, default=7,
                        help="Timing samples per implementation and grid (default: 7)")
    parser.add_argument("--output", type=Path,
                        help="Also save the JSON results at this path")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    baseline_dir = args.baseline_dir.resolve()
    if not (baseline_dir / "lib" / "LIB_terrain_stl.py").is_file():
        parser.error("--baseline-dir must contain lib/LIB_terrain_stl.py")
    report = json.dumps(benchmark(baseline_dir, args.repeats), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
