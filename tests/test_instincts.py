"""Lot I — src/instincts.py, src/agent_tools/instinct_tools.py,
routes/instincts_routes.py.

Confidence math (decay + retire), upsert merge semantics, project scoping
and promotion, render_block injection (threshold/limit/tie-break/
relevance), extraction parsing + grounding, evolve clustering (+ generate),
export/import round trip, the tool handler's actions, and the HTTP routes.
"""
from __future__ import annotations

import json
import time

import pytest

from src import instincts


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(instincts, "DATA_DIR", str(data_dir), raising=False)
    return data_dir


# ── confidence math ─────────────────────────────────────────────────────

def test_initial_confidence_buckets():
    assert instincts._initial_confidence(1) == 0.3
    assert instincts._initial_confidence(2) == 0.3
    assert instincts._initial_confidence(3) == 0.5
    assert instincts._initial_confidence(5) == 0.5
    assert instincts._initial_confidence(6) == 0.7
    assert instincts._initial_confidence(10) == 0.7
    assert instincts._initial_confidence(11) == 0.85
    assert instincts._initial_confidence(50) == 0.85


def test_effective_confidence_decays_by_week_and_floors():
    now = time.time()
    item = {"confidence": 0.5, "last_observed": now - 10 * 7 * 86400}
    # 10 weeks * 0.02 = 0.2 decay -> 0.3, well above the floor.
    assert instincts.effective_confidence(item, now) == pytest.approx(0.3, abs=1e-6)

    item_floor = {"confidence": 0.2, "last_observed": now - 100 * 7 * 86400}
    assert instincts.effective_confidence(item_floor, now) == instincts.CONFIDENCE_FLOOR


def test_effective_confidence_ceiling():
    now = time.time()
    item = {"confidence": 0.99, "last_observed": now}
    assert instincts.effective_confidence(item, now) == instincts.CONFIDENCE_CEILING


def test_effective_confidence_falls_back_to_updated_then_created():
    now = time.time()
    item = {"confidence": 0.5, "updated": now - 7 * 86400}
    assert instincts.effective_confidence(item, now) == pytest.approx(0.48, abs=1e-6)
    item2 = {"confidence": 0.5, "created": now - 7 * 86400}
    assert instincts.effective_confidence(item2, now) == pytest.approx(0.48, abs=1e-6)


def test_contradict_retires_once_effective_confidence_drops_low_enough(isolated_store):
    record = instincts.upsert("alice", {
        "trigger": "when x", "action": "do y", "confidence": 0.3,
    })
    iid = record["id"]
    for _ in range(3):
        instincts.contradict("alice", iid)
    stored = instincts.get("alice", iid)
    # 0.3 - 3*0.10 floors at 0.1 <= RETIRE_THRESHOLD -> retired.
    assert stored["status"] == "retired"


def test_confirm_raises_confidence_up_to_the_ceiling(isolated_store):
    record = instincts.upsert("alice", {"trigger": "when x", "action": "do y", "confidence": 0.9})
    iid = record["id"]
    instincts.confirm("alice", iid)
    instincts.confirm("alice", iid)
    stored = instincts.get("alice", iid)
    assert stored["confidence"] == instincts.CONFIDENCE_CEILING


# ── upsert merge ─────────────────────────────────────────────────────────

def test_upsert_merges_by_trigger_action_scope_project(isolated_store):
    first = instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes",
        "action": "use the router factory",
        "domain": "workflow", "scope": "project", "project": "/repo",
        "evidence": [{"session_id": "s1", "turn_ts": 1, "excerpt": "a"}],
    })
    second = instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes",
        "action": "use the router factory",
        "domain": "workflow", "scope": "project", "project": "/repo",
        "evidence": [{"session_id": "s2", "turn_ts": 2, "excerpt": "b"}],
    })
    assert first["id"] == second["id"]
    assert second["observations"] == 2
    assert len(second["evidence"]) == 2


def test_upsert_never_lowers_confidence_on_re_observation(isolated_store):
    record = instincts.upsert("alice", {"trigger": "when x", "action": "do y", "confidence": 0.9})
    iid = record["id"]
    again = instincts.upsert("alice", {"trigger": "when x", "action": "do y"})
    assert again["id"] == iid
    assert again["confidence"] >= 0.9


