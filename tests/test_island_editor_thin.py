"""
test_island_editor_thin.py

The **Printability** layer of UI_island_editor.py — the land under *Min width*,
marked in red over the province it is in.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_stress.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_thin.py -v

The measurement is covered by test_thin_features.py and nothing here re-tests
it.  What this file guards is what the layer means *in this window*, which is
the whole reason it is here rather than only in the province viewer:

* it is the preview of **Amputate**, and shares that edit's knob — one width,
  marked and then removed, so the two cannot disagree;
* and everything marked really is what the cut takes, which is the claim the
  tooltip makes and the only one that makes the layer worth looking at;
* it is measured once per (document, width) and read from the cache after
  that, because ``_redraw`` runs on every click and half a second of Nunavut
  thirty times over is a window that has stopped responding.
"""

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

import UI_island_editor as M

pytestmark = pytest.mark.usefixtures("qapp")

MM = M.DEFAULT_PRINT_SCALE / 1000.0        # one print-mm, in metres


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    monkeypatch.setattr(M.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(M.QMessageBox, "warning", lambda *a, **k: None)


def spit(body_mm=10.0, spit_mm=0.4, reach_mm=6.0):
    """A square with a thin spit off its side: one patch of land under 1 mm."""
    body = box(0, 0, body_mm * MM, body_mm * MM)
    middle = body_mm / 2.0
    tail = box(body_mm * MM, (middle - spit_mm / 2.0) * MM,
               (body_mm + reach_mm) * MM, (middle + spit_mm / 2.0) * MM)
    return unary_union([body, tail])


def dumbbell(throat_mm=0.2, lobe_mm=10.0, bar_mm=3.0):
    """Two lobes on a bar: thin land that is also holding something up.

    The spit above has no neck at all -- what hangs off it is below
    ``min_lobe_mm2``, so the stress measurement finds nothing and a test about
    the two layers together would be testing one layer. This shape is the
    fixture of test_island_editor_stress.py, and it is in both files rather
    than shared because a fixture read in one place is worth more than one
    imported from another.
    """
    left = box(0, 0, lobe_mm * MM, lobe_mm * MM)
    right = box((lobe_mm + bar_mm) * MM, 0,
                (2 * lobe_mm + bar_mm) * MM, lobe_mm * MM)
    middle = lobe_mm / 2.0
    bar = box(lobe_mm * MM, (middle - throat_mm / 2.0) * MM,
              (lobe_mm + bar_mm) * MM, (middle + throat_mm / 2.0) * MM)
    return unary_union([left, right, bar])


def _editor(qtbot, geom):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(geom)
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    return window


@pytest.fixture
def editor(qtbot):
    window = _editor(qtbot, spit())
    window.width_spin.setValue(1.0)
    return window


def _show(window):
    window.btn_thin.setChecked(True)
    window._toggle_thin(True)


class TestShowingTheLayer:

    def test_nothing_is_measured_until_it_is_asked_for(self, editor):
        assert editor._thin is None

    def test_the_toggle_needs_a_document(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        assert not window.btn_thin.isEnabled()

    def test_showing_finds_the_spit(self, editor):
        _show(editor)
        assert editor._thin.patches == 1
        assert not editor._thin.clean

    def test_the_marks_land_on_the_map(self, editor):
        before = len(editor.ax.collections)
        _show(editor)
        assert len(editor.ax.collections) > before

    def test_one_artist_for_the_whole_province(self, editor):
        """Nunavut has 7 250 thin patches and geopandas would make 7 250
        artists; the layer is drawn as one geometry."""
        before = len(editor.ax.collections)
        _show(editor)
        assert len(editor.ax.collections) == before + 1

    def test_taking_it_off_takes_the_marks(self, editor):
        _show(editor)
        with_marks = len(editor.ax.collections)
        editor.btn_thin.setChecked(False)
        editor._toggle_thin(False)
        assert len(editor.ax.collections) < with_marks

    def test_the_status_line_counts_the_patches(self, editor):
        _show(editor)
        assert "1 thin patch(es)" in editor.status.text()
        assert "under 1 mm" in editor.status.text()

    def test_a_clean_province_says_so(self, qtbot):
        """'Nothing under the width' is an answer, and not the same answer as
        the layer being off."""
        window = _editor(qtbot, box(0, 0, 10 * MM, 10 * MM))
        window.width_spin.setValue(1.0)
        _show(window)
        assert window._thin.clean
        assert "nothing under 1 mm" in window.status.text()

    def test_an_islet_under_the_width_is_counted_apart(self, qtbot):
        """A part with nothing printable in it wants removing, where a neck
        wants widening -- so the status line says it separately."""
        window = _editor(qtbot, unary_union([
            box(0, 0, 10 * MM, 10 * MM),
            box(20 * MM, 0, 20.5 * MM, 0.5 * MM)]))
        window.width_spin.setValue(1.0)
        _show(window)
        assert window._thin.whole_parts == 1
        assert "1 island(s) too thin end to end" in window.status.text()


class TestItIsThePreviewOfAmputate:
    """The case for the layer being in an editor at all."""

    def test_it_shares_min_width_with_amputate(self, editor):
        """One knob, so the preview and the cut cannot disagree."""
        editor.width_spin.setValue(2.0)
        assert editor._thin_params().width_mm == 2.0
        assert editor._thin_params().width_mm == editor.width_spin.value()

    def test_a_new_width_is_a_new_answer(self, editor):
        _show(editor)
        narrow = editor._thin.patches
        editor.width_spin.setValue(0.1)       # finer than the spit is wide
        assert editor._thin.patches < narrow
        assert editor._thin.clean

    def test_nothing_marked_survives_the_cut(self, editor):
        """The claim the tooltip makes: the red is what Amputate takes."""
        _show(editor)
        marked = editor._thin.thin
        editor.amputate()
        assert editor.doc.intersection(marked).area == pytest.approx(
            0.0, abs=(0.001 * MM) ** 2)

    def test_the_marks_follow_the_cut(self, editor):
        """Measured again on the document the edit left behind."""
        _show(editor)
        assert editor._thin.patches == 1
        editor.amputate()
        assert editor._thin.patches == 0

    def test_and_follow_an_undo(self, editor):
        _show(editor)
        editor.amputate()
        assert editor._thin.clean
        editor.undo_stack.undo()
        assert editor._thin.patches == 1


class TestItIsMeasuredOnceAndKept:
    """``_redraw`` runs on every click; the measurement must not."""

    def test_a_redraw_reuses_the_report(self, editor):
        _show(editor)
        held = editor._thin
        editor._redraw(preserve_view=True)
        assert editor._thin is held

    def test_a_selection_click_reuses_it(self, editor):
        _show(editor)
        held = editor._thin
        editor.select_all()
        assert editor._thin is held

    def test_a_new_width_does_not(self, editor):
        _show(editor)
        held = editor._thin
        editor.width_spin.setValue(0.3)
        assert editor._thin is not held

    def test_a_new_document_does_not(self, editor):
        _show(editor)
        held = editor._thin
        editor.brush_spin.setValue(2.0)
        editor._stroke = [(4 * MM, 12.0 * MM), (6 * MM, 12.0 * MM)]
        editor._brush_mode = M.DRAW
        editor._finish_stroke()
        assert editor.doc is not held, "the fixture edit must land"
        assert editor._thin is not held

    def test_a_new_print_scale_does_not(self, editor):
        """The key is the ground radius, because that is what was measured
        with: turning the scale changes the answer as turning the width does."""
        _show(editor)
        held = editor._thin
        editor.scale_spin.setValue(editor.scale_spin.value() * 2)
        assert editor._thin is not held

    def test_the_layer_being_off_costs_a_redraw_nothing(self, editor):
        editor._redraw(preserve_view=True)
        assert editor._thin is None


class TestWorstPatch:

    def test_it_needs_a_patch_to_go_to(self, editor):
        assert not editor.btn_worst_patch.isEnabled()
        _show(editor)
        assert editor.btn_worst_patch.isEnabled()

    def test_it_frames_the_patch(self, editor):
        """A one-millimetre fault is a few pixels on a whole province."""
        _show(editor)
        patch = editor._thin.worst[0]
        editor.zoom_to_worst_patch()
        (x0, x1), (y0, y1) = editor.ax.get_xlim(), editor.ax.get_ylim()
        minx, miny, maxx, maxy = patch.bounds
        assert x0 < (minx + maxx) / 2 < x1
        assert y0 < (miny + maxy) / 2 < y1
        # Framed with its surroundings, not filling the view: a fault means
        # nothing without the land around it.
        assert (x1 - x0) > (maxx - minx)

    def test_it_says_what_it_went_to(self, editor):
        _show(editor)
        editor.zoom_to_worst_patch()
        assert "At the worst patch" in editor.status.text()
        assert "mm² of land under 1 mm" in editor.status.text()

    def test_it_goes_grey_on_a_clean_province(self, qtbot):
        window = _editor(qtbot, box(0, 0, 10 * MM, 10 * MM))
        window.width_spin.setValue(1.0)
        _show(window)
        assert not window.btn_worst_patch.isEnabled()


class TestItSitsBesideTheOtherLayers:

    @pytest.fixture
    def both(self, qtbot):
        """A province with thin land *and* a neck holding a lobe up."""
        window = _editor(qtbot, dumbbell())
        window.width_spin.setValue(1.0)
        _show(window)
        window.btn_stress.setChecked(True)
        window._toggle_stress(True)
        while window._stress_thread is not None:
            M.QApplication.processEvents()
        assert window._thin.patches, "the fixture must have thin land"
        assert window._stress.necks, "and a neck, or this tests one layer"
        return window

    def test_both_layers_can_be_on_at_once(self, both):
        """Two different claims about the same coast, and the viewer shows
        them together too."""
        assert both.btn_thin.isChecked() and both.btn_stress.isChecked()

    def test_the_thin_layer_is_drawn_under_the_stress_marks(self, both):
        """A neck at risk has to stay findable on a shore painted red end to
        end, so the narrower claim sits on top.

        Each layer is drawn alone onto a cleared axes and the two bands of
        zorder compared: the stress marks are a shaded region *and* lines, so
        comparing collections against lines would compare the wrong artists.
        """
        def band(draw):
            both.ax.clear()
            draw()
            artists = list(both.ax.collections) + list(both.ax.lines)
            assert artists, "the layer drew nothing to compare"
            return (min(a.get_zorder() for a in artists),
                    max(a.get_zorder() for a in artists))

        thin_lo, thin_hi = band(both._draw_thin)
        stress_lo, stress_hi = band(both._draw_stress)
        assert stress_lo > thin_hi
        both._redraw(preserve_view=True)

    def test_planning_locks_the_layer(self, editor):
        editor._set_planning(True)
        assert not editor.grp_thin.isEnabled()
        editor._set_planning(False)
        assert editor.grp_thin.isEnabled()

    def test_the_red_is_the_viewers_red(self):
        """The same fault marked the same colour in both windows."""
        import UI_province_viewer as V
        assert M.THIN_COLOUR == V.THIN_COLOUR
        assert M.THIN_ZOOM_SHARE == V.THIN_ZOOM_SHARE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
