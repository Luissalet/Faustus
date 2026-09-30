import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
from types import SimpleNamespace

import pytest
from src import plugin_marketplace as market


def manifest(plugin_id="fixture"):
    return {"schema": 1, "id": plugin_id, "name": "Synthetic plugin", "purpose": "Fixture only", "mcp": {"command": "never-executed"}}


@pytest.fixture
def environment(tmp_path):
    root, data = tmp_path / "checkout", tmp_path / "state"
    folder = root / "plugins" / "fixture"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps(manifest()))
    catalog = root / "plugins" / "marketplace.json"
    catalog.write_text(json.dumps({"schema": 1, "plugins": [{"id": "fixture", "repository_url": "https://github.com/synthetic/fixture.git", "required": True}]}))
    return root, data, {"repo_root": root, "data_dir": data}


def source(tmp_path, plugin_id="fixture"):
    path = tmp_path / "existing-source"
    path.mkdir()
    (path / "faustus-plugin.json").write_text(json.dumps(manifest(plugin_id)))
    (path / "keep.txt").write_text("dirty local work")
    return path


def test_list_and_validated_link_unlink_preserve_source(environment, tmp_path):
    root, data, options = environment
    row = market.list_marketplace(**options)["plugins"][0]
    assert row["required"] and row["can_install"] and row["state"] == "not_installed"
    path = source(tmp_path)
    linked = market.link("fixture", path, **options)
    assert linked["state"] == "linked" and not linked["can_install"]
    assert linked["local_path"] == str(path.resolve())
    assert (data / "plugin-marketplace-links.json").exists()
    market.unlink("fixture", **options)
    assert path.joinpath("keep.txt").read_text() == "dirty local work"
    assert market.list_marketplace(**options)["plugins"][0]["state"] == "not_installed"
    assert root.joinpath("plugins/fixture/plugin.json").exists()


@pytest.mark.parametrize("kind", ["arbitrary", "wrong_manifest", "bad_manifest", "wrong_remote"])
def test_invalid_link_never_writes_registry(environment, tmp_path, monkeypatch, kind):
    _, data, options = environment
    path = tmp_path / "invalid"
    path.mkdir()
    if kind == "wrong_manifest":
        (path / "faustus-plugin.json").write_text(json.dumps(manifest("other")))
    elif kind == "bad_manifest":
        (path / "faustus-plugin.json").write_text("{broken")
    elif kind == "wrong_remote":
        (path / ".git").mkdir()
        monkeypatch.setattr(market, "_git", lambda args: "https://github.com/synthetic/other")
    with pytest.raises(market.MarketplaceError):
        market.link("fixture", path, **options)
    assert not (data / "plugin-marketplace-links.json").exists()


def test_legacy_git_origin_matches_without_moving(environment, tmp_path, monkeypatch):
    _, _, options = environment
    path = tmp_path / "legacy"
    path.mkdir()
    (path / ".git").mkdir()
    monkeypatch.setattr(market, "_git", lambda args: "https://GitHub.com/Synthetic/Fixture/")
    assert market.link("fixture", path, **options)["state"] == "linked"
    assert path.exists()


def test_clone_publishes_only_valid_source_and_preserves_manifest(environment, monkeypatch):
    root, _, options = environment
    original = (root / "plugins/fixture/plugin.json").read_bytes()
    calls = []
    def git(args):
        calls.append(args)
        stage = Path(args[-1])
        (stage / "faustus-plugin.json").write_text(json.dumps(manifest()))
        return ""
    monkeypatch.setattr(market, "_git", git)
    row = market.install("fixture", **options)
    assert row["state"] == "cloned"
    assert row["local_path"] == row["clone_path"] and row["source_kind"] == "cloned"
    assert calls[0][:2] == ["clone", "--"]
    assert (root / "plugins/fixture/plugin.json").read_bytes() == original
    assert not list((root / "plugins/fixture").glob(".repository-stage-*"))
    with pytest.raises(market.MarketplaceError, match="Existing"):
        market.install("fixture", **options)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["git_failed", "git_timeout", "invalid_identity"])
