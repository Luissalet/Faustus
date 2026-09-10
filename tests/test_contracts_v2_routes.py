"""`GET /api/contracts/schemas` and `GET /api/contracts/schemas/{name}` — the
read-only view of the eight spec v2 JSON Schemas, added to the same router
`test_contracts_mcp_and_routes.py` already exercises for the Phase 0 routes.
Pure like its neighbours: the schema served is the file on disk, nothing is
written, and an unknown name is a clean 404.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.contracts_routes import setup_contracts_routes

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "docs" / "spec" / "v2" / "schemas"

ALL_NAMES = ("tool_descriptor", "tool_invocation", "tool_result", "evidence_ref",
             "task_state", "question_request", "approval_request", "event_envelope")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("routes.contracts_routes.require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_contracts_routes())
    return TestClient(app)


def test_the_listing_names_all_eight_schemas_with_their_version(client):
    body = client.get("/api/contracts/schemas").json()
    rows = {r["name"]: r for r in body["schemas"]}
    assert set(rows) == set(ALL_NAMES)
    for name, row in rows.items():
        assert row["version"] == "1.0"
        assert row["title"]  # non-empty, read from the file


@pytest.mark.parametrize("name", ALL_NAMES)
def test_one_schema_is_served_byte_identical_to_the_file_on_disk(client, name):
    on_disk = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    served = client.get(f"/api/contracts/schemas/{name}").json()
    assert served == on_disk


def test_an_unknown_schema_name_is_a_clean_404(client):
    r = client.get("/api/contracts/schemas/not_a_real_schema")
    assert r.status_code == 404
    assert "not_a_real_schema" in r.json()["detail"]


def test_serving_a_schema_writes_nothing(client, tmp_path, monkeypatch):
    """Pure means pure, the same rule `test_validating_a_manifest_installs_nothing`
    already applies to `/skill/validate`."""
    import services.memory.skills as skills_mod

    def explode(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("reading a schema touched the skills store")

    monkeypatch.setattr(skills_mod.SkillsManager, "__init__", explode)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert client.get("/api/contracts/schemas").status_code == 200
    assert client.get("/api/contracts/schemas/task_state").status_code == 200
    assert sorted(p.name for p in tmp_path.iterdir()) == before
