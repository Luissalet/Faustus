"""Real marketplace HTTP routes over temporary repositories and fake Git."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.plugin_marketplace_routes import setup_plugin_marketplace_routes
from src import plugin_marketplace as market


def manifest(plugin_id):
    return {"schema": 1, "id": plugin_id, "name": "Fixture " + plugin_id,
        "purpose": "Synthetic route fixture", "mcp": {"command": "never-executed"}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    root, data = tmp_path / "checkout", tmp_path / "state"
    rows = [{"id": "optional", "repository_url": "https://github.com/synthetic/optional.git", "required": False},
        {"id": "required", "repository_url": "https://github.com/synthetic/required.git", "required": True}]
    for row in rows:
        directory = root / "plugins" / row["id"]
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text(json.dumps(manifest(row["id"])), encoding="utf-8")
    (root / "plugins/marketplace.json").write_text(json.dumps({"schema": 1, "plugins": rows}), encoding="utf-8")
    real_paths = market._paths
    monkeypatch.setattr(market, "_paths", lambda repo_root=None, data_dir=None:
        real_paths(root if repo_root is None else repo_root, data if data_dir is None else data_dir))
    calls, control = [], {}

    def git(args):
        assert args[:2] == ["clone", "--"]
        calls.append(list(args))
        plugin_id = args[-2].rsplit("/", 1)[-1].removesuffix(".git")
        if control.get("fail") == plugin_id:
            raise market.MarketplaceError("git_failed", "Synthetic clone failure")
        stage = Path(args[-1])
        assert stage.parent == root / "plugins" / plugin_id
        (stage / "faustus-plugin.json").write_text(json.dumps(manifest(plugin_id)), encoding="utf-8")
        (stage / "dirty.txt").write_text("preserve local edits", encoding="utf-8")
        return ""

    monkeypatch.setattr(market, "_git", git)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user == "admin")

    @app.middleware("http")
    async def fixture_identity(request, call_next):
        request.state.current_user = request.headers.get("X-Fixture-User")
        return await call_next(request)

    app.include_router(setup_plugin_marketplace_routes())
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, root=root, data=data, calls=calls, control=control)


ADMIN = {"X-Fixture-User": "admin"}


def get_rows(api):
    response = api.client.get("/api/plugin-marketplace", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["root"] == str(api.root.resolve())
    return {row["id"]: row for row in response.json()["plugins"]}


@pytest.mark.parametrize("user", [None, "member"])
@pytest.mark.parametrize("operation", ["list", "install", "link", "unlink"])
def test_admin_check_precedes_backend_filesystem_and_git(api, monkeypatch, user, operation):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unauthorized request reached filesystem setup")

    monkeypatch.setattr(market, "_paths", forbidden)
    headers = {} if user is None else {"X-Fixture-User": user}
    response = (api.client.get("/api/plugin-marketplace", headers=headers) if operation == "list"
        else api.client.post("/api/plugin-marketplace/optional/" + operation, headers=headers,
            json={"path": str(api.root)} if operation == "link" else None))
    assert response.status_code == 403
    assert calls == [] and api.calls == [] and not api.data.exists()


@pytest.mark.parametrize("body", [{}, {"path": ""}, {"path": None}, {"path": 42},
    {"path": {"nested": "value"}}, {"path": "x" * 4097}])
def test_invalid_link_body_is_rejected_without_registry_or_clone(api, body):
    response = api.client.post("/api/plugin-marketplace/optional/link", headers=ADMIN, json=body)
    assert response.status_code == 422
    assert not api.data.exists() and api.calls == []


@pytest.mark.parametrize("plugin_id", ["unknown", "..", "optional%5C..%5Coutside", "optional%2F..%2Foutside"])
@pytest.mark.parametrize("operation", ["install", "link", "unlink"])
def test_only_known_catalog_ids_can_select_a_source_path(api, plugin_id, operation):
    source = api.root.parent / "source"
    source.mkdir()
    (source / "faustus-plugin.json").write_text(json.dumps(manifest("optional")), encoding="utf-8")
    response = api.client.post("/api/plugin-marketplace/" + plugin_id + "/" + operation, headers=ADMIN,
        json={"path": str(source)} if operation == "link" else None)
    assert response.status_code == 404, response.text
    assert api.calls == [] and not api.data.exists()
    assert not (api.root / "plugins/optional/repository").exists()
    assert (source / "faustus-plugin.json").exists()


def test_get_rows_and_install_use_real_backend_and_required_first(api):
    rows = get_rows(api)
    keys = {"id", "name", "purpose", "repository_url", "required", "source_kind", "local_path",
        "clone_path", "state", "can_install"}
    assert set(rows) == {"optional", "required"}
    for row in rows.values():
        assert set(row) == keys
        assert row["source_kind"] == "none" and row["state"] == "not_installed"
        assert row["local_path"] is None and row["can_install"] is True
        assert row["clone_path"] == str(api.root / "plugins" / row["id"] / "repository")
    original = (api.root / "plugins/optional/plugin.json").read_bytes()
    response = api.client.post("/api/plugin-marketplace/optional/install", headers=ADMIN,
        json={"repository_url": "https://github.com/foreign/not-used.git", "path": "outside"})
    assert response.status_code == 200, response.text
    assert [call[-2] for call in api.calls] == ["https://github.com/synthetic/required.git",
        "https://github.com/synthetic/optional.git"]
    installed = get_rows(api)
    assert response.json() == installed["optional"]
    for row in installed.values():
        assert row["source_kind"] == "cloned" and row["state"] == "cloned"
        assert row["local_path"] == row["clone_path"] and row["can_install"] is False
        assert Path(row["local_path"]).joinpath("dirty.txt").exists()
    assert (api.root / "plugins/optional/plugin.json").read_bytes() == original


def test_required_clone_failure_prevents_optional_publication(api):
    api.control["fail"] = "required"
    response = api.client.post("/api/plugin-marketplace/optional/install", headers=ADMIN)
    assert response.status_code == 503 and "Synthetic clone failure" in response.json()["detail"]
    assert len(api.calls) == 1 and api.calls[0][-2].endswith("/required.git")
    assert not (api.root / "plugins/optional/repository").exists()
    assert not list(api.root.glob("plugins/*/.repository-stage-*"))


def test_link_row_matches_get_and_unlink_preserves_local_files(api):
    source = api.root.parent / "linked source"
    source.mkdir()
    (source / "faustus-plugin.json").write_text(json.dumps(manifest("optional")), encoding="utf-8")
    (source / "dirty.txt").write_text("preserve user work", encoding="utf-8")
    response = api.client.post("/api/plugin-marketplace/optional/link", headers=ADMIN, json={"path": str(source)})
    assert response.status_code == 200, response.text
    row = get_rows(api)["optional"]
    assert response.json() == row
    assert row["state"] == "linked" and row["source_kind"] == "linked"
    assert row["local_path"] == str(source.resolve()) and row["can_install"] is False
    assert api.calls == []  # Local linking has no clone prerequisite.
    removed = api.client.post("/api/plugin-marketplace/optional/unlink", headers=ADMIN)
    assert removed.status_code == 200 and removed.json() == {"id": "optional", "unlinked": True}
    assert source.joinpath("dirty.txt").read_text(encoding="utf-8") == "preserve user work"
    assert get_rows(api)["optional"]["state"] == "not_installed"


def test_unlink_does_not_remove_a_managed_clone(api):
    installed = api.client.post("/api/plugin-marketplace/optional/install", headers=ADMIN)
    assert installed.status_code == 200
    source = Path(installed.json()["local_path"])
    response = api.client.post("/api/plugin-marketplace/optional/unlink", headers=ADMIN)
    assert response.status_code == 200
    assert source.joinpath("dirty.txt").read_text(encoding="utf-8") == "preserve local edits"
    assert get_rows(api)["optional"]["state"] == "cloned"


@pytest.mark.parametrize("kind", ["missing", "wrong_identity"])
def test_invalid_link_identity_does_not_write_registry(api, kind):
    source = api.root.parent / "invalid source"
    if kind == "wrong_identity":
        source.mkdir()
        (source / "faustus-plugin.json").write_text(json.dumps(manifest("foreign")), encoding="utf-8")
    response = api.client.post("/api/plugin-marketplace/optional/link", headers=ADMIN, json={"path": str(source)})
    assert response.status_code == 400
    assert not api.data.exists() and api.calls == []
