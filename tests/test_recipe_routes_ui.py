"""HTTP the Studio "Recetas" view reads: GET /api/recipes/{id} (the whole
recipe) next to the list, the verification and the dry-run / real-run routes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import derivation_ops as ops
from src import recipe_learning as rl

OWNER = "admin"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import routes.derivation_routes as dr
    monkeypatch.setattr(dr, "effective_user", lambda request: OWNER)
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(dr.setup_derivation_routes())
    return TestClient(app)


def _recipe():
    steps = [{"tool": "write_file", "args": {"path": "salida_a.txt", "content": "hecho"}}]
    return rl.propose(OWNER, request="crea salida_a.txt", steps=steps, title="Escribir")


def test_show_returns_the_whole_recipe_and_404_for_an_unknown_one(client):
    recipe = _recipe()
    body = client.get(f"/api/recipes/{recipe.id}").json()
    assert body["recipe"]["id"] == recipe.id and body["recipe"]["step_templates"] and body["recipe"]["params"]
    r = client.get("/api/recipes/does-not-exist")
    assert r.status_code == 404 and r.json()["error_class"] == "recipes.not_found"


def test_list_rows_say_how_the_last_real_run_ended(client):
    recipe = _recipe()
    (row,) = [r for r in ops.run_recipe(OWNER, {"action": "list"})["recipes"] if r["id"] == recipe.id]
    assert row["steps"] == 1 and row["executions"] == 0 and row["last_status"] is None and row["learned"] is True


def test_a_real_run_from_the_view_is_recorded_and_listed(client, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    recipe = _recipe()
    (name, *_) = [p["name"] for p in recipe.params]
    case_b = {p["name"]: p["example"] for p in recipe.params}
    case_b[name] = "salida_b.txt"
    crit = [{"kind": "file_exists", "path": "{{%s}}" % name}]
    dry = client.post(f"/api/recipes/{recipe.id}/try", json={"params": case_b}).json()
    assert dry["kind"] == "structural_dry_run" and dry["verified"] is False and dry["executed"] is False
    out = client.post(f"/api/recipes/{recipe.id}/execute",
                      json={"params": case_b, "workspace": str(ws), "criteria": crit}).json()
    assert out["status"] in ("verified", "criteria_failed", "step_failed", "executed_unverified", "incomplete")
    ver = client.get(f"/api/recipes/{recipe.id}/verification").json()
    assert ver["cases"] and ver["cases"][0]["status"] == out["status"]
    (row,) = [r for r in ops.run_recipe(OWNER, {"action": "list"})["recipes"] if r["id"] == recipe.id]
    assert row["executions"] == 1 and row["last_status"] == out["status"]