def test_upsert_caps_evidence_at_twelve(isolated_store):
    for i in range(15):
        instincts.upsert("alice", {
            "trigger": "when x", "action": "do y",
            "evidence": [{"session_id": f"s{i}", "turn_ts": i, "excerpt": str(i)}],
        })
    items = instincts.list_instincts("alice")
    assert len(items) == 1
    assert len(items[0]["evidence"]) == 12


def test_different_projects_get_distinct_records(isolated_store):
    a = instincts.upsert("alice", {"trigger": "when x", "action": "do y", "scope": "project", "project": "/repo-a"})
    b = instincts.upsert("alice", {"trigger": "when x", "action": "do y", "scope": "project", "project": "/repo-b"})
    assert a["id"] != b["id"]
    items = instincts.list_instincts("alice")
    assert len(items) == 2


def test_owners_never_see_each_others_instincts(isolated_store):
    instincts.upsert("alice", {"trigger": "when x", "action": "do y"})
    assert instincts.list_instincts("bob") == []


# ── list scoping ─────────────────────────────────────────────────────────

def test_list_instincts_scopes_to_project_plus_global(isolated_store):
    instincts.upsert("alice", {"trigger": "when a", "action": "do a", "scope": "project", "project": "p1"})
    instincts.upsert("alice", {"trigger": "when b", "action": "do b", "scope": "project", "project": "p2"})
    instincts.upsert("alice", {"trigger": "when c", "action": "do c", "scope": "global", "confidence": 0.9})

    p1_items = instincts.list_instincts("alice", project="p1")
    triggers = {it["trigger"] for it in p1_items}
    assert triggers == {"when a", "when c"}

    p1_no_global = instincts.list_instincts("alice", project="p1", include_global=False)
    assert {it["trigger"] for it in p1_no_global} == {"when a"}


def test_list_instincts_min_confidence_filters(isolated_store):
    instincts.upsert("alice", {"trigger": "when a", "action": "do a", "confidence": 0.9})
    instincts.upsert("alice", {"trigger": "when b", "action": "do b", "confidence": 0.2})
    high = instincts.list_instincts("alice", min_confidence=0.5)
    assert len(high) == 1 and high[0]["trigger"] == "when a"


# ── promotion: 2 projects >= 0.8 -> global; 1 project -> not ─────────────

def test_promote_dry_run_and_real_merge_across_two_projects(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes", "action": "use the router factory",
        "scope": "project", "project": "/repo-a", "confidence": 0.9,
    })
    instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes", "action": "use the router factory",
        "scope": "project", "project": "/repo-b", "confidence": 0.85,
    })

    dry = instincts.promote("alice", dry_run=True)
    assert dry["dry_run"] is True
    assert len(dry["candidates"]) == 1
    assert set(dry["candidates"][0]["projects"]) == {"/repo-a", "/repo-b"}

    result = instincts.promote("alice")
    assert result["dry_run"] is False
    assert len(result["promoted"]) == 1
    global_id = result["promoted"][0]

    global_record = instincts.get("alice", global_id)
    assert global_record["scope"] == "global"
    assert set(global_record["promoted_from"]) == {"/repo-a", "/repo-b"}

    active = instincts.list_instincts("alice")
    assert all(it["scope"] == "global" for it in active if it["trigger"] == "when writing new fastapi routes")

    # Retired project copies are kept on disk, not deleted.
    raw = instincts._load_raw("alice")
    project_copies = [v for v in raw.values() if v.get("scope") == "project"]
    assert len(project_copies) == 2
    assert all(v["status"] == "retired" for v in project_copies)


def test_promote_does_not_fire_for_a_single_project(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes", "action": "use the router factory",
        "scope": "project", "project": "/repo-a", "confidence": 0.95,
    })
    dry = instincts.promote("alice", dry_run=True)
    assert dry["candidates"] == []
    result = instincts.promote("alice")
    assert result["promoted"] == []


def test_promote_does_not_fire_below_confidence_threshold(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes", "action": "use the router factory",
        "scope": "project", "project": "/repo-a", "confidence": 0.5,
    })
    instincts.upsert("alice", {
        "trigger": "when writing new fastapi routes", "action": "use the router factory",
        "scope": "project", "project": "/repo-b", "confidence": 0.5,
    })
    result = instincts.promote("alice")
    assert result["promoted"] == []


