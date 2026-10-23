"""
test_border_split_ui.py

The ``harnesses/UI_border_split.py`` window, headlessly: import a bar, split
it to the bed, save the pieces -- and the paths that refuse.

Both panels are vispy scenes, which build offscreen with GL-context warnings
but build; the split runs on the worker directly rather than on a thread.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_border_split_ui.py -v
"""

import pytest
import trimesh

from harnesses import UI_border_split as U

pytestmark = pytest.mark.usefixtures("qapp")

#: A 300 mm bar: one piece on a 320 mm bed, three or more on a 120 mm one.
BAR = trimesh.creation.box((300.0, 20.0, 2.0))


@pytest.fixture
def window(qtbot, monkeypatch):
    win = U.BorderSplitTester()
    qtbot.addWidget(win)
    win.dialogs = []
    for kind in ("critical", "warning"):
        monkeypatch.setattr(U.QMessageBox, kind,
                            lambda *a, kind=kind: win.dialogs.append(
                                (kind, a[1])))
    return win


def _import(window, monkeypatch, path):
    monkeypatch.setattr(U.QFileDialog, "getOpenFileName",
                        lambda *a, **k: (str(path), ""))
    window.import_stl()


def _split(window, bed):
    """``split`` without its thread: the same worker, run here."""
    window.bed_w_spin.setValue(bed)
    window.bed_d_spin.setValue(bed)
    window.margin_spin.setValue(0.0)
    worker = U.SplitWorker(window.source, (bed, bed), 0.0)
    worker.done.connect(window._on_split)
    worker.failed.connect(window._on_split_failed)
    worker.run()


@pytest.fixture
def bar_path(tmp_path):
    path = tmp_path / "bar.stl"
    BAR.export(path)
    return path


def test_it_opens_with_nothing_to_do(window):
    assert not window.btn_split.isEnabled()
    assert not window.btn_save.isEnabled()


def test_import_enables_split(window, monkeypatch, bar_path):
    _import(window, monkeypatch, bar_path)
    assert window.source is not None
    assert window.btn_split.isEnabled() and not window.btn_save.isEnabled()
    assert window._before_meshes


def test_split_then_save(window, monkeypatch, bar_path, tmp_path):
    _import(window, monkeypatch, bar_path)
    _split(window, 120.0)
    assert len(window.pieces) >= 3
    assert all(p.is_watertight for p in window.pieces)
    assert window.btn_save.isEnabled()
    assert len(window._after_meshes) == len(window.pieces)

    out = tmp_path / "pieces"
    out.mkdir()
    monkeypatch.setattr(U.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: str(out))
    window.save_pieces()
    saved = sorted(out.glob("bar_*.stl"))
    assert len(saved) == len(window.pieces)
    assert saved[0].name == "bar_001.stl"


def test_a_bar_that_fits_stays_whole(window, monkeypatch, bar_path):
    _import(window, monkeypatch, bar_path)
    _split(window, 320.0)
    assert len(window.pieces) == 1


def test_changing_the_bed_drops_the_split(window, monkeypatch, bar_path):
    _import(window, monkeypatch, bar_path)
    _split(window, 120.0)
    window.bed_w_spin.setValue(200.0)
    assert window.pieces is None and not window.btn_save.isEnabled()
    assert window.status.text().startswith("Parameters changed")


def test_a_failed_split_is_reported(window, monkeypatch, bar_path):
    _import(window, monkeypatch, bar_path)
    _split(window, 0.0)                          # no usable bed
    assert window.dialogs[-1] == ("critical", "Split failed")
    assert window.status.text().startswith("Split failed")


def test_an_unreadable_file_is_reported(window, monkeypatch, tmp_path):
    bad = tmp_path / "bad.stl"
    bad.write_text("solid nothing\nendsolid\n", encoding="utf-8")
    _import(window, monkeypatch, bad)
    assert window.source is None
    assert window.dialogs and window.dialogs[-1][0] in ("critical", "warning")


def test_cancelling_the_dialogs_changes_nothing(window, monkeypatch,
                                                bar_path):
    _import(window, monkeypatch, "")
    assert window.source is None
    _import(window, monkeypatch, bar_path)
    _split(window, 120.0)
    monkeypatch.setattr(U.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: "")
    window.save_pieces()
    assert window.status.text().startswith("Split into")
