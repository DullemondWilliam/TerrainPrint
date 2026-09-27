"""Standalone visual harnesses (``UI_*``) for the ``lib/LIB_*`` modules.

Each one is a throwaway inspection tool: it loads a file, makes a single lib
call, and draws before vs after so the effect is visible.  They are deliberately
independent of ``app/`` and of each other — see ``docs/BUILDING_UI_HARNESSES.md``.

The combined editors and viewers (``UI_island_editor``, ``UI_terrain_editor``,
``UI_province_viewer``) are *not* harnesses and stay in the project root.
"""
