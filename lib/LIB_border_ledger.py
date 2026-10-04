#!/usr/bin/env python3
"""
LIB_border_ledger.py

Which provinces share a border, and which of those borders have been cleaned.

``LIB_shared_border`` conditions one seam at a time.  A country has dozens, and
the question a whole-country tool has to answer before any of them is opened is
**which are done**.  This module answers it in two parts:

* :func:`find_borders` -- every pair in a set of provinces that shares a seam,
  found with ``LIB_shared_border``'s own seam finder, so "these two are
  neighbours" means exactly what it means to the simplifier.
* :class:`BorderLedger` -- a small JSON file kept beside the saved provinces,
  recording the seam each cleaned border was left with.

    from lib.LIB_border_ledger import BorderLedger, find_borders

    borders = find_borders({"CAN_Alberta": alberta, "CAN_Saskatchewan": sask})
    ledger = BorderLedger.load("Data/JSonProvinces-Bordered")
    for border in borders:
        print(border.key, ledger.is_cleaned(border))

Why the seam is recorded, not just a flag
-----------------------------------------
A flag says the border *was* cleaned.  It goes on saying so after the province
is re-exported from the island editor, or re-cleaned against different
settings, and a ledger that has stopped describing the files is worse than none.
So a border counts as cleaned only while the seam the two files share today is
still the seam that was recorded -- to within :data:`MATCH_TOLERANCE_M`, which
absorbs the round trip through EPSG:4326 the files are saved in and nothing
else.  A re-edited province drops its borders back to "not cleaned" by itself.

Cleaning one border leaves the others alone: ``simplify_shared_border`` pins
the seam's endpoints and rebuilds the pair out of its own union, so a third
province's seam with either of them is not touched.  That is what makes a
per-border ledger meaningful at all.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shapely import wkt
from shapely.strtree import STRtree

from lib.LIB_shared_border import _shared_seam

#: The ledger's file name, inside the folder the cleaned provinces are saved to.
LEDGER_NAME = "border_ledger.json"
LEDGER_VERSION = 1

#: How far, in metres, today's seam may sit from the recorded one and still be
#: the same seam.  Saving to EPSG:4326 and reading back moves vertices by well
#: under a millimetre; a re-cleaned seam moves by hundreds of metres (0.19 print-
#: mm is 1.5 km).  One metre sits between the two by orders of magnitude either
#: way.
MATCH_TOLERANCE_M = 1.0

#: Separates the two province names in a border's key.  Not a character a file
#: name can hold on Windows, so it can never be part of a name.
KEY_SEPARATOR = "|"


def border_key(name_a, name_b):
    """The key of the border between two provinces, the same either way round."""
    first, second = sorted((name_a, name_b))
    return f"{first}{KEY_SEPARATOR}{second}"


@dataclass
class Border:
    """One seam two provinces share."""
    a: str                          #: first province name (sorts first)
    b: str                          #: second province name
    seam: object = None             #: the seam as the files have it now

    @property
    def key(self):
        return border_key(self.a, self.b)

    @property
    def vertices(self):
        return sum(len(part.coords) for part in self.seam.geoms)

    def involves(self, name):
        return name in (self.a, self.b)


def find_borders(provinces):
    """
    Every pair of provinces that shares a seam.

    Parameters
    ----------
    provinces : dict[str, shapely geometry]
        Province name to its dissolved geometry, all in one metric CRS.

    Returns
    -------
    list[Border]
        Sorted by key.  Pairs that merely touch at a point are not borders.

    A spatial index keeps this from being every-pair-against-every-pair: the
    27 Canadian and US files take about 0.3 s, almost all of it on the few seams
    that are real.
    """
    names = sorted(provinces)
    geoms = [provinces[name] for name in names]
    tree = STRtree(geoms)
    found = []
    for i, name in enumerate(names):
        for j in sorted(int(k) for k in tree.query(geoms[i])):
            if j <= i:
                continue
            seam = _shared_seam(geoms[i], geoms[j])
            if seam is not None:
                found.append(Border(name, names[j], seam))
    return sorted(found, key=lambda border: border.key)


@dataclass
class BorderLedger:
    """The cleaned borders of one folder of provinces."""
    folder: Path
    entries: dict = field(default_factory=dict)

    @property
    def path(self):
        return Path(self.folder) / LEDGER_NAME

    @classmethod
    def load(cls, folder):
        """The ledger in ``folder``, empty if there is none or it is unreadable.

        Unreadable is treated as absent: the cost is that every border reads as
        not cleaned, which is the safe direction to be wrong in.
        """
        ledger = cls(Path(folder))
        try:
            data = json.loads(ledger.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ledger
        if isinstance(data, dict) and isinstance(data.get("borders"), dict):
            ledger.entries = data["borders"]
        return ledger

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": LEDGER_VERSION, "borders": self.entries},
                       indent=2, sort_keys=True),
            encoding="utf-8")

    def record(self, border_or_key, result, params=None):
        """
        Note a border as cleaned, with the seam it was left with.

        ``result`` is the :class:`~lib.LIB_shared_border.SeamResult` that was
        saved.  The before/after figures are kept for the list to show; the
        seam itself is what :meth:`is_cleaned` checks against.
        """
        key = getattr(border_or_key, "key", border_or_key)
        entry = {
            "seam_wkt": result.seam.wkt,
            "vertices_before": int(result.vertices_before),
            "vertices_after": int(result.vertices_after),
            "max_deviation_mm": round(float(result.max_deviation_mm), 4),
            "seam_length_mm": round(float(result.seam_length_mm), 2),
            "cleaned_at": datetime.now().isoformat(timespec="seconds"),
        }
        if params is not None:
            entry["params"] = {
                name: getattr(params, name) for name in
                ("tolerance_mm", "min_segment_mm", "min_angle_deg",
                 "smooth_iterations", "nozzle_mm", "max_deviation_mm")}
        self.entries[key] = entry
        return entry

    def entry(self, border):
        return self.entries.get(border.key)

    def recorded_seam(self, border):
        entry = self.entry(border)
        if not entry:
            return None
        try:
            return wkt.loads(entry["seam_wkt"])
        except Exception:
            return None

    def status(self, border):
        """
        ``"cleaned"``, ``"changed"`` or ``"not cleaned"``.

        ``"changed"`` is a border that was cleaned once but whose files no longer
        carry that seam: it needs cleaning again, and saying why is kinder than
        silently listing it as never done.
        """
        recorded = self.recorded_seam(border)
        if recorded is None:
            return "not cleaned"
        if border.seam is None:
            return "changed"
        try:
            drift = border.seam.hausdorff_distance(recorded)
        except Exception:
            return "changed"
        return "cleaned" if drift <= MATCH_TOLERANCE_M else "changed"

    def is_cleaned(self, border):
        return self.status(border) == "cleaned"
