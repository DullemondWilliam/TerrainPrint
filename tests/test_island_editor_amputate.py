"""
test_island_editor_amputate.py

The **Amputate** action of UI_island_editor.py — drop every piece of land
narrower than *Min width*.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_selection.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_amputate.py -v

The opening itself is covered by test_thin_features.py. What this file guards
is the editor's side of it:

* it is one undoable step like every other edit, and a province with nothing
  thin in it does **not** become one — an opening hands back the same polygon
  with its vertices reordered, which the WKB check in ``_apply`` would take for
  a change and put a no-op on the stack (FR-2.13 says it must not);
* a province that is *entirely* below the width is refused, by name, rather
  than emptied;
* and the report says what the cut cost, including the slivers that survive at
  sharp corners — because claiming a perfect result when 131 remain is how a
  tool stops being believed.
"""

import pytest
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

MM = M.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


def square(x_mm, y_mm, size_mm=4.0):
    return box(x_mm * MM, y_mm * MM,
               (x_mm + size_mm) * MM, (y_mm + size_mm) * MM)


def necked(x_mm, neck_mm=0.4):
    """Two 4 mm squares joined by a neck of the given printed width."""
    half = neck_mm / 2.0
    return unary_union([
        box(x_mm * MM, 0, (x_mm + 4) * MM, 4 * MM),
        box((x_mm + 4) * MM, (2 - half) * MM,
            (x_mm + 6) * MM, (2 + half) * MM),
        box((x_mm + 6) * MM, 0, (x_mm + 10) * MM, 4 * MM)])


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(M.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(M.QMessageBox, "warning", lambda *a, **k: None)


def _editor(qtbot, geometry):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(1100, 700)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(geometry)
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    return window


@pytest.fixture
def province(qtbot):
    """A fat island, a necked pair, and an islet below the width."""
    return _editor(qtbot, unary_union([square(0.0, 0.0, 10.0),
                                       necked(20.0),
                                       square(40.0, 0.0, 0.5)]))


class TestTheCut:

    def test_the_button_needs_a_province(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        assert not window.btn_amputate.isEnabled()

    def test_it_is_live_once_one_is_loaded(self, province):
        assert province.btn_amputate.isEnabled()

    def test_the_islet_goes_and_the_neck_is_cut(self, province):
        before = len(province.parts)
        province.amputate()
        # The islet vanishes; the necked pair becomes two. Three either way,
        # which is why the parts are checked by shape and not by counting.
        assert before == 3
        widths = sorted(round((p.bounds[2] - p.bounds[0]) / MM, 1)
                        for p in province.parts)
        assert widths == [4.0, 4.0, 10.0]

    def test_what_is_left_has_nothing_thin_in_it(self, province):
        from lib.LIB_thin_features import thin_features
        province.amputate()
        assert thin_features(province.doc).clean

    def test_it_is_one_undoable_step(self, province):
        before = province.doc
        province.amputate()
        assert province.undo_stack.undoText().startswith("Amputate below")
        province.undo_stack.undo()
        assert province.doc is before

    def test_the_width_decides_what_goes(self, qtbot):
        # A 1.5 mm neck survives at 1 mm and is cut at 2 mm.
        window = _editor(qtbot, necked(0.0, neck_mm=1.5))
        window.width_spin.setValue(1.0)
        window.amputate()
        assert len(window.parts) == 1
        window.width_spin.setValue(2.0)
        window.amputate()
        assert len(window.parts) == 2

    def test_the_width_is_printed_millimetres(self, qtbot):
        """A 0.9 mm neck at 1:8M is 1.8 mm at 1:4M — thin, then not."""
        window = _editor(qtbot, necked(0.0, neck_mm=0.9))
        window.scale_spin.setValue(M.DEFAULT_PRINT_SCALE // 2)
        window.amputate()
        assert len(window.parts) == 1            # comfortable at 1:4M
        window.scale_spin.setValue(M.DEFAULT_PRINT_SCALE)
        window.amputate()
        assert len(window.parts) == 2            # thin at 1:8M

    def test_the_scores_panel_follows_it(self, province):
        province.amputate()
        text = province.lbl_scores.text()
        assert "-" in text                       # land and coast both fell
        assert "press Recalculate" in text


class TestWhatItRefusesToDo:

    def test_a_province_with_nothing_thin_is_not_an_edit(self, qtbot):
        """An opening reorders the vertices of a shape it did not change.

        Left to the WKB comparison in ``_apply`` that reads as an edit, and a
        no-op would go on the undo stack — which FR-2.13 forbids and which
        would make Undo lie about what it was undoing.
        """
        window = _editor(qtbot, square(0.0, 0.0, 10.0))
        window.amputate()
        assert not window.undo_stack.canUndo()
        assert "Nothing under 1 mm" in window.status.text()

    def test_a_province_entirely_below_the_width_is_refused(self, qtbot):
        window = _editor(qtbot, square(0.0, 0.0, 0.5))
        before = window.doc
        window.amputate()
        assert window.doc is before
        assert not window.undo_stack.canUndo()

    def test_amputating_with_nothing_loaded_is_harmless(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        window.amputate()                        # must not raise
        assert window.doc is None

    def test_a_zero_width_changes_nothing(self, province):
        province.width_spin.setValue(0.0)
        province.amputate()
        assert not province.undo_stack.canUndo()


class TestTheReport:
    """What the cut cost, in the terms the decision was made in."""

    def test_it_names_the_width_and_the_landmasses(self, province):
        province.amputate()
        text = province.status.text()
        assert "Amputated below 1 mm" in text
        assert "landmass(es)" in text

    def test_it_reports_land_and_outline_separately(self, province):
        """They disagree, and the disagreement is the point.

        A small island is nearly all coast, so dropping one costs far more
        outline than land — a report giving only the area would make every cut
        look free.
        """
        province.amputate()
        text = province.status.text()
        assert "of the land gone" in text
        assert "of the outline" in text

    def test_a_clean_result_says_so(self, province):
        province.amputate()
        assert "nothing left under the width" in province.status.text()

    def test_surviving_slivers_are_admitted(self, qtbot):
        """Sharp corners outlive the mitre bevel, and saying otherwise lies.

        British Columbia keeps 131 of them. A report that claimed a perfect
        result would be wrong in the one direction that matters.
        """
        # A wedge sharp enough that the mitre bevel leaves a trace at its tip.
        wedge = Polygon([(10 * MM, 4 * MM), (30 * MM, 5 * MM),
                         (10 * MM, 6 * MM)])
        window = _editor(qtbot,
                         unary_union([box(0, 0, 10 * MM, 10 * MM), wedge]))
        window.amputate()
        text = window.status.text()
        assert "sliver(s) left at sharp corners" in text
        assert "nothing left under the width" not in text
