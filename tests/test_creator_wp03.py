"""WP03 — Creator media library and lineage.

Real sqlite (own engine, per ``test_artifact_identity.py``'s pattern), real
``artifact_store.collect()``/``persist()`` to produce occurrences, a real PNG
via Pillow for every image case, and real ffmpeg-generated fixture files
(``ffmpeg`` is installed in this environment — a system tool, not a model, so
using it to build a 1-second test clip is not "installing dependencies" or
"running inference") for the video/audio proxy path. ffprobe itself is
monkeypatched where the point of the test is the JSON-parsing/returncode
contract, not the real binary's output.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_identity as identity
from src import artifact_store
from src.contracts import ExecutionResult
from src.contracts.blob import ArtifactOccurrence, new_occurrence_id
from src.creator import library, lineage

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wp03.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    db_mod.Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def lib_db(tmp_path):
    return str(tmp_path / "creator" / "library.db")


def _png_bytes(size=(40, 30), color=(200, 40, 40)):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make(tmp_path, run_id, filename, body, *, owner="alice", project_id="p1",
         kind_hint=None, recipe="", skill_id=""):
    src_dir = tmp_path / f"src-{run_id}-{filename}"
    src_dir.mkdir(exist_ok=True)
    (src_dir / filename).write_bytes(body)
    result = ExecutionResult.parse({
        "run_id": run_id, "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": [filename],
    })
    provenance = {}
    if recipe:
        provenance["recipe"] = recipe
    collected = artifact_store.collect(result, source_dir=str(src_dir), owner=owner,
                                       project_id=project_id, skill_id=skill_id,
                                       skill_version="1.0.0" if skill_id else "",
                                       provenance=provenance or None)
    artifact_store.persist(collected.artifacts)
    return collected.artifacts[0]


def _tiny_video(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32:d=1",
                    "-frames:v", "5", str(path)], capture_output=True, timeout=30, check=True)
    return path.read_bytes()


def _tiny_audio(tmp_path, name="clip.wav"):
    path = tmp_path / name
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(path)], capture_output=True, timeout=30, check=True)
    return path.read_bytes()


# ── library.list_items ──────────────────────────────────────────────────

def test_list_items_requires_project_id(own_database, lib_db):
    with pytest.raises(ValueError):
        library.list_items("alice", "", db_path=lib_db)


def test_list_items_scopes_by_owner_project_kind_and_q(own_database, tmp_path, lib_db):
    img = _make(tmp_path, "r1", "pic.png", _png_bytes(), owner="alice", project_id="p1")
    doc = _make(tmp_path, "r2", "notes.md", b"# hi\n", owner="alice", project_id="p1")
    _make(tmp_path, "r3", "other.png", _png_bytes(), owner="alice", project_id="OTHER")
    _make(tmp_path, "r4", "mallory.png", _png_bytes(), owner="mallory", project_id="p1")

    items = library.list_items("alice", "p1", db_path=lib_db)
    assert {i["id"] for i in items} == {img.id, doc.id}

    only_images = library.list_items("alice", "p1", kind="image", db_path=lib_db)
    assert [i["id"] for i in only_images] == [img.id]

    by_q = library.list_items("alice", "p1", q="notes", db_path=lib_db)
    assert [i["id"] for i in by_q] == [doc.id]


def test_list_items_tag_matches_recipe_or_skill_id(own_database, tmp_path, lib_db):
    tagged = _make(tmp_path, "r1", "a.png", _png_bytes(), recipe="creator.hero_shot")
    untagged = _make(tmp_path, "r2", "b.png", _png_bytes())

    result = library.list_items("alice", "p1", tag="creator.hero_shot", db_path=lib_db)
    assert [i["id"] for i in result] == [tagged.id]
    assert untagged.id not in {i["id"] for i in result}


def test_list_items_attaches_image_dimensions_and_caches(own_database, tmp_path, lib_db):
    img = _make(tmp_path, "r1", "pic.png", _png_bytes(size=(40, 30)), owner="alice")
    items = library.list_items("alice", "p1", db_path=lib_db)
    entry = next(i for i in items if i["id"] == img.id)
    assert entry["media"]["width"] == 40
    assert entry["media"]["height"] == 30
    assert entry["media"]["probe_tool"] == "pillow"
    assert entry["media"]["cached"] is False

    again = library.list_items("alice", "p1", db_path=lib_db)
    entry2 = next(i for i in again if i["id"] == img.id)
    assert entry2["media"]["cached"] is True
    assert entry2["media"]["width"] == 40


def test_list_items_document_kind_has_no_media_block(own_database, tmp_path, lib_db):
    doc = _make(tmp_path, "r1", "notes.md", b"# hi\n", owner="alice")
    items = library.list_items("alice", "p1", db_path=lib_db)
    entry = next(i for i in items if i["id"] == doc.id)
    assert entry["media"] is None


def test_extract_metadata_video_uses_ffprobe_when_returncode_is_zero(own_database, lib_db):
    fake_json = json.dumps({
        "format": {"duration": "3.5"},
        "streams": [{"codec_type": "video", "width": 640, "height": 360,
                     "codec_name": "h264"}],
    })

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_json, stderr="")

    meta = library.extract_metadata(
        sha256="a" * 64, path="/does/not/matter.mp4", kind="video",
        db_path=lib_db, which=lambda name: "/usr/bin/ffprobe", run=fake_run)
    assert meta["duration"] == 3.5
    assert meta["width"] == 640
    assert meta["codec"] == "h264"
    assert meta["probe_tool"] == "ffprobe"


def test_extract_metadata_video_ignores_a_nonzero_returncode(own_database, lib_db):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="{}", stderr="boom")

    meta = library.extract_metadata(
        sha256="b" * 64, path="/x.mp4", kind="video", db_path=lib_db,
        which=lambda name: "/usr/bin/ffprobe", run=fake_run)
    assert meta["probe_tool"] == "none"
    assert meta["duration"] is None


def test_extract_metadata_without_ffprobe_installed_is_a_documented_gap(own_database, lib_db):
    meta = library.extract_metadata(
        sha256="c" * 64, path="/x.mp4", kind="video", db_path=lib_db,
        which=lambda name: None)
    assert meta["probe_tool"] == "none"
    assert meta["duration"] is None


# ── proxies: image (PIL, always available) ──────────────────────────────

def test_generate_image_proxy_is_a_new_owner_scoped_occurrence(own_database, tmp_path):
    src = _make(tmp_path, "r1", "pic.png", _png_bytes(size=(800, 600)), owner="alice")
    result = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    proxy = result["occurrence"]
    assert result["created"] is True
    assert proxy["id"] != src.id
    assert proxy["derived_from"] == [src.id]
    assert proxy["kind"] == "image"
    assert proxy["recipe"] == "creator.proxy.image_thumbnail"

    # The proxy is a real, owner-scoped occurrence readable back through identity.
    fetched = identity.for_owner(proxy["id"], owner="alice")
    assert fetched.owner == "alice"
    assert fetched.relations.derived_from == (src.id,)


def test_generate_image_proxy_is_idempotent(own_database, tmp_path):
    src = _make(tmp_path, "r1", "pic.png", _png_bytes(), owner="alice")
    first = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    second = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    assert first["occurrence"]["id"] == second["occurrence"]["id"]
    assert first["created"] is True
    assert second["created"] is False


def test_generate_proxy_two_owners_of_identical_bytes_get_two_proxies(own_database, tmp_path):
    """AST02's acceptance line: bytes identical does not mean permissions
    shared. Both owners publish the SAME PNG bytes (one blob) but each gets
    their OWN proxy occurrence, never a shared one."""
    body = _png_bytes()
    alice_src = _make(tmp_path, "r1", "pic.png", body, owner="alice", project_id="p1")
    bob_src = _make(tmp_path, "r2", "pic.png", body, owner="bob", project_id="p1")
    assert alice_src.sha256 == bob_src.sha256  # same bytes, deduplicated blob
    assert alice_src.id != bob_src.id          # different occurrences

    a_proxy = library.generate_proxy("alice", alice_src.id, store_dir=str(tmp_path / "store"))
    b_proxy = library.generate_proxy("bob", bob_src.id, store_dir=str(tmp_path / "store"))
    assert a_proxy["occurrence"]["id"] != b_proxy["occurrence"]["id"]
    with pytest.raises(identity.NotTheOwner):
        identity.for_owner(a_proxy["occurrence"]["id"], owner="bob")


def test_generate_proxy_owner_mismatch_is_not_found(own_database, tmp_path):
    src = _make(tmp_path, "r1", "pic.png", _png_bytes(), owner="alice")
    with pytest.raises(library.LibraryItemNotFound):
        library.generate_proxy("mallory", src.id, store_dir=str(tmp_path / "store"))


def test_generate_proxy_unknown_kind_is_rejected(own_database, tmp_path):
    doc = _make(tmp_path, "r1", "notes.md", b"# hi\n", owner="alice")
    with pytest.raises(ValueError):
        library.generate_proxy("alice", doc.id, store_dir=str(tmp_path / "store"))


def test_generate_video_proxy_without_ffmpeg_is_409_worthy(own_database, tmp_path):
    body = _tiny_video(tmp_path) if FFMPEG else b"not a real video"
    src = _make(tmp_path, "r1", "clip.mp4", body, owner="alice")
    with pytest.raises(library.ProxyUnavailable):
        library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"),
                               which=lambda name: None)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed on this machine")
def test_generate_video_proxy_with_real_ffmpeg(own_database, tmp_path):
    src = _make(tmp_path, "r1", "clip.mp4", _tiny_video(tmp_path), owner="alice")
    result = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    proxy = result["occurrence"]
    assert proxy["kind"] == "image"
    assert proxy["recipe"] == "creator.proxy.video_frame"
    assert proxy["derived_from"] == [src.id]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed on this machine")
def test_generate_audio_proxy_with_real_ffmpeg(own_database, tmp_path):
    src = _make(tmp_path, "r1", "clip.wav", _tiny_audio(tmp_path), owner="alice")
    result = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    proxy = result["occurrence"]
    assert proxy["recipe"] == "creator.proxy.audio_waveform"
    assert proxy["derived_from"] == [src.id]


# ── lineage ──────────────────────────────────────────────────────────────

def _link(owner, project_id, parent_ids, *, recipe="creator.compose",
         recipe_version="2", engine="comfyui", engine_job_id="job-1"):
    """A minimal occurrence with a `derived_from` relation, for lineage tests
    that do not need a real proxy render."""
    body = _png_bytes()
    import hashlib
    digest = hashlib.sha256(body + str(parent_ids).encode()).hexdigest()
    identity.ensure_blob(sha256=digest, byte_size=len(body), filename=f"{digest}.png",
                         media_type="image/png")
    occ = ArtifactOccurrence.parse({
        "id": new_occurrence_id(), "kind": "image", "blob_sha256": digest,
        "owner": owner, "project_id": project_id,
        "provenance": {"recipe": recipe, "recipe_version": recipe_version,
                       "engine": engine, "engine_job_id": engine_job_id,
                       "source_artifact_ids": list(parent_ids)},
        "relations": {"derived_from": list(parent_ids)},
    })
    return identity.record_occurrence(occ)


def test_direct_parents_reads_the_relations_field(own_database):
    root = _link("alice", "p1", [])
    child = _link("alice", "p1", [root.id])
    edges = lineage.direct_parents("alice", child.id)
    assert edges == [{
        "child_occurrence": child.id, "parent_occurrence": root.id,
        "recipe_id": "creator.compose@2", "params_hash": "",
        "engine_build": "job-1",
    }]


def test_ancestors_walks_a_chain_and_dedupes_diamonds(own_database):
    root = _link("alice", "p1", [])
    mid = _link("alice", "p1", [root.id])
    leaf = _link("alice", "p1", [mid.id, root.id])  # diamond: two paths to root
    edges = lineage.ancestors("alice", leaf.id)
    pairs = {(e["child_occurrence"], e["parent_occurrence"]) for e in edges}
    assert pairs == {(leaf.id, mid.id), (leaf.id, root.id), (mid.id, root.id)}


def test_descendants_walks_forward_through_the_project(own_database):
    root = _link("alice", "p1", [])
    mid = _link("alice", "p1", [root.id])
    leaf = _link("alice", "p1", [mid.id])
    edges = lineage.descendants("alice", "p1", root.id)
    pairs = {(e["child_occurrence"], e["parent_occurrence"]) for e in edges}
    assert pairs == {(mid.id, root.id), (leaf.id, mid.id)}


def test_lineage_owner_mismatch_is_not_found(own_database):
    root = _link("alice", "p1", [])
    with pytest.raises(lineage.LineageItemNotFound):
        lineage.direct_parents("mallory", root.id)


def test_rebuild_graph_scopes_to_owner_and_project(own_database):
    a_root = _link("alice", "p1", [])
    a_child = _link("alice", "p1", [a_root.id])
    _link("bob", "p1", [])  # different owner, must not appear
    _link("alice", "p2", [])  # different project, must not appear
    graph = lineage.rebuild_graph("alice", "p1")
    assert {n["occurrence_id"] for n in graph["nodes"]} == {a_root.id, a_child.id}
    assert len(graph["edges"]) == 1


# ── export manifest ──────────────────────────────────────────────────────

def test_export_manifest_enumerates_concrete_occurrences_with_lineage(own_database, tmp_path, lib_db):
    src = _make(tmp_path, "r1", "pic.png", _png_bytes(), owner="alice", project_id="p1",
               recipe="creator.import")
    proxy_result = library.generate_proxy("alice", src.id, store_dir=str(tmp_path / "store"))
    manifest = library.export_manifest("alice", "p1")
    assert manifest["project_id"] == "p1"
    by_id = {e["occurrence_id"]: e for e in manifest["occurrences"]}
    assert src.id in by_id
    assert by_id[src.id]["sha256"] == src.sha256
    assert by_id[src.id]["byte_size"] == src.byte_size
    proxy_id = proxy_result["occurrence"]["id"]
    assert proxy_id in by_id
    assert by_id[proxy_id]["derived_from"][0]["parent_occurrence"] == src.id


def test_export_manifest_requires_project_id(own_database):
    with pytest.raises(ValueError):
        library.export_manifest("alice", "")


# ── HTTP layer ─────────────────────────────────────────────────────────

@pytest.fixture()
def route_client(own_database, tmp_path, monkeypatch, lib_db):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)
    import src.creator.library as library_mod
    monkeypatch.setattr(library_mod, "default_db_path", lambda: lib_db)

    from routes.creator_library_routes import setup_creator_library_routes
    app = FastAPI()
    app.include_router(setup_creator_library_routes())
    return TestClient(app), tmp_path


def test_route_flag_off_is_404(own_database, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "creator_enabled" else default)
    from routes.creator_library_routes import setup_creator_library_routes
    app = FastAPI()
    app.include_router(setup_creator_library_routes())
    client = TestClient(app)
    assert client.get("/api/creator/library?project_id=p1").status_code == 404
    assert client.get("/api/creator/library/occ_x/lineage").status_code == 404
    assert client.post("/api/creator/library/occ_x/proxy").status_code == 404
    assert client.get("/api/creator/library/export-manifest?project_id=p1").status_code == 404


def test_route_list_requires_project_id(route_client):
    client, _ = route_client
    resp = client.get("/api/creator/library")
    assert resp.status_code == 400


def test_route_list_and_proxy_and_lineage_and_manifest(route_client, tmp_path):
    client, tmp = route_client
    src = _make(tmp, "r1", "pic.png", _png_bytes(), owner="__odysseus_local__", project_id="p1")

    listed = client.get("/api/creator/library?project_id=p1")
    assert listed.status_code == 200
    assert {i["id"] for i in listed.json()["items"]} == {src.id}

    proxy_resp = client.post(f"/api/creator/library/{src.id}/proxy")
    assert proxy_resp.status_code == 200
    proxy_id = proxy_resp.json()["occurrence"]["id"]

    lineage_resp = client.get(f"/api/creator/library/{proxy_id}/lineage")
    assert lineage_resp.status_code == 200
    body = lineage_resp.json()
    assert body["ancestors"][0]["parent_occurrence"] == src.id

    manifest_resp = client.get("/api/creator/library/export-manifest?project_id=p1")
    assert manifest_resp.status_code == 200
    ids = {e["occurrence_id"] for e in manifest_resp.json()["occurrences"]}
    assert {src.id, proxy_id} <= ids


def test_route_proxy_video_without_ffmpeg_is_409(route_client, tmp_path, monkeypatch):
    client, tmp = route_client
    body = _tiny_video(tmp_path) if FFMPEG else b"not a real video"
    src = _make(tmp, "r1", "clip.mp4", body, owner="__odysseus_local__")

    import src.creator.library as library_mod
    monkeypatch.setattr(library_mod.shutil, "which", lambda name: None)
    resp = client.post(f"/api/creator/library/{src.id}/proxy")
    assert resp.status_code == 409


def test_route_owner_mismatch_is_404(route_client, tmp_path):
    client, tmp = route_client
    src = _make(tmp, "r1", "pic.png", _png_bytes(), owner="someone_else", project_id="p1")

    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.current_user = "mallory"
        return await call_next(request)

    from routes.creator_library_routes import setup_creator_library_routes
    app.include_router(setup_creator_library_routes())
    other_client = TestClient(app)
    resp = other_client.post(f"/api/creator/library/{src.id}/proxy")
    assert resp.status_code == 404
    resp2 = other_client.get(f"/api/creator/library/{src.id}/lineage")
    assert resp2.status_code == 404
