"""Lote 71 — PLAN-03: `GET /api/agents/leases` surfaces the process-wide
`ResourceOwnershipRegistry` (src/resource_ownership.py) so a screen can show
"who edits what" and any refused/conflicting claims. Before this route
existed there was no way to read the registry over HTTP at all — the map's
own gap note ("ninguna pantalla muestra qué agente edita qué fichero", 0
results in studio/src/screens) was literally true: nothing served the data a
screen would need. This proves the route now does, and that a denied
acquisition is recorded as a conflict the row can point at.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.agent_leases_routes as leases_routes
from src.resource_ownership import ResourceOwnershipRegistry


@pytest.fixture
def registry(monkeypatch):
    reg = ResourceOwnershipRegistry(default_ttl_seconds=300)
    monkeypatch.setattr(leases_routes, "get_registry", lambda: reg)
    return reg


def _client(monkeypatch, *, user=""):
    monkeypatch.setattr(leases_routes, "require_user", lambda request: user)
    app = FastAPI()
    app.include_router(leases_routes.setup_agent_leases_routes())
    return TestClient(app)


def test_no_leases_is_an_empty_list_not_an_error(monkeypatch, registry):
    c = _client(monkeypatch)
    r = c.get("/api/agents/leases")
    assert r.status_code == 200
    assert r.json() == {"leases": [], "conflicts": []}


def test_an_active_lease_is_listed_with_who_task_and_since(monkeypatch, registry):
    registry.acquire("file", "src/app.py", "sa0-abc123", task_id="task-1", now=100.0)
    c = _client(monkeypatch)
    r = c.get("/api/agents/leases")
    assert r.status_code == 200
    body = r.json()
    assert len(body["leases"]) == 1
    row = body["leases"][0]
    assert row["resource"] == "src/app.py"
    assert row["kind"] == "file"
    assert row["owner_agent"] == "sa0-abc123"
    assert row["task_id"] == "task-1"
    assert row["since"] == 100.0
    assert row["expires_at"] == pytest.approx(400.0)
    assert body["conflicts"] == []


def test_two_specialists_on_the_same_file_shows_as_a_conflict(monkeypatch, registry):
    """The acceptance criterion PLAN-03 exists for: two specialists must not
    step on the same file. The lease refuses the second one outright — this
    checks the refusal is ALSO visible as a conflict row, not just a quiet
    `False` only the caller inside the process ever saw."""
    registry.acquire("file", "src/shared.py", "sa0-worker-a", task_id="task-A", now=0.0)
    ok = registry.acquire("file", "src/shared.py", "sa1-worker-b", task_id="task-B", now=1.0)
    assert ok is False

    c = _client(monkeypatch)
    body = c.get("/api/agents/leases").json()
    assert len(body["leases"]) == 1
    assert body["leases"][0]["owner_agent"] == "sa0-worker-a"
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["resource"] == "src/shared.py"
    assert conflict["holder_agent"] == "sa0-worker-a"
    assert conflict["holder_task_id"] == "task-A"
    assert conflict["requester_agent"] == "sa1-worker-b"
    assert conflict["requester_task_id"] == "task-B"
    assert conflict["at"] == 1.0


def test_a_dead_workers_lease_is_reclaimed_and_stops_appearing(monkeypatch, registry):
    """PLAN-03's other half: a worker that dies without releasing must not
    fence the file forever. The route reads live state, so once the TTL has
    elapsed the lease is gone from the list the moment anything touches it."""
    registry.acquire("file", "src/orphan.py", "sa0-dead-worker", task_id="task-X",
                      ttl_seconds=10, now=0.0)
    c = _client(monkeypatch)
    # Still held well within the TTL.
    assert len(c.get("/api/agents/leases").json()["leases"]) == 1

    # A fresh acquire past the TTL from a different owner reaps the dead
    # lease (mirrors `owner_of`/`acquire`'s lazy reaping) and takes it over —
    # the screen sees the NEW owner, not the dead one still listed.
    assert registry.acquire("file", "src/orphan.py", "sa2-new-worker", task_id="task-Y",
                             now=20.0) is True
    body = c.get("/api/agents/leases").json()
    assert len(body["leases"]) == 1
    assert body["leases"][0]["owner_agent"] == "sa2-new-worker"


def test_requires_an_authenticated_caller(monkeypatch, registry):
    from fastapi import HTTPException

    def unauthenticated(request):
        raise HTTPException(401, "Authentication required")

    monkeypatch.setattr(leases_routes, "require_user", unauthenticated)
    app = FastAPI()
    app.include_router(leases_routes.setup_agent_leases_routes())
    c = TestClient(app)
    r = c.get("/api/agents/leases")
    assert r.status_code == 401


def test_route_is_registered_on_the_app():
    # `from app import app` — the real router table, not a bare FastAPI()
    # with only this router mounted (which would pass even if app.py never
    # wired it in) — same shape tests/test_l55_app_wiring.py uses. The app's
    # own auth MIDDLEWARE (ahead of the route's own `require_user`) 401s an
    # unconfigured instance unless the request carries the internal-tool
    # loopback bypass header — the same real, already-there path
    # tests/test_l55_app_wiring.py uses, not a test-only shortcut.
    from app import app as real_app
    from core import middleware
    c = TestClient(real_app, client=("127.0.0.1", 51234))
    r = c.get("/api/agents/leases", headers={middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN})
    assert r.status_code == 200
    assert r.json().keys() == {"leases", "conflicts"}
