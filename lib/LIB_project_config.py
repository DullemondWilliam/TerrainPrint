"""
project_config.py

The project's magic numbers, in one place instead of scattered across modules.

The numbers this pipeline turns on -- what scale the map prints at, what CRS the
geometry is worked in, how tall a border bar is, how big the print bed is -- were
each declared wherever they were first needed.  That let the same figure drift
into several spellings: the print scale was ``DEFAULT_SCALE`` in
``LIB_border_connectors``, ``DEFAULT_PRINT_SCALE`` in ``UI_island_editor``, and a
bare ``8_000_000`` in a spin box in ``UI_dovetail_fitter``, with nothing keeping
the three in step.  A number the whole pipeline agrees on should be written once.

Two files, merged, local winning
--------------------------------
``project_config.json``
    Tracked in git.  Project truths: everyone working on this gets the same
    print scale and the same bed size, and a change to one is a reviewable diff.

``project_config.local.json``
    Gitignored.  Whatever is true of *this desk* rather than of the project --
    principally the screen calibration, which is measured against particular
    monitors and is not merely useless on someone else's but wrong.

This mirrors the split the repository already uses for ``.claude/settings.json``
and ``.claude/settings.local.json``.

Nothing here is required to exist.  Every value has a built-in default, so a
missing, unreadable or half-written config costs you the customisation and not
the program.

Reading
-------
    from lib.LIB_project_config import get

    scale = get("print.scale")              # 8000000
    crs   = get("crs.working")              # "EPSG:3347"
"""

import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ROOT, "project_config.json")
LOCAL_CONFIG_PATH = os.path.join(_ROOT, "project_config.local.json")

