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
