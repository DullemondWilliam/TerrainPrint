"""
test_island_editor_scores.py

The **Scores** panel of UI_island_editor.py — what the session has cost,
against the province as it was imported.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_selection.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_scores.py -v

The measures themselves are covered by test_shape_scores.py. What this file
guards is the panel never showing a figure that does not describe what is on
screen, which is the only way a scoreboard can actually mislead:

* the baseline is the file as imported, and stays there through every edit,
  every undo and every save;
* the two cheap rows follow every edit, because they are arithmetic;
* the rasterised row is **held** and marked stale the moment the document or
  the print scale moves — a 99% agreement left sitting under an edit it never
  saw would be worse than a blank;
* and "could not be measured" is never rendered as agreement.
"""

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

MM = M.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


def square(x_mm, y_mm, size_mm=4.0):
    return box(x_mm * MM, y_mm * MM,
               (x_mm + size_mm) * MM, (y_mm + size_mm) * MM)


#: Two 4 mm islands and a 0.5 mm islet, so an edit can be small but real.
ISLANDS = [square(0.0, 0.0), square(6.0, 0.0), square(12.0, 0.0, 0.5)]


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(M.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(M.QMessageBox, "warning", lambda *a, **k: None)


@pytest.fixture
def editor(qtbot):
    """A shown editor holding the three islands, as if just imported."""
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(1000, 700)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(unary_union(ISLANDS))
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    yield window
    window.undo_stack.clear()


def _drop_the_islet(editor):
    """One real edit: remove the 0.5 mm island."""
    editor.selected = [i for i, part in enumerate(editor.parts)
                       if part.area < (1.0 * MM) ** 2]
    editor._sync_buttons()
    editor.remove_selected()


class TestTheBaseline:
    """Measured against the file as opened, and nothing else."""

    def test_a_fresh_import_is_identical_to_itself(self, editor):
        assert "identical to the original" in editor.lbl_scores.text()
        assert not editor.btn_recalc.isEnabled()

    def test_the_baseline_survives_an_edit(self, editor):
        before = editor.original_doc
        _drop_the_islet(editor)
        assert editor.original_doc is before
        assert editor.doc is not before

    def test_undoing_back_to_it_is_identical_again(self, editor):
        _drop_the_islet(editor)
        assert "press Recalculate" in editor.lbl_scores.text()
        editor.undo_stack.undo()
        assert "identical to the original" in editor.lbl_scores.text()

    def test_saving_does_not_move_the_baseline(self, editor, monkeypatch,
                                               tmp_path):
        """The question is what was given up from the source file.

        Writing the province out is not a new starting point; a session that
        saves twice has not thereby forgiven itself the first half of its edits.
        """
        _drop_the_islet(editor)
        edited = editor.doc
        monkeypatch.setattr(M.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (str(tmp_path / "out.geojson"), ""))
        editor.source_crs = M.EPSG_METRIC
        editor.save_geojson()
        assert editor.original_doc is not edited
        assert "press Recalculate" in editor.lbl_scores.text()

    def test_nothing_imported_says_so(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        assert "Nothing imported" in window.lbl_scores.text()
        assert not window.grp_scores.isEnabled()


class TestTheCheapRows:
    """Coast and land are arithmetic, so they follow every edit at once."""

    def test_they_report_the_original_on_import(self, editor):
        text = editor.lbl_scores.text()
        # Two 4 mm squares and a 0.5 mm islet: 16 + 16 + 2 = 34 mm of outline.
        assert "34.0" in text
        assert "+0.0%" in text

    def test_an_edit_moves_them_with_no_button_press(self, editor):
        _drop_the_islet(editor)
        text = editor.lbl_scores.text()
        assert "Coast" in text and "-5.9%" in text      # 2 of 34 mm gone
        assert "-0.8%" in text                          # 0.25 of 32 mm²

    def test_a_quarter_millimetre_islet_is_not_rounded_away(self, editor):
        """Whole millimetres would report this edit as no change at all."""
        _drop_the_islet(editor)
        assert "+0.0%" not in editor.lbl_scores.text().split("Agree")[0]

    def test_they_are_printed_millimetres(self, editor):
        editor.scale_spin.setValue(M.DEFAULT_PRINT_SCALE // 2)
        # Half the reduction, twice the outline: 34 mm becomes 68.
        assert "68.0" in editor.lbl_scores.text()

    def test_they_survive_an_undo_and_redo(self, editor):
        _drop_the_islet(editor)
        edited = editor.lbl_scores.text()
        editor.undo_stack.undo()
        editor.undo_stack.redo()
        assert editor.lbl_scores.text() == edited


class TestTheRasterisedRow:
    """Held, and retired the instant it stops describing the document."""

    def test_it_waits_to_be_asked(self, editor):
        _drop_the_islet(editor)
        assert "press Recalculate" in editor.lbl_scores.text()
        assert editor.btn_recalc.isEnabled()

    def test_recalculating_fills_it_in(self, editor):
        _drop_the_islet(editor)
        editor.recalculate_scores()
        text = editor.lbl_scores.text()
        assert "Agree" in text and "%" in text
        assert "mm/px" in text
        assert not editor.btn_recalc.isEnabled()        # now fresh

    def test_it_samples_at_a_quarter_of_the_nozzle(self, editor):
        from lib.LIB_project_config import get
        _drop_the_islet(editor)
        editor.recalculate_scores()
        expected = get("seam.nozzle_mm") / 4.0
        assert f"@ {expected:g} mm/px" in editor.lbl_scores.text()

    def test_a_further_edit_retires_it(self, editor):
        _drop_the_islet(editor)
        editor.recalculate_scores()
        assert "%" in editor.lbl_scores.text().split("Agree")[1]
        editor.selected = [0]
        editor._sync_buttons()
        editor.remove_selected()
        assert "press Recalculate" in editor.lbl_scores.text()
        assert editor.btn_recalc.isEnabled()

    def test_a_new_print_scale_retires_it(self, editor):
        """Every figure in the panel is a printed one, the pixel included."""
        _drop_the_islet(editor)
        editor.recalculate_scores()
        editor.scale_spin.setValue(M.DEFAULT_PRINT_SCALE // 2)
        assert "press Recalculate" in editor.lbl_scores.text()
        assert editor.btn_recalc.isEnabled()

    def test_an_undo_retires_it_too(self, editor):
        editor.selected = [0]
        editor._sync_buttons()
        editor.remove_selected()
        editor.recalculate_scores()
        fresh = editor.lbl_scores.text()
        assert "%" in fresh.split("Agree")[1]
        editor.undo_stack.undo()
        assert "identical to the original" in editor.lbl_scores.text()

    def test_it_reports_what_was_added_and_lost(self, editor):
        _drop_the_islet(editor)
        editor.recalculate_scores()
        text = editor.lbl_scores.text()
        assert "+" in text.split("Agree")[1]
        assert "−" in text.split("Agree")[1]            # a real minus sign

    def test_recalculating_with_nothing_loaded_is_harmless(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        window.recalculate_scores()                      # must not raise
        assert window._scores is None


class TestUnmeasuredIsNotAgreement:
    """The one way a scoreboard can actively mislead."""

    def test_a_failed_comparison_says_so(self, editor, monkeypatch):
        from lib.LIB_shape_scores import ShapeScores
        _drop_the_islet(editor)
        monkeypatch.setattr(M, "score_shapes",
                            lambda *a, **k: ShapeScores(measured=False))
        editor.recalculate_scores()
        text = editor.lbl_scores.text()
        assert "could not be measured" in text
        assert "%" not in text.split("Agree")[1]

    def test_a_coarsened_pixel_is_declared(self, editor, monkeypatch):
        """A score read at a coarser grid than asked for must not hide it."""
        from lib.LIB_shape_scores import ShapeScores
        _drop_the_islet(editor)
        monkeypatch.setattr(
            M, "score_shapes",
            lambda *a, **k: ShapeScores(agreement=0.5, pixel_mm=2.0,
                                        coarsened=True))
        editor.recalculate_scores()
        assert "coarsened" in editor.lbl_scores.text()

    def test_a_raising_comparison_leaves_the_old_reading_retired(self, editor,
                                                                 monkeypatch):
        _drop_the_islet(editor)

        def boom(*a, **k):
            raise RuntimeError("no")

        monkeypatch.setattr(M, "score_shapes", boom)
        editor.recalculate_scores()
        assert editor._scores is None
        assert "press Recalculate" in editor.lbl_scores.text()
