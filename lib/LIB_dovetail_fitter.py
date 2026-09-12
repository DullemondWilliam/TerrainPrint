#!/usr/bin/env python3
"""
LIB_dovetail_fitter.py

Key already-printed province STLs to the border with dovetail connectors.

``LIB_border_connectors`` knows how to make dovetail lobes and carve their
matching pockets, but only for a province mesh built *in memory* whose local
origin is already known.  In practice the province pieces are produced by a
separate GeoJSON→STL step and land on disk in an unknown local print-mm frame
(and sometimes with a flipped Y axis).  This module bridges that gap: it loads
each province STL, **reconciles** its frame against the province GeoJSON, then
carves dovetail pockets in exactly the right place — while building the matching
tabbed border piece from the *same* lobe set so the two halves mate.

    from lib.LIB_dovetail_fitter import fit_dovetails
    result = fit_dovetails(
        "borders/…/borders.geojson",
        [("Prince Edward Island",
          "borders/…/name_Prince_Edward_Island.geojson",
          "print/pei.stl")],
        out_dir="keyed")

Reconciliation
--------------
The map scale is known (``ConnectorParams.scale``), so the metres→print-mm
mapping is fixed up to a translation and an optional Y-flip.  For each province
the flat base footprint is lifted off the STL and matched against the scaled
GeoJSON footprint; the best-scoring ``{translation, flip}`` (by area IoU) wins,
and anything below ``min_fit_score`` is rejected with a clear error rather than
silently mis-placing pockets.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import trimesh
from shapely.affinity import affine_transform

from lib.LIB_border_connectors import (ConnectorParams, EPSG_METRIC,
                                       build_border_mesh, extrude_solid,
                                       generate_lobes, _load_union_m,
                                       _safe_union)
from lib.LIB_stl_reconcile import (ReconcileReport, reconcile,
                                   base_footprint as _base_footprint)

log = logging.getLogger(__name__)

#: A pocket cutter dips this far below the base so the boolean sees a clean
#: intersecting solid rather than coplanar touching faces.
_UNDERCUT_MM = 0.1


@dataclass
class FitResult:
    """Result of :func:`fit_dovetails`."""
    provinces: dict = field(default_factory=dict)   #: name -> notched Trimesh
    border: object = None                           #: tabbed border Trimesh (or None)
    reports: list = field(default_factory=list)     #: ReconcileReport per province


def fit_dovetails(border_geojson, province_specs, params=None,
                  out_dir=None, min_fit_score=0.90):
    """
    Carve dovetail pockets into province STLs and build the matching border tabs.

    Parameters
    ----------
    border_geojson : str | pathlib.Path
        The carved border region (defines the corridor the lobes sit along).
    province_specs : iterable of (name, geojson, stl)
        For each printed province: a label, the path to its (trimmed) GeoJSON,
        and its STL (a path or an in-memory ``trimesh.Trimesh``).
    params : ConnectorParams, optional
        Dovetail geometry and — crucially — the map ``scale`` the STLs were
        printed at.  Defaults to ``ConnectorParams()``.
    out_dir : str | pathlib.Path, optional
        If given, writes ``<name>_keyed.stl`` per province and
        ``border_connectors.stl`` for the tabbed border.
    min_fit_score : float
        Reject a province whose best footprint overlap is below this.

    Returns
    -------
    FitResult
        The notched province meshes, the tabbed border mesh, and a
        :class:`ReconcileReport` per province.

    Raises
    ------
    ValueError
        If the border is empty, a province cannot be reconciled, or any output
        mesh fails the watertight / positive-volume gate.
    """
    params = params or ConnectorParams()
    specs = list(province_specs)
    if not specs:
        raise ValueError("province_specs is empty; nothing to key.")

    border_geom_m = _load_union_m(border_geojson)
    if border_geom_m is None or border_geom_m.is_empty:
        raise ValueError(f"{border_geojson}: no border geometry to key against.")

    # Load every province footprint (metres) and generate ONE shared lobe set,
    # so the pockets and the border tabs are cut from the exact same profile.
    prov_geoms = {}
    for name, geojson, _stl in specs:
        geom = _load_union_m(geojson)
        if geom is None or geom.is_empty:
            raise ValueError(f"{name}: province GeoJSON {geojson} is empty.")
        prov_geoms[name] = geom
    prov_union_m = _safe_union(list(prov_geoms.values()))
    lobes_m = generate_lobes(border_geom_m, prov_union_m, params)

    result = FitResult()
    for name, _geojson, stl in specs:
        mesh = _load_mesh(stl)
        report, matrix = reconcile(name, mesh, prov_geoms[name], params,
                                   min_fit_score)
        notched, n = _notch(mesh, lobes_m, matrix, report.base_z_mm, params)
        _validate(notched, name)
        report.n_pockets = n
        result.provinces[name] = notched
        result.reports.append(report)

    result.border = build_border_mesh(border_geom_m, lobes_m, params)
    if result.border is not None:
        _validate(result.border, "border")

    if out_dir is not None:
        _export(result, out_dir)

    log.info("Keyed %d province(s) to the border (%d lobes).",
             len(result.provinces), len(lobes_m))
    return result


# ---------------------------------------------------------------------------
# Pocket carving
# ---------------------------------------------------------------------------
def _notch(mesh, lobes_m, matrix, base_z, params):
    """Carve dovetail pockets into the underside of one province STL."""
    mpm = params.mpm
    z0 = base_z - _UNDERCUT_MM
    z1 = base_z + params.dovetail_mm
    cutters = []
    for lobe in lobes_m:
        # Oversize the pocket by the assembly clearance, then map the lobe from
        # metres into this STL's recovered print-mm frame and extrude it.
        lobe_c = lobe.buffer(params.clearance_mm * mpm)
        poly_mm = affine_transform(lobe_c, matrix)
        cutter = extrude_solid(poly_mm, z0, z1)
        if cutter is not None:
            cutters.append(cutter)
    if not cutters:
        return mesh, 0
    tool = cutters[0] if len(cutters) == 1 else trimesh.boolean.union(cutters)
    return trimesh.boolean.difference([mesh, tool]), len(cutters)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_mesh(stl):
    """Return a Trimesh from a Trimesh or a path to an STL."""
    if isinstance(stl, trimesh.Trimesh):
        return stl
    loaded = trimesh.load(str(stl), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"{stl} did not load as a single mesh.")
    return loaded


def _validate(mesh, name):
    """Reject an empty, non-watertight, or negative-volume result."""
    if mesh is None or mesh.is_empty:
        raise ValueError(f"{name}: notching produced an empty mesh.")
    if not mesh.is_watertight or mesh.volume <= 0.0:
        raise ValueError(
            f"{name}: result failed the watertight / positive-volume gate "
            f"(watertight={mesh.is_watertight}, volume={mesh.volume:.4f}).")


def _safe_name(name):
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "province"


def _export(result, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, mesh in result.provinces.items():
        mesh.export(str(out_dir / f"{_safe_name(name)}_keyed.stl"))
    if result.border is not None:
        result.border.export(str(out_dir / "border_connectors.stl"))


def fit_dovetails_from_dir(border_dir, stl_dir, params=None, out_dir=None,
                           stl_pattern="{name}.stl", min_fit_score=0.90):
    """
    Convenience: pair ``<border_dir>/name_*.geojson`` with STLs in ``stl_dir``.

    ``name_Prince_Edward_Island.geojson`` is matched to
    ``stl_dir / stl_pattern.format(name="Prince_Edward_Island")``.  Provinces
    whose STL is missing are skipped (logged), so a partial print still keys.
    """
    border_dir = Path(border_dir)
    stl_dir = Path(stl_dir)
    specs = []
    for gj in sorted(border_dir.glob("name_*.geojson")):
        stem = gj.stem[len("name_"):]
        stl = stl_dir / stl_pattern.format(name=stem)
        if not stl.exists():
            log.warning("No STL for %s at %s; skipping.", stem, stl)
            continue
        specs.append((stem, str(gj), str(stl)))
    if not specs:
        raise ValueError(f"No province GeoJSON/STL pairs found under {border_dir}.")
    return fit_dovetails(border_dir / "borders.geojson", specs, params=params,
                         out_dir=out_dir, min_fit_score=min_fit_score)
