"""The mutating project-context tool: `manage_project_context` (plan §9, §21).

Two of these tests are the reason the tool exists as its own module.

``test_a_project_id_in_the_arguments_is_ignored`` — a model that can name the
destination project can move one project's documents into another. The
destination comes from the session, on the server, and the result says the
argument was ignored rather than quietly honouring it.

``test_two_candidates_ask_instead_of_guessing`` — "this document" with two
equally plausible answers is a question, not a race won by the later one. The
tool asks, and mutates nothing while it asks.

The rest guard the properties a caller repeats to the user: attach is
idempotent, detach never deletes the source, a refusal about somebody else's
document does not name it, and a chat outside a project says so instead of
failing obscurely.
"""

import asyncio
import copy
import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import services.projects as projects_mod
from src.project_context.references import TurnReference, TurnReferenceRegistry
from src.project_context.resolvers.document import DocumentResolver
from src.project_context.resolvers.filesystem import FilesystemResolver
from src.project_context.service import ProjectContextService
from src.tools import project_context as tool

OWNER = "luis"
OTHER = "mallory"
SECRET_TITLE = "Q3 restructuring memo"
PROJECT = {"id": "prj_1", "name": "Faustus", "owner": OWNER, "enabled": True}


class FakeProjectStore:
    """The ``ProjectStore`` link API and nothing else, so these tests exercise
    the tool against the documented contract rather than against projects.json.
    Deduplication key is the plan's: (kind, canonical ref, version policy, pin).
    """

    def __init__(self):
        self.links = {}

    @staticmethod
    def _canonical(link):
        ref = link.get("ref_id") or link.get("path") or ""
        return os.path.normcase(ref) if link.get("path") else ref

    def _key(self, link):
        return (link.get("kind"), self._canonical(link),
                link.get("version_policy"), link.get("pinned_version"))

    def _rows(self, project_id):
        return self.links.setdefault(project_id, [])

    def normalize_link(self, raw):
        return dict(raw or {})

    def list_links(self, project_id, *, owner=None, kind="", enabled_only=False):
        rows = [copy.deepcopy(r) for r in self._rows(project_id)]
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows

    def get_link(self, project_id, link_id, *, owner=None):
        for row in self._rows(project_id):
            if row.get("id") == link_id:
                return copy.deepcopy(row)
        return None

    def upsert_link(self, project_id, link, *, owner=None):
        rows = self._rows(project_id)
        for row in rows:
            if self._key(row) == self._key(link):
                return copy.deepcopy(row), True
        rows.append(copy.deepcopy(link))
        return copy.deepcopy(link), False

    def patch_link(self, project_id, link_id, patch, *, owner=None):
        for row in self._rows(project_id):
            if row.get("id") == link_id:
                row.update(dict(patch or {}))
                return copy.deepcopy(row)
        raise KeyError(link_id)

    def remove_link(self, project_id, link_id, *, owner=None):
        rows = self._rows(project_id)
        kept = [r for r in rows if r.get("id") != link_id]
        if len(kept) == len(rows):
            return False
        self.links[project_id] = kept
        return True

    def context_revision(self, project_id):
        return 0


class Wiring:
    """Everything one call needs, so a test reads as the scenario it is."""

    def __init__(self, store, svc, reg, db_factory):
        self.store, self.svc, self.reg, self.db_factory = store, svc, reg, db_factory

    def call(self, args, *, session_id="s1", owner=OWNER, turn_id="t1", run_id="r1"):
        return asyncio.run(tool.do_manage_project_context(
            json.dumps(args), session_id=session_id, owner=owner,
            run_id=run_id, turn_id=turn_id))


@pytest.fixture
def db_factory(tmp_path):
    url = "sqlite:///" + (tmp_path / "tool.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def wired(db_factory, monkeypatch):
    store = FakeProjectStore()
    svc = ProjectContextService(
        store=store,
        resolver_map={"document": DocumentResolver(db_factory),
                      "file": FilesystemResolver("file"),
                      "folder": FilesystemResolver("folder")},
        clock=lambda: 1_700_000_000.0,
    )
    # No ambient active-document pointer: what "this document" means here is
    # decided by what the test recorded, not by a process-wide global left
    # over from another test module.
    reg = TurnReferenceRegistry(active_provider=lambda session_id, owner: ("", ""))
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda session_id, owner=None: copy.deepcopy(PROJECT))
    monkeypatch.setattr(tool, "service", lambda: svc)
    monkeypatch.setattr(tool, "registry", lambda: reg)
    return Wiring(store, svc, reg, db_factory)


