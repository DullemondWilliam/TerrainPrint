"""
test_border_connectors.py

``lobe_at`` -- one connector put by hand -- against ``generate_lobes``, which
places them along the whole corridor: the same profile, on the side clicked.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_border_connectors.py -v
"""

import numpy as np
import pytest
from shapely.geometry import box

from lib.LIB_border_connectors import (ConnectorParams, build_border_mesh,
                                       generate_lobes, lobe_at)

P = ConnectorParams()
MPM = P.mpm
# A vertical corridor 2 mm wide with land 50 mm deep either side.
CORRIDOR = box(-1 * MPM, 0, 1 * MPM, 100 * MPM)
LAND = box(-50 * MPM, 0, 50 * MPM, 100 * MPM).difference(CORRIDOR)


def test_reproduces_a_generated_lobe_at_its_place():
    generated = generate_lobes(CORRIDOR, LAND, P)
    assert generated
    for lobe in generated:
        # Click just outside the corridor, level with the lobe's middle.
        side = 1 if lobe.centroid.x > 0 else -1
        placed = lobe_at(CORRIDOR, side * 2 * MPM, lobe.centroid.y, P)
        assert placed.symmetric_difference(lobe).area < 1e-6 * lobe.area


@pytest.mark.parametrize("side", [-1, 1])
def test_reaches_out_on_the_side_clicked(side):
    lobe = lobe_at(CORRIDOR, side * 3 * MPM, 50 * MPM, P)
    far = lobe.difference(CORRIDOR)
    assert far.area > 0
    assert (far.centroid.x > 0) == (side > 0)
    assert lobe.centroid.y == pytest.approx(50 * MPM, abs=1e-6 * MPM)


def test_no_corridor_no_lobe():
    assert lobe_at(None, 0.0, 0.0, P) is None


def _slope(xs_m, ys_m):
    """Ground rising 0.05 mm per print-mm along the corridor, from 1 mm."""
    return 1.0 + 0.05 * np.asarray(ys_m) / MPM


@pytest.mark.parametrize("lobed", [False, True])
def test_a_draped_top_follows_the_ground(lobed):
    lobes = generate_lobes(CORRIDOR, LAND, P) if lobed else []
    mesh = build_border_mesh(CORRIDOR, lobes, P, _slope)
    assert mesh.is_watertight and mesh.volume > 0
    # Origin at the corridor's (and lobes') bottom-left; the corridor starts
    # at y = 0 either way.
    v = mesh.vertices
    top = v[v[:, 2] > P.thickness_mm + 1e-6]
    assert len(top)
    assert top[:, 2] == pytest.approx(1.0 + 0.05 * top[:, 1], abs=1e-6)
    assert mesh.bounds[1][2] == pytest.approx(1.0 + 0.05 * 100)


def test_a_draped_top_never_drops_below_the_thickness():
    mesh = build_border_mesh(CORRIDOR, [], P, lambda xs, ys: np.zeros(len(xs)))
    assert mesh.bounds[1][2] == pytest.approx(P.thickness_mm)
    assert mesh.is_watertight
