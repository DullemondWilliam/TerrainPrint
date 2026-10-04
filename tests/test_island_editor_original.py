"""
test_island_editor_original.py

The **Original** layer of UI_island_editor.py — the province as it was
imported, laid over the one being edited at a chosen opacity.

These are pytest-style (not unittest) because they need pytest-qt's ``qapp`` /
``qtbot`` fixtures, as in test_island_editor_stress.py.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_island_editor_original.py -v

What matters about this layer is which shape it draws and where in the stack:

* the **imported** shape, which undo walks back to and saving does not move —
  not the last saved state and not the state when the layer was switched on;
* over the islands, so the tint reads on the land you have kept and the land
  you have added is the part without it;
* under the thin and stress marks, which are claims about the land as it is
  *now* and must not be dimmed by a picture of how it was.
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


def two_islands():
    """A body and a thin spit, so Amputate has something to take."""
    body = box(0, 0, 10 * MM, 10 * MM)
    spit = box(10 * MM, 4.8 * MM, 16 * MM, 5.2 * MM)
    return unary_union([body, spit])


def dumbbell(throat_mm=0.2, lobe_mm=10.0, bar_mm=3.0):
    """Two lobes on a bar: a shape with a neck, which ``two_islands`` has not.

    The spit there holds 2.4 mm² of land, below ``stress.min_lobe_mm2``, so the
    stress layer finds nothing on it and a test about the two layers together
    would be testing one layer.
    """
    left = box(0, 0, lobe_mm * MM, lobe_mm * MM)
    right = box((lobe_mm + bar_mm) * MM, 0,
                (2 * lobe_mm + bar_mm) * MM, lobe_mm * MM)
    middle = lobe_mm / 2.0
    bar = box(lobe_mm * MM, (middle - throat_mm / 2.0) * MM,
              (lobe_mm + bar_mm) * MM, (middle + throat_mm / 2.0) * MM)
    return unary_union([left, right, bar])


def _loaded(qtbot, geom):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(geom)
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    window.width_spin.setValue(1.0)
    return window


@pytest.fixture
def editor(qtbot):
    window = M.IslandEditor()
    qtbot.addWidget(window)
    window.resize(900, 600)
    window.show()
    qtbot.waitExposed(window)
    doc = M._as_multi(two_islands())
    window.original_doc = doc
    window._set_state(doc, [], preserve_view=False)
    window.width_spin.setValue(1.0)
    return window


def _show(window):
    window.btn_original.setChecked(True)
    window._toggle_original(True)


def ghosts(window):
    """The artists drawn by the Original layer, by their opacity."""
    return [c for c in window.ax.collections
            if c.get_alpha() is not None and c.get_alpha() < 1.0]


class TestShowingIt:

    def test_it_needs_a_document(self, qtbot):
        window = M.IslandEditor()
        qtbot.addWidget(window)
        assert not window.btn_original.isEnabled()

    def test_it_is_offered_once_one_is_open(self, editor):
        """Enabled on the group *and* on the button: a group that is live with
        a dead button in it is a control nobody can press."""
        assert editor.grp_original.isEnabled()
        assert editor.btn_original.isEnabled()

    def test_nothing_is_drawn_until_it_is_asked_for(self, editor):
        assert ghosts(editor) == []

    def test_showing_draws_one_artist(self, editor):
        """A province is up to 223 parts and this is a backdrop, not something
        to pick out of."""
        _show(editor)
        assert len(ghosts(editor)) == 1

    def test_taking_it_off_takes_the_artist(self, editor):
        _show(editor)
        editor.btn_original.setChecked(False)
        editor._toggle_original(False)
        assert ghosts(editor) == []


class TestTheShapeItDraws:

    def test_it_is_the_imported_shape_not_the_edited_one(self, editor):
        """The point of the layer: what is under the tint has changed, what
        the tint draws has not."""
        _show(editor)
        before = ghosts(editor)[0].get_paths()
        editor.amputate()                      # the spit goes
        assert editor.doc is not editor.original_doc
        after = ghosts(editor)[0].get_paths()
        assert len(before) == len(after)
        assert (sum(len(p.vertices) for p in before)
                == sum(len(p.vertices) for p in after))

    def test_it_still_draws_the_import_after_an_undo(self, editor):
        _show(editor)
        editor.amputate()
        editor.undo_stack.undo()
        assert editor.doc is editor.original_doc
        assert len(ghosts(editor)) == 1


class TestTheOpacity:

    def test_the_slider_sets_the_alpha(self, editor):
        _show(editor)
        editor.opacity_slider.setValue(75)
        assert ghosts(editor)[0].get_alpha() == pytest.approx(0.75)

    def test_the_percentage_follows_it(self, editor):
        editor.opacity_slider.setValue(75)
        assert editor.lbl_opacity.text() == "75%"

    def test_it_reads_out_before_the_layer_is_ever_shown(self, editor):
        """The box says what the slider is set to from the first draw."""
        assert editor.lbl_opacity.text().endswith("%")

    def test_the_default_is_a_ghost_and_not_a_layer(self, editor):
        """Enough to see where the coast has moved, not so much that it hides
        what it is over."""
        assert 0 < M.ORIGINAL_OPACITY[2] < 100

    def test_turning_it_with_the_layer_off_draws_nothing(self, editor):
        editor.opacity_slider.setValue(90)
        assert ghosts(editor) == []


class TestWhereItSits:

    def test_it_is_over_the_islands(self, editor):
        """So the tint reads on the land you kept, and the land you added is
        the part without it."""
        _show(editor)
        island_z = max(c.get_zorder() for c in editor.ax.collections
                       if c.get_alpha() is None)
        assert ghosts(editor)[0].get_zorder() > island_z

    def test_it_is_under_the_thin_marks(self, editor):
        """Those are a claim about the land as it is now, and must not be
        dimmed by a picture of how it was."""
        _show(editor)
        editor.btn_thin.setChecked(True)
        editor._toggle_thin(True)
        assert editor._thin.patches, "the fixture must have thin land"
        thin_z = max(c.get_zorder() for c in editor.ax.collections
                     if c.get_alpha() is None)
        assert ghosts(editor)[0].get_zorder() < thin_z

    def test_it_is_under_the_stress_marks(self, qtbot):
        window = _loaded(qtbot, dumbbell())
        _show(window)
        window.btn_stress.setChecked(True)
        window._toggle_stress(True)
        qtbot.waitUntil(lambda: window._stress_thread is None, timeout=60_000)
        assert window._stress.necks, "the fixture must have a neck"
        marks = [a.get_zorder() for a in window.ax.lines if a.get_zorder() > 1]
        assert marks, "the neck must have been drawn"
        assert ghosts(window)[0].get_zorder() < min(marks)

    def test_a_bridge_plan_does_not_grey_it_out(self, editor):
        """Unlike the two groups under it: the shape this draws is the
        imported one, which is the one thing a plan cannot invalidate."""
        editor._set_planning(True)
        assert editor.grp_original.isEnabled()
        assert not editor.grp_thin.isEnabled()
        editor._set_planning(False)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