# ── render_block: threshold / limit / tie-break / relevance ─────────────

def test_render_block_empty_when_nothing_qualifies(isolated_store):
    instincts.upsert("alice", {"trigger": "when a", "action": "do a", "confidence": 0.2})
    assert instincts.render_block("alice", "p1", threshold=0.7) == ""


def test_render_block_formats_and_respects_limit(isolated_store):
    for i in range(10):
        instincts.upsert("alice", {
            "trigger": f"when task {i}", "action": f"do thing {i}",
            "confidence": 0.9, "scope": "project", "project": "p1",
        })
    block = instincts.render_block("alice", "p1", threshold=0.5, limit=3)
    assert block.startswith("Learned instincts (confidence):")
    assert block.count("\n- [project") == 3


def test_render_block_project_wins_ties_over_global(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when a", "action": "do a", "confidence": 0.8,
        "scope": "global",
    })
    instincts.upsert("alice", {
        "trigger": "when b", "action": "do b", "confidence": 0.8,
        "scope": "project", "project": "p1",
    })
    block = instincts.render_block("alice", "p1", threshold=0.5, limit=1)
    assert "when b" in block
    assert "when a" not in block


def test_render_block_relevance_boost_can_reorder_near_ties(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when deploying to production", "action": "run the smoke tests first",
        "confidence": 0.75, "scope": "project", "project": "p1",
    })
    instincts.upsert("alice", {
        "trigger": "when writing documentation", "action": "keep examples short",
        "confidence": 0.751, "scope": "project", "project": "p1",
    })
    # With no query, the (barely) higher-confidence doc instinct wins.
    block_no_query = instincts.render_block("alice", "p1", threshold=0.5, limit=1)
    assert "documentation" in block_no_query

    # A query about deploying should boost the deploy instinct to the top.
    block_with_query = instincts.render_block(
        "alice", "p1", threshold=0.5, limit=1, user_message="I'm deploying to production now",
    )
    assert "deploying" in block_with_query


# ── extraction: parsing, grounding, contradicts ──────────────────────────

def test_should_extract_and_looks_like_correction():
    assert instincts.should_extract(2, 0, "") is True
    assert instincts.should_extract(0, 2, "") is True
    assert instincts.should_extract(0, 0, "that's wrong, still broken") is True
    assert instincts.should_extract(0, 0, "thanks, looks good") is False
    assert instincts.looks_like_correction("no funciona") is True
    assert instincts.looks_like_correction("gracias, perfecto") is False


@pytest.mark.asyncio
async def test_extract_from_turn_stores_grounded_instinct_and_drops_ungrounded_one(isolated_store, monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )
    reply = (
        "<instinct>\n"
        "trigger: when writing new fastapi routes\n"
        "action: use the router factory in routes/ and register in app.py\n"
        "domain: workflow\n"
        'evidence: "use the router factory in routes"\n'
        "contradicts: none\n"
        "</instinct>\n"
        "<instinct>\n"
        "trigger: when doing something unrelated\n"
        "action: an invented action\n"
        "domain: workflow\n"
        'evidence: "this exact quote was never said in the conversation"\n'
        "contradicts: none\n"
        "</instinct>"
    )

    async def _fake_call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)

    messages = [
        {"role": "user", "content": "please add an invoices endpoint"},
        {"role": "assistant", "content": "Sure — I'll use the router factory in routes/ for this."},
    ]
    stored = await instincts.extract_from_turn(
        "alice", "sess-1", messages, project="/repo", project_name="repo", workspace="/repo",
    )
    assert len(stored) == 1
    assert stored[0]["trigger"] == "when writing new fastapi routes"
    assert stored[0]["scope"] == "project"
    assert stored[0]["project"] == "/repo"


