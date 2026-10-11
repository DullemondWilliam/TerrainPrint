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
                                       generate_lobes, lobe_at, lobe_pose)

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


# -- a connector's own turn and size ------------------------------------------

def _centres(lobe):
    c = lobe.exterior.coords
    root = ((c[0][0] + c[3][0]) / 2, (c[0][1] + c[3][1]) / 2)
    far = ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2)
    return np.array(root), np.array(far)


def test_unturned_and_unscaled_is_the_plain_lobe():
    assert lobe_at(CORRIDOR, 3 * MPM, 50 * MPM, P, 0.0, 1.0).equals(
        lobe_at(CORRIDOR, 3 * MPM, 50 * MPM, P))


@pytest.mark.parametrize("angle", [-45.0, -20.0, 15.0, 45.0])
def test_turning_pivots_on_the_corridor_edge(angle):
    lobe = lobe_at(CORRIDOR, 3 * MPM, 50 * MPM, P, angle)
    x, y, got, scale = lobe_pose(lobe, CORRIDOR, P)
    assert (x, y) == pytest.approx((1 * MPM, 50 * MPM))
    assert got == pytest.approx(angle)
    assert scale == pytest.approx(1.0)
    root, far = _centres(lobe)
    direction = np.degrees(np.arctan2(*(far - root)[::-1]))
    assert direction == pytest.approx(angle)      # the normal there is +x


@pytest.mark.parametrize("scale", [0.6, 1.5, 2.0])
def test_scaling_keeps_the_shape(scale):
    plain = lobe_at(CORRIDOR, 3 * MPM, 50 * MPM, P)
    grown = lobe_at(CORRIDOR, 3 * MPM, 50 * MPM, P, 0.0, scale)
    c = grown.exterior.coords
    neck = np.hypot(c[0][0] - c[3][0], c[0][1] - c[3][1]) / MPM
    mouth = np.hypot(c[1][0] - c[2][0], c[1][1] - c[2][1]) / MPM
    assert neck == pytest.approx(P.neck_mm * scale)
    assert mouth / neck == pytest.approx(P.mouth_mm / P.neck_mm)
    # Reach grows; the root still sinks the same overlap into the corridor.
    assert grown.bounds[2] / MPM == pytest.approx(1 + P.reach_mm * scale)
    assert grown.bounds[0] == pytest.approx(plain.bounds[0])
    assert lobe_pose(grown, CORRIDOR, P)[3] == pytest.approx(scale)



def _spiked_pair():
    """Two squares, one with a spike: invalid, and make_valid hands it back
    as a GeometryCollection holding a MultiPolygon *and* the spike's line --
    the shape a reprojected Save Borders corridor came back as."""
    from shapely.geometry import MultiPolygon, Polygon
    spiked = Polygon([(0, 0), (10, 0), (10, 10), (5, 10), (5, 15), (5, 10),
                      (0, 10)])
    other = Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])
    return MultiPolygon([spiked, other])


def test_polys_reaches_into_a_collection():
    from shapely.validation import make_valid
    from lib.LIB_border_connectors import _polys
    repaired = make_valid(_spiked_pair())
    assert repaired.geom_type == "GeometryCollection"
    assert "MultiPolygon" in [g.geom_type for g in repaired.geoms]
    polys = _polys(repaired)
    assert len(polys) == 2
    assert sum(p.area for p in polys) == 200.0


def test_repair_keeps_the_shape():
    """It used to come back None: every polygon was inside the collection's
    MultiPolygon, which _polys never looked into."""
    from lib.LIB_border_connectors import _repair, _safe_union
    assert _repair(_spiked_pair()).area == 200.0
    assert _safe_union([_spiked_pair()]).area == 200.0


def test_the_stl_lib_reaches_in_too():
    from shapely.validation import make_valid
    from lib.LIB_border_stl import _polys
    assert len(_polys(make_valid(_spiked_pair()))) == 2