def make_document(db_factory, *, doc_id="doc_1", owner=OWNER,
                  title="Voice architecture", content="# Voice\n\nDecisions.\n"):
    db = db_factory()
    try:
        db.add(cdb.Document(id=doc_id, title=title, current_content=content,
                            version_count=1, owner=owner, language="markdown"))
        db.commit()
    finally:
        db.close()
    return doc_id


def document_exists(db_factory, doc_id):
    db = db_factory()
    try:
        return db.query(cdb.Document).filter(cdb.Document.id == doc_id).first() is not None
    finally:
        db.close()


def note(reg, doc_id, label, *, turn="t1", session="s1", owner=OWNER):
    """Record a reference as a producing tool would.

    ``created_at`` is left at 0 so the registry stamps its own clock: a
    hand-written timestamp older than the TTL is evicted on the next read, and
    the test would then be asserting about an empty registry.
    """
    reg.note(TurnReference(kind="document", ref_id=doc_id, label=label,
                           source_tool="create_document", turn_id=turn,
                           session_id=session, owner=owner, relation="created"))


# ── rule 1: the model never chooses the project ────────────────────────────

def test_a_project_id_in_the_arguments_is_ignored(wired):
    """The destination is the session's project. An id in the body changes
    nothing and is reported as ignored, so the agent cannot claim it attached
    something to a project it named."""
    doc_id = make_document(wired.db_factory)
    out = wired.call({"action": "attach", "project_id": "prj_somewhere_else",
                      "source": {"kind": "document", "id": doc_id}})

    assert out["ok"] is True
    assert out["project"]["id"] == "prj_1"
    assert out["ignored_project_id"] == "prj_somewhere_else"
    assert "resolved from this chat" in out["ignored_reason"]
    assert wired.store.list_links("prj_somewhere_else") == []
    assert [row["ref_id"] for row in wired.store.list_links("prj_1")] == [doc_id]


def test_the_ignored_project_id_is_reported_on_a_read_too(wired):
    out = wired.call({"action": "list", "project_id": "prj_somewhere_else"})
    assert out["ok"] is True
    assert out["project"]["id"] == "prj_1"
    assert out["ignored_project_id"] == "prj_somewhere_else"


# ── rule 2: "this document" is resolved, and asked about when unclear ──────

def test_active_document_is_resolved_and_stored_as_a_real_document(wired):
    """The shortcut saves a small model from copying an id it can see. What is
    STORED is the canonical kind and the real id — never 'active_document'."""
    doc_id = make_document(wired.db_factory)
    note(wired.reg, doc_id, "Voice architecture")

    out = wired.call({"action": "attach", "source": {"kind": "active_document"}})

    assert out["ok"] is True
    assert out["link"]["kind"] == "document"
    assert out["link"]["ref_id"] == doc_id
    assert out["link"]["label"] == "Voice architecture"


def test_two_candidates_ask_instead_of_guessing(wired):
    """Two documents created by the same operation are two candidates. Picking
    the later one attaches the wrong half of a pair and the user has nothing to
    notice it by, so the tool asks — and writes nothing while it asks."""
    make_document(wired.db_factory, doc_id="doc_a", title="Spec A")
    make_document(wired.db_factory, doc_id="doc_b", title="Spec B")
    note(wired.reg, "doc_a", "Spec A")
    note(wired.reg, "doc_b", "Spec B")

    out = wired.call({"action": "attach", "source": {"kind": "active_document"}})

    assert out["needs_clarification"] is True
    assert out["ok"] is False
    assert {c["id"] for c in out["candidates"]} == {"doc_a", "doc_b"}
    assert {c["label"] for c in out["candidates"]} == {"Spec A", "Spec B"}
    assert wired.store.list_links("prj_1") == [], "an ambiguous request mutated the project"


def test_an_explicit_id_settles_what_the_registry_could_not(wired):
    """Priority 1 of plan §10: an id the user gave outranks everything."""
    make_document(wired.db_factory, doc_id="doc_a", title="Spec A")
    make_document(wired.db_factory, doc_id="doc_b", title="Spec B")
    note(wired.reg, "doc_a", "Spec A")
    note(wired.reg, "doc_b", "Spec B")

    out = wired.call({"action": "attach",
                      "source": {"kind": "active_document", "id": "doc_b"}})

    assert out["ok"] is True
    assert out["link"]["ref_id"] == "doc_b"


