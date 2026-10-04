"""
test_harness_viewport.py

Every harness panel keeps the size the window gives it, whatever shape the
imported data happens to be.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_harness_viewport.py -v

This is one test file across all the harnesses rather than a few lines in each
of their own, because the defect it guards is a *shared* one: they were all
written from the same skeleton, so they all had it, and a new harness copied
from that skeleton would have it again.

The defect.  matplotlib satisfies ``set_aspect("equal")`` in one of two ways.
The default, ``adjustable="box"``, keeps the limits and reshapes the axes box —
so a north-south province turned the panel into a vertical hairline, an
east-west one into a letterbox, and the window rearranged itself around
whatever file was imported last.  ``adjustable="datalim"`` keeps the box the
window gave it and widens the limits instead, which is what a map panel wants.

The catch, and why this is not a one-word change: matplotlib refuses
``datalim`` on axes joined by ``sharex``/``sharey`` — it raises at *draw* time,
not when the aspect is set, so a harness with both looks fine until something
is plotted.  The two-panel harnesses therefore link their panels by hand
(``_link_views``), and ``test_panels_are_not_shared`` is what stops anyone
putting ``sharex`` back.
"""

import matplotlib
import pytest

from harnesses import UI_border_regions, UI_border_stl, UI_butterfly_keys
from harnesses import UI_dovetail_fitter
from harnesses import UI_island_bridges, UI_island_merge, UI_island_removal
from harnesses import UI_lake_removal, UI_shared_border, UI_terrain_stl
from harnesses import UI_terrain_trim

pytestmark = pytest.mark.usefixtures("qapp")

#: Every harness that draws on matplotlib axes, with its window class.
#: UI_border_split is absent on purpose: both its panels are vispy scenes,
#: which fill the widget they are given and never resize themselves to the
#: mesh.
HARNESSES = [
    (UI_border_regions, "BorderRegionsTester"),
    (UI_border_stl, "BorderStlTester"),
    (UI_butterfly_keys, "ButterflyKeysTester"),
    (UI_dovetail_fitter, "DovetailFitterTester"),
    (UI_island_bridges, "IslandBridgesTester"),
    (UI_island_merge, "IslandMergeTester"),
    (UI_island_removal, "IslandRemovalTester"),
    (UI_lake_removal, "LakeRemovalTester"),
    (UI_shared_border, "SharedBorderTester"),
    (UI_terrain_stl, "TerrainStlTester"),
    (UI_terrain_trim, "TerrainTrimTester"),
]
IDS = [module.__name__.rsplit(".", 1)[-1] for module, _ in HARNESSES]


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Modal dialogs block forever headlessly; stub them for every test."""
    for module, _ in HARNESSES:
        box = getattr(module, "QMessageBox", None)
        if box is not None:
            monkeypatch.setattr(box, "critical", lambda *a, **k: None)
            monkeypatch.setattr(box, "warning", lambda *a, **k: None)


def _window(module, class_name):
    return getattr(module, class_name)()


def _map_axes(window):
    """The harness's matplotlib axes, in figure order."""
    return list(window.figure.axes)


def _boxes(window):
    """Each panel's position in the figure, as (width, height)."""
    return [(round(ax.get_position().width, 4),
             round(ax.get_position().height, 4))
            for ax in _map_axes(window)]


def _plot_shape(window, width, height):
    """Draw a box of the given shape into every panel, then lay the figure out."""
    for ax in _map_axes(window):
        ax.plot([0, width], [0, height])
    window.figure.canvas.draw()


