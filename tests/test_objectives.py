"""Project objectives — the JSONL store, the delta compiler, the graph impact
scores, and every hook they hang off (system prompt, post-compaction reminder,
agent tool dispatch, HTTP API).

The point being tested throughout: the dashboard is updated with typed deltas
compiled deterministically, and nothing here may ever raise into a chat or
dispatch hot path — a broken objectives file costs the feature, not the turn.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import objectives as obj  # noqa: E402
from services.projects import ProjectStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(str(tmp_path / "data"))


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "covernet"
    ws.mkdir()
    return str(ws)


@pytest.fixture()
def project(store, workspace):
    return store.create("Covernet", workspace=workspace)


def _add(project, title, actor="user", **extra):
    result = obj.apply_deltas(project, [{"op": "ADD", "title": title, **extra}], actor)
    assert not result["conflicts"], result["conflicts"]
    return result["applied"][0]["id"]


# ── store: roundtrip, atomicity, corruption ───────────────────────────


def test_state_round_trips_through_jsonl(project):
    oid = _add(project, "Ship the dashboard", priority=1, status="in_progress")
    state = obj.load_state(project)
    stored = state["objectives"][oid]
    assert stored["title"] == "Ship the dashboard"
    assert stored["status"] == "in_progress" and stored["priority"] == 1
    assert stored["owner"] == "user" and stored["last_actor"] == "user"
    # One JSON object per line on disk, no half-written tmp file left behind.
    path = obj.objectives_path(project)
    assert os.path.isfile(path) and not os.path.exists(path + ".tmp")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            assert isinstance(json.loads(line), dict)


def test_dep_edges_are_separate_records(project):
    a = _add(project, "Foundation")
    b = _add(project, "Feature", deps=[a])
    with open(obj.objectives_path(project), encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh]
    assert {"t": "dep", "from": b, "to": a} in recs
    state = obj.load_state(project)
    assert state["edges"] == [{"from": b, "to": a}]


def test_corrupt_file_is_renamed_and_treated_as_empty(project):
    _add(project, "Real work")
    path = obj.objectives_path(project)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"t":"obj","id":"OBJ-1"\nNOT JSON AT ALL\n')
    state = obj.load_state(project)
    assert state == {"objectives": {}, "edges": []}
    assert os.path.isfile(path + ".corrupt")   # kept, never silently destroyed
    # And the store keeps working from empty.
    assert _add(project, "Fresh start") == "OBJ-1"


def test_ids_are_monotonic_including_dropped(project):
    a = _add(project, "First")
    assert a == "OBJ-1"
    result = obj.apply_deltas(project, [{"op": "KILL", "id": a}], "user")
    assert result["applied"][0]["op"] == "KILL"
    # The dropped record still occupies its number: the next id is OBJ-2,
    # and it never becomes OBJ-1 again even though OBJ-1 is dropped.
    assert _add(project, "Second") == "OBJ-2"
    assert obj.load_state(project)["objectives"]["OBJ-1"]["status"] == "dropped"


def test_log_rotation_keeps_the_newest_half(project, monkeypatch):
    monkeypatch.setattr(obj, "MAX_LOG_BYTES", 2000)
    for i in range(60):
        obj.add_evidence(project, "OBJ-1", "manual", f"ref-{i}", 0.5, "x" * 80)
    size = os.path.getsize(obj.log_path(project))
    assert size <= 2000
    records = obj.read_log(project, limit=1000)
    assert records                              # newest survived
    assert records[-1]["ref"] == "ref-59"
    assert records[0]["ref"] != "ref-0"          # oldest rotated away


# ── the delta compiler ────────────────────────────────────────────────


def test_add_edit_kill_happy_path(project):
    a = _add(project, "Design the schema", priority=2)
    result = obj.apply_deltas(project, [
        {"op": "EDIT", "id": a, "status": "in_progress", "notes": "started"},
    ], "user")
    assert result["applied"] == [{"op": "EDIT", "id": a,
                                  "fields": {"status": "in_progress", "notes": "started"},
                                  "rationale": ""}]
    result = obj.apply_deltas(project, [
        {"op": "KILL", "id": a, "rationale": "superseded"},
    ], "agent")
    assert result["applied"][0]["fields"] == {"status": "dropped"}
    assert obj.load_state(project)["objectives"][a]["last_actor"] == "agent"


def test_apply_order_is_adds_then_edits_then_kills(project):
    # An EDIT listed before the ADD it targets still lands: brenner_bot order.
    result = obj.apply_deltas(project, [
        {"op": "EDIT", "id": "OBJ-1", "status": "done"},
        {"op": "ADD", "title": "Created in the same batch"},
    ], "user")
    assert [a["op"] for a in result["applied"]] == ["ADD", "EDIT"]
    assert result["state"]["objectives"][0]["status"] == "done"


def test_duplicate_title_conflicts_case_insensitively(project):
    _add(project, "Ship It")
    result = obj.apply_deltas(project, [{"op": "ADD", "title": "  ship it "}], "user")
    assert not result["applied"]
    assert "duplicate title" in result["conflicts"][0]["reason"]
    # A dropped objective frees its title.
    obj.apply_deltas(project, [{"op": "KILL", "id": "OBJ-1"}], "user")
    assert _add(project, "Ship It") == "OBJ-2"


def test_unknown_id_and_bad_values_conflict_without_stopping_the_batch(project):
    result = obj.apply_deltas(project, [
        {"op": "ADD", "title": ""},                                # bad title
        {"op": "ADD", "title": "Valid", "status": "bogus"},        # bad status
        {"op": "ADD", "title": "Valid", "priority": 9},            # bad priority
        {"op": "ADD", "title": "Lands", "priority": 1},            # fine
        {"op": "EDIT", "id": "OBJ-99", "status": "done"},          # unknown id
        {"op": "KILL", "id": "OBJ-98"},                            # unknown id
        {"op": "FROB", "id": "OBJ-1"},                             # unknown op
    ], "user")
    assert [a["op"] for a in result["applied"]] == ["ADD"]
    assert result["applied"][0]["fields"]["title"] == "Lands"
    reasons = "; ".join(c["reason"] for c in result["conflicts"])
    assert "title" in reasons and "status" in reasons and "priority" in reasons
    assert "does not exist" in reasons and "unknown op" in reasons
    assert len(result["conflicts"]) == 6


def test_empty_edit_is_skipped_not_a_conflict(project):
    a = _add(project, "Quiet")
    result = obj.apply_deltas(project, [{"op": "EDIT", "id": a}], "user")
    assert result["applied"] == [] and result["conflicts"] == []


def test_agent_kill_requires_a_rationale_user_kill_does_not(project):
    a = _add(project, "Doomed")
    result = obj.apply_deltas(project, [{"op": "KILL", "id": a}], "agent")
    assert "rationale" in result["conflicts"][0]["reason"]
    result = obj.apply_deltas(project, [{"op": "KILL", "id": a}], "user")
    assert result["applied"]


def test_human_edit_wins_over_a_stale_agent_edit(project):
    a = _add(project, "Contested", actor="agent")
    # A user touches it after the agent's snapshot...
    obj.apply_deltas(project, [{"op": "EDIT", "id": a, "priority": 1}], "user")
    # ...so the agent's edit based on the old updated_at must conflict.
    result = obj.apply_deltas(project, [
        {"op": "EDIT", "id": a, "status": "done",
         "base_updated_at": "2020-01-01T00:00:00Z"},
    ], "agent")
    assert not result["applied"]
    assert "human edit wins" in result["conflicts"][0]["reason"]
    assert obj.load_state(project)["objectives"][a]["status"] != "done"
    # Without a base (or when the last change was the agent's own) it applies.
    result = obj.apply_deltas(project, [{"op": "EDIT", "id": a, "status": "done"}], "agent")
    assert result["applied"]


def test_unknown_dep_conflicts_and_cycles_are_refused(project):
    a = _add(project, "A")
    b = _add(project, "B", deps=[a])
    result = obj.apply_deltas(project, [{"op": "ADD", "title": "C", "deps": ["OBJ-77"]}], "user")
    assert "unknown dep" in result["conflicts"][0]["reason"]
    # A depending on B would close A ← B ← A.
    result = obj.apply_deltas(project, [{"op": "EDIT", "id": a, "deps": [b]}], "user")
    assert any("cycle" in c["reason"] for c in result["conflicts"])
    assert {"from": a, "to": b} not in obj.load_state(project)["edges"]
    # The original edge (replaced set was just the cyclic one) is gone though:
    # deps REPLACE outgoing edges when provided.
    result = obj.apply_deltas(project, [{"op": "EDIT", "id": b, "deps": []}], "user")
    assert result["applied"][0]["fields"]["deps"] == []
    assert obj.load_state(project)["edges"] == []


def test_deltas_replace_outgoing_edges(project):
    a = _add(project, "A")
    b = _add(project, "B")
    c = _add(project, "C", deps=[a])
    result = obj.apply_deltas(project, [{"op": "EDIT", "id": c, "deps": [b]}], "user")
    assert result["applied"][0]["fields"]["deps"] == [b]
    assert obj.load_state(project)["edges"] == [{"from": c, "to": b}]


def test_every_outcome_is_logged(project):
    a = _add(project, "Logged")
    obj.apply_deltas(project, [{"op": "EDIT", "id": "OBJ-9", "status": "done"}], "agent",
                     session_id="sess-1")
    log = obj.read_log(project)
    kinds = [(r["kind"], r["op"]) for r in log]
    assert ("delta", "ADD") in kinds and ("conflict", "EDIT") in kinds
    conflict = next(r for r in log if r["kind"] == "conflict")
    assert conflict["session"] == "sess-1" and "does not exist" in conflict["reason"]
    obj.add_evidence(project, a, "dispatch", "job-1", 0.6, "2 files changed")
    assert obj.read_log(project)[-1]["kind"] == "evidence"


# ── graph impact scores ───────────────────────────────────────────────


def _fixture_state():
    """OBJ-2 and OBJ-3 depend on OBJ-1; OBJ-4 depends on OBJ-2. OBJ-1 is the
    structural keystone but carries the WORST human priority — the divergence
    the hint exists to flag."""
    now = "2099-01-01T00:00:00Z"   # future → staleness 0, deterministic

    def o(i, priority):
        return {"t": "obj", "id": f"OBJ-{i}", "title": f"Objective {i}",
                "status": "open", "priority": priority, "owner": "user",
                "notes": "", "created_at": now, "updated_at": now, "last_actor": "user"}

    return {
        "objectives": {f"OBJ-{i}": o(i, 4 if i == 1 else 1) for i in range(1, 5)},
        "edges": [{"from": "OBJ-2", "to": "OBJ-1"},
                  {"from": "OBJ-3", "to": "OBJ-1"},
                  {"from": "OBJ-4", "to": "OBJ-2"}],
    }


def test_impact_scores_are_deterministic_and_rank_the_keystone(project):
    state = _fixture_state()
    scores = obj.impact_scores(state)
    assert scores == obj.impact_scores(state)          # same state, same numbers
    assert set(scores) == {"OBJ-1", "OBJ-2", "OBJ-3", "OBJ-4"}
    # Everything depends (directly or not) on OBJ-1: highest pagerank.
    assert scores["OBJ-1"]["components"]["pagerank"] == 1.0
    # OBJ-2 sits on the only OBJ-4 → OBJ-1 path: only nonzero betweenness.
    assert scores["OBJ-2"]["components"]["betweenness"] == 1.0
    assert scores["OBJ-3"]["components"]["betweenness"] == 0.0
    # OBJ-1 directly blocks two of the three other objectives.
    assert scores["OBJ-1"]["components"]["blocker_ratio"] == round(2 / 3, 4)
    # Rounded components, weighted sum.
    for entry in scores.values():
        c = entry["components"]
        expected = round(c["pagerank"] * 0.3 + c["betweenness"] * 0.3
                         + c["blocker_ratio"] * 0.2 + c["staleness"] * 0.1
                         + c["priority_boost"] * 0.1, 4)
        assert entry["score"] == expected


def test_priority_hint_fires_only_on_divergence(project):
    scores = obj.impact_scores(_fixture_state())
    # OBJ-1: worst human priority (P4 → last by priority) but the top
    # structural score, and it blocks open work → hinted.
    assert scores["OBJ-1"]["hint"] == "structurally blocking; consider raising priority"
    assert all(scores[o]["hint"] is None for o in ("OBJ-2", "OBJ-3", "OBJ-4"))
    # Same graph with aligned priorities: no hint anywhere.
    aligned = _fixture_state()
    aligned["objectives"]["OBJ-1"]["priority"] = 1
    assert all(v["hint"] is None for v in obj.impact_scores(aligned).values())


def test_dropped_and_done_objectives_change_the_graph(project):
    state = _fixture_state()
    state["objectives"]["OBJ-1"]["status"] = "done"
    scores = obj.impact_scores(state)
    assert scores["OBJ-1"]["components"]["blocker_ratio"] == 0.0   # done blocks nothing
    state["objectives"]["OBJ-2"]["status"] = "dropped"
    assert "OBJ-2" not in obj.impact_scores(state)


# ── the system prompt section ─────────────────────────────────────────


def test_system_block_gains_the_objectives_section(store, project):
    a = _add(project, "Foundation", priority=1)
    _add(project, "Feature work", status="in_progress", priority=2, deps=[a])
    block = store.system_block(project)
    assert "## Project objectives" in block
    assert "OBJ-1 [open] (P1) Foundation" in block
    assert "OBJ-2 [in_progress] (P2) Feature work — blocked by OBJ-1" in block
    assert "project_objectives" in block          # the standing instruction
    assert "never rewrite the whole list" in block


def test_resolved_deps_are_not_listed_and_dropped_objectives_vanish(store, project):
    a = _add(project, "Foundation")
    b = _add(project, "Feature", deps=[a])
    _add(project, "Gone soon")
    obj.apply_deltas(project, [{"op": "EDIT", "id": a, "status": "done"},
                               {"op": "KILL", "id": "OBJ-3"}], "user")
    block = store.system_block(project)
    assert "blocked by" not in block              # the only dep is resolved
    assert "Gone soon" not in block
    assert f"{b} [open]" in block


def test_no_objectives_means_no_section(store, project):
    assert "## Project objectives" not in store.system_block(project)


def test_objectives_section_respects_the_cap(store, project):
    for i in range(60):
        _add(project, f"Objective number {i} with a reasonably long title padding {'x' * 60}")
    section = obj.objectives_block(project)
    assert len(section) <= obj.MAX_SECTION_CHARS
    assert "truncated" in section
    assert "## Project objectives" in store.system_block(project)


def test_a_broken_objectives_file_costs_the_section_not_the_block(store, project, monkeypatch):
    _add(project, "Fine")
    monkeypatch.setattr(obj, "objectives_block",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banana")))
    block = store.system_block(project)            # must not raise
    assert "<project_context>" in block


# ── the post-compaction reminder ──────────────────────────────────────


def test_post_compact_reminder_outside_a_project_is_none(monkeypatch):
    from src import context_compactor as cc
    import services.projects as projects_mod
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: None)

    class _Sess:
        id = "orphan"

    assert cc.post_compact_reminder(_Sess(), "luis") is None
    assert cc.post_compact_reminder(None, "luis") is None


def _reminder_with_agents_md(project, workspace, monkeypatch, tmp_path, *, approve):
    """One post-compaction reminder for a workspace that has an AGENTS.md.

    The reminder re-injects the project's standing rules, so it is gated by the
    same trust check as the system prompt itself (src/workspace_trust.py). The
    store is pinned to tmp_path so this never touches the real DATA_DIR.
    """
    from src import context_compactor as cc
    from src import project_instructions as pi
    from src import workspace_trust as wt
    import services.projects as projects_mod

    monkeypatch.setattr(wt, "DATA_DIR", str(tmp_path / "trust"))
    _add(project, "Survive compaction", priority=1)
    with open(os.path.join(workspace, "AGENTS.md"), "w", encoding="utf-8") as fh:
        fh.write("Run pytest before claiming done.\n")
    pi.invalidate()
    if approve:
        assert wt.trust(workspace, wt.digest_for(workspace), by="tester")["ok"] is True
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: project)

    class _Sess:
        id = "sess-1"

    msg = cc.post_compact_reminder(_Sess(), "luis")
    pi.invalidate()
    return msg


def test_post_compact_reminder_carries_objectives_and_rules(project, workspace, monkeypatch, tmp_path):
    msg = _reminder_with_agents_md(project, workspace, monkeypatch, tmp_path, approve=True)
    assert msg["role"] == "system"
    assert msg["metadata"] == {"compacted": True}   # trim keeps it resident
    assert msg["content"].startswith("[Post-compaction reminder]")
    assert "Survive compaction" in msg["content"]
    assert "AGENTS.md" in msg["content"] and "still apply" in msg["content"]
    assert "Run pytest before claiming done." in msg["content"]


def test_post_compact_reminder_does_not_smuggle_an_unapproved_agents_md(
        project, workspace, monkeypatch, tmp_path):
    """The reminder is a second door into the system role, and it is gated too.

    A folder whose instruction files nobody approved gets the note here as well;
    the objectives (which the user's own project owns) still come through."""
    msg = _reminder_with_agents_md(project, workspace, monkeypatch, tmp_path, approve=False)
    assert "Survive compaction" in msg["content"]
    assert "Run pytest before claiming done." not in msg["content"]
    assert "NOT approved" in msg["content"] and "AGENTS.md" in msg["content"]


def test_post_compact_reminder_never_raises(monkeypatch):
    from src import context_compactor as cc
    import services.projects as projects_mod

    def _boom(sid, owner=None):
        raise RuntimeError("projects.json is a banana")

    monkeypatch.setattr(projects_mod, "project_for_session", _boom)

    class _Sess:
        id = "sess-1"

    assert cc.post_compact_reminder(_Sess(), "luis") is None


# ── the agent tool dispatch ───────────────────────────────────────────


def _tool_block(args):
    from src.agent_tools import ToolBlock
    return ToolBlock(tool_type="project_objectives", content=json.dumps(args))


async def test_tool_dispatch_outside_a_project_is_an_error(monkeypatch):
    import services.projects as projects_mod
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: None)
    desc, result = await _execute_tool_block_impl(_tool_block({"action": "list"}),
                                                  session_id="sess-1", owner="luis")
    assert result["exit_code"] == 1
    assert "not attached to a project" in result["error"]


