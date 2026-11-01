"""Reproduce the documented, pre-existing sampler/mesh interpolation mismatch."""

import json
from pathlib import Path
import sys

import numpy as np
from affine import Affine
from rasterio.io import MemoryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.LIB_terrain_stl import TerrainParams, surface_sampler, terrain_to_stl


def main():
    params = TerrainParams(scale=8_000_000, z_exaggeration=10, base_height_mm=2,
                           simplify_mm=0)
    data = np.array([[0, 0], [0, 8000]], dtype=np.float32)
    transform = Affine.translation(7_000_000, 2_000_000) * Affine.scale(8000, -8000)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=2, height=2, count=1,
                         dtype="float32", crs="EPSG:3347", transform=transform) as raster:
            raster.write(data, 1)
        result = terrain_to_stl(memory.name, params=params)
        sample = surface_sampler(memory.name, params=params)
        x, y = transform * (1, 1)
        # The two top triangles share this diagonal; its midpoint height is
        # the average of its two mesh vertices.
        assert set(result.mesh.faces[0]) & set(result.mesh.faces[1]) == {1, 2}
        mesh_z = float(result.mesh.vertices[[1, 2], 2].mean())
        sampled_z = float(sample([x], [y])[0])
        print(json.dumps({"mesh_midpoint_mm": mesh_z, "sampler_midpoint_mm": sampled_z,
                          "gap_mm": sampled_z - mesh_z}, indent=2))


if __name__ == "__main__":
    main()
