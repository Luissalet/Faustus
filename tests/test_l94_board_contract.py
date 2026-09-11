"""Lote 94 (OBJ-6) -- Studio<->backend board contract verification.

CONTRATO_BOARD.md is the wire contract between `studio/src/adapters/board.ts`
and `routes/board_routes.py`/`src/project_board.py`. This test starts the
real board router under a `TestClient`, creates real issues (with a comment,
a commit ref, and a `blocked_by` link, so every field the adapter's TS
interfaces declare is actually populated), captures the ACTUAL JSON bodies
the server sends back for `listIssues`/`getIssue`/`getSummary` and one 404,
and hands them to `studio/checks/l94-board-contract.check.mjs` (an esbuild
bundle of `board.ts`, run under Node with `fetch` replaying exactly those
captured bodies) to prove the adapter parses REAL server responses -- not a
hand-typed fixture that could quietly drift from what the server sends.

While building this, it surfaced two real contract bugs, both fixed in
`routes/board_routes.py` (not owned by any earlier lote's file list, but
Lote 94's own brief explicitly authorizes fixing the backend when the
contract says what Studio assumes):

1. Every board mutation error (`board.not_found`, `board.invalid_transition`,
   `board.claimed`, ...) raised `HTTPException(status, tool_error_detail(...))`
   -- a dict `detail`, which FastAPI serializes as `{"detail": {"code": ...,
   "message": ...}}`. `core.middleware`'s OBS-03 middleware only backfills a
   TOP-LEVEL `error_class` when the body doesn't already have one, and since
   the specific class lived at `detail.code` (not top level), every error
   response actually carried a GENERIC status-derived `error_class`
   (`resource.not_found`, `schema.contract_violation`, ...) instead of the
   `board.*` vocabulary CONTRATO_BOARD documents and `BoardApiError.errorClass`
   (`studio/src/adapters/board.ts`) is built to read. Fixed with a flat
   `_error()` helper mirroring `routes/git_routes.py`'s own (the pattern rule
   7/BRIEF_CIERRE already names).
2. `PATCH .../issues/{id}` built its patch dict with
   `payload.model_dump(exclude_none=True)` -- so a client that explicitly
   sends `{"assignee": null}` to CLEAR the assignee (exactly what
   `IssueDetail.tsx` does) had that key silently dropped, and the route
   answered "Nothing to update" instead of clearing it, even though
   `project_board.update_issue` fully supports a falsy `assignee` in the
   patch dict. Fixed by switching to `exclude_unset=True`, which keeps a key
   the client actually sent (null or not) and drops only keys never sent.

`tests/test_l92_board_routes.py`'s own four assertions that used to read
`resp.json()["detail"]["code"]` were updated to `resp.json()["error_class"]`
to match the corrected (and now CONTRATO_BOARD-compliant) shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import board_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod  # noqa: E402

OWNER = "luis"

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l94-board-contract.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    yield


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    monkeypatch.setattr(board_routes, "get_project_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or None
    monkeypatch.setattr(board_routes, "effective_user", _owner_from_header)
    app = FastAPI()
    app.include_router(board_routes.setup_board_routes())
    return TestClient(app)


def _hdr():
    return {"x-test-owner": OWNER}


def test_adapter_parses_real_board_responses(client, store, tmp_path):
    root = tmp_path / "ContractProj"
    root.mkdir()
    project = store.create(name="ContractProj", folder="ContractProj", workspace=str(root),
                            owner=OWNER, scaffold_memory=False)
    pid = project["id"]

    # Real backend calls (not synthetic bodies): capture the actual response
    # for every URL board.ts's request functions would build.
    responses = {}

    def call(method, path, **kwargs):
        resp = client.request(method, path, headers=_hdr(), **kwargs)
        responses[f"{method} {path}"] = {"status": resp.status_code, "body": resp.json()}
        return resp

    # issueB exists first so issueA can carry a real `blocked_by` link to it.
    resp_b = call("POST", f"/api/projects/{pid}/board/issues",
                  json={"type": "task", "title": "blocker task"})
    assert resp_b.status_code == 201, resp_b.text
    issue_b = resp_b.json()["issue"]["id"]

    resp_a = call("POST", f"/api/projects/{pid}/board/issues", json={
        "type": "bug", "title": "retry loop leaks a session",
        "priority": "P1", "labels": ["backend"],
        "links": [{"kind": "blocked_by", "target": issue_b}],
    })
    assert resp_a.status_code == 201, resp_a.text
    issue_a = resp_a.json()["issue"]["id"]

    comment_body = "reproduced locally, patch incoming"
    resp_comment = call("POST", f"/api/projects/{pid}/board/issues/{issue_a}/comments",
                         json={"body_md": comment_body})
    assert resp_comment.status_code == 201, resp_comment.text

    resp_ref = call("POST", f"/api/projects/{pid}/board/issues/{issue_a}/refs",
                     json={"kind": "commit", "value": "deadbeef", "label": "fix attempt"})
    assert resp_ref.status_code == 201, resp_ref.text

    # The three read routes the adapter's contract check exercises, captured
    # AFTER every mutation above so their bodies are the real, final state.
    resp_list = call("GET", f"/api/projects/{pid}/board/issues")
    assert resp_list.status_code == 200
    resp_get = call("GET", f"/api/projects/{pid}/board/issues/{issue_a}")
    assert resp_get.status_code == 200
    assert resp_get.json()["issue"]["blocked_by"] == [issue_b]
    resp_summary = call("GET", f"/api/projects/{pid}/board/summary")
    assert resp_summary.status_code == 200
    board_key = resp_summary.json()["key"]

    # A real 404, to prove the JS side can read `error_class` at the top
    # level (see this file's docstring, bug #1).
    resp_404 = call("GET", f"/api/projects/{pid}/board/issues/NOPE-999")
    assert resp_404.status_code == 404
    assert resp_404.json()["error_class"] == "board.not_found"

    fixtures = {
        "meta": {
            "projectId": pid, "issueAId": issue_a, "issueBId": issue_b,
            "boardKey": board_key, "commentBody": comment_body,
        },
        "responses": responses,
    }
    fixtures_path = tmp_path / "fixtures.json"
    fixtures_path.write_text(json.dumps(fixtures), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(_CHECK), str(fixtures_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout
