"""OPS-04 - portabilidad de proyectos y conocimiento (src/project_export.py).

Reuses, not reimplements: services.projects.ProjectStore (the one project
registry), src.project_identity (the .faustus/project.json marker), and the
real Session/ChatMessage tables (chats are DB rows, not files). Same test
harness as tests/test_project_identity.py -- a file-backed sqlite DB bound
onto core.database, and a ProjectStore of its own installed as the module
singleton -- reused rather than re-invented here.
"""
from __future__ import annotations

import json
import os
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base, ChatMessage as DbChatMessage, Session as DbSession
from services import projects as projects_mod
from services.projects import ProjectStore

from src import project_export as pex
from src import project_identity


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "export.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    return st


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def _add_session(Session, sid, *, project_id, folder=None, messages=()):
    db = Session()
    try:
        db.add(DbSession(id=sid, name=sid, endpoint_url="http://ep", model="m",
                         folder=folder, project_id=project_id))
        for i, (role, content) in enumerate(messages):
            db.add(DbChatMessage(id=f"{sid}-m{i}", session_id=sid, role=role, content=content))
        db.commit()
    finally:
        db.close()


# ── secret filtering ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name,is_secret", [
    (".env", True), (".env.production", True), ("api.key", True), ("cert.pem", True),
    ("credentials.json", True), ("my_secret_notes.md", True), ("token_cache.json", True),
    ("MEMORY.md", False), ("decisions.md", False), ("notes.txt", False),
])
def test_is_secret_name(name, is_secret):
    assert pex._is_secret_name(name) is is_secret


# ── export ───────────────────────────────────────────────────────────────


def test_export_includes_memory_files_and_excludes_secrets(db, store, workspace):
    project = store.create("Demo", workspace=workspace)
    mem_dir = os.path.join(workspace, ".odysseus")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "MEMORY.md"), "w") as f:
        f.write("# decisions\n- picked FastAPI")
    with open(os.path.join(mem_dir, ".env"), "w") as f:
        f.write("SECRET_KEY=do-not-export-me")

    bundle_path = pex.export_project(project["id"])
    with zipfile.ZipFile(bundle_path) as zf:
        names = zf.namelist()
        assert "files/.odysseus/MEMORY.md" in names
        assert "files/.odysseus/.env" not in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["project_id"] == project["id"]
        assert ".odysseus/.env" in manifest["excluded_for_secrets"]
        assert "skills" in manifest["not_covered"]


def test_export_includes_chats_bound_to_the_project(db, store, workspace):
    project = store.create("Demo", workspace=workspace)
    _add_session(db, "s1", project_id=project["id"],
                messages=[("user", "hello"), ("assistant", "hi there")])
    _add_session(db, "other-project-chat", project_id="not-this-project")

    bundle_path = pex.export_project(project["id"])
    with zipfile.ZipFile(bundle_path) as zf:
        chats = json.loads(zf.read("chats.json"))
        assert [c["id"] for c in chats] == ["s1"]
        assert [m["content"] for m in chats[0]["messages"]] == ["hello", "hi there"]


def test_export_unknown_project_raises(db, store):
    with pytest.raises(ValueError):
        pex.export_project("does-not-exist")


# ── preview_import: lost references are named, not dropped ─────────────────


