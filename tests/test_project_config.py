"""
test_project_config.py

The project's magic numbers: defaults, the tracked file that overrides them, and
the gitignored local file that overrides that.

Run with:  python -m pytest tests/test_project_config.py -v

The thing worth pinning here is that nothing in the chain is allowed to be
load-bearing. A config is a convenience: if the file is missing, unreadable, or
half-written, the pipeline must come up on its built-in numbers rather than fall
over -- these are the values every tool reads at import time, so a raise here
takes the whole program with it.
"""

import json

import pytest

from lib import LIB_project_config as P


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Redirect both config files into a throwaway directory."""
    project = tmp_path / "project_config.json"
    local = tmp_path / "project_config.local.json"
    monkeypatch.setattr(P, "CONFIG_PATH", str(project))
    monkeypatch.setattr(P, "LOCAL_CONFIG_PATH", str(local))
    return project, local


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestTheDefaultsAlwaysAnswer:
    """No file has to exist for the project to have numbers."""

    def test_with_no_files_at_all(self, files):
        assert P.get("print.scale") == 8_000_000
        assert P.get("crs.working") == "EPSG:3347"

    @pytest.mark.parametrize("junk", ["{ not json", "", "[1, 2, 3]", "null"])
    def test_with_an_unusable_file(self, files, junk):
        project, _ = files
        project.write_text(junk, encoding="utf-8")
        assert P.get("print.scale") == 8_000_000

    def test_with_a_directory_where_the_file_should_be(self, tmp_path,
                                                       monkeypatch):
        monkeypatch.setattr(P, "CONFIG_PATH", str(tmp_path))
        assert P.get("print.scale") == 8_000_000

    def test_an_unknown_key_answers_with_the_caller_s_default(self, files):
        assert P.get("print.nonesuch", 42) == 42
        assert P.get("nonesuch.deeper.still") is None

    def test_a_partial_file_only_overrides_what_it_names(self, files):
        """Setting one number must not blank every number beside it."""
        project, _ = files
        _write(project, {"print": {"scale": 4_000_000}})
        assert P.get("print.scale") == 4_000_000
        assert P.get("print.bed_mm") == 256.0
        assert P.get("crs.working") == "EPSG:3347"


class TestTheFilesOverrideInOrder:
    """Defaults, then the tracked file, then the local one."""

    def test_the_project_file_beats_the_defaults(self, files):
        project, _ = files
        _write(project, {"print": {"scale": 2_000_000}})
        assert P.get("print.scale") == 2_000_000

    def test_the_local_file_beats_the_project_file(self, files):
        project, local = files
        _write(project, {"print": {"scale": 2_000_000}})
        _write(local, {"print": {"scale": 3_000_000}})
        assert P.get("print.scale") == 3_000_000

    def test_a_local_override_leaves_its_neighbours_alone(self, files):
        project, local = files
        _write(project, {"print": {"scale": 2_000_000, "bed_mm": 300.0}})
        _write(local, {"print": {"bed_mm": 180.0}})
        assert P.get("print.bed_mm") == 180.0
        assert P.get("print.scale") == 2_000_000

    def test_a_value_can_be_a_whole_branch(self, files):
        _, local = files
        _write(local, {"screen": {"monitors": {"X@1x1": {"ppi": 99.0}}}})
        assert P.get("screen.monitors")["X@1x1"]["ppi"] == 99.0


class TestWritingLocalValues:
    """Machine-specific findings go to the local file, never the tracked one."""

    def test_it_writes_where_it_says(self, files):
        project, local = files
        P.update_local("screen.monitors", {"X@1x1": {"ppi": 99.0}})
        assert not project.exists()
        assert json.loads(local.read_text())["screen"]["monitors"]

    def test_it_reads_back_through_get(self, files):
        P.update_local("print.scale", 1_234_567)
        assert P.get("print.scale") == 1_234_567

    def test_it_keeps_what_was_already_local(self, files):
        P.update_local("screen.monitors", {"X@1x1": {"ppi": 99.0}})
        P.update_local("print.scale", 500_000)
        assert P.get("screen.monitors")["X@1x1"]["ppi"] == 99.0
        assert P.get("print.scale") == 500_000

    def test_it_builds_the_branch_it_needs(self, files):
        P.update_local("a.deeply.nested.key", "here")
        assert P.get("a.deeply.nested.key") == "here"

    def test_an_unwritable_local_file_is_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(P, "LOCAL_CONFIG_PATH", str(tmp_path))  # a directory
        P.update_local("print.scale", 1)                            # must not raise


class TestTheShippedFileAgreesWithTheDefaults:
    """
    The JSON in the repo and the DEFAULTS dict carry the same keys deliberately,
    so the file can be read as documentation and the dict as its fallback. They
    drift the moment one is edited alone, which is what this catches.
    """

    def test_every_shipped_key_is_a_known_one(self):
        shipped = P._read(P.CONFIG_PATH)

        def walk(node, default, trail=""):
            for key, value in node.items():
                here = f"{trail}.{key}".lstrip(".")
                assert key in default, f"{here} is in the file but not DEFAULTS"
                if isinstance(value, dict):
                    walk(value, default[key], here)

        walk(shipped, P.DEFAULTS)

    def test_the_two_agree_on_the_print_scale(self):
        """The number this whole pipeline turns on, in both places."""
        assert P._read(P.CONFIG_PATH)["print"]["scale"] == \
            P.DEFAULTS["print"]["scale"]
