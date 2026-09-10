"""HTTP surface for CTX-05/CTX-06/PERF-03 additions to
routes/context_engine_routes.py — exercised the way the rest of this file's
sibling tests do: a real FastAPI app + TestClient, never a mock of the route
layer itself (rule 7).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.context_engine_routes import setup_context_engine_routes
from src.context_engine import cache, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()

    @app.middleware("http")
    async def _as_luis(request, call_next):
        request.state.current_user = "luis"
        return await call_next(request)

    app.include_router(setup_context_engine_routes())
    try:
        yield TestClient(app)
    finally:
        store.use_path(None)
        cache.reset_working_set()


# ── CTX-06: health + reconstruct-task ───────────────────────────────────────

def test_health_route_flags_repeated_tool_output(client):
    messages = [
        {"role": "user", "content": "what is in the log"},
        {"role": "tool", "content": "same log line"},
        {"role": "tool", "content": "same log line"},
        {"role": "tool", "content": "same log line"},
    ]
    out = client.post("/api/context/health", json={"messages": messages})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["ok"] is True
    assert any(f["key"] == "repetition" for f in body["flags"])


def test_reconstruct_task_route(client):
    messages = [
        {"role": "system", "content": "Never push directly to main."},
        {"role": "user", "content": "Ship the fix in src/app.py"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "edit_file", "arguments": "{}"}}]},
    ]
    out = client.post("/api/context/reconstruct-task", json={"messages": messages})
    assert out.status_code == 200, out.text
    task = out.json()["task"]
    assert task["goal"] == "Ship the fix in src/app.py"
    assert any("push directly to main" in c.lower() for c in task["constraints"])
    assert task["next_action"] == "tool: edit_file"


# ── PERF-03: cache invalidate never leaks across owners ─────────────────────

def test_cache_invalidate_only_touches_this_owners_scope(client):
    ws = cache.working_set()
    ws.put("luis|p1", "candidates:file:a.txt", "luis-cached")
    ws.put("otro|p1", "candidates:file:a.txt", "otro-cached")

    out = client.post("/api/context/cache/invalidate", json={"project_id": "p1"})
    assert out.status_code == 200, out.text
    assert out.json()["dropped"] >= 1

    assert ws.get("luis|p1", "candidates:file:a.txt") is None
    assert ws.get("otro|p1", "candidates:file:a.txt") == "otro-cached"


# ── CTX-05: selection controls over HTTP ────────────────────────────────────

def test_selection_set_list_and_unset_round_trip(client):
    out = client.post("/api/context/selection",
                      json={"kind": "exclude", "ref": "file:secret.md",
                            "project_id": "proj1", "session_id": "sess1"})
    assert out.status_code == 200, out.text

    listed = client.get("/api/context/selection",
                        params={"project_id": "proj1", "session_id": "sess1"})
    assert listed.status_code == 200
    refs = [row["ref"] for row in listed.json()["controls"]["exclude"]]
    assert "file:secret.md" in refs

    removed = client.request("DELETE", "/api/context/selection",
                             json={"kind": "exclude", "ref": "file:secret.md",
                                   "project_id": "proj1", "session_id": "sess1"})
    assert removed.status_code == 200 and removed.json()["removed"] is True

    listed2 = client.get("/api/context/selection",
                         params={"project_id": "proj1", "session_id": "sess1"})
    refs2 = [row["ref"] for row in listed2.json()["controls"]["exclude"]]
    assert "file:secret.md" not in refs2


def test_selection_rejects_an_unknown_kind(client):
    out = client.post("/api/context/selection",
                      json={"kind": "bogus", "ref": "file:x.md"})
    assert out.status_code == 400