def test_no_active_document_asks_for_an_id_rather_than_picking_one(wired):
    out = wired.call({"action": "attach", "source": {"kind": "active_document"}})
    assert out["exit_code"] == 1
    assert "source.id" in out["error"]
    assert wired.store.list_links("prj_1") == []


# ── what the confirmation is allowed to claim ──────────────────────────────

def test_attach_is_idempotent_and_says_which_of_the_two_happened(wired):
    """Saying "add this to the project" twice leaves one link. The second
    answer says `deduplicated`, so the agent tells the user the true one of two
    true things."""
    doc_id = make_document(wired.db_factory)
    args = {"action": "attach", "source": {"kind": "document", "id": doc_id}}

    first = wired.call(args)
    second = wired.call(args)

    assert first["ok"] is True and first["action"] == "attached"
    assert first["deduplicated"] is False
    assert second["ok"] is True and second["action"] == "deduplicated"
    assert second["deduplicated"] is True
    assert second["link"]["id"] == first["link"]["id"]
    assert len(wired.store.list_links("prj_1")) == 1


def test_detach_removes_the_link_and_never_the_document(wired):
    doc_id = make_document(wired.db_factory)
    attached = wired.call({"action": "attach",
                           "source": {"kind": "document", "id": doc_id}})
    out = wired.call({"action": "detach", "link_id": attached["link"]["id"]})

    assert out["ok"] is True
    assert out["action"] == "detached"
    assert "not deleted" in out["message"]
    assert wired.store.list_links("prj_1") == []
    assert document_exists(wired.db_factory, doc_id) is True


def test_a_source_owned_by_somebody_else_does_not_leak_its_title(wired):
    """A refusal that names the document has already given away the document."""
    make_document(wired.db_factory, doc_id="doc_secret", owner=OTHER,
                  title=SECRET_TITLE, content="layoffs\n")
    out = wired.call({"action": "attach",
                      "source": {"kind": "document", "id": "doc_secret"}})

    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert SECRET_TITLE not in repr(out)
    assert "layoffs" not in repr(out)
    assert "doc_secret" in out["error"], "the refusal must still say WHICH source"
    assert wired.store.list_links("prj_1") == []


def test_attaching_a_source_that_does_not_exist_says_which_one(wired):
    out = wired.call({"action": "attach",
                      "source": {"kind": "document", "id": "doc_nope"}})
    assert out["ok"] is False
    assert "doc_nope" in out["error"]
    assert out["state"] == "missing"


def test_a_chat_with_no_project_gets_an_error_that_says_what_to_do(wired,
                                                                   monkeypatch):
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda session_id, owner=None: None)
    out = wired.call({"action": "attach", "source": {"kind": "document", "id": "d"}})

    assert out["exit_code"] == 1
    assert "not attached to a project" in out["error"]


def test_an_unknown_action_names_the_ones_that_exist(wired):
    out = wired.call({"action": "obliterate"})
    assert out["exit_code"] == 1
    assert "attach" in out["error"] and "detach" in out["error"]


def test_invalid_json_is_an_error_not_an_exception(wired):
    out = asyncio.run(tool.do_manage_project_context(
        "{not json", session_id="s1", owner=OWNER))
    assert out["exit_code"] == 1
    assert "Invalid JSON" in out["error"]


def test_list_reports_what_is_actually_linked(wired):
    doc_id = make_document(wired.db_factory)
    wired.call({"action": "attach", "source": {"kind": "document", "id": doc_id},
                "role": "decision", "tags": ["voice"]})
    out = wired.call({"action": "list"})

    assert out["ok"] is True
    assert out["count"] == 1
    assert out["links"][0]["ref_id"] == doc_id
    assert out["links"][0]["role"] == "decision"
    assert out["links"][0]["tags"] == ["voice"]


def test_update_changes_policy_without_repointing_the_link(wired):
    doc_id = make_document(wired.db_factory)
    attached = wired.call({"action": "attach",
                           "source": {"kind": "document", "id": doc_id}})
    out = wired.call({"action": "update", "link_id": attached["link"]["id"],
                      "retrieval_policy": "on_demand", "label": "Voice decisions"})

    assert out["ok"] is True
    assert out["link"]["retrieval_policy"] == "on_demand"
    assert out["link"]["label"] == "Voice decisions"
    assert out["link"]["ref_id"] == doc_id


