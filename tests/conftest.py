"""
conftest.py

Shared fixtures for the test suite.

The one that has to be here rather than in a test file is ``_fixed_config``:
every test runs against the **same, fixed settings**, whoever runs it and
whatever their desk has saved.

It used to copy this desk's ``project_config.local.json`` into each test, on
the argument that a suite measuring against different numbers from the windows
was testing something nobody runs.  The cost was a suite whose result depended
on the desk: 25 tests failed here once the local file said a thin-land width
of 0.8 mm, because they had been written against the shipped 1.0 mm -- not
because anything was broken.  So now:

* the **project** file is replaced by a copy of the built-in
  ``LIB_project_config.DEFAULTS`` -- the shipped settings, fixed in code, so a
  change to the tracked JSON does not quietly change what the tests measure;
* the **local** file is an empty one in the test's own tmp directory, which
  also keeps the windows' saves (they write settings back as they are turned)
  away from the real file.

The libraries never read either file themselves -- their settings arrive
through their APIs -- so this governs the windows, which load settings through
``LIB_project_config.settings_files()`` and the libraries' ``load_*_params``.
"""

import json

import pytest

from lib import LIB_project_config as config


@pytest.fixture(autouse=True)
def _fixed_config(tmp_path, monkeypatch):
    """Point both config files at fixed, throwaway copies, for every test."""
    # Deliberately *not* tmp_path/"project_config.local.json": that is the
    # name test_screen_calibration.py's own fixture gives the file it wants
    # empty, in the same tmp_path.
    project = tmp_path / "fixed_config.json"
    project.write_text(json.dumps(config.DEFAULTS, indent=2), encoding="utf-8")
    local = tmp_path / "sandboxed_config.local.json"
    local.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(project))
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", str(local))
    return local