#: The values the project runs on if no config file says otherwise. This is the
#: documentation of what each knob means as much as it is a fallback -- the JSON
#: alongside it carries the same keys with the same numbers, so the two can be
#: read together.
DEFAULTS = {
    "crs": {
        # Statistics Canada Lambert, metres. Geometry is worked in a projected
        # metre CRS so that a kilometre buffer is a real ground kilometre.
        "working": "EPSG:3347",
        # What GeoJSON arrives in (WGS 84 lon/lat).
        "source": "EPSG:4326",
    },
    "print": {
        # The map's reduction, as 1:N. One printed millimetre is N millimetres
        # of ground -- at 8 000 000 that is 8 km, and a whole province fits a
        # desk-sized sheet. Every tool that sizes anything for paper reads this.
        "scale": 8_000_000,
        # Printer bed, and the margin kept clear of its edges (mm).
        "bed_mm": 256.0,
        "bed_margin_mm": 10.0,
        # Border pieces: total height, and the dovetail layer within it (mm).
        "border_thickness_mm": 2.0,
        "dovetail_mm": 1.0,
    },
    "terrain": {
        # Vertical exaggeration. At 1:8,000,000 one print-mm is 8 km, so true
        # scale (x1) puts Newfoundland's 1578 m of relief at 0.20 mm -- invisible.
        # Share one value across a whole map: pieces meshed at different
        # exaggerations do not meet at the seam.
        "exaggeration": 10.0,
        # Solid base under the terrain (mm). Must exceed print.dovetail_mm, or
        # the connector pocket cuts through the bottom of the piece.
        "base_mm": 2.0,
        # Elevation the base measures from. Sea level, so below-sea-level land
        # (-17.7 m in Labrador) rests on the base instead of punching through.
        # Shared across a map for the same reason as the exaggeration.
        "floor_m": 0.0,
        # Sampling pitch on paper: target_res_m = spacing_mm * print.scale/1000,
        # so 0.4 mm at 1:8M is 3200 m -- about what a 0.4 mm nozzle resolves.
        "spacing_mm": 0.4,
        # How the warp resamples when coarsening. 'max' keeps the summits a big
        # downsample otherwise loses: 1578 m against bilinear's 1165 m on
        # Labrador, where the true peak is 1652 m.
        "resampling": "max",
        # Refuse to mesh a grid larger than this, checked before reading pixels.
        "max_triangles": 1_500_000,
    },
    "seam": {
        # Conditioning the border two provinces share, in printed millimetres.
        # Every value is a printer limit expressed on paper, not a map opinion.
        # Douglas-Peucker tolerance: detail finer than this says nothing at
        # print scale.
        "tolerance_mm": 0.2,
        # The printer's resolution on paper. Two jobs: seam runs shorter than
        # this are counted as unprintable in the report, and by default they
        # are also what gets collapsed. Reporting always uses this number, so
        # turning the filter down does not make the seam *look* printable.
        "nozzle_mm": 0.3,
        # Collapse seam runs shorter than this; defaults to nozzle_mm. DP
        # measures perpendicular deviation and so leaves tight zigzag intact;
        # this is what removes it. Set to 0 to skip the pass -- the report
        # still measures against nozzle_mm.
        "min_segment_mm": 0.3,
        # The sharpest corner worth printing: below this a seam vertex comes
        # out as a fragile spur on one province and a slot on the other. Used
        # for the report, so a seam is described against the printer and not
        # against how hard the filter was told to push.
        "spike_angle_deg": 20.0,
        # Remove corners sharper than this; defaults to spike_angle_deg. Set to
        # 0 to skip the pass -- the report still measures against the limit.
        "min_angle_deg": 20.0,
        # Chaikin smoothing passes over what survives. 0 keeps the seam angular.
        "smooth_iterations": 0,
        # Refuse a simplification that drags the border further than this from
        # where it really runs.
        "max_deviation_mm": 1.5,
    },
    "paths": {
        # Where the province pipeline reads and writes, relative to the repo.
        "provinces_in": "Data/JSon-Provinces-Orig",
        "provinces_out": "Data/JSonProvinces-Cleaned",
    },
    "island_editor": {
        # Printed sizes, converted to ground units through print.scale at the
        # moment of use. See UI_island_editor for why they are stated on paper.
        "buffer_mm": 0.25,
        "brush_mm": 0.125,
        "min_area_mm2": 0.02,
    },
    "thin": {
        # The narrowest land worth printing, in printed millimetres. Land below
        # this does not survive being a solid: a neck snaps when the piece is
        # lifted, a spit breaks off in the post, an islet is simply not there.
        # The same figure as bridges.width_mm, and for the same reason -- a neck
        # you already have and a bridge you would add need the same width to
        # survive handling -- but a separate knob, because one is a limit you
        # measure against and the other is a size you build to.
        "width_mm": 1.0,
    },
    "bridges": {
        # Straight fixed-width bridges between islands, in printed millimetres.
        # Unlike the island_editor buffer, whose neck width falls out of the gap
        # it is closing, these are a specification: every bridge is this wide.
        # 1 mm is 8 km at 1:8,000,000 and 3.3 nozzle widths -- a causeway you
        # can print and handle.
        "width_mm": 1.0,
        # How far each end is pushed past the coastline into the land, so the
        # union is a lap joint. One width gives a square of overlap at each end.
        "embed_mm": 1.0,
        # Refuse to span further than this, which also decides which islands
        # are left separate. 5 mm is 40 km of water, and an aspect of 5 at the
        # default width -- stiff enough to survive handling.
        "max_span_mm": 5.0,
        # Coastline sampling pitch when hunting for a landing. A quarter of the
        # bridge width: finer cannot move a landing by a visible amount.
        "sample_mm": 0.25,
        # What a bad landing is worth, in mm of span. A fully grazing landing
        # must save 2 mm of crossing to be taken over a square one; likewise a
        # landing with no bite on the shore.
        "angle_penalty_mm": 2.0,
        "anchor_penalty_mm": 2.0,
        # Reject a landing biting less than this fraction of the width x
        # embedment a straight shore would give -- a bridge welded on by a
        # sliver snaps off.
        "min_anchor": 0.5,
        # Slenderness limit as span/width. Past 15 a 1 mm bridge is a
        # matchstick; the fix is a wider bridge, never a second one.
        "max_aspect": 15.0,
        # Bracing: a second bridge is added to a pair only when two bridges
        # could be placed this many crossings apart, because that is when a
        # single one acts as a hinge the pair can pivot about.
        "max_per_pair": 2,
        "hinge_ratio": 4.0,
    },
}


def _read(path):
    """One config file as a dict, empty if absent or unusable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Unreadable is as good as absent: a permissions problem, a directory in
        # its place, or half-written JSON should cost the customisation, never
        # the program. ValueError covers json's own JSONDecodeError.
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base, over):
    """``over`` laid on top of ``base``, recursing into dicts."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load():
    """
    The whole configuration: defaults, then the project file, then the local one.

    Re-read on every call rather than cached. These files are a few hundred
    bytes and are read at import time or on a button press, never in a draw
    loop, so the cost is nothing next to a cache that could serve a stale
    calibration to the tool that just rewrote it.
    """
    merged = _merge(DEFAULTS, _read(CONFIG_PATH))
    return _merge(merged, _read(LOCAL_CONFIG_PATH))


def get(dotted, default=None, config=None):
    """
    One value by dotted path -- ``get("print.scale")``.

    Falls back to ``default`` if the path is absent, and to the built-in
    ``DEFAULTS`` before that, so a caller only passes ``default`` for a key this
    module does not know about.
    """
    node = load() if config is None else config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def update_local(dotted, value):
    """
    Write one value into the **local** config, leaving the project file alone.

    Anything a machine discovers about itself belongs here rather than in the
    tracked file: it is true of this desk, not of the project.
    """
    data = _read(LOCAL_CONFIG_PATH)
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    try:
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError:
        pass        # non-fatal: the setting is lost, the program is not
    return data