def test_failed_clone_cleans_only_owned_stage(environment, monkeypatch, failure):
    root, _, options = environment
    def git(args):
        stage = Path(args[-1])
        (stage / "partial.txt").write_text("partial")
        if failure != "invalid_identity":
            raise market.MarketplaceError(failure, "Synthetic failure")
        (stage / "faustus-plugin.json").write_text(json.dumps(manifest("other")))
    monkeypatch.setattr(market, "_git", git)
    with pytest.raises(market.MarketplaceError):
        market.install("fixture", **options)
    assert not (root / "plugins/fixture/repository").exists()
    assert not list((root / "plugins/fixture").glob(".repository-stage-*"))
    assert (root / "plugins/fixture/plugin.json").exists()


def test_existing_dirty_target_is_never_touched(environment, monkeypatch):
    root, _, options = environment
    target = root / "plugins/fixture/repository"
    target.mkdir()
    (target / "dirty").write_text("keep")
    monkeypatch.setattr(market, "_git", lambda args: pytest.fail("Git must not run"))
    with pytest.raises(market.MarketplaceError) as error:
        market.install("fixture", **options)
    assert error.value.code == "already_present"
    assert (target / "dirty").read_text() == "keep"


def test_git_transport_has_no_shell_timeout_or_secret_error(monkeypatch):
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="secret remote password")
    monkeypatch.setattr(market.subprocess, "run", run)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_DIR", "unrelated")
    with pytest.raises(market.MarketplaceError) as error:
        market._git(["clone", "--", "https://github.com/synthetic/fixture", "temporary"])
    assert "secret" not in str(error.value)
    args, kwargs = seen[0]
    assert kwargs["shell"] is False and kwargs["timeout"] == market.GIT_TIMEOUT_S
    assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == market.os.devnull
    assert "GIT_CONFIG_COUNT" not in kwargs["env"] and "GIT_DIR" not in kwargs["env"]


@pytest.mark.parametrize("url", ["file:///local", "https://user:secret@example.com/repo", "https://example.com/repo?token=secret", "-option"])
def test_catalog_refuses_untrusted_git_urls(environment, url):
    root, _, options = environment
    (root / "plugins/marketplace.json").write_text(json.dumps({"schema": 1, "plugins": [{"id": "fixture", "repository_url": url}]}))
    with pytest.raises(market.MarketplaceError):
        market.list_marketplace(**options)


def test_broken_registry_is_not_overwritten(environment, tmp_path):
    _, data, options = environment
    data.mkdir()
    registry = data / "plugin-marketplace-links.json"
    registry.write_text("{broken")
    with pytest.raises(market.MarketplaceError):
        market.link("fixture", source(tmp_path), **options)
    assert registry.read_text() == "{broken"