async def test_tool_dispatch_list_and_apply(project, monkeypatch):
    import services.projects as projects_mod
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: project)

    desc, result = await _execute_tool_block_impl(
        _tool_block({"action": "apply",
                     "deltas": [{"op": "ADD", "title": "Via the tool", "priority": 1,
                                 "rationale": "asked for"}]}),
        session_id="sess-1", owner="luis")
    assert desc == "project_objectives: apply"
    assert result["applied"][0]["id"] == "OBJ-1" and not result["conflicts"]

    desc, result = await _execute_tool_block_impl(_tool_block({"action": "list"}),
                                                  session_id="sess-1", owner="luis")
    assert desc == "project_objectives: list"
    assert [o["id"] for o in result["objectives"]] == ["OBJ-1"]
    assert "OBJ-1" in result["scores"]
    # The applied delta is attributed to the agent and the session in the log.
    rec = obj.read_log(project)[0]
    assert rec["actor"] == "agent" and rec["session"] == "sess-1"


async def test_tool_dispatch_rejects_garbage_without_raising(project, monkeypatch):
    import services.projects as projects_mod
    from src.tool_execution import _execute_tool_block_impl
    monkeypatch.setattr(projects_mod, "project_for_session", lambda sid, owner=None: project)
    for content in ("not json", json.dumps({"action": "reset"}), json.dumps([1, 2])):
        from src.agent_tools import ToolBlock
        block = ToolBlock(tool_type="project_objectives", content=content)
        _, result = await _execute_tool_block_impl(block, session_id="s", owner="luis")
        assert result["exit_code"] == 1 and result["error"]


