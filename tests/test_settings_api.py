"""
test_settings_api.py

Settings reach the libraries through their APIs, in a structure.

Every library that has tunable settings exposes them as a dataclass whose
defaults are the **shipped** values, fixed in code, and a ``load_*_params``
function that fills that structure from config files.  This file holds every
one of them to the same contract, so a library cannot quietly go back to
reading the config on its own:

* built directly, the structure is the shipped settings -- whatever the config
  files on this desk say;
* loaded, each field comes from its config key, later files winning, and a key
  no file mentions keeps the shipped value;
* an override passed to the loader wins over every file.

Run with:  ./.venv/Scripts/python.exe -m pytest tests/test_settings_api.py -v
"""

import dataclasses
import json

import pytest

from lib import LIB_project_config as config
from lib.LIB_border_connectors import ConnectorParams, load_connector_params
from lib.LIB_border_split import BedParams, load_bed_params
from lib.LIB_island_bridges import BridgeParams, load_bridge_params
from lib.LIB_shape_scores import ScoreParams, load_score_params
from lib.LIB_shared_border import SeamParams, load_seam_params
from lib.LIB_stress_points import StressParams, load_stress_params
from lib.LIB_terrain_stl import TerrainParams, load_terrain_params
from lib.LIB_thin_features import ThinParams, load_thin_params

import lib.LIB_border_connectors as connectors
import lib.LIB_border_split as split
import lib.LIB_island_bridges as bridges
import lib.LIB_shape_scores as scores
import lib.LIB_shared_border as shared
import lib.LIB_stress_points as stress
import lib.LIB_terrain_stl as terrain
import lib.LIB_thin_features as thin

#: (structure, loader, its key map) for every library with settings.
LIBRARIES = [
    pytest.param(ThinParams, load_thin_params, thin._THINPARAMS_KEYS,
                 id="thin"),
    pytest.param(BridgeParams, load_bridge_params, bridges._BRIDGEPARAMS_KEYS,
                 id="bridges"),
    pytest.param(SeamParams, load_seam_params, shared._SEAMPARAMS_KEYS,
                 id="seam"),
    pytest.param(ScoreParams, load_score_params, scores._SCOREPARAMS_KEYS,
                 id="scores"),
    pytest.param(StressParams, load_stress_params, stress._STRESSPARAMS_KEYS,
                 id="stress"),
    pytest.param(ConnectorParams, load_connector_params,
                 connectors._CONNECTORPARAMS_KEYS, id="connectors"),
    pytest.param(TerrainParams, load_terrain_params,
                 terrain._TERRAINPARAMS_KEYS, id="terrain"),
    pytest.param(BedParams, load_bed_params, split._BEDPARAMS_KEYS, id="bed"),
]


