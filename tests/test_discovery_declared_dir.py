"""A discovered app whose cwd holds its own manifest gets `{X_DIR}` filled even
when its bridge is a module rather than a script path under that dir."""
from types import SimpleNamespace

from src import connector_discovery, connectors, plugins


def _preset():
    preset = connectors.get_preset("nightingale")
    assert preset is not None
    return preset


def test_declared_manifest_in_cwd_fills_the_dir_placeholder(tmp_path, monkeypatch):
    preset = _preset()
    monkeypatch.setattr(plugins, "read_app_manifest",
                        lambda path: SimpleNamespace(id=preset.id) if str(path) == str(tmp_path) else None)
    values = connector_discovery.suggested_values(preset, "http://127.0.0.1:5189", str(tmp_path))
    key = connector_discovery._dir_placeholder(preset)
    assert values[key] == str(tmp_path)


def test_a_manifest_for_another_app_does_not_count(tmp_path, monkeypatch):
    preset = _preset()
    monkeypatch.setattr(plugins, "read_app_manifest", lambda path: SimpleNamespace(id="someone-else"))
    values = connector_discovery.suggested_values(preset, "http://127.0.0.1:5189", str(tmp_path))
    assert connector_discovery._dir_placeholder(preset) not in values
