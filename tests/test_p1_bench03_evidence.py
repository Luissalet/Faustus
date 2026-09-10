"""Lote 49b (P1) - BENCH-03: evidence inspector backend.

`routes/evidence_routes.py::resolve_evidence` (POST /api/evidence/resolve)
resolves an `EvidenceRef` (src/contracts/tool.py) back to its exact source
range and says plainly whether it still matches - reusing
`src/context_ledger.py::verify_read_evidence` as the one hashing authority,
never a second one.

The requirement's own acceptance criterion, almost verbatim: "Si un archivo
cambio desde la verificacion, el inspector no muestra su version nueva como
si fuera la prueba original." -> `still_valid` must be False and the fresh
text must come back in a clearly separate field (`current_content`), never
silently standing in for the captured evidence itself (which this endpoint
never even returns - the caller already holds it).
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.evidence_routes as er
from src.contracts.base import now_iso


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(er, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(er, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(er.setup_evidence_routes())
    return TestClient(app)


def _evidence_for(content: str, *, source_ref: str, kind: str = "lines", value: str = "1-3", source_type: str = "file"):
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "schema_version": "1.0",
        "evidence_id": "evi_test_1",
        "owner_id": "admin",
        "project_id": None,
        "source_type": source_type,
        "source_ref": source_ref,
        "source_revision": digest[:16],
        "content_sha256": digest,
        "captured_at": now_iso(),
        "locator": {"kind": kind, "value": value},
        "derived_from": [],
        "retention": "task",
    }


def _repo(tmp_path, text="line1\nline2\nline3\n"):
    (tmp_path / "a.txt").write_text(text, encoding="utf-8")
    return tmp_path


def test_unchanged_file_resolves_as_still_valid(client, tmp_path):
    repo = _repo(tmp_path)
    # The "lines" locator's captured content has no trailing newline - it is
    # "\n".join(lines[start-1:end]), the same convention src/read_plan.py
    # uses for a ranged read (and what evidence_for_read hashes in production).
    captured = "line1\nline2\nline3"
    ev = _evidence_for(captured, source_ref="a.txt")
    r = client.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["still_valid"] is True
    assert body["current_available"] is True
    assert body["current_content"] == captured
    assert body["reason"] is None


def test_changed_file_is_flagged_stale_and_never_passed_off_as_the_original(client, tmp_path):
    """The core requirement: once the file drifted, the fresh text comes
    back in `current_content` (clearly the *new* read), `still_valid` is
    False, and there is an explanatory `reason` - the endpoint never
    returns the new text as if it were the captured evidence."""
    repo = _repo(tmp_path)
    captured_range = "line1\nline2\nline3\n"
    ev = _evidence_for(captured_range, source_ref="a.txt")
    # The file changes after the evidence was captured.
    (repo / "a.txt").write_text("line1\nCHANGED\nline3\n", encoding="utf-8")
    r = client.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["still_valid"] is False
    assert body["current_available"] is True
    assert "CHANGED" in body["current_content"]
    # The response never contains the captured text as ground truth beside
    # the fresh one under the same name - only the evidence's own stamped
    # hash/locator (in `evidence`), which the caller compares itself.
    assert body["evidence"]["content_sha256"] == hashlib.sha256(captured_range.encode()).hexdigest()
    assert body["reason"] and "changed" in body["reason"].lower()


def test_missing_file_is_reported_not_silently_swallowed(client, tmp_path):
    ev = _evidence_for("gone\n", source_ref="missing.txt")
    r = client.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["still_valid"] is False
    assert body["current_available"] is False
    assert body["current_content"] is None
    assert "no longer exists" in body["reason"]


def test_path_outside_the_workspace_is_refused(client, tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    ev = _evidence_for("secret\n", source_ref=str(outside))
    r = client.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(repo)})
    assert r.status_code == 400


def test_non_file_source_type_is_named_as_unsupported_not_guessed(client, tmp_path):
    ev = _evidence_for("<html>ok</html>", source_ref="https://example.com/page", source_type="web", kind="whole", value="whole")
    r = client.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["still_valid"] is None
    assert body["current_available"] is False
    assert "not supported" in body["reason"]


def test_malformed_evidence_mapping_is_a_clean_400(client):
    r = client.post("/api/evidence/resolve", json={"evidence": {"not": "an evidence ref"}})
    assert r.status_code == 400


def test_non_admin_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "get_current_user", lambda request: "bob")
    monkeypatch.setattr(er, "owner_is_admin_or_single_user", lambda owner: False)
    app = FastAPI()
    app.include_router(er.setup_evidence_routes())
    c = TestClient(app)
    ev = _evidence_for("x\n", source_ref="a.txt")
    r = c.post("/api/evidence/resolve", json={"evidence": ev, "workspace": str(tmp_path)})
    assert r.status_code == 403
