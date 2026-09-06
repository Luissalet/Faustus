"""Turn references, wired to the tools that produce them (plan §10, §11).

``tests/test_project_context_references.py`` proves the registry decides
correctly. This file proves the two things that make that decision reachable
at all:

* the document tools actually RECORD what they produced, with the session and
  owner that scope it — without which "this document" falls back to a
  process-wide pointer that two chats share;
* a child session created from a chat with a project resolves the SAME project
  as its parent, so a delegated worker inherits identity instead of
  re-deriving it from a folder name it does not have.

The second one is the invariant plan §11.5 states: a child inherits its
project before its first prompt or tool call.
"""

import asyncio
import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import core.session_manager as sm_mod
import services.projects as projects_mod
import src.database as src_db
import src.project_context.references as refs
from src.agent_tools import document_tools
from src.agent_tools.document_tools import (
    CreateDocumentTool, EditDocumentTool, UpdateDocumentTool, set_active_document,
)
from src.agent_tools.subagent_tools import SUBAGENT_FOLDER
from src.project_context.references import TurnReferenceRegistry

OWNER = "luis"
OTHER = "mallory"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real schema on a temp file, bound everywhere the tools look it up."""
    url = "sqlite:///" + (tmp_path / "refs.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(src_db, "SessionLocal", factory)
    monkeypatch.setattr(sm_mod, "SessionLocal", factory)
    return factory


@pytest.fixture
def turn_registry(monkeypatch):
    """A private registry for the test, with no ambient active pointer: the
    process singleton would otherwise carry entries between test modules."""
    reg = TurnReferenceRegistry(active_provider=lambda session_id, owner: ("", ""))
    monkeypatch.setattr(refs, "_REGISTRY", reg)
    set_active_document(None)
    return reg


def make_session(db, session_id="s1", *, owner=OWNER, folder=None, project_id=None):
    session = db()
    try:
        session.add(cdb.Session(id=session_id, name=session_id,
                                endpoint_url="http://localhost", model="m",
                                owner=owner, rag=False, headers={},
                                folder=folder, project_id=project_id))
        session.commit()
    finally:
        session.close()
    return session_id


def create_document(session_id="s1", *, title="Voice architecture", owner=OWNER,
                    turn_id="t1", run_id="r1", body="# Voice\n\nDecisions.\n"):
    return asyncio.run(CreateDocumentTool().execute(
        f"{title}\nmarkdown\n{body}",
        {"session_id": session_id, "owner": owner, "turn_id": turn_id, "run_id": run_id},
    ))


# ── the producers record what they produced ───────────────────────────────

def test_creating_a_document_leaves_a_created_reference(db, turn_registry):
    make_session(db)
    result = create_document()
    assert result.get("doc_id"), result

    recorded = turn_registry.for_turn("s1", owner=OWNER)
    assert len(recorded) == 1
    ref = recorded[0]
    assert ref.kind == "document"
    assert ref.ref_id == result["doc_id"]
    assert ref.relation == "created"
    assert ref.label == "Voice architecture"
    assert ref.source_tool == "create_document"


def test_the_reference_carries_the_session_and_the_owner_that_scope_it(db,
                                                                       turn_registry):
    """Without these two the registry is a process-wide pointer again."""
    make_session(db)
    create_document()
    ref = turn_registry.for_turn("s1", owner=OWNER)[0]
    assert ref.session_id == "s1"
    assert ref.owner == OWNER
    assert ref.turn_id == "t1"
    assert ref.run_id == "r1"


def test_two_documents_in_one_turn_are_two_references(db, turn_registry):
    """The case the process-global pointer cannot represent: after this turn
    the pointer holds Spec B alone, and the registry holds both."""
    make_session(db)
    first = create_document(title="Spec A")
    second = create_document(title="Spec B")

    recorded = turn_registry.for_turn("s1", turn_id="t1", owner=OWNER)
    assert [r.ref_id for r in recorded] == [first["doc_id"], second["doc_id"]]
    assert {r.label for r in recorded} == {"Spec A", "Spec B"}
    assert document_tools.get_active_document() == second["doc_id"]


def test_the_registry_does_not_cross_owners(db, turn_registry):
    make_session(db, "s1", owner=OWNER)
    make_session(db, "s2", owner=OTHER)
    create_document("s1", title="Mine", owner=OWNER)
    create_document("s2", title="Theirs", owner=OTHER)

    mine = turn_registry.for_turn("s1", owner=OWNER)
    theirs = turn_registry.for_turn("s2", owner=OTHER)
    assert [r.label for r in mine] == ["Mine"]
    assert [r.label for r in theirs] == ["Theirs"]
    assert turn_registry.for_turn("s1", owner=OTHER) == []


def test_editing_a_document_records_it_as_opened_not_created(db, turn_registry):
    make_session(db)
    doc_id = create_document()["doc_id"]

    asyncio.run(UpdateDocumentTool().execute(
        "# Voice\n\nRewritten.\n",
        {"session_id": "s1", "owner": OWNER, "doc_id": doc_id, "turn_id": "t2"}))
    asyncio.run(EditDocumentTool().execute(
        "<<<FIND>>>\nRewritten.\n<<<REPLACE>>>\nEdited.\n<<<END>>>",
        {"session_id": "s1", "owner": OWNER, "doc_id": doc_id, "turn_id": "t3"}))

    by_relation = [(r.relation, r.turn_id, r.source_tool)
                   for r in turn_registry.for_turn("s1", owner=OWNER)]
    assert by_relation == [
        ("created", "t1", "create_document"),
        ("opened", "t2", "update_document"),
        ("opened", "t3", "edit_document"),
    ]


# ── a child session inherits its parent's project (plan §11) ──────────────

@pytest.fixture
def project_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (data_dir / "projects.json").write_text(json.dumps([{
        "id": "prj_1", "name": "Faustus", "owner": OWNER, "enabled": True,
        "folder": "Faustus", "workspace": str(workspace), "context_items": [],
    }]), encoding="utf-8")
    store = projects_mod.ProjectStore(str(data_dir))
    monkeypatch.setattr(projects_mod, "_store", store)
    return store


def test_a_child_session_resolves_the_same_project_as_its_parent(db, project_store):
    """The invariant of plan §11.5. Note the child is filed under the
    sub-agent folder, which no project claims — so the folder fallback cannot
    be what resolves it. Only the inherited project_id can."""
    make_session(db, "parent", folder="Faustus", project_id="prj_1")
    parent = projects_mod.project_context_for_session("parent", OWNER)
    assert parent.project_id == "prj_1"

    manager = sm_mod.SessionManager()
    manager.create_session(
        session_id="child", name="🤖 worker", endpoint_url="http://localhost",
        model="m", rag=False, owner=OWNER,
        folder=SUBAGENT_FOLDER, mode="agent", project_id=parent.project_id,
    )

    child = projects_mod.project_context_for_session("child", OWNER)
    assert child.project_id == parent.project_id
    assert child.project_name == parent.project_name
    assert child.source == "direct"

    session = db()
    try:
        row = session.query(cdb.Session).filter(cdb.Session.id == "child").first()
        assert row.folder == SUBAGENT_FOLDER
        assert row.project_id == "prj_1"
    finally:
        session.close()
    assert project_store.get_by_folder(SUBAGENT_FOLDER, OWNER) is None


def test_a_child_of_a_project_less_chat_inherits_nothing(db, project_store):
    """The empty context is a value, not a crash: no project in, no project out."""
    make_session(db, "loose")
    parent = projects_mod.project_context_for_session("loose", OWNER)
    assert parent.project_id == "" and parent.source == "none"

    manager = sm_mod.SessionManager()
    manager.create_session(session_id="child2", name="worker",
                           endpoint_url="http://localhost", model="m", owner=OWNER,
                           folder=SUBAGENT_FOLDER, mode="agent",
                           project_id=parent.project_id or None)
    assert projects_mod.project_context_for_session("child2", OWNER).project_id == ""


def test_the_delegation_path_hands_those_three_fields_to_create_session():
    """The DB behaviour above is only reached if the delegation actually
    passes them. It used to assign `child.folder` on the returned dataclass
    afterwards, which wrote to nothing durable — the child's row kept folder
    NULL and no project at all."""
    source = _source("src", "agent_tools", "subagent_tools.py")
    call = source[source.index("sm.create_session("):][:800]
    assert "folder=SUBAGENT_FOLDER" in call
    assert 'mode="agent"' in call
    assert "project_id=child_project_id" in call
    assert "project_context_for_session" in source
    assert "child.folder = SUBAGENT_FOLDER" not in source, "the post-hoc patch-up is back"