def test_cli_bootstrap_defaults_to_required_and_never_launches(environment, monkeypatch, capsys):
    root, data, options = environment
    cli_path = Path(__file__).resolve().parents[1] / "scripts/plugin-marketplace.py"
    spec = importlib.util.spec_from_file_location("market_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    rows = [{"id": "required", "required": True, "can_install": True}, {"id": "optional", "required": False, "can_install": True}]
    monkeypatch.setattr(market, "list_marketplace", lambda **kw: {"plugins": rows})
    installed = []
    monkeypatch.setattr(market, "install", lambda plugin_id, **kw: installed.append(plugin_id) or {"id": plugin_id})
    assert cli.main(["--root", str(root), "--data-dir", str(data), "bootstrap"]) == 0
    assert installed == ["required"]
    installed.clear()
    assert cli.main(["bootstrap", "--all"]) == 0
    assert installed == ["required", "optional"]
    installed.clear()
    assert cli.main(["bootstrap", "optional"]) == 0
    assert installed == ["optional"]


def add_required(environment):
    root, _, _ = environment
    folder = root / "plugins/required"
    folder.mkdir()
    (folder / "plugin.json").write_text(json.dumps(manifest("required")))
    (root / "plugins/marketplace.json").write_text(json.dumps({"schema": 1, "plugins": [
        {"id": "fixture", "repository_url": "https://github.com/synthetic/fixture", "required": False},
        {"id": "required", "repository_url": "https://github.com/synthetic/required", "required": True}]}))


def test_optional_install_acquires_required_first(environment, monkeypatch):
    add_required(environment)
    calls = []
    def git(args):
        plugin_id = args[-2].rsplit("/", 1)[-1]
        calls.append(plugin_id)
        Path(args[-1], "faustus-plugin.json").write_text(json.dumps(manifest(plugin_id)))
        return ""
    monkeypatch.setattr(market, "_git", git)
    row = market.install("fixture", **environment[2])
    assert calls == ["required", "fixture"] and row["state"] == "cloned"


def test_required_failure_prevents_optional_clone(environment, monkeypatch):
    add_required(environment)
    calls = []
    def git(args):
        calls.append(args[-2])
        raise market.MarketplaceError("git_failed", "Synthetic failure")
    monkeypatch.setattr(market, "_git", git)
    with pytest.raises(market.MarketplaceError) as error:
        market.install("fixture", **environment[2])
    assert error.value.code == "git_failed" and len(calls) == 1
    assert calls[0].endswith("/required")
    assert not (environment[0] / "plugins/fixture/repository").exists()


def test_valid_linked_required_is_reused_and_link_has_no_prerequisite(environment, tmp_path, monkeypatch):
    add_required(environment)
    required_source = source(tmp_path, "required")
    market.link("required", required_source, **environment[2])
    calls = []
    def git(args):
        calls.append(args[-2])
        Path(args[-1], "faustus-plugin.json").write_text(json.dumps(manifest()))
        return ""
    monkeypatch.setattr(market, "_git", git)
    market.install("fixture", **environment[2])
    assert calls == ["https://github.com/synthetic/fixture"]
    assert (required_source / "keep.txt").read_text() == "dirty local work"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git unavailable")
@pytest.mark.parametrize("origin", ["https://github.com/synthetic/fixture.git", "git@github.com:synthetic/fixture.git", "ssh://git@github.com/synthetic/fixture.git"])
def test_actual_git_legacy_link_reads_origin_only(environment, tmp_path, origin):
    path = tmp_path / "real-git"
    market._git(["init", str(path)])
    market._git(["-C", str(path), "config", "remote.origin.url", origin])
    (path / "dirty.txt").write_text("keep this local edit")
    row = market.link("fixture", path, **environment[2])
    assert row["state"] == "linked" and row["source_kind"] == "linked"
    assert (path / "dirty.txt").read_text() == "keep this local edit"


def test_unknown_ssh_alias_is_not_guessed(environment, tmp_path, monkeypatch):
    path = tmp_path / "alias"
    path.mkdir()
    (path / ".git").mkdir()
    monkeypatch.setattr(market, "_git", lambda args: "git@personal-alias:synthetic/fixture.git")
    with pytest.raises(market.MarketplaceError):
        market.link("fixture", path, **environment[2])


def test_git_timeout_is_normalized(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(market.subprocess, "run", run)
    with pytest.raises(market.MarketplaceError) as error:
        market._git(["remote", "get-url", "origin"])
    assert error.value.code == "git_timeout"


def test_disappeared_link_is_reported_and_unlink_preserves_other_files(environment, tmp_path):
    path = source(tmp_path)
    market.link("fixture", path, **environment[2])
    shutil.rmtree(path)
    row = market.list_marketplace(**environment[2])["plugins"][0]
    assert row["state"] == "invalid_link" and not row["can_install"]
    market.unlink("fixture", **environment[2])
    assert market.list_marketplace(**environment[2])["plugins"][0]["can_install"]


def test_versioned_catalog_resolves_builtin_manifests_without_writing_them():
    root = Path(__file__).resolve().parents[1]
    before = {path: path.read_bytes() for path in (root / "plugins").glob("*/plugin.json")}
    rows = market._catalog(root)
    assert any(row["required"] and row["id"] == "hoardhub" for row in rows)
    assert all(row["id"] in {path.parent.name for path in before} for row in rows)
    assert {path: path.read_bytes() for path in before} == before


def test_invalid_required_link_prevents_optional_clone(environment, tmp_path, monkeypatch):
    add_required(environment)
    path = source(tmp_path, "required")
    market.link("required", path, **environment[2])
    shutil.rmtree(path)
    monkeypatch.setattr(market, "_git", lambda args: pytest.fail("No clone until required source is repaired"))
    with pytest.raises(market.MarketplaceError) as error:
        market.install("fixture", **environment[2])
    assert error.value.code == "required_unavailable"
