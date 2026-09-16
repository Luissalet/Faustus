"""A12 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A12): "Parallel tools
return oversized responses then server restarts" -> "Original results
readable by authorized owner with integrity metadata".

Exercises real Faustus code, not a stand-in for it:

* `src.tool_result_offload.offload_if_oversized` — the real offload
  mechanism (new this lot), called the way the wiring in `T5_wiring.md`
  calls it for two "parallel" tool calls with oversized results.
* `src.artifact_store` / `src.artifact_identity` — the real content-addressed
  store the offload writes into (sha256, owner, bytes, retention).
* `routes/artifact_routes.py` (`setup_artifact_routes()`) via a real
  `fastapi.testclient.TestClient` — the real HTTP surface an owner uses to
  read metadata back, with only `_owner()` (the auth-resolution seam, same
  pattern already used by `tests/test_p1_art_08_library.py`) pinned to a
  fixed identity so this test does not also have to stand up full cookie
  auth to prove artifact ownership and integrity.

"Server restarts" is simulated the way the rest of this lot's neighbours
simulate it (`tests/test_artifact_cutover.py` et al.): a fresh SQLAlchemy
engine bound to the SAME on-disk sqlite file and a fresh `FastAPI`/
`TestClient` app, with no Python-level cache surviving the swap (there is
none in `src.artifact_store`/`src.artifact_identity` — both are DB/disk
reads on every call, which this test also demonstrates by working at all
after the swap).
"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store
from tests.acceptance.conftest import record_evidence


@pytest.fixture()
def sqlite_path(tmp_path):
    return tmp_path / "a12.db"


def _bind_database(sqlite_path, monkeypatch):
    url = "sqlite:///" + sqlite_path.as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture()
def world(sqlite_path, tmp_path, monkeypatch):
    engine = _bind_database(sqlite_path, monkeypatch)
    store_dir = tmp_path / "store"
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(store_dir))
    yield store_dir
    engine.dispose()


def _big_result(tag: str, n_chars: int = 30000) -> dict:
    return {"output": f"{tag}-line\n" * (n_chars // (len(tag) + 6)), "exit_code": 0}


@pytest.mark.acceptance("A12")
def test_two_parallel_oversized_results_survive_a_restart_with_matching_integrity(
    world, sqlite_path, tmp_path, monkeypatch, request,
):
    from src import tool_result_offload as offload

    owner = "alice"
    session_id = "s-a12"
    run_id = "run-a12"

    result_a = _big_result("bash-output", 30000)
    result_b = _big_result("browser-page", 45000)
    assert offload._string_chars(result_a) > offload.offload_threshold_chars()
    assert offload._string_chars(result_b) > offload.offload_threshold_chars()

    # Two tools "in parallel": same session/run, different call_id/tool —
    # exactly what two concurrent tool_use blocks in one round look like.
    truncated_a = offload.offload_if_oversized(
        result_a, owner=owner, session_id=session_id, run_id=run_id,
        call_id="call-A", tool="bash")
    truncated_b = offload.offload_if_oversized(
        result_b, owner=owner, session_id=session_id, run_id=run_id,
        call_id="call-B", tool="browser_read")

    for truncated, original in ((truncated_a, result_a), (truncated_b, result_b)):
        assert truncated[offload.OFFLOAD_MARKER] is True
        assert truncated["artifact_id"], "storage must have succeeded with a real owner"
        assert len(truncated["output"]) < len(original["output"]), \
            "the in-prompt copy must actually be smaller, not just annotated"
        assert "artifact_id" not in json.dumps(truncated["output"])  # preview text itself is bounded, not the full JSON

    assert truncated_a["artifact_id"] != truncated_b["artifact_id"], \
        "two distinct oversized results must land in two distinct artifacts"

    expected_sha_a = truncated_a["offload_sha256"]
    expected_sha_b = truncated_b["offload_sha256"]

    # --- "the server restarts": new engine bound to the same sqlite file,
    # a brand-new FastAPI app and TestClient, nothing carried over in
    # process memory except the paths (store dir, db file) a real restart
    # would also recover from disk/config. ---
    engine2 = _bind_database(sqlite_path, monkeypatch)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes import artifact_routes as routes

        monkeypatch.setattr(routes, "_owner", lambda request: owner)
        app = FastAPI()
        app.include_router(routes.setup_artifact_routes())

        with TestClient(app) as client:
            for artifact_id, expected_sha in (
                (truncated_a["artifact_id"], expected_sha_a),
                (truncated_b["artifact_id"], expected_sha_b),
            ):
                resp = client.get(f"/api/artifacts/{artifact_id}")
                assert resp.status_code == 200, resp.text
                meta = resp.json()["artifact"]
                assert meta["sha256"] == expected_sha
                assert meta["byte_size"] > 0

                download = client.get(meta["download_url"])
                assert download.status_code == 200, download.text
                recomputed = hashlib.sha256(download.content).hexdigest()
                assert recomputed == expected_sha, \
                    "sha256 recomputed from the downloaded bytes must match the stored one"

        # And the tool-facing range read also works post-restart, scoped by
        # the same real ownership check the routes use.
        read_a = offload.read_artifact_range(
            truncated_a["artifact_id"], owner=owner, start=0, end=200)
        assert read_a["text"], "range read must return real bytes after the restart"
        full_a = json.loads(
            offload.read_artifact_range(
                truncated_a["artifact_id"], owner=owner, start=0,
                end=read_a["total_chars"])["text"])
        assert full_a["output"] == result_a["output"], \
            "the FULL original text (not just the truncated preview) must be recoverable"
    finally:
        engine2.dispose()

    record_evidence(
        request,
        artifact_ids=[truncated_a["artifact_id"], truncated_b["artifact_id"]],
        session_id=session_id, run_id=run_id,
        module="src.tool_result_offload",
    )
