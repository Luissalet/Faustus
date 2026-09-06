"""Chat-to-project identity: the folder organises, `project_id` identifies.

Until now a chat found its project through `sessions.folder` — a string the
user renames in the sidebar. Renaming it broke the link silently, and dragging
a chat into another folder re-homed it to a different project without anyone
deciding that. These tests pin the new order: the stable id first, the folder
only as a fallback, and never the other way round.
"""

import dataclasses
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database as db_mod  # noqa: E402
from core.database import Base, Session as DbSession  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from services.projects import ProjectStore  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A file-backed sqlite DB bound everywhere the code under test reads it.

    File-backed rather than in-memory: `services.projects` opens its own
    session through a lazy import, and the backfill has to be visible to a
    later read from a different connection.
    """
    import core.session_manager as sm_mod

    url = "sqlite:///" + (tmp_path / "identity.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A ProjectStore of its own, installed as the module singleton."""
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    return st


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


def _add_session(Session, sid, *, folder=None, project_id=None, owner=None):
    db = Session()
    try:
        db.add(DbSession(
            id=sid, name=sid, endpoint_url="http://ep", model="m",
            folder=folder, project_id=project_id, owner=owner,
        ))
        db.commit()
    finally:
        db.close()


def _row(Session, sid):
    db = Session()
    try:
        return db.query(DbSession).filter(DbSession.id == sid).first()
    finally:
        db.close()


# ── resolution order ──────────────────────────────────────────────────


def test_project_id_survives_renaming_the_folder(db, store, workspace):
    """The whole point. The chat is bound to the project, not to a string the
    user is free to retype in the sidebar."""
    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    _add_session(db, "s1", folder="Faustus", project_id=project["id"])

    store.update(project["id"], {"folder": "Faustus (2026)"})
    db_row = _row(db, "s1")
    assert db_row.folder == "Faustus"          # the chat was NOT moved

    resolved, source = projects_mod._resolve_project_for_session("s1")
    assert resolved["id"] == project["id"]
    assert source == "direct"


def test_a_legacy_session_resolves_by_folder_and_is_backfilled(db, store, workspace):
    """Existing chats have no project_id. They keep working through the folder,
    and the id is written once so the next rename cannot break them."""
    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    _add_session(db, "s1", folder="Faustus")
    assert _row(db, "s1").project_id is None

    resolved, source = projects_mod._resolve_project_for_session("s1")
    assert resolved["id"] == project["id"]
    assert source == "legacy_folder"
    assert _row(db, "s1").project_id == project["id"]

    # And now it is direct — the fallback ran exactly once.
    assert projects_mod._resolve_project_for_session("s1")[1] == "direct"


