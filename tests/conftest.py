"""
conftest.py

Shared fixtures for the test suite.

The one that has to be here rather than in a test file is ``_local_config``:
windows now write settings back to ``project_config.local.json`` as they are
turned (see ``UI_island_editor.SAVED_SETTINGS``), and a test that sets a spin
box would otherwise rewrite the config of whoever ran the suite -- a print
scale of 1:200 000 left behind by ``test_island_editor_scale.py``, a 0.05 mm
Min width left by the Printability tests.

It redirects the *local* file to a copy in the test's own tmp directory, so
reads see exactly what the real file says and writes go nowhere near it.  A
copy rather than an empty file on purpose: this desk's local config carries
real overrides -- ``stress.min_lobe_mm2`` is 15.0 against the shipped 25.0 --
and a suite that measured against different numbers from the windows it is
testing would be testing something nobody runs.
"""

import shutil

import pytest

from lib import LIB_project_config as config


@pytest.fixture(autouse=True)
def _local_config(tmp_path, monkeypatch):
    """Point the writable config at a throwaway copy, for every test."""
    # Deliberately *not* tmp_path/"project_config.local.json": that is the
    # name test_screen_calibration.py's own fixture gives the file it wants
    # empty, in the same tmp_path, and a copy of the real config sitting at
    # that path handed it this desk's two monitors to be surprised by.
    sandbox = tmp_path / "sandboxed_config.local.json"
    try:
        shutil.copyfile(config.LOCAL_CONFIG_PATH, sandbox)
    except OSError:
        pass                    # absent is fine: load() treats it as empty
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", str(sandbox))
    return sandbox