@pytest.mark.asyncio
async def test_extract_from_turn_rejects_malformed_trigger_and_bad_domain(isolated_store, monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )
    reply = (
        "<instinct>\n"
        "trigger: routes should use the factory\n"  # doesn't start with when/cuando
        "action: use the router factory\n"
        "domain: workflow\n"
        'evidence: "use the router factory"\n'
        "</instinct>\n"
        "<instinct>\n"
        "trigger: when writing tests\n"
        "action: write a fixture first\n"
        "domain: not-a-real-domain\n"
        'evidence: "write a fixture first"\n'
        "</instinct>"
    )

    async def _fake_call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    messages = [{"role": "assistant", "content": "use the router factory. write a fixture first."}]
    stored = await instincts.extract_from_turn(
        "alice", "sess-1", messages, project="/repo", project_name="repo", workspace="/repo",
    )
    assert stored == []


@pytest.mark.asyncio
async def test_extract_from_turn_contradicts_an_existing_instinct(isolated_store, monkeypatch):
    existing = instincts.upsert("alice", {
        "trigger": "when adding an endpoint", "action": "skip validation for speed",
        "scope": "project", "project": "/repo", "confidence": 0.6,
    })
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )
    reply = (
        "<instinct>\n"
        "trigger: when adding an endpoint\n"
        "action: always validate input with pydantic\n"
        "domain: code-style\n"
        'evidence: "always validate input"\n'
        f"contradicts: {existing['id']}\n"
        "</instinct>"
    )

    async def _fake_call(*a, **k):
        return reply

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)
    messages = [{"role": "assistant", "content": "From now on always validate input with real data."}]
    await instincts.extract_from_turn(
        "alice", "sess-1", messages, project="/repo", project_name="repo", workspace="/repo",
    )
    contradicted = instincts.get("alice", existing["id"])
    assert contradicted["contradictions"] == 1
    assert contradicted["confidence"] < 0.6


@pytest.mark.asyncio
async def test_extract_from_turn_never_raises_on_model_failure(isolated_store, monkeypatch):
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *a, **k: ("http://local/v1", "qwen-test", {}),
    )

    async def _boom(*a, **k):
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr("src.llm_core.llm_call_async", _boom)
    stored = await instincts.extract_from_turn(
        "alice", "sess-1", [{"role": "user", "content": "hi"}],
        project="/repo", project_name="repo", workspace="/repo",
    )
    assert stored == []


@pytest.mark.asyncio
async def test_extract_from_turn_skips_ownerless_sessions(isolated_store):
    stored = await instincts.extract_from_turn(
        None, "sess-1", [{"role": "user", "content": "hi"}],
        project="/repo", project_name="repo", workspace="/repo",
    )
    assert stored == []


# ── evolve: clustering + generate ─────────────────────────────────────────

def test_evolve_clusters_related_instincts(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when writing fastapi routes for invoices", "action": "use the router factory",
        "domain": "workflow", "confidence": 0.8,
    })
    instincts.upsert("alice", {
        "trigger": "when writing fastapi routes for customers", "action": "use the router factory too",
        "domain": "workflow", "confidence": 0.8,
    })
    instincts.upsert("alice", {
        "trigger": "when reviewing a pull request", "action": "check for missing tests",
        "domain": "testing", "confidence": 0.8,
    })
    result = instincts.evolve("alice")
    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert len(cluster["instinct_ids"]) == 2
    assert cluster["suggested"] in ("skill", "command")


def test_evolve_generate_writes_a_draft_skill(isolated_store):
    instincts.upsert("alice", {
        "trigger": "when writing fastapi routes for invoices", "action": "use the router factory",
        "domain": "workflow", "confidence": 0.8,
    })
    instincts.upsert("alice", {
        "trigger": "when writing fastapi routes for customers", "action": "use the router factory too",
        "domain": "workflow", "confidence": 0.8,
    })
    result = instincts.evolve("alice", generate=True)
    assert result["created_skill_ids"]

    from services.memory.skills import SkillsManager
    sm = SkillsManager(str(isolated_store))
    skills = sm.load(owner="alice")
    assert any(s["name"] == result["created_skill_ids"][0] for s in skills)
    created = next(s for s in skills if s["name"] == result["created_skill_ids"][0])
    assert created["status"] == "draft"
    assert created["source"] == "learned"
    assert created["category"] == "instincts"


# ── export / import round trip ────────────────────────────────────────────

