"""VER-06 · an accepted diff's approval is invalidated when the file moves on,
and a human's accept can never read as a passing test.

`services/review_state.py` already recorded a signature (`content_sha256`)
per accepted path and could tell a caller `approval_still_valid` — this pins
the two things that were still missing:

  * `routes/workspace_routes.py::review_decide` never passed `content=` at
    accept time, so no signature was ever actually captured through the
    route a real accept goes through.
  * `GET /api/workspace/review/{message_id}` returned the raw entry with no
    derived staleness at all, and nothing distinguishing `tests_status`
    (an automatic verification result) from `human_approved` (the accept).

Both close together: `services/review_state.py::status_payload` (the pure
function) and its wiring into the two routes.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workspace_routes as wr
from services import review_state as rs


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    return TestClient(app)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("print('v1')\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# services/review_state.py::status_payload — pure function
# ---------------------------------------------------------------------------

def test_status_payload_marks_a_drifted_approval_stale(data_dir):
    rs.init("m1", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    entry = rs.decide("m1", "a.py", "accept", content="version one")

    fresh = rs.status_payload("m1", entry, current_content={"a.py": "version one"})
    assert fresh["approvals"] == [{
        "path": "a.py", "at": entry["human_approved"]["a.py"]["at"],
        "diff_sha256": entry["human_approved"]["a.py"]["content_sha256"],
        "stale": False,
    }]

    drifted = rs.status_payload("m1", entry, current_content={"a.py": "version TWO"})
    assert drifted["approvals"][0]["stale"] is True
    assert drifted["approvals"][0]["diff_sha256"] == fresh["approvals"][0]["diff_sha256"]


def test_status_payload_accepts_a_callable_getter(data_dir):
    rs.init("m2", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    entry = rs.decide("m2", "a.py", "accept", content="only version")

    payload = rs.status_payload("m2", entry, current_content=lambda p: "only version")
    assert payload["approvals"][0]["stale"] is False


def test_status_payload_conserves_tests_status_separately_from_approval(data_dir):
    """The literal VER-06 outcome: accepting manually must never turn a
    recorded test failure into a pass — status_payload only ever echoes
    whatever tests_status init() was given, untouched by any accept."""
    rs.init("m3", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef",
           tests_status={"ran": True, "ok": False, "summary": "2 failed"})
    entry = rs.decide("m3", "a.py", "accept", content="anything")

    payload = rs.status_payload("m3", entry)
    assert payload["tests_status"] == {"ran": True, "ok": False, "summary": "2 failed"}
    assert payload["approvals"][0]["path"] == "a.py"  # accepted, but tests stayed failed


def test_status_payload_without_current_content_is_not_stale_by_default(data_dir):
    """No filesystem access given at all: nothing to contradict the
    approval with, so it reads as still valid — never a manufactured fail."""
    rs.init("m4", session_id="s1", workspace="/ws", files=["a.py"], checkpoint="deadbeef")
    entry = rs.decide("m4", "a.py", "accept", content="v1")
    payload = rs.status_payload("m4", entry)
    assert payload["approvals"][0]["stale"] is False


# ---------------------------------------------------------------------------
# route wiring
# ---------------------------------------------------------------------------

def test_get_review_exposes_stale_after_the_file_changes_post_accept(client, ws, data_dir):
    rs.init("rm1", session_id="s1", workspace=str(ws), files=["a.py"], checkpoint="")
    r = client.post("/api/workspace/review/rm1/decide", json={"path": "a.py", "decision": "accept"})
    assert r.status_code == 200, r.text
    # The route itself captured the accept-time signature.
    assert r.json()["state"]["human_approved"]["a.py"]["content_sha256"]

    before = client.get("/api/workspace/review/rm1")
    assert before.status_code == 200
    assert before.json()["approvals"] == [{
        "path": "a.py",
        "at": before.json()["approvals"][0]["at"],
        "diff_sha256": before.json()["approvals"][0]["diff_sha256"],
        "stale": False,
    }]

    (ws / "a.py").write_text("print('v2 — changed after approval')\n", encoding="utf-8")

    after = client.get("/api/workspace/review/rm1")
    assert after.status_code == 200
    assert after.json()["approvals"][0]["stale"] is True
    # Same signature recorded, still visible — a reader can see WHAT was
    # approved even once it is stale, not just that it no longer matches.
    assert after.json()["approvals"][0]["diff_sha256"] == before.json()["approvals"][0]["diff_sha256"]


def test_get_review_carries_tests_status_untouched_by_accept(client, ws, data_dir):
    rs.init("rm2", session_id="s1", workspace=str(ws), files=["a.py"], checkpoint="",
           tests_status={"ran": True, "ok": False, "summary": "1 failed"})

    r = client.post("/api/workspace/review/rm2/decide", json={"path": "a.py", "decision": "accept"})
    assert r.status_code == 200

    payload = client.get("/api/workspace/review/rm2").json()
    # Accepting the file changed nothing about the recorded test outcome.
    assert payload["tests_status"] == {"ran": True, "ok": False, "summary": "1 failed"}
    assert rs.is_human_approved(rs.get("rm2"), "a.py") is True
