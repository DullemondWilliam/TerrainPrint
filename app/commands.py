"""
app.commands

Undoable Step 2 operations.

Every cleanup action is one :class:`QUndoCommand` pushed onto a single
``QUndoStack``.  Undo is snapshot-based: the command captures each affected
region's geometry before and after applying the operation, then swaps between
them.  Geometry objects are immutable in practice (``core.cleanup`` always
returns new geometry), so holding references is cheap and exact — no
re-computation on undo, and therefore no risk of undo drifting from the
original state.
"""

from PySide6.QtGui import QUndoCommand
from shapely import STRtree

from core import cleanup


def _differs(a, b) -> bool:
    """Whether two geometries differ by value (cheap identity check first)."""
    if a is b:
        return False
    try:
        return a.wkb != b.wkb
    except Exception:
        return True


def _part_mapping(before, after) -> dict:
    """
    Map each old landmass index to the one that succeeded it, or None.

    Used to keep the Step 2 selection pointing at the *result* of an edit rather
    than clearing it: merging three islands should leave the merged landmass
    selected, and simplifying should leave you on the same island you were
    looking at.  Correspondence is by representative point — every operation
    here either keeps a part where it was, absorbs it into a larger one, or
    removes it, so "which new part covers the old one's interior" answers all
    three cases with one rule.

    A part that nothing covers maps to None, which the caller reads as "this
    landmass genuinely no longer exists" and clears the selection for.
    """
    old_parts = cleanup.island_parts(before)
    new_parts = cleanup.island_parts(after)
    if not old_parts or not new_parts:
        return {index: None for index in range(len(old_parts))}

    tree = STRtree(new_parts)
    mapping = {}
    for index, part in enumerate(old_parts):
        probe = part.representative_point()
        candidates = [int(i) for i in tree.query(probe)]
        hit = next((i for i in candidates if new_parts[i].intersects(probe)), None)
        if hit is None:
            # Simplification can shift an outline just off its own old interior
            # point; fall back to the new part it overlaps most.
            overlaps = [(new_parts[i].intersection(part).area, i)
                        for i in (int(j) for j in tree.query(part))]
            overlaps = [(a, i) for a, i in overlaps if a > 0]
            hit = max(overlaps)[1] if overlaps else None
        mapping[index] = hit
    return mapping


class GeometryCommand(QUndoCommand):
    """
    Base command: applies ``operation`` to the project's region geometries.

    ``operation`` receives ``{name: geometry}`` and returns the new mapping.
    Only regions whose geometry actually changed are recorded, so an operation
    that does nothing produces a no-op command (see :attr:`changed`).
    """

    def __init__(self, state, text: str, operation, on_change=None, only=None):
        super().__init__(text)
        self._state = state
        self._on_change = on_change

        # ``only`` restricts the operation to a set of region names, so the
        # bulk cleanup steps act on the provinces ticked in the selector.
        self._before = {r.name: r.geometry for r in state.regions
                        if only is None or r.name in only}
        result = operation(dict(self._before)) or {}

        # Keep only genuine changes.  Operations may hand back a rebuilt-but-
        # identical geometry, so compare by value (WKB) rather than identity —
        # otherwise a no-op would still land on the undo stack.
        self._after = {name: geom for name, geom in result.items()
                       if name in self._before and geom is not None
                       and _differs(geom, self._before[name])}
        self._before = {name: self._before[name] for name in self._after}

        # Recorded once, here, so undo and redo both hand the UI a mapping in
        # the direction it is actually moving.
        self._forward_map = {name: _part_mapping(self._before[name], geom)
                             for name, geom in self._after.items()}
        self._backward_map = {name: _part_mapping(geom, self._before[name])
                              for name, geom in self._after.items()}
        self._last_map = self._forward_map

    @property
    def changed(self) -> bool:
        """True if the operation actually altered anything."""
        return bool(self._after)

    def part_mapping(self, region_name: str) -> dict:
        """
        Old landmass index -> new index (or None) for the move just applied.

        Reflects the last redo/undo, so a caller reconciling its selection gets
        the mapping for the direction the user actually went.
        """
        return self._last_map.get(region_name, {})

    @property
    def touched_regions(self) -> set:
        return set(self._after)

    # QUndoStack.push() calls redo() immediately, which applies the result.
    def redo(self) -> None:
        self._last_map = self._forward_map
        self._apply(self._after)

    def undo(self) -> None:
        self._last_map = self._backward_map
        self._apply(self._before)

    def _apply(self, snapshot: dict) -> None:
        for region in self._state.regions:
            if region.name in snapshot:
                region.geometry = snapshot[region.name]
        if self._on_change:
            # Hand the command over so the caller can re-resolve its selection
            # onto whatever this edit produced (see :meth:`part_mapping`).
            self._on_change(self)


# ---------------------------------------------------------------------------
# Concrete operations
# ---------------------------------------------------------------------------
class SimplifyCommand(GeometryCommand):
    """Topology-aware simplification across the selected regions."""

    def __init__(self, state, coast_tol_m, internal_tol_m, on_change=None, only=None):
        super().__init__(
            state,
            f"Simplify (coast {coast_tol_m:,.0f} m / internal {internal_tol_m:,.0f} m)",
            lambda geoms: cleanup.simplify_regions(geoms, coast_tol_m, internal_tol_m),
            on_change, only,
        )


class FilterIslandsCommand(GeometryCommand):
    """Drop parts below an area threshold."""

    def __init__(self, state, min_area_m2, on_change=None, only=None):
        super().__init__(
            state,
            f"Filter islands < {min_area_m2 / 1e6:,.0f} km²",
            lambda geoms: {n: cleanup.filter_small_islands(g, min_area_m2)
                           for n, g in geoms.items()},
            on_change, only,
        )


class RemoveIslandsCommand(GeometryCommand):
    """Remove hand-picked parts from one region."""

    def __init__(self, state, region_name, indices, on_change=None):
        indices = list(indices)
        super().__init__(
            state,
            f"Remove {len(indices)} island(s) from {region_name}",
            lambda geoms: {region_name: cleanup.remove_islands(
                geoms[region_name], indices)} if region_name in geoms else {},
            on_change,
        )


class MergeIslandsCommand(GeometryCommand):
    """Merge hand-picked parts of one region, bridging them with an isthmus."""

    def __init__(self, state, region_name, indices, bridge_width_m,
                 on_change=None):
        indices = list(indices)
        super().__init__(
            state,
            f"Merge {len(indices)} island(s) in {region_name}",
            lambda geoms: {region_name: cleanup.merge_islands(
                geoms[region_name], indices, bridge_width_m)}
            if region_name in geoms else {},
            on_change,
        )


class CloseGapsCommand(GeometryCommand):
    """Morphologically close gaps between nearby parts."""

    def __init__(self, state, gap_m, on_change=None, only=None):
        super().__init__(
            state,
            f"Close island gaps ({gap_m:,.0f} m)",
            lambda geoms: {n: cleanup.close_island_gaps(g, gap_m)
                           for n, g in geoms.items()},
            on_change, only,
        )


class RemoveLakesCommand(GeometryCommand):
    """Strip interior rings (lakes) from the selected regions."""

    def __init__(self, state, on_change=None, only=None):
        super().__init__(
            state,
            "Remove lakes",
            lambda geoms: {n: cleanup.strip_lakes(g) for n, g in geoms.items()},
            on_change, only,
        )
