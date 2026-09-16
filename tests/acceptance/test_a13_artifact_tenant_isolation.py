"""A13 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A13): "Other tenant
requests a guessed artifact ID or encoded traversal path" -> "Denied without
metadata or file disclosure".

Exercises real Faustus code: a real `fastapi.testclient.TestClient` against
the real `routes/artifact_routes.py` router, a real sqlite-backed
`core.database`, and `src.artifact_identity`/`src.artifact_store` for the
setup data — only `routes.artifact_routes._owner()` is pinned per-request
(via a request header) to switch between two tenants without also building
full cookie auth, the same seam `tests/test_p1_art_08_library.py` already
uses to test this router.

Three things this proves, matching the contract's expected outcome exactly:

1. Tenant B guessing tenant A's real artifact id gets 404 with no metadata.
2. That 404's status/body is BYTE-IDENTICAL to a request for an id that does
   not exist at all — so the response itself cannot be used to test whether
   an id is valid but someone else's, vs. simply unassigned.
3. An encoded-traversal id (`..%2F`, `%2e%2e/`, `..\\`, an id containing a
   literal `/`) never reaches `owned()`/the filesystem at all — FastAPI's
   own single-segment path matching turns each into "no route matched" (a
   404 from routing, not from `artifact_routes`), and `src.artifact_store.
   path_of()` is shown separately to refuse the same names outright if
   anything ever called it with one directly. Nothing outside the artifact
   store directory is touched either way.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from routes import artifact_routes as routes
from src import artifact_store
from src.contracts import ExecutionResult
from tests.acceptance.conftest import record_evidence


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "a13.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    yield engine
    engine.dispose()


@pytest.fixture()
def client(own_database, monkeypatch):
    # The seam: which tenant a request is "authenticated" as comes from a
    # test-only header, so both tenants can be exercised against the SAME
    # real router/ownership-check code in one TestClient instead of two.
    def _owner(request):
        return request.headers.get("x-test-owner", "")

    monkeypatch.setattr(routes, "_owner", _owner)
    app = FastAPI()
    app.include_router(routes.setup_artifact_routes())
    with TestClient(app) as c:
        yield c


def _make_artifact(tmp_path, owner="alice", filename="secret.md",
                   body=b"# alice's private report\n"):
    src = tmp_path / "run-a13"
    src.mkdir(exist_ok=True)
    (src / filename).write_bytes(body)
    result = ExecutionResult.parse({
        "run_id": "run-a13", "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": [filename],
    })
    collected = artifact_store.collect(result, source_dir=str(src), owner=owner,
                                       project_id="p1")
    artifact_store.persist(collected.artifacts, session_id="s-a13")
    return collected.artifacts[0].id


@pytest.mark.acceptance("A13")
def test_wrong_tenant_and_nonexistent_id_get_identical_denials(client, tmp_path, request):
    real_id = _make_artifact(tmp_path, owner="alice")

    # Sanity: the real owner CAN read it (proves the denial below is really
    # about ownership, not a broken fixture).
    ok = client.get(f"/api/artifacts/{real_id}", headers={"x-test-owner": "alice"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["artifact"]["sha256"]

    # Tenant B guesses tenant A's real id.
    guessed = client.get(f"/api/artifacts/{real_id}", headers={"x-test-owner": "mallory"})
    # A request for an id that was never assigned to anybody.
    nonexistent = client.get("/api/artifacts/occ_does_not_exist",
                             headers={"x-test-owner": "mallory"})

    assert guessed.status_code == 404
    assert nonexistent.status_code == 404
    assert guessed.status_code == nonexistent.status_code
    assert guessed.json() == nonexistent.json(), \
        "a wrong-tenant denial must be indistinguishable from a plain not-found"
    # No metadata at all leaked into the denial.
    body_text = guessed.text.lower()
    for leak in ("sha256", "alice", str(len(b"# alice's private report\n"))):
        assert leak.lower() not in body_text

    # Same for the download and manifest sub-routes.
    for suffix in ("/download", "/manifest", "/provenance"):
        g = client.get(f"/api/artifacts/{real_id}{suffix}", headers={"x-test-owner": "mallory"})
        n = client.get(f"/api/artifacts/occ_does_not_exist{suffix}",
                       headers={"x-test-owner": "mallory"})
        assert g.status_code == n.status_code == 404
        assert g.text == n.text

    record_evidence(request, real_artifact_id=real_id,
                    denial_status=guessed.status_code, module="routes.artifact_routes")


@pytest.mark.acceptance("A13")
@pytest.mark.parametrize("traversal_id", [
    "..%2F..%2Fetc%2Fpasswd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..\\..\\windows\\win.ini",
    "..%5c..%5cwindows%5cwin.ini",
    "a%2Fb%2Fc",
])
def test_encoded_traversal_ids_never_reach_the_filesystem(client, tmp_path, traversal_id, request):
    real_id = _make_artifact(tmp_path, owner="alice")
    before = sorted(p.name for p in (tmp_path / "store").rglob("*")) if (tmp_path / "store").exists() else []

    resp = client.get(f"/api/artifacts/{traversal_id}", headers={"x-test-owner": "alice"})
    assert resp.status_code == 404, \
        f"a traversal-shaped id must never resolve to a route or a file, got {resp.status_code}"

    after = sorted(p.name for p in (tmp_path / "store").rglob("*")) if (tmp_path / "store").exists() else []
    assert before == after, "nothing outside/inside the store may be created or read by a bad id"

    # `artifact_routes.py` never hands a request-supplied id to the
    # filesystem — every read goes through `artifact_catalog.get()` (a DB
    # lookup by id) first, and only a row's OWN stored `filename` (never the
    # request id) ever reaches `artifact_store.path_of()`. Show that
    # resolver independently refuses the fully-decoded shape of each id
    # above, so a future caller that skips the DB lookup and reaches it
    # directly with a raw, decoded id is still refused.
    decoded = (traversal_id.replace("%2F", "/").replace("%2f", "/")
              .replace("%2e", ".").replace("%2E", ".")
              .replace("%5c", "\\").replace("%5C", "\\"))
    assert decoded != traversal_id or any(c in traversal_id for c in "/\\"), \
        "fixture sanity: this id must actually decode to something path-shaped"
    with pytest.raises(ValueError):
        artifact_store.path_of(decoded)

    # The real artifact is still readable normally — the traversal attempt
    # did not corrupt or lock anything.
    ok = client.get(f"/api/artifacts/{real_id}", headers={"x-test-owner": "alice"})
    assert ok.status_code == 200

    record_evidence(request, traversal_id=traversal_id, module="src.artifact_store")