# ── registration ───────────────────────────────────────────────────────────
#
# A tool registered in nine places out of eleven fails in a way that looks like
# the model being stupid: the schema is offered, the fence parses, and dispatch
# says "Unknown tool". So this reads the real structures.

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def test_the_tool_is_registered_everywhere_a_tool_must_be():
    from src.agent_tools import TOOL_TAGS
    from src.tool_capabilities import (
        KNOWN_CAPABILITY_TOOLS, ResultIntegrity, ToolEffect, capabilities_for_tool,
    )
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_parsing import _TOOL_NAME_MAP, _raw_openai_tool_call_to_block
    from src.tool_policy import _COMMON_TOOL_NAMES
    from src.tool_preflight import PROJECT_TOOLS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS
    from src.tools import do_manage_project_context

    name = "manage_project_context"

    # 1. the implementation, re-exported from the tools package
    assert callable(do_manage_project_context)

    # 2. dispatch tag (also what the fence regex accepts)
    assert name in TOOL_TAGS

    # 3. native function schema
    schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == name)
    assert schema["parameters"]["required"] == ["action"]
    assert {"attach", "detach", "update", "refresh", "inspect", "list"} == set(
        schema["parameters"]["properties"]["action"]["enum"])
    # Plan §21, verbatim: the project comes from the chat.
    assert "never invent a project ID" in schema["description"]

    # 4. native function call → tool block
    block = function_call_to_tool_block(name, {"action": "list"})
    assert block is not None and block.tool_type == name
    assert json.loads(block.content)["action"] == "list"

    # 5/6. text-protocol name map and the args→content branch behind it
    assert _TOOL_NAME_MAP[name] == name
    raw = _raw_openai_tool_call_to_block(
        {"function": {"name": name, "arguments": '{"action": "list"}'}})
    assert raw is not None and raw.tool_type == name
    assert json.loads(raw.content)["action"] == "list"

    # 7. the dispatcher branch
    assert f'elif tool == "{name}":' in _source("src", "tool_execution.py")

    # 8. retrieval description (agent mode selects tools by embedding these)
    assert len(BUILTIN_TOOL_DESCRIPTIONS[name]) > 40

    # 9. common-name list
    assert name in _COMMON_TOOL_NAMES

    # 10. capabilities: it WRITES. Not the READ_WORKSPACE class its read-only
    # sibling sits in, and not WRITE_WORKSPACE either — it never writes file
    # content, only typed link records in the project's own registry.
    assert name in KNOWN_CAPABILITY_TOOLS
    caps = capabilities_for_tool(name)
    assert ToolEffect.WRITE_PRIVATE in caps.effects
    assert ToolEffect.READ_WORKSPACE not in caps.effects
    assert ToolEffect.WRITE_WORKSPACE not in caps.effects
    assert caps.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED

    # 11. security: privileged, and NOT a plan-mode tool — plan mode
    # investigates and must not change what a project knows.
    assert name in NON_ADMIN_BLOCKED_TOOLS
    assert name not in PLAN_MODE_READONLY_TOOLS
    assert "project_context" in PLAN_MODE_READONLY_TOOLS  # the read half stays

    # 12. preflight: pruned in a chat with no project, exactly like its siblings
    assert name in PROJECT_TOOLS


def test_the_turn_route_forces_the_tool_in_when_the_chat_has_a_project():
    """Add-when-there-is-one and prune-when-there-is-not must agree, or the
    tool is retrieved away in the one chat where it works."""
    source = _source("routes", "chat_routes.py")
    anchor = source.index("if project_for_session(session, _user):")
    assert "manage_project_context" in source[anchor:anchor + 900]


def test_a_subagent_may_read_the_project_context_but_never_change_it():
    """The child inherits the project; a worker still does not get to decide
    what the project knows."""
    from src.agent_tools.subagent_tools import (
        SUBAGENT_DISABLED_TOOLS, SUBAGENT_LEAN_DENYLIST, worker_disabled_tools,
    )

    assert "manage_project_context" in SUBAGENT_DISABLED_TOOLS
    assert "manage_project_context" in worker_disabled_tools("[cart.py] add a helper")
    assert "project_context" not in SUBAGENT_LEAN_DENYLIST
    assert "project_context" not in worker_disabled_tools("[cart.py] add a helper")
