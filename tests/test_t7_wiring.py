"""Lot T7 wiring gap: `routes/budget_routes.py` (A31, `GET
/api/runs/{run_id}/budget`) is a real, individually-tested router
(`tests/acceptance/test_a31_budget_reservations.py` mounts it on a bare
`FastAPI()` and exercises it end to end) but `app.py` — not owned by this
lot, see `docs/spec/paridad/CONTRATO.md`'s file-ownership table — never
registers it, so the path does not answer on the server a client actually
talks to. Same shape as `tests/test_l55_app_wiring.py` (lot 55, item 1):
`from app import app`, the real app object, not a router rebuilt in
isolation which would pass even if `app.py` never wired it.

The exact one-line diff app.py needs is in
`.../scratchpad/paridad_wave1/T7_wiring.md` (§1). Once applied, this test
should flip from `xfail(strict=True)` to a plain passing assertion.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from core import middleware


@pytest.fixture()
def client():
    return TestClient(app, client=("127.0.0.1", 51234))


def test_budget_route_is_registered_on_the_real_app(client, monkeypatch):
    from src import budget_account
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp / "budget.sqlite3")
    # The internal tool token is the same path the agent's own tools use;
    # "Setup required" (401) is what an unauthenticated client gets on a
    # fresh data dir, which says nothing about routing.
    headers = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}
    response = client.get("/api/runs/some-run/budget", headers=headers)
    # An unmounted path answers the app's generic 404 with no `run_id` key at
    # all (see test_unmounted_sibling_path_still_answers_a_generic_404_for_contrast
    # in tests/test_l55_app_wiring.py for the same contrast on a different
    # route); the real handler answers 200 with the snapshot shape.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == "some-run"
    assert body["opened"] is False