def _changed(value):
    """A value of the same kind, different from ``value``."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 3
    if isinstance(value, float):
        return value * 1.5 + 0.25
    if isinstance(value, str):
        return "EPSG:3857" if value != "EPSG:3857" else "EPSG:4326"
    raise TypeError(value)


def _write(path, flat):
    """Write ``{"a.b": v}`` as nested JSON."""
    nested = {}
    for dotted, value in flat.items():
        node = nested
        *parents, leaf = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    path.write_text(json.dumps(nested), encoding="utf-8")
    return path


def _every_key_changed(key_map):
    return {dotted: _changed(config.shipped(dotted))
            for dotted in key_map.values()}


@pytest.mark.parametrize("cls, loader, key_map", LIBRARIES)
class TestEveryLibrary:

    def test_every_field_is_mapped_to_a_real_key(self, cls, loader, key_map):
        names = {f.name for f in dataclasses.fields(cls)}
        for field_name, dotted in key_map.items():
            assert field_name in names, f"{cls.__name__} has no {field_name}"
            assert config.shipped(dotted) is not None, f"no shipped {dotted}"

    def test_built_directly_it_is_the_shipped_settings(self, cls, loader,
                                                       key_map):
        built = cls()
        for field_name, dotted in key_map.items():
            assert getattr(built, field_name) == config.shipped(dotted)

    def test_built_directly_it_ignores_the_local_file(self, cls, loader,
                                                      key_map, _fixed_config):
        """The whole point: a desk's saved settings never reach a library
        that was not handed them."""
        _write(_fixed_config, _every_key_changed(key_map))
        built = cls()
        for field_name, dotted in key_map.items():
            assert getattr(built, field_name) == config.shipped(dotted)

    def test_loaded_every_field_comes_from_the_file(self, cls, loader,
                                                    key_map, tmp_path):
        values = _every_key_changed(key_map)
        loaded = loader(_write(tmp_path / "settings.json", values))
        for field_name, dotted in key_map.items():
            assert getattr(loaded, field_name) == pytest.approx(values[dotted])

    def test_a_key_no_file_mentions_keeps_the_shipped_value(self, cls, loader,
                                                            key_map, tmp_path):
        loaded = loader(_write(tmp_path / "empty.json", {}))
        assert loaded == cls()

    def test_later_files_win(self, cls, loader, key_map, tmp_path):
        field_name, dotted = next(iter(key_map.items()))
        shipped = config.shipped(dotted)
        first = _write(tmp_path / "first.json", {dotted: _changed(shipped)})
        second = _write(tmp_path / "second.json",
                        {dotted: _changed(_changed(shipped))})
        assert getattr(loader(first, second), field_name) == \
            pytest.approx(_changed(_changed(shipped)))
        assert getattr(loader(second, first), field_name) == \
            pytest.approx(_changed(shipped))

    def test_an_override_beats_every_file(self, cls, loader, key_map,
                                          tmp_path):
        field_name, dotted = next(iter(key_map.items()))
        path = _write(tmp_path / "settings.json",
                      {dotted: _changed(config.shipped(dotted))})
        wanted = _changed(_changed(_changed(config.shipped(dotted))))
        assert getattr(loader(path, **{field_name: wanted}), field_name) == \
            pytest.approx(wanted)

    def test_a_missing_file_costs_nothing(self, cls, loader, key_map,
                                          tmp_path):
        assert loader(tmp_path / "not-there.json") == cls()


class TestProjectConfig:

    def test_shipped_reads_no_file(self, _fixed_config):
        _write(_fixed_config, {"thin.width_mm": 0.123})
        assert config.shipped("thin.width_mm") == \
            config.DEFAULTS["thin"]["width_mm"]
        assert config.get("thin.width_mm") == 0.123

    def test_settings_files_follow_the_paths_when_called(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "p.json"))
        monkeypatch.setattr(config, "LOCAL_CONFIG_PATH",
                            str(tmp_path / "l.json"))
        assert config.settings_files() == (str(tmp_path / "p.json"),
                                           str(tmp_path / "l.json"))

    def test_load_files_layers_over_the_defaults(self, tmp_path):
        first = _write(tmp_path / "a.json", {"thin.width_mm": 2.0,
                                             "print.scale": 4000000})
        second = _write(tmp_path / "b.json", {"thin.width_mm": 3.0})
        merged = config.load_files(first, second)
        assert config.get("thin.width_mm", config=merged) == 3.0
        assert config.get("print.scale", config=merged) == 4000000
        assert config.get("seam.nozzle_mm", config=merged) == \
            config.shipped("seam.nozzle_mm")

    def test_load_files_with_none_is_the_defaults(self):
        assert config.load_files() == config.DEFAULTS
        assert config.load_files(None) == config.DEFAULTS


class TestNoLibraryReadsTheConfigItself:
    """A guard against the old habit creeping back: no library module calls
    the file-reading ``get`` at import or in a default."""

    @pytest.mark.parametrize("module", [thin, bridges, shared, scores, stress,
                                        connectors, terrain, split])
    def test_no_bare_config_reads(self, module):
        import inspect
        source = inspect.getsource(module)
        assert "import get as _cfg" not in source
        assert "_from_config(" not in source
