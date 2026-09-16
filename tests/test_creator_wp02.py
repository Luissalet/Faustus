"""WP02 — Creator domain (documents CAS + profile) and routes.

Real sqlite store, real threads for the concurrency case, real ProjectStore
for the profile case, real TestClient for the HTTP layer. No mocks except
the ``AUTH_ENABLED=false`` no-login mode every other route test in this
repo uses.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.projects import ProjectStore
from src.creator import profile as profile_mod
from src.creator.documents import DOCUMENT_KINDS
from src.creator.errors import DocumentNotFound, InvalidDocument, RevisionConflict
from src.creator.store import DocumentStore


def _canvas_content(width=64, height=64):
    return {
        "width": width,
        "height": height,
        "operation_semantics_version": 1,
        "layers": [],
        "base_asset_ref": "occ_base",
    }


# ----------------------------------------------------------------------
# DocumentStore — CAS, dedupe, conflicts, owner scoping
# ----------------------------------------------------------------------

def test_create_and_get_roundtrip(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    assert doc.revision == 1
    assert doc.state == "proposed"
    fetched = store.get("alice", doc.id)
    assert fetched is not None
    assert fetched.content == _canvas_content()


def test_invalid_content_is_rejected_structurally(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    with pytest.raises(InvalidDocument):
        store.create("alice", "proj1", "canvas", {"width": 1})  # missing required keys


def test_apply_command_bumps_revision_and_updates_content(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    result = store.apply_command(
        "alice", doc.id, "cmd-1", doc.revision,
        {"type": "patch_content", "patch": {"width": 128}},
    )
    assert result["applied"] is True
    assert result["deduped"] is False
    assert result["doc"].revision == 2
    assert result["doc"].content["width"] == 128


def test_dedupe_same_command_id_returns_same_result(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    op = {"type": "patch_content", "patch": {"width": 200}}
    r1 = store.apply_command("alice", doc.id, "cmd-x", doc.revision, op)
    r2 = store.apply_command("alice", doc.id, "cmd-x", doc.revision, op)
    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r1["doc"].revision == r2["doc"].revision == 2
    # Applying the SAME command_id again does not create a second revision.
    assert store.get("alice", doc.id).revision == 2


def test_dedupe_survives_even_if_expected_revision_now_looks_stale(tmp_path):
    """A retried command_id must return the original result even after the
    document has since moved on for OTHER reasons — dedupe is checked before
    the revision comparison, never mixed up with a fresh conflict."""
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    op1 = {"type": "patch_content", "patch": {"width": 111}}
    r1 = store.apply_command("alice", doc.id, "cmd-a", 1, op1)
    assert r1["doc"].revision == 2
    op2 = {"type": "patch_content", "patch": {"width": 222}}
    r2 = store.apply_command("alice", doc.id, "cmd-b", 2, op2)
    assert r2["doc"].revision == 3
    # Retry cmd-a with its ORIGINAL expected_revision (1) — now stale by
    # itself, but dedupe short-circuits before the conflict check.
    r1_retry = store.apply_command("alice", doc.id, "cmd-a", 1, op1)
    assert r1_retry["deduped"] is True
    assert r1_retry["doc"].revision == 2
    assert r1_retry["doc"].content["width"] == 111


def test_revision_conflict_raises_with_current_revision_and_writes_nothing(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    with pytest.raises(RevisionConflict) as exc_info:
        store.apply_command(
            "alice", doc.id, "cmd-stale", 999,
            {"type": "patch_content", "patch": {"width": 5}},
        )
    assert exc_info.value.current_revision == 1
    # Nothing lost / nothing written: document unchanged, no history entry.
    assert store.get("alice", doc.id).revision == 1
    assert store.history("alice", doc.id) == []


def test_conflict_then_correct_retry_with_same_command_id_still_applies(tmp_path):
    """A caller that got a 409, re-read, and retried the SAME command_id with
    the corrected expected_revision must actually apply — the failed attempt
    must not have been persisted as a dedupe entry."""
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    op = {"type": "patch_content", "patch": {"width": 42}}
    with pytest.raises(RevisionConflict):
        store.apply_command("alice", doc.id, "cmd-retry", 5, op)
    result = store.apply_command("alice", doc.id, "cmd-retry", 1, op)
    assert result["applied"] is True
    assert result["deduped"] is False
    assert result["doc"].revision == 2


def test_owner_mismatch_is_indistinguishable_from_nonexistent(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    assert store.get("mallory", doc.id) is None
    with pytest.raises(DocumentNotFound):
        store.apply_command(
            "mallory", doc.id, "cmd-1", doc.revision,
            {"type": "patch_content", "patch": {"width": 9}},
        )
    assert store.history("mallory", doc.id) == []


def test_history_records_command_id_expected_revision_op_and_result_revision(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())
    store.apply_command("alice", doc.id, "cmd-1", 1, {"type": "set_state", "state": "accepted"})
    store.apply_command("alice", doc.id, "cmd-2", 2, {"type": "patch_content", "patch": {"width": 7}})
    history = store.history("alice", doc.id)
    assert [h["command_id"] for h in history] == ["cmd-1", "cmd-2"]
    assert [h["result_revision"] for h in history] == [2, 3]
    assert history[0]["expected_revision"] == 1
    assert history[0]["op"] == {"type": "set_state", "state": "accepted"}


def test_cas_under_two_real_concurrent_writers_no_lost_update(tmp_path):
    """Two real OS threads race to bump the same document. SQLite's own
    BEGIN IMMEDIATE serializes them: exactly one of the two well-formed
    commands must see a 409, the other must apply, and the final revision
    must reflect exactly one successful write from this pair (2), never a
    silently lost update."""
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    doc = store.create("alice", "proj1", "canvas", _canvas_content())

    results = {}
    barrier = threading.Barrier(2)

    def worker(name, width):
        barrier.wait()
        try:
            r = store.apply_command(
                "alice", doc.id, f"cmd-{name}", 1,
                {"type": "patch_content", "patch": {"width": width}},
            )
            results[name] = ("ok", r["doc"].revision)
        except RevisionConflict as exc:
            results[name] = ("conflict", exc.current_revision)

    t1 = threading.Thread(target=worker, args=("t1", 111))
    t2 = threading.Thread(target=worker, args=("t2", 222))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    outcomes = [results["t1"][0], results["t2"][0]]
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 1
    final = store.get("alice", doc.id)
    assert final.revision == 2
    # The surviving writer's width made it in; nothing was silently dropped.
    winner = "t1" if results["t1"][0] == "ok" else "t2"
    expected_width = 111 if winner == "t1" else 222
    assert final.content["width"] == expected_width


def test_all_five_kinds_validate_a_minimal_valid_document(tmp_path):
    store = DocumentStore(str(tmp_path / "docs.sqlite3"))
    contents = {
        "canvas": _canvas_content(),
        "timeline": {
            "clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
            "duration_ticks": "100",
            "tracks": [{"id": "t1", "kind": "video", "locked": False, "clips": []}],
        },
        "transcript": {
            "source_asset_ref": "occ_audio",
            "sample_rate": 16000,
            "cues": [],
            "alignment_status": "not_aligned",
        },
        "song": {
            "language": "en",
            "sections": [{"id": "s1", "kind": "verse", "lyrics": "la la"}],
            "takes": [],
            "selected_take": None,
        },
        "storyboard": {
            "shots": [{
                "id": "sh1",
                "brief": "wide shot",
                "duration": {"start_ticks": "0", "duration_ticks": "10"},
                "clock": {"ticks_per_second_numerator": "24", "ticks_per_second_denominator": "1"},
                "reference_assets": [],
                "selected_take": None,
            }],
        },
    }
    assert set(contents) == set(DOCUMENT_KINDS)
    for kind, content in contents.items():
        doc = store.create("alice", "proj1", kind, content)
        assert doc.kind == kind
        assert doc.revision == 1


# ----------------------------------------------------------------------
# CreatorProfile — over ProjectStore.context_items, no parallel table
# ----------------------------------------------------------------------

def test_legacy_project_without_profile_returns_defaults_and_writes_nothing(tmp_path):
    ps = ProjectStore(str(tmp_path))
    project = ps.create("Legacy Proj", owner="alice", scaffold_memory=False)
    before = ps.get(project["id"], "alice")
    assert before["context_items"] == []

    result = profile_mod.get_profile(ps, "alice", project["id"])
    assert result["default_kind"] == "canvas"
    assert result["schema_version"] == profile_mod.PROFILE_SCHEMA_VERSION

    after = ps.get(project["id"], "alice")
    assert after["context_items"] == []  # nothing created by a read
    assert after["context_revision"] == before["context_revision"]


def test_set_profile_persists_as_a_single_context_item_not_a_new_table(tmp_path):
    ps = ProjectStore(str(tmp_path))
    project = ps.create("Proj", owner="alice", scaffold_memory=False)

    result = profile_mod.set_profile(ps, "alice", project["id"], {"default_kind": "timeline"})
    assert result["default_kind"] == "timeline"

    row = ps.get(project["id"], "alice")
    items = row["context_items"]
    assert len(items) == 1
    assert items[0]["kind"] == "document"
    assert items[0]["ref_id"] == profile_mod.PROFILE_REF_ID

    # A second set_profile updates the SAME item, does not add a second one.
    profile_mod.set_profile(ps, "alice", project["id"], {"default_kind": "song"})
    row2 = ps.get(project["id"], "alice")
    assert len(row2["context_items"]) == 1
    assert profile_mod.get_profile(ps, "alice", project["id"])["default_kind"] == "song"


def test_profile_on_foreign_or_missing_project_is_none(tmp_path):
    ps = ProjectStore(str(tmp_path))
    project = ps.create("Proj", owner="alice", scaffold_memory=False)
    assert profile_mod.get_profile(ps, "mallory", project["id"]) is None
    assert profile_mod.set_profile(ps, "mallory", project["id"], {"default_kind": "song"}) is None
    assert profile_mod.get_profile(ps, "alice", "no-such-project") is None


# ----------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------

@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects"))
    monkeypatch.setattr(projects_mod, "_store", ps)

    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "creator_docs.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_routes import setup_creator_routes
    app = FastAPI()
    app.include_router(setup_creator_routes())
    client = TestClient(app)
    project = ps.create("HTTP Proj", owner="__odysseus_local__", scaffold_memory=False)
    return client, project["id"]


def test_route_flag_off_is_404_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import services.projects as projects_mod
    ps = ProjectStore(str(tmp_path / "projects_off"))
    monkeypatch.setattr(projects_mod, "_store", ps)
    project = ps.create("Off Proj", owner="__odysseus_local__", scaffold_memory=False)

    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "creator_docs_off.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: False if key == "creator_enabled" else default)

    from routes.creator_routes import setup_creator_routes
    app = FastAPI()
    app.include_router(setup_creator_routes())
    client = TestClient(app)

    resp = client.get(f"/api/creator/documents?project_id={project['id']}")
    assert resp.status_code == 404
    resp2 = client.post("/api/creator/documents",
                         json={"project_id": project["id"], "kind": "canvas", "content": _canvas_content()})
    assert resp2.status_code == 404
    assert doc_store.list_for_project("__odysseus_local__", project["id"]) == []


def test_route_full_document_lifecycle(route_client):
    client, project_id = route_client

    caps = client.get(f"/api/creator/capabilities?project_id={project_id}")
    assert caps.status_code == 200
    assert set(caps.json()["document_kinds"]) == set(DOCUMENT_KINDS)

    created = client.post("/api/creator/documents",
                           json={"project_id": project_id, "kind": "canvas", "content": _canvas_content()})
    assert created.status_code == 200
    doc = created.json()
    assert doc["revision"] == 1

    fetched = client.get(f"/api/creator/documents/{doc['id']}")
    assert fetched.status_code == 200
    assert fetched.headers["etag"] == "1"

    cmd = client.post(
        f"/api/creator/documents/{doc['id']}/commands",
        json={"command_id": "c1", "expected_revision": 1,
              "op": {"type": "patch_content", "patch": {"width": 999}}},
    )
    assert cmd.status_code == 200
    body = cmd.json()
    assert body["document"]["revision"] == 2
    assert body["deduped"] is False

    conflict = client.post(
        f"/api/creator/documents/{doc['id']}/commands",
        json={"command_id": "c2", "expected_revision": 1,
              "op": {"type": "patch_content", "patch": {"width": 1}}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 2

    dedupe = client.post(
        f"/api/creator/documents/{doc['id']}/commands",
        json={"command_id": "c1", "expected_revision": 1,
              "op": {"type": "patch_content", "patch": {"width": 999}}},
    )
    assert dedupe.status_code == 200
    assert dedupe.json()["deduped"] is True

    hist = client.get(f"/api/creator/documents/{doc['id']}/history")
    assert hist.status_code == 200
    assert [h["command_id"] for h in hist.json()["commands"]] == ["c1"]


def test_route_document_owned_by_someone_else_is_404(route_client, tmp_path, monkeypatch):
    client, project_id = route_client
    created = client.post("/api/creator/documents",
                           json={"project_id": project_id, "kind": "canvas", "content": _canvas_content()})
    doc_id = created.json()["id"]

    # Force a distinct authenticated user for the next request — `get_current_user`
    # reads `request.state.current_user` directly, set here by a fake auth
    # middleware, independent of AUTH_ENABLED.
    from routes.creator_routes import setup_creator_routes
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.current_user = "mallory"
        return await call_next(request)

    app.include_router(setup_creator_routes())
    other_client = TestClient(app)
    resp = other_client.get(f"/api/creator/documents/{doc_id}")
    assert resp.status_code == 404


def test_route_profile_get_put_roundtrip(route_client):
    client, project_id = route_client
    got = client.get(f"/api/creator/profile?project_id={project_id}")
    assert got.status_code == 200
    assert got.json()["profile"]["default_kind"] == "canvas"

    put = client.put(f"/api/creator/profile?project_id={project_id}", json={"default_kind": "timeline"})
    assert put.status_code == 200
    assert put.json()["profile"]["default_kind"] == "timeline"

    got2 = client.get(f"/api/creator/profile?project_id={project_id}")
    assert got2.json()["profile"]["default_kind"] == "timeline"