def test_export_import_round_trip(isolated_store):
    instincts.upsert("alice", {"trigger": "when a", "action": "do a", "confidence": 0.7})
    instincts.upsert("alice", {"trigger": "when b", "action": "do b", "confidence": 0.5, "scope": "global"})

    dumped = instincts.export_json("alice")
    parsed = json.loads(dumped)
    assert len(parsed) == 2

    result = instincts.import_json("bob", dumped)
    assert result["count"] == 2
    assert instincts.list_instincts("bob")

    # Re-importing into the SAME owner is a pure dedup no-op (ids already exist).
    result2 = instincts.import_json("alice", dumped)
    assert result2["count"] == 0
    assert result2["skipped"] == 2


def test_import_json_rejects_non_array_and_skips_bad_rows(isolated_store):
    with pytest.raises(ValueError):
        instincts.import_json("alice", json.dumps({"not": "a list"}))

    text = json.dumps([{"trigger": "when a", "action": "do a"}, {"trigger": "missing action"}, "not a dict"])
    result = instincts.import_json("alice", text)
    assert result["count"] == 1
    assert result["skipped"] == 2


def test_import_scope_override(isolated_store):
    text = json.dumps([{"trigger": "when a", "action": "do a", "scope": "project", "project": "p1"}])
    instincts.import_json("alice", text, scope_override="global")
    items = instincts.list_instincts("alice")
    assert items[0]["scope"] == "global"


# ── status ────────────────────────────────────────────────────────────────

def test_status_counts_and_top_and_pending_promotions(isolated_store):
    instincts.upsert("alice", {"trigger": "when a", "action": "do a", "domain": "workflow", "confidence": 0.9})
    instincts.upsert("alice", {"trigger": "when b", "action": "do b", "domain": "testing", "scope": "global", "confidence": 0.9})
    s = instincts.status("alice")
    assert s["total"] == 2
    assert s["by_scope"] == {"project": 1, "global": 1}
    assert s["by_domain"] == {"workflow": 1, "testing": 1}
    assert len(s["top"]) == 2
    assert s["pending_promotions"] == []


# ── project_key / detect_project_name ─────────────────────────────────────

def test_project_key_prefers_workspace_over_project_id():
    assert instincts.project_key("/repo", "proj-1") == "/repo"
    assert instincts.project_key(None, "proj-1") == "proj-1"
    assert instincts.project_key(None, None) == ""


def test_detect_project_name_falls_back_to_basename(tmp_path):
    ws = tmp_path / "my-cool-app"
    ws.mkdir()
    assert instincts.detect_project_name(str(ws)) == "my-cool-app"
    assert instincts.detect_project_name("") == ""


def test_detect_project_name_reads_git_remote(tmp_path):
    ws = tmp_path / "workdir"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.com/org/real-repo-name.git\n',
        encoding="utf-8",
    )
    assert instincts.detect_project_name(str(ws)) == "real-repo-name"


# ── tool handler ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_do_manage_instincts_add_view_confirm_retire(isolated_store):
    from src.agent_tools.instinct_tools import do_manage_instincts

    added = await do_manage_instincts(json.dumps({
        "action": "add", "trigger": "when x", "do": "do y", "domain": "workflow",
    }), owner="alice")
    iid = added["results"]["id"]
    assert added["results"]["source"] == "manual"
    assert added["results"]["confidence"] == 0.6

    viewed = await do_manage_instincts(json.dumps({"action": "view", "id": iid}), owner="alice")
    assert viewed["results"]["trigger"] == "when x"

    confirmed = await do_manage_instincts(json.dumps({"action": "confirm", "id": iid}), owner="alice")
    assert confirmed["results"]["confirmations"] == 1

    listed = await do_manage_instincts(json.dumps({"action": "list"}), owner="alice")
    assert "when x" in listed["results"]

    retired = await do_manage_instincts(json.dumps({"action": "retire", "id": iid}), owner="alice")
    assert retired["results"]["status"] == "retired"

    missing = await do_manage_instincts(json.dumps({"action": "view", "id": "nope"}), owner="alice")
    assert missing.get("exit_code") == 1


@pytest.mark.asyncio
async def test_do_manage_instincts_requires_action_and_rejects_unknown(isolated_store):
    from src.agent_tools.instinct_tools import do_manage_instincts

    missing_action = await do_manage_instincts(json.dumps({}), owner="alice")
    assert missing_action.get("exit_code") == 1

    unknown = await do_manage_instincts(json.dumps({"action": "wat"}), owner="alice")
    assert unknown.get("exit_code") == 1

    bad_json = await do_manage_instincts("not json", owner="alice")
    assert bad_json.get("exit_code") == 1