def test_an_ambiguous_folder_is_never_backfilled(db, store, tmp_path, caplog):
    """Two projects claiming one folder is a conflict to report, not a coin to
    flip: guessing would bind the chat to a project the user never chose, and
    the guess would then outlive the ambiguity."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # Written straight to the file: store.create() refuses a duplicate folder,
    # so this state only arises from a hand-edited or pre-guard projects.json.
    store.create("Alpha", folder="Shared", workspace=str(tmp_path / "a"))
    rows = store._load()
    rows.append({
        "id": "beta000", "owner": None, "enabled": True, "name": "Beta",
        "folder": "Shared", "workspace": str(tmp_path / "b"), "instructions": "",
        "context_items": [], "pinned": False, "archived": False,
    })
    store._save(rows)

    _add_session(db, "s1", folder="Shared")
    resolved, source = projects_mod._resolve_project_for_session("s1")

    assert source == "legacy_folder"
    assert resolved is not None                      # still resolves by folder
    assert _row(db, "s1").project_id is None         # but nothing was decided
    assert "claimed by 2 projects" in caplog.text


def test_a_disabled_project_still_means_no_project(db, store, workspace):
    """Unchanged from before `project_id` existed, and it must stay unchanged:
    disabling a project is how the user turns its context off."""
    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    store.update(project["id"], {"enabled": False})
    _add_session(db, "legacy", folder="Faustus")
    _add_session(db, "bound", folder="Faustus", project_id=project["id"])

    assert projects_mod.project_for_session("legacy") is None
    assert projects_mod.project_for_session("bound") is None
    assert _row(db, "legacy").project_id is None      # and nothing was bound


def test_no_folder_and_no_id_is_simply_no_project(db, store, workspace):
    store.create("Faustus", folder="Faustus", workspace=workspace)
    _add_session(db, "s1")
    assert projects_mod._resolve_project_for_session("s1") == (None, "none")


def test_another_owner_gets_nothing(db, store, workspace):
    """Not an error and not a redacted name — just no project."""
    project = store.create("Faustus", folder="Faustus", workspace=workspace, owner="luis")
    _add_session(db, "s1", folder="Faustus", project_id=project["id"], owner="luis")

    assert projects_mod.project_for_session("s1", "otro") is None
    ctx = projects_mod.project_context_for_session("s1", "otro")
    assert ctx.project_id == "" and ctx.project_name == "" and ctx.source == "none"


def test_it_never_raises_even_with_a_broken_database(db, store, monkeypatch):
    """It runs on every turn of every chat. A broken DB must cost the project
    block, never the message."""
    def explode():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db_mod, "SessionLocal", explode)
    assert projects_mod.project_for_session("s1") is None
    assert projects_mod.project_context_for_session("s1").source == "none"


def test_the_execution_context_carries_the_scope(db, store, workspace):
    project = store.create("Faustus", folder="Faustus", workspace=workspace, owner="luis")
    _add_session(db, "s1", folder="Faustus", project_id=project["id"], owner="luis")

    ctx = projects_mod.project_context_for_session("s1", "luis")
    assert ctx.project_id == project["id"]
    assert ctx.project_name == "Faustus"
    assert ctx.workspace == project["workspace"]
    assert ctx.session_id == "s1"
    assert ctx.source == "direct"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.project_id = "otro"          # a run cannot change project half-way


# ── create_session persists what it is given ──────────────────────────


def test_create_session_persists_folder_mode_and_project(db, store, workspace):
    """The latent subagent bug. `delegate_agents` set `child.folder` and
    `child.mode` on the returned object inside a try/except; the dataclass
    accepted the attributes, nothing persisted them, and every child session
    ended up with folder NULL — which, under folder-based resolution, meant no
    project at all."""
    from core.session_manager import SessionManager

    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    manager = SessionManager()
    manager.create_session(
        session_id="child", name="worker", endpoint_url="http://ep", model="m",
        folder="Agents", mode="agent", project_id=project["id"],
    )

    row = _row(db, "child")
    assert row.folder == "Agents"
    assert row.mode == "agent"
    assert row.project_id == project["id"]
    assert row.to_dict()["project_id"] == project["id"]


def test_set_session_project_validates_ownership(db, store, workspace):
    project = store.create("Faustus", folder="Faustus", workspace=workspace, owner="luis")
    _add_session(db, "s1", folder="Otra", owner="luis")

    from core.session_manager import SessionManager
    manager = SessionManager()

    assert manager.set_session_project("s1", project["id"], owner="otro") is False
    assert _row(db, "s1").project_id is None

    assert manager.set_session_project("s1", project["id"], owner="luis") is True
    assert _row(db, "s1").project_id == project["id"]

    assert manager.set_session_project("ghost", project["id"]) is False


# ── the route ─────────────────────────────────────────────────────────


def test_patching_the_folder_does_not_change_the_project(db, store, workspace, monkeypatch):
    """Moving a chat between sidebar folders is organisation. Changing which
    project it belongs to is a decision, and it is a different operation."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes.session_routes as sr
    from core.session_manager import SessionManager

    monkeypatch.setattr(sr, "SessionLocal", db)
    monkeypatch.setattr(sr, "_auth_disabled", lambda: True)

    project = store.create("Faustus", folder="Faustus", workspace=workspace)
    _add_session(db, "s1", folder="Faustus", project_id=project["id"])

    manager = SessionManager()
    app = FastAPI()
    app.include_router(sr.setup_session_routes(manager, {"REQUEST_TIMEOUT": 5}))
    client = TestClient(app)

    moved = client.patch("/api/session/s1", data={"folder": "Archivo 2026"})
    assert moved.status_code == 200
    assert moved.json()["folder"] == "Archivo 2026"

    row = _row(db, "s1")
    assert row.folder == "Archivo 2026"
    assert row.project_id == project["id"]
    assert projects_mod._resolve_project_for_session("s1") == (
        projects_mod.project_for_session("s1"), "direct",
    )
    assert projects_mod.project_for_session("s1")["id"] == project["id"]