# ── the HTTP API ──────────────────────────────────────────────────────


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import project_routes

    monkeypatch.setattr(project_routes, "get_store", lambda: store)
    monkeypatch.setattr(project_routes, "effective_user", lambda request: None)
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_api_crud_and_dashboard(client, project):
    pid = project["id"]
    created = client.post(f"/api/projects/{pid}/objectives",
                          json={"title": "Via the API", "priority": 1, "status": "open"})
    assert created.status_code == 200
    body = created.json()
    assert body["id"] == "OBJ-1" and body["title"] == "Via the API" and body["deps"] == []

    second = client.post(f"/api/projects/{pid}/objectives",
                         json={"title": "Dependent", "deps": ["OBJ-1"]}).json()
    assert second["deps"] == ["OBJ-1"]

    patched = client.patch(f"/api/projects/{pid}/objectives/OBJ-1",
                           json={"status": "in_progress", "notes": "started"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_progress"
    assert patched.json()["last_actor"] == "user"

    dashboard = client.get(f"/api/projects/{pid}/objectives").json()
    assert [o["id"] for o in dashboard["objectives"]] == ["OBJ-1", "OBJ-2"]
    assert dashboard["edges"] == [{"from": "OBJ-2", "to": "OBJ-1"}]
    assert set(dashboard["scores"]) == {"OBJ-1", "OBJ-2"}
    assert dashboard["log"] and dashboard["log"][0]["kind"] == "delta"

    dropped = client.delete(f"/api/projects/{pid}/objectives/OBJ-2")
    assert dropped.status_code == 200 and dropped.json()["success"] is True
    dashboard = client.get(f"/api/projects/{pid}/objectives").json()
    assert next(o for o in dashboard["objectives"] if o["id"] == "OBJ-2")["status"] == "dropped"
    assert any(r["kind"] == "delta" and r["op"] == "KILL"
               and r["rationale"] == "removed from dashboard" for r in dashboard["log"])


def test_api_maps_conflicts_to_http_errors(client, project):
    pid = project["id"]
    client.post(f"/api/projects/{pid}/objectives", json={"title": "Unique"})
    dup = client.post(f"/api/projects/{pid}/objectives", json={"title": "unique"})
    assert dup.status_code == 400 and "duplicate" in dup.json()["detail"]
    missing = client.patch(f"/api/projects/{pid}/objectives/OBJ-42", json={"status": "done"})
    assert missing.status_code == 404
    assert client.patch(f"/api/projects/{pid}/objectives/OBJ-1", json={}).status_code == 400
    assert client.get("/api/projects/nope/objectives").status_code == 404


def test_api_refuses_a_project_without_a_workspace(client, store):
    bare = store.create("No folder")
    assert client.get(f"/api/projects/{bare['id']}/objectives").status_code == 400


def test_api_deltas_endpoint_reports_conflicts_in_band(client, project):
    pid = project["id"]
    res = client.post(f"/api/projects/{pid}/objectives/deltas", json={"deltas": [
        {"op": "ADD", "title": "From the coordinator", "priority": 2},
        {"op": "EDIT", "id": "OBJ-99", "status": "done"},
    ]})
    assert res.status_code == 200
    body = res.json()
    assert body["applied"][0]["id"] == "OBJ-1"
    assert "does not exist" in body["conflicts"][0]["reason"]
    assert body["state"]["objectives"][0]["title"] == "From the coordinator"
    # The batch endpoint acts as the agent — an agent KILL needs a rationale.
    res = client.post(f"/api/projects/{pid}/objectives/deltas",
                      json={"deltas": [{"op": "KILL", "id": "OBJ-1"}]})
    assert "rationale" in res.json()["conflicts"][0]["reason"]


# ── the dispatch evidence hook ────────────────────────────────────────


def test_dispatch_settle_records_objective_evidence(project, monkeypatch):
    from src import dispatch
    import services.projects as projects_mod
    oid = _add(project, "Wire the evidence hook")
    monkeypatch.setattr(projects_mod, "project_for_session",
                        lambda sid, owner=None: project if sid == "chat-1" else None)

    class _Job:
        id = "job-7"
        owner = "luis"
        session_id = "chat-1"
        status = "done"
        args = {"tasks": [{"name": "t1", "instruction": f"finish {oid} and OBJ-99 today"}]}
        changes = {"added": ["a.py"], "modified": ["b.py"]}

    dispatch._record_objective_evidence(_Job())
    records = [r for r in obj.read_log(project) if r["kind"] == "evidence"]
    assert len(records) == 1                       # OBJ-99 does not exist
    assert records[0]["id"] == oid and records[0]["source"] == "dispatch"
    assert records[0]["ref"] == "job-7" and records[0]["confidence"] == 0.6
    assert "2 file(s) changed" in records[0]["note"]


def test_dispatch_settle_evidence_never_breaks_the_verdict(monkeypatch):
    from src import dispatch

    def _boom(job):
        raise RuntimeError("objectives store is a banana")

    monkeypatch.setattr(dispatch, "_record_objective_evidence", _boom)
    job = dispatch.DispatchJob(owner="luis", args={"tasks": [{"instruction": "do OBJ-1"}]},
                               workspace=None, endpoint_url="http://x", model="m",
                               headers=None, title="t")
    job.result = {"subagents": [{"status": "done"}]}
    dispatch._settle(job)                          # must not raise
    assert job.status == "done" and job.verdict