@pytest.mark.asyncio
async def test_do_manage_instincts_export_import_and_evolve(isolated_store):
    from src.agent_tools.instinct_tools import do_manage_instincts

    await do_manage_instincts(json.dumps({
        "action": "add", "trigger": "when a", "do": "do a", "domain": "workflow",
    }), owner="alice")
    exported = await do_manage_instincts(json.dumps({"action": "export"}), owner="alice")
    dumped = exported["results"]
    assert json.loads(dumped)

    imported = await do_manage_instincts(
        json.dumps({"action": "import", "json": dumped}), owner="bob",
    )
    assert imported["results"]["count"] == 1

    evolved = await do_manage_instincts(json.dumps({"action": "evolve"}), owner="alice")
    assert "clusters" in evolved["results"]

    status_result = await do_manage_instincts(json.dumps({"action": "status"}), owner="alice")
    assert status_result["results"]["total"] == 1

    promoted = await do_manage_instincts(json.dumps({"action": "promote", "dry_run": True}), owner="alice")
    assert promoted["results"]["dry_run"] is True


# ── HTTP routes ────────────────────────────────────────────────────────

def _client(monkeypatch):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient
    from routes.instincts_routes import setup_instincts_routes

    app = FastAPI()
    app.include_router(setup_instincts_routes())

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


def test_routes_add_list_view_confirm_retire_delete(isolated_store, monkeypatch):
    client = _client(monkeypatch)
    headers = {"x-user": "alice"}

    r = client.post("/api/instincts", json={"trigger": "when a", "action": "do a"}, headers=headers)
    assert r.status_code == 200
    iid = r.json()["id"]

    r = client.get("/api/instincts", headers=headers)
    assert r.status_code == 200
    assert r.json()["count"] == 1

    r = client.get(f"/api/instincts/{iid}", headers=headers)
    assert r.status_code == 200
    assert r.json()["trigger"] == "when a"

    r = client.post(f"/api/instincts/{iid}/confirm", json={}, headers=headers)
    assert r.status_code == 200
    assert r.json()["confirmations"] == 1

    r = client.post(f"/api/instincts/{iid}/retire", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "retired"

    r = client.delete(f"/api/instincts/{iid}", headers=headers)
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    r = client.get(f"/api/instincts/{iid}", headers=headers)
    assert r.status_code == 404


def test_routes_status_export_promote_evolve_do_not_collide_with_id_route(isolated_store, monkeypatch):
    client = _client(monkeypatch)
    headers = {"x-user": "alice"}
    client.post("/api/instincts", json={"trigger": "when a", "action": "do a"}, headers=headers)

    r = client.get("/api/instincts/status", headers=headers)
    assert r.status_code == 200
    assert "total" in r.json()

    r = client.get("/api/instincts/export", headers=headers)
    assert r.status_code == 200
    assert json.loads(r.json()["json"])

    r = client.post("/api/instincts/promote", json={"dry_run": True}, headers=headers)
    assert r.status_code == 200
    assert r.json()["dry_run"] is True

    r = client.post("/api/instincts/evolve", json={}, headers=headers)
    assert r.status_code == 200
    assert "clusters" in r.json()


def test_routes_import_round_trip(isolated_store, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/instincts", json={"trigger": "when a", "action": "do a"}, headers={"x-user": "alice"})
    exported = client.get("/api/instincts/export", headers={"x-user": "alice"}).json()["json"]

    r = client.post("/api/instincts/import", json={"json": exported}, headers={"x-user": "bob"})
    assert r.status_code == 200
    assert r.json()["count"] == 1

    r = client.post("/api/instincts/import", json={"json": "not json"}, headers={"x-user": "bob"})
    assert r.status_code == 400


def test_routes_owner_isolation(isolated_store, monkeypatch):
    client = _client(monkeypatch)
    client.post("/api/instincts", json={"trigger": "when a", "action": "do a"}, headers={"x-user": "alice"})
    r = client.get("/api/instincts", headers={"x-user": "bob"})
    assert r.json()["count"] == 0
