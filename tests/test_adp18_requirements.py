"""ADP-18/19/20 -- versioned requirements, context, evidence matrix.

Covers the 7 acceptance scenarios named across the three fichas bundled into
W1-D (CONTRATO_ADP_W1.md):

  1. renombrar simbolo -> stale                  (test_evidence_*)
  2. borrar test -> degrada                      (test_evidence_*)
  3. cambiar requisito tras verificar -> stale    (test_evidence_*)
  4. id inexistente                               (test_context_unknown_id_is_explicit,
                                                    test_matrix_unknown_key_raises)
  5. fichero fuera del workspace -> rechazado     (test_link_outside_workspace_rejected)
  6. mismo REQ-1 en dos proyectos                 (test_ids_independent_per_project)
  7. presupuesto agotado -> omitted               (test_context_budget_exhausted_omits)

Same fixture shape as tests/test_l92_board_store.py (direct `Store(path)`)
and tests/test_l92_board_tools.py (real `projects_mod.ProjectStore` + a
tmp_path workspace) -- no new test infrastructure invented here.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src.requirements import store as req_store  # noqa: E402
from src.requirements import context as req_context  # noqa: E402
from src.requirements import evidence as req_evidence  # noqa: E402

OWNER = "luis"
PROJECT = "proj-1"
OTHER_PROJECT = "proj-2"


def _run_async(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path):
    return req_store.Store(tmp_path / "requirements.sqlite3")


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "tests").mkdir()
    return ws


# ---------------------------------------------------------------------------
# store.py -- identity, versioning, proposal/decision split
# ---------------------------------------------------------------------------
def test_ids_independent_per_project(store):
    """Acceptance 6: 'mismo REQ-1 en dos proyectos' -- the same readable key
    in two projects must never collide or be confused for one another."""
    a = store.create(PROJECT, title="proj-1's first requirement")
    b = store.create(OTHER_PROJECT, title="proj-2's first requirement")
    assert a["key"] == "REQ-1"
    assert b["key"] == "REQ-1"
    assert a["id"] != b["id"]
    assert store.get(PROJECT, "REQ-1")["title"] == "proj-1's first requirement"
    assert store.get(OTHER_PROJECT, "REQ-1")["title"] == "proj-2's first requirement"


def test_update_creates_revision_and_never_edits_the_past(store):
    r = store.create(PROJECT, title="v1", text="first text")
    assert r["current_revision"] == 1
    store.update(PROJECT, r["key"], {"text": "second text"}, by="human", actor=OWNER)
    store.update(PROJECT, r["key"], {"text": "third text"}, by="human", actor=OWNER)

    revs = store.revisions(PROJECT, r["key"])
    assert [rv["revision"] for rv in revs] == [1, 2, 3]
    assert revs[0]["text"] == "first text"   # the past is not rewritten
    assert revs[1]["text"] == "second text"
    assert revs[2]["text"] == "third text"
    assert store.get(PROJECT, r["key"])["current_revision"] == 3


def test_model_proposal_is_always_born_proposed(store):
    r = store.create(PROJECT, title="model idea", proposed_by="model", status="accepted")
    assert r["status"] == "proposed"
    assert r["proposed_by"] == "model"


def test_model_cannot_accept_or_reject(store):
    r = store.create(PROJECT, title="needs a human decision")
    with pytest.raises(req_store.RequirementsError) as exc:
        store.update(PROJECT, r["key"], {"status": "accepted"}, by="model")
    assert exc.value.error_class == "requirements.model_cannot_decide"
    with pytest.raises(req_store.RequirementsError):
        store.update(PROJECT, r["key"], {"status": "rejected"}, by="model")
    # A human doing the same thing is fine.
    accepted = store.update(PROJECT, r["key"], {"status": "accepted"}, by="human", actor=OWNER)
    assert accepted["status"] == "accepted"


def test_unpatched_fields_do_not_bump_revision(store):
    r = store.create(PROJECT, title="stable title")
    same = store.update(PROJECT, r["key"], {"title": "stable title"}, by="human")
    assert same["current_revision"] == 1  # no real change, no new revision


# ---------------------------------------------------------------------------
# store.py -- sidecar parser (`.faustus/requirements.yaml`)
# ---------------------------------------------------------------------------
def test_sidecar_parses_the_documented_shape():
    text = (
        "requirements:\n"
        "  - key: REQ-1\n"
        "    title: Users can reset their password\n"
        "    status: accepted\n"
        "    text: |\n"
        "      A user must be able to request a reset link by email.\n"
        "      The link expires after one hour.\n"
        "    acceptance:\n"
        "      - Reset link expires after 1 hour\n"
        "      - Old password stops working once reset completes\n"
    )
    parsed = req_store.parse_sidecar(text)
    assert parsed["errors"] == []
    assert len(parsed["items"]) == 1
    item = parsed["items"][0]
    assert item["key"] == "REQ-1"
    assert item["status"] == "accepted"
    assert "reset link by email" in item["text"]
    assert item["acceptance"] == [
        "Reset link expires after 1 hour",
        "Old password stops working once reset completes",
    ]


def test_sidecar_tolerates_malformed_input_without_raising():
    assert req_store.parse_sidecar("")["items"] == []
    assert req_store.parse_sidecar("not a requirements file at all")["errors"]
    tabbed = req_store.parse_sidecar("requirements:\n\t- key: REQ-1\n")
    assert tabbed["items"] == []
    assert tabbed["errors"]


def test_read_sidecar_rejects_path_outside_workspace(tmp_path, workspace):
    # No `.faustus/requirements.yaml` under the workspace -> empty, no crash.
    result = req_store.Store().read_sidecar(str(workspace))
    assert result == {"items": [], "errors": [], "path": ""}


# ---------------------------------------------------------------------------
# evidence.py -- the coverage matrix, with fakes (no LLM, no subprocess)
# ---------------------------------------------------------------------------
def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_matrix_all_four_dimensions_true_when_everything_resolves(store, workspace, monkeypatch):
    monkeypatch.setattr(req_store, "default_path", lambda: store.path)
    src_file = workspace / "src" / "auth.py"
    _write(src_file, "def login(user, pw):\n    return True\n")
    test_file = workspace / "tests" / "test_auth.py"
    _write(test_file, "def test_login():\n    assert True\n")

    r = store.create(PROJECT, title="Users can log in", acceptance=["works"], status="accepted")
    key = r["key"]
    _link(store, workspace, PROJECT, key, "implements", "src/auth.py@login")
    _link(store, workspace, PROJECT, key, "tests", "tests/test_auth.py@test_login")
    _link(store, workspace, PROJECT, key, "evidences", "run_123")

    m = req_evidence.matrix(PROJECT, key, workspace=str(workspace), persist=False)
    assert m["linked"] and m["implemented"] and m["tested"] and m["verified"]
    assert not m["stale"]


def _link(store, workspace, project_id, key, kind, target):
    """`link_evidence` but against an explicit `Store` instance (the module-
    level convenience always resolves `default_path()`, which the fixtures
    here override at the module-function level -- see `monkeypatch` in each
    test that calls this)."""
    content_hash = None
    if kind in ("implements", "tests"):
        path = target.split("@")[0].split("::")[0]
        abs_path = workspace / path
        if abs_path.is_file():
            import hashlib
            content_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return store.add_link(project_id, key, kind=kind, target=target, content_hash=content_hash)


def test_evidence_renaming_symbol_marks_stale(store, workspace):
    src_file = workspace / "src" / "auth.py"
    _write(src_file, "def login(user, pw):\n    return True\n")
    r = store.create(PROJECT, title="Users can log in")
    key = r["key"]
    _link(store, workspace, PROJECT, key, "implements", "src/auth.py@login")

    req = store.get_or_raise(PROJECT, key)
    link = req["links"][0]
    state, meta = req_evidence.resolve_link_state(link, req, workspace=str(workspace))
    assert state == "linked"

    _write(src_file, "def login_v2(user, pw):\n    return True\n")  # renamed
    req = store.get_or_raise(PROJECT, key)
    link = req["links"][0]
    state, meta = req_evidence.resolve_link_state(link, req, workspace=str(workspace))
    assert state == "stale"
    assert meta["reason"] == "symbol_not_found"


def test_evidence_deleting_target_degrades_not_stale(store, workspace):
    """Acceptance 2: 'borrar test -> degrada' -- a target that no longer
    exists at all is `unknown` (evidence gone), distinct from `stale`
    (evidence present but out of date)."""
    test_file = workspace / "tests" / "test_auth.py"
    _write(test_file, "def test_login():\n    assert True\n")
    r = store.create(PROJECT, title="Users can log in")
    key = r["key"]
    _link(store, workspace, PROJECT, key, "tests", "tests/test_auth.py@test_login")

    req = store.get_or_raise(PROJECT, key)
    link = req["links"][0]
    state, _ = req_evidence.resolve_link_state(link, req, workspace=str(workspace))
    assert state == "linked"

    os.remove(test_file)
    req = store.get_or_raise(PROJECT, key)
    link = req["links"][0]
    state, meta = req_evidence.resolve_link_state(link, req, workspace=str(workspace))
    assert state == "unknown"
    assert meta["reason"] == "target_missing"


def test_evidence_requirement_change_after_verify_marks_stale(store, workspace):
    """Acceptance 3: 'cambiar requisito tras verificar -> stale'."""
    r = store.create(PROJECT, title="Users can log in")
    key = r["key"]
    link = _link(store, workspace, PROJECT, key, "evidences", "run_123")
    assert link["req_revision_at_link"] == 1

    req = store.get_or_raise(PROJECT, key)
    state, _ = req_evidence.resolve_link_state(req["links"][0], req, workspace=str(workspace))
    assert state == "linked"

    store.update(PROJECT, key, {"text": "changed after verification"}, by="human")
    req = store.get_or_raise(PROJECT, key)
    state, meta = req_evidence.resolve_link_state(req["links"][0], req, workspace=str(workspace))
    assert state == "stale"
    assert meta["reason"] == "requirement_changed_since_verification"


def test_evidence_implements_comment_never_sets_verified(store, workspace, monkeypatch):
    """`@implements REQ-N` is detected as a LINK candidate only -- it must
    never, by itself, make `verified` true (only a `kind == 'evidences'`
    link can)."""
    monkeypatch.setattr(req_store, "default_path", lambda: store.path)
    src_file = workspace / "src" / "auth.py"
    _write(src_file, "# @implements REQ-1\ndef login():\n    pass\n")
    detected = req_evidence.scan_implements_comments(src_file.read_text())
    r = store.create(PROJECT, title="Users can log in")
    key = r["key"]
    assert detected == [key]

    _link(store, workspace, PROJECT, key, "implements", "src/auth.py@login")
    m = req_evidence.matrix(PROJECT, key, workspace=str(workspace), persist=False)
    assert m["implemented"] is True
    assert m["verified"] is False  # an implements link alone never verifies


def test_link_outside_workspace_rejected(store, workspace):
    """Acceptance 5: 'fichero fuera del workspace -> rechazado'."""
    r = store.create(PROJECT, title="x")
    with pytest.raises(req_store.RequirementsError) as exc:
        req_evidence.link_evidence(
            PROJECT, r["key"], kind="implements", target="../../etc/passwd", workspace=str(workspace),
        )
    assert exc.value.error_class == "requirements.path_outside_workspace"
    # Also refused via an absolute path escaping the workspace.
    with pytest.raises(req_store.RequirementsError):
        req_evidence.link_evidence(
            PROJECT, r["key"], kind="implements", target="/etc/passwd", workspace=str(workspace),
        )


def test_matrix_unknown_key_raises(store, workspace):
    """Acceptance 4: 'id inexistente' -- never fabricate a matrix for a key
    that does not exist."""
    with pytest.raises(req_store.NotFoundError):
        req_evidence.matrix(PROJECT, "REQ-999", workspace=str(workspace))


# ---------------------------------------------------------------------------
# context.py -- for_task budgeting
# ---------------------------------------------------------------------------
def test_context_unknown_id_is_explicit(store, monkeypatch):
    monkeypatch.setattr(req_store, "default_path", lambda: store.path)
    store.create(PROJECT, title="A")
    result = req_context.for_task(PROJECT, keys=["REQ-1", "REQ-999"], budget_chars=10_000)
    assert result["unknown"] == ["REQ-999"]
    assert [r["key"] for r in result["requirements"]] == ["REQ-1"]


def test_context_budget_exhausted_omits(store, monkeypatch):
    """Acceptance 7: 'presupuesto agotado -> omitted' -- never silently
    truncate a requirement to make it fit."""
    monkeypatch.setattr(req_store, "default_path", lambda: store.path)
    store.create(PROJECT, title="A", text="x" * 100, acceptance=["a1"])
    store.create(PROJECT, title="B", text="y" * 100, acceptance=["b1"])
    result = req_context.for_task(PROJECT, keys=["REQ-1", "REQ-2"], budget_chars=150)
    assert result["requirements"], "at least the first one fits"
    assert result["omitted"], "the second one must not be silently cut down to fit"
    included_keys = {r["key"] for r in result["requirements"]}
    omitted_keys = {o["key"] for o in result["omitted"]}
    assert included_keys | omitted_keys == {"REQ-1", "REQ-2"}
    assert not (included_keys & omitted_keys)
    for r in result["requirements"]:  # whole-or-nothing: never a truncated acceptance list
        assert r["acceptance"]


def test_context_never_leaks_another_projects_requirement(store, monkeypatch):
    monkeypatch.setattr(req_store, "default_path", lambda: store.path)
    store.create(PROJECT, title="proj-1's requirement")
    store.create(OTHER_PROJECT, title="proj-2's requirement")
    result = req_context.for_task(PROJECT, keys=["REQ-1"], budget_chars=10_000)
    assert result["requirements"][0]["title"] == "proj-1's requirement"


# ---------------------------------------------------------------------------
# Agent tools (req_list/req_get/req_matrix/req_propose/req_link)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    yield


@pytest.fixture()
def project_store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def project(project_store, tmp_path):
    root = tmp_path / "Faustus"
    root.mkdir()
    return project_store.create(name="Faustus", folder="Faustus", workspace=str(root), owner=OWNER,
                                 scaffold_memory=False)


def _ctx(project_id, owner=OWNER):
    return {"owner": owner, "project_id": project_id}


def test_tools_refuse_without_a_project():
    from src.agent_tools.requirement_tools import ReqListTool
    result = _run_async(ReqListTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "requirements.no_project"


def test_req_propose_is_always_a_model_proposal(project):
    from src.agent_tools.requirement_tools import ReqProposeTool, ReqGetTool
    ctx = _ctx(project["id"])
    created = _run_async(ReqProposeTool().execute(
        json.dumps({"title": "Sessions must expire", "acceptance": ["30 min idle timeout"]}), ctx))
    assert created["exit_code"] == 0
    assert created["requirement"]["status"] == "proposed"
    assert created["requirement"]["proposed_by"] == "model"

    got = _run_async(ReqGetTool().execute(json.dumps({"key": created["requirement"]["key"]}), ctx))
    assert got["requirement"]["status"] == "proposed"


def test_req_link_via_tool_rejects_path_outside_workspace(project):
    from src.agent_tools.requirement_tools import ReqProposeTool, ReqLinkTool
    ctx = _ctx(project["id"])
    created = _run_async(ReqProposeTool().execute(json.dumps({"title": "x"}), ctx))
    key = created["requirement"]["key"]
    result = _run_async(ReqLinkTool().execute(
        json.dumps({"key": key, "kind": "implements", "target": "../../../etc/passwd"}), ctx))
    assert result["exit_code"] == 1
    assert result["error_class"] == "requirements.path_outside_workspace"


def test_req_link_via_tool_accepts_workspace_target(project):
    from src.agent_tools.requirement_tools import ReqProposeTool, ReqLinkTool
    workspace = project["workspace"]
    os.makedirs(os.path.join(workspace, "src"), exist_ok=True)
    with open(os.path.join(workspace, "src", "auth.py"), "w") as f:
        f.write("def login():\n    pass\n")
    ctx = _ctx(project["id"])
    created = _run_async(ReqProposeTool().execute(json.dumps({"title": "x"}), ctx))
    key = created["requirement"]["key"]
    result = _run_async(ReqLinkTool().execute(
        json.dumps({"key": key, "kind": "implements", "target": "src/auth.py@login"}), ctx))
    assert result["exit_code"] == 0
    assert result["link"]["kind"] == "implements"


def test_req_matrix_via_tool_whole_project(project):
    from src.agent_tools.requirement_tools import ReqProposeTool, ReqMatrixTool
    ctx = _ctx(project["id"])
    _run_async(ReqProposeTool().execute(json.dumps({"title": "x"}), ctx))
    result = _run_async(ReqMatrixTool().execute("{}", ctx))
    assert result["exit_code"] == 0
    assert len(result["matrix"]) == 1


# ---------------------------------------------------------------------------
# Registration: req_* tools wired into TOOL_HANDLERS/TOOL_TAGS/schemas/
# capabilities -- same table every neighbor tool (board_*) is checked
# against.
# ---------------------------------------------------------------------------
REQ_TOOL_NAMES = ("req_list", "req_get", "req_matrix", "req_propose", "req_link")


def test_req_tools_registered_in_handlers_and_tags():
    import src.agent_tools as at
    for name in REQ_TOOL_NAMES:
        assert name in at.TOOL_HANDLERS, name
        assert name in at.TOOL_TAGS, name


def test_req_tools_have_function_schemas():
    import src.agent_tools as at  # noqa: F401 - import order avoids the circular-import trap
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {f["function"]["name"] for f in FUNCTION_TOOL_SCHEMAS}
    for name in REQ_TOOL_NAMES:
        assert name in names, name
    # No duplicate tool names anywhere in the schema list.
    all_names = [f["function"]["name"] for f in FUNCTION_TOOL_SCHEMAS]
    assert len(all_names) == len(set(all_names))


def test_req_tools_have_capabilities():
    from src.tool_capabilities import TOOL_CAPABILITIES
    for name in REQ_TOOL_NAMES:
        assert name in TOOL_CAPABILITIES, name


def test_a_link_can_be_withdrawn_but_never_across_projects(store):
    """Withdrawing a link is scoped to its requirement: a link id from project A
    is not deletable through project B (W4-A follow-up: the Requirements tab
    needed a DELETE the API did not have)."""
    s = store
    r = s.create("pA", title="t", text="x", source="human", proposed_by="human")
    link = s.add_link("pA", r["key"], kind="implements", target="src/a.py")
    assert s.remove_link("pB", r["key"], link["id"]) is False
    assert [l["id"] for l in s.list_links("pA", r["key"])] == [link["id"]]
    assert s.remove_link("pA", r["key"], link["id"]) is True
    assert s.list_links("pA", r["key"]) == []