@pytest.mark.parametrize("module,class_name", HARNESSES, ids=IDS)
class TestPanelSize:

    def test_panels_are_sized_by_the_window_not_the_data(self, module,
                                                         class_name):
        """A tall sliver and a wide letterbox must give the same panel."""
        tall = _window(module, class_name)
        _plot_shape(tall, 1, 1000)
        wide = _window(module, class_name)
        _plot_shape(wide, 1000, 1)
        assert _boxes(tall) == _boxes(wide)

    def test_the_panel_is_a_useful_size(self, module, class_name):
        """Not merely equal — the old bug gave equally *collapsed* panels.

        Under ``adjustable="box"`` a 1:1000 sliver came out at well under 1% of
        the figure width, so a test that only compared two shapes could pass on
        two hairlines.
        """
        window = _window(module, class_name)
        _plot_shape(window, 1, 1000)
        for width, height in _boxes(window):
            assert width > 0.1, "panel collapsed horizontally"
            assert height > 0.1, "panel collapsed vertically"

    def test_the_data_is_still_all_on_screen(self, module, class_name):
        """Widening the limits must grow the view, never crop it."""
        window = _window(module, class_name)
        _plot_shape(window, 1, 1000)
        for ax in _map_axes(window):
            (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
            assert x0 <= 0 and x1 >= 1
            assert y0 <= 0 and y1 >= 1000

    def test_every_panel_asks_for_datalim(self, module, class_name):
        window = _window(module, class_name)
        for ax in _map_axes(window):
            assert ax.get_aspect() == 1, "geometry must not be distorted"
            assert ax.get_adjustable() == "datalim"

    def test_panels_are_not_shared(self, module, class_name):
        """``sharex``/``sharey`` and a fixed box cannot coexist.

        matplotlib raises only at draw time, so this is checked directly rather
        than waiting for a harness to fail in front of someone.
        """
        window = _window(module, class_name)
        for ax in _map_axes(window):
            for axis in ("x", "y"):
                siblings = ax._shared_axes[axis].get_siblings(ax)
                assert len(list(siblings)) == 1, f"{axis} axis is shared"

    def test_geometry_keeps_its_shape(self, module, class_name):
        """A fixed box must not have been bought by distorting the map.

        Equal aspect means one data unit is the same length on both axes; that
        is the whole reason the panels cannot simply be told to fill the slot.
        """
        window = _window(module, class_name)
        _plot_shape(window, 1, 1000)
        for ax in _map_axes(window):
            (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
            pos = ax.get_position()
            fig_w, fig_h = window.figure.get_size_inches()
            units_per_inch_x = (x1 - x0) / (pos.width * fig_w)
            units_per_inch_y = (y1 - y0) / (pos.height * fig_h)
            assert units_per_inch_x == pytest.approx(units_per_inch_y, rel=1e-6)


class TestTwoPanelHarnessesStayAligned:
    """Hand-linking has to do what sharing did: both panels, one frame."""

    PAIRS = [(module, name) for module, name in HARNESSES
             if name in ("BorderRegionsTester", "IslandBridgesTester",
                         "IslandMergeTester", "IslandRemovalTester",
                         "LakeRemovalTester", "SharedBorderTester")]
    PAIR_IDS = [module.__name__.rsplit(".", 1)[-1] for module, _ in PAIRS]

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_the_harness_offers_a_link(self, module, class_name):
        window = _window(module, class_name)
        assert len(_map_axes(window)) == 2
        assert hasattr(window, "_link_views")

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_linking_carries_the_view_across(self, module, class_name):
        """Both panels end on one view.

        Not on the literal numbers asked for: a view that does not already fit
        the panel is settled to one that does, which is the whole point of the
        fixed box. What matters is that the two agree afterwards.
        """
        window = _window(module, class_name)
        window._set_view(window.ax_before, (100.0, 300.0), (-50.0, 50.0))
        assert window.ax_after.get_xlim() == window.ax_before.get_xlim()
        assert window.ax_after.get_ylim() == window.ax_before.get_ylim()

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_a_toolbar_drag_moves_both_panels(self, module, class_name):
        """Pan, zoom-to-rectangle and Home reach the axes, not ``_on_scroll``.

        Sharing used to carry those across for free. Unsharing for the fixed
        box gave that up, so the panels are linked by a limit callback instead
        — and ``ax.clear()`` throws the callbacks away, so this drags *after* a
        redraw, which is where a link connected only in ``__init__`` would
        already have been lost.
        """
        window = _window(module, class_name)
        _plot_shape(window, 1000, 1000)
        for panel in (window.ax_before, window.ax_after):
            other = (window.ax_after if panel is window.ax_before
                     else window.ax_before)
            panel.set_xlim(100.0, 300.0)            # what the toolbar does
            assert other.get_xlim() == panel.get_xlim()
            assert other.get_ylim() == panel.get_ylim()

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_linking_hands_over_a_view_that_already_fits(self, module,
                                                         class_name, caplog):
        """The copied view must satisfy "equal" before matplotlib sees it.

        ``datalim`` widens the before panel's limits *during* the draw, so a
        link that reads them beforehand hands the after panel a data-shaped
        view it then has to override — matplotlib logs "Ignoring fixed y limits
        to fulfill fixed data aspect" and picks the frame itself, which is
        precisely the control this whole change is about keeping.
        """
        window = _window(module, class_name)
        window.ax_before.plot([0, 1], [0, 1000])       # nothing like the panel
        window._link_views()
        with caplog.at_level("WARNING", logger="matplotlib"):
            window.figure.canvas.draw()
        assert "Ignoring fixed" not in caplog.text, caplog.text
        assert window.ax_before.get_xlim() == window.ax_after.get_xlim()
        assert window.ax_before.get_ylim() == window.ax_after.get_ylim()

    @staticmethod
    def _scroll(window, panel, button="up"):
        event = matplotlib.backend_bases.MouseEvent(
            "scroll_event", window.figure.canvas, x=0, y=0, button=button)
        event.inaxes = panel
        event.xdata, event.ydata = 50.0, 50.0
        window._on_scroll(event)

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_scrolling_either_panel_moves_both(self, module, class_name):
        window = _window(module, class_name)
        _plot_shape(window, 100, 100)
        for panel in (window.ax_before, window.ax_after):
            self._scroll(window, panel)
            assert window.ax_after.get_xlim() == window.ax_before.get_xlim()
            assert window.ax_after.get_ylim() == window.ax_before.get_ylim()

    @pytest.mark.parametrize("module,class_name", PAIRS, ids=PAIR_IDS)
    def test_scrolling_actually_zooms(self, module, class_name):
        """The wheel must change the view, not merely keep the panels equal.

        This is the failure the alignment test above cannot see. Linking on
        each limit change means a wheel zoom fires the link once on x, where
        settling the aspect against a y that has not moved yet pulls x back to
        exactly where it started — two panels in perfect agreement, showing the
        same unchanged view, with the wheel doing nothing at all.
        """
        window = _window(module, class_name)
        _plot_shape(window, 100, 100)
        span = lambda: (window.ax_before.get_xlim()[1]
                        - window.ax_before.get_xlim()[0])
        before = span()
        self._scroll(window, window.ax_before, "up")
        assert span() == pytest.approx(before / 1.2, rel=1e-9)
        self._scroll(window, window.ax_before, "down")
        assert span() == pytest.approx(before, rel=1e-9)
