"""
UI_shared_border.py

A standalone visual harness for ``LIB_shared_border.simplify_shared_border``.

Workflow
--------
1. **Import Province A …** — one side of the border.
2. **Import Province B …** — its neighbour.  As soon as both are in, the seam
                             they share is found and the status line reports
                             what is wrong with it: how many vertices, how many
                             segments below a nozzle width, how many spikes.
3. **Tolerance / Min seg / Min angle / Smooth / Nozzle / Max drift** — the
                             conditioning parameters, in printed millimetres.
                             Defaults come from ``project_config.json``.
4. **Simplify Seam**       — run the lib off the UI thread with a progress bar.
5. **Zoom to Seam**        — the seam is a thin thing on two large provinces;
                             this frames it so the change is actually visible.
6. **Save Both …**         — write the rebuilt pair into a chosen folder.

The **left** panel is the pair as imported with the original seam in red; the
**right** is the rebuilt pair with the conditioned seam in green and the
original ghosted behind it, so the drift is visible rather than merely reported.
The two panels move together -- pan or zoom either and the other follows, which
is what makes a before/after on a border worth looking at -- and they keep a
**constant size** whatever shape the seam is, rather than collapsing into two
tall slivers for a north-south border like BC/Alberta and two letterboxes for an
east-west one.  See the note above the axes for how those two wants are
reconciled.

Run with:  python harnesses/UI_shared_border.py

The window itself is :class:`ui.seam_editor.SeamEditor`, shared with
``UI_border_editor.py``; this file only gives it a title and a ``main``.

What the harness is for
-----------------------
The lib reports gap and overlap, and refuses a result that has either.  So the
question this harness answers is not "did it work" but **"is the simplification
one I want"** -- which is a judgement about a picture:

*Did it keep the border honest?*  The ghosted original shows where the new seam
departs from it, and by how much; ``max_deviation_mm`` is the number for the
same thing.

*Did it actually fix the printability?*  Sub-nozzle and spike counts are shown
before and after, measured against the printer rather than against the filter.

*Did it take too much?*  A seam simplified past recognition still passes every
check in the lib.  Only looking at it tells you that.
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

# Runnable by path from anywhere (python harnesses/UI_shared_border.py), so put the
# project root on sys.path before importing ui.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.seam_editor import SeamEditor


class SharedBorderTester(SeamEditor):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shared Border — Visual Tester")


def main() -> int:
    app = QApplication(sys.argv)
    window = SharedBorderTester()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