def test_preview_import_reports_a_file_the_zip_no_longer_actually_has(db, store, workspace):
    project = store.create("Demo", workspace=workspace)
    os.makedirs(os.path.join(workspace, ".odysseus"), exist_ok=True)
    with open(os.path.join(workspace, ".odysseus", "MEMORY.md"), "w") as f:
        f.write("notes")
    bundle_path = pex.export_project(project["id"])

    # Simulate a truncated/tampered bundle: manifest still claims the file,
    # the zip entry is gone.
    tampered = bundle_path + ".tampered.zip"
    with zipfile.ZipFile(bundle_path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            if name == "files/.odysseus/MEMORY.md":
                continue
            dst.writestr(name, src.read(name))

    preview = pex.preview_import(tampered)
    assert preview["lost_references"] == [".odysseus/MEMORY.md"]


def test_preview_import_clean_bundle_has_no_lost_references(db, store, workspace):
    project = store.create("Demo", workspace=workspace)
    os.makedirs(os.path.join(workspace, ".odysseus"), exist_ok=True)
    with open(os.path.join(workspace, ".odysseus", "MEMORY.md"), "w") as f:
        f.write("notes")
    bundle_path = pex.export_project(project["id"])
    preview = pex.preview_import(bundle_path)
    assert preview["lost_references"] == []
    assert preview["file_count"] == 1


# ── import: dry_run writes nothing, real run does, ids are honest ─────────


def test_dry_run_import_writes_nothing(db, store, workspace, tmp_path):
    project = store.create("Demo", workspace=workspace)
    os.makedirs(os.path.join(workspace, ".odysseus"), exist_ok=True)
    with open(os.path.join(workspace, ".odysseus", "MEMORY.md"), "w") as f:
        f.write("notes")
    bundle_path = pex.export_project(project["id"])

    target = tmp_path / "elsewhere"
    target.mkdir()
    report = pex.import_project(bundle_path, target_workspace=str(target), dry_run=True)
    assert report["dry_run"] is True
    assert not os.path.exists(os.path.join(str(target), ".odysseus", "MEMORY.md"))
    assert not os.path.isfile(project_identity.marker_path(str(target)))


def test_import_onto_a_fresh_machine_writes_files_and_reports_new_id_honestly(db, store, workspace, tmp_path, monkeypatch):
    """KNOWN GAP (see module docstring + this lot's report):
    ProjectStore.create() cannot be told which id to use, so a first import
    onto a machine that never had this project gets a NEW id -- reported
    explicitly via id_preserved=False rather than pretended away.

    A "fresh machine" means a ProjectStore that has never heard of this
    project id -- not merely a different workspace path on the same store,
    so a second, empty store is installed right before the import call."""
    project = store.create("Demo", workspace=workspace)
    os.makedirs(os.path.join(workspace, ".odysseus"), exist_ok=True)
    with open(os.path.join(workspace, ".odysseus", "MEMORY.md"), "w") as f:
        f.write("# notes\ndecision: use sqlite")
    _add_session(db, "s1", project_id=project["id"], messages=[("user", "hi")])
    bundle_path = pex.export_project(project["id"])

    fresh_store = ProjectStore(str(tmp_path / "fresh_machine_data"))
    monkeypatch.setattr(projects_mod, "_store", fresh_store)

    target = tmp_path / "elsewhere"
    target.mkdir()
    report = pex.import_project(bundle_path, target_workspace=str(target), dry_run=False)

    assert report["id_preserved"] is False
    assert report["original_project_id"] == project["id"]
    assert report["project_id"] != project["id"]
    assert report["files_written"] == 1
    assert report["chats_imported"] == 1
    with open(os.path.join(str(target), ".odysseus", "MEMORY.md")) as f:
        assert f.read() == "# notes\ndecision: use sqlite"
    marker = project_identity.read_marker(str(target))
    assert marker["project_id"] == report["project_id"]

    # The imported chat is a NEW row (fresh id), never a collision with s1.
    new_id = report["imported_chat_ids"][0]
    assert new_id != "s1"
    reader = db()
    try:
        row = reader.query(DbSession).filter(DbSession.id == new_id).first()
        assert row is not None and row.project_id == report["project_id"]
        msgs = reader.query(DbChatMessage).filter(DbChatMessage.session_id == new_id).all()
        assert [m.content for m in msgs] == ["hi"]
    finally:
        reader.close()


def test_import_onto_the_same_registered_project_preserves_identity(db, store, workspace, tmp_path):
    """The case OPS-04's acceptance text is actually about: the SAME project
    (same id, already known to this store) moved to a new path."""
    project = store.create("Demo", workspace=workspace)
    os.makedirs(os.path.join(workspace, ".odysseus"), exist_ok=True)
    with open(os.path.join(workspace, ".odysseus", "MEMORY.md"), "w") as f:
        f.write("notes")
    bundle_path = pex.export_project(project["id"])

    new_disk_path = tmp_path / "new_disk_location"
    new_disk_path.mkdir()
    report = pex.import_project(bundle_path, target_workspace=str(new_disk_path), dry_run=False)
    assert report["id_preserved"] is True
    assert report["project_id"] == project["id"]


def test_import_refuses_to_overwrite_a_differently_marked_workspace(db, store, workspace, tmp_path):
    project = store.create("Demo", workspace=workspace)
    bundle_path = pex.export_project(project["id"])

    other = tmp_path / "someone_elses_project"
    other.mkdir()
    project_identity.write_marker(str(other), "a-totally-different-project-id")

    preview = pex.import_project(bundle_path, target_workspace=str(other), dry_run=True)
    assert preview["would_conflict"] is True
    with pytest.raises(ValueError, match="different project"):
        pex.import_project(bundle_path, target_workspace=str(other), dry_run=False)


def test_import_never_writes_outside_the_target_workspace(db, store, workspace, tmp_path):
    """A manifest entry with a path-traversal-shaped relative path must not
    escape target_workspace, even though export never produces one itself
    -- defence for a hand-edited or malicious bundle."""
    project = store.create("Demo", workspace=workspace)
    bundle_path = pex.export_project(project["id"])

    poisoned = bundle_path + ".poisoned.zip"
    with zipfile.ZipFile(bundle_path) as src, zipfile.ZipFile(poisoned, "w") as dst:
        manifest = json.loads(src.read("manifest.json"))
        manifest["files"] = [{"path": "../../../escape.txt", "sha256": pex._sha256_bytes(b"evil"), "bytes": 4}]
        for name in src.namelist():
            if name == "manifest.json":
                continue
            dst.writestr(name, src.read(name))
        dst.writestr("manifest.json", json.dumps(manifest))
        dst.writestr("files/../../../escape.txt", b"evil")

    target = tmp_path / "victim"
    target.mkdir()
    pex.import_project(poisoned, target_workspace=str(target), dry_run=False)
    assert not os.path.isfile(str(tmp_path / "escape.txt"))
