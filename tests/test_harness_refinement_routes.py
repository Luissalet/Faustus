"""REST surface of the harness proposals: propose, list, get, approve, reject,
undo, the log, and the one asymmetry that matters: the model's loopback token
may ask for a proposal but cannot approve, reject or undo one."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import middleware
from core.database import Base
from core.database import ChatMessage as DbChatMessage
from core.database import Session as DbSession
from routes.harness_proposals_routes import setup_harness_proposals_routes
from services.projects import ProjectStore
from src import agent_defs
from src.harness_refinement import proposer, runner, store, targets
from src.memory import MemoryManager
from src.skills_runtime import sleep_optimize as so

NOW = datetime(2026, 10, 1, 12, 0, 0)
TOOL_HEADERS = {middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN}
CORRECTION = [("user", "write the release notes for v2", None),
              ("assistant", "Here are the release notes: long long long...", None),
              ("user", "that's wrong, I asked for three bullet points", None)]
CLEAN = [("user", "what is the capital of France?", None), ("assistant", "Paris.", None),
         ("user", "perfecto, gracias", None)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for mod in (store, targets, agent_defs):
        monkeypatch.setattr(mod, "DATA_DIR", str(data))
    monkeypatch.setattr(so, "DATA_DIR", str(data), raising=False)
    root = data / "skill_proposals"
    monkeypatch.setattr(so, "PROPOSALS_ROOT", str(root), raising=False)
    monkeypatch.setattr(so, "PROPOSALS_FILE", str(root / "proposals.json"), raising=False)
    monkeypatch.setattr(so, "SNAPSHOT_DIR", str(root / "snapshots"), raising=False)
    monkeypatch.setattr(so, "VERSIONS_FILE", str(root / "versions.json"), raising=False)
    projects = ProjectStore(str(data))
    monkeypatch.setattr(targets, "_project_store", lambda: projects)
    monkeypatch.setattr(targets, "_memory_manager", lambda: MemoryManager(str(data)))
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.database.SessionLocal", factory, raising=False)
    project = projects.create("Demo", instructions="Answer briefly.", scaffold_memory=False)
    monkeypatch.setattr("services.projects.project_for_session", lambda sid, owner=None: project)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    proposer._INFLIGHT.clear()

    answer = {"propose": True, "reason": "User wanted bullets.", "axis": "prompt_layer",
              "target": f"project:{project['id']}", "op": "update",
              "after": "Answer briefly. Use bullet points when asked."}

    async def fake_llm(url, model, prompt, user_initiated):
        return json.dumps(answer)

    monkeypatch.setattr(proposer, "_default_llm", fake_llm)
    monkeypatch.setattr(proposer, "_resolve_endpoint", lambda owner, url, model: ("http://stub/v1/chat/completions", "stub"))

    app = FastAPI()

    @app.middleware("http")
    async def _user(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(setup_harness_proposals_routes())
    return type("Env", (), {"client": TestClient(app), "data": data, "projects": projects, "db": factory,
                            "project": project})


def _seed(env, sid, turns, owner=""):
    db = env.db()
    db.add(DbSession(id=sid, name=sid, endpoint_url="http://x", model="m", owner=owner, message_count=0))
    for i, (role, text, events) in enumerate(turns):
        db.add(DbChatMessage(id=f"{sid}-m{i}", session_id=sid, role=role, content=text,
                             timestamp=NOW + timedelta(seconds=i * 10),
                             meta_data=json.dumps({"tool_events": events}) if events else None))
    db.commit()
    db.close()


def _propose(env, sid="s1", **body):
    return env.client.post("/api/harness/proposals/propose", json={"session_id": sid, **body})


def _instructions(env):
    return env.projects.get(env.project["id"])["instructions"]


def test_propose_then_list_and_get_show_a_pending_proposal(env):
    _seed(env, "s1", CORRECTION)
    resp = _propose(env)
    assert resp.status_code == 200 and resp.json()["status"] == "proposed"
    pid = resp.json()["proposal"]["id"]
    assert _instructions(env) == "Answer briefly."                       # proposing applied nothing

    listed = env.client.get("/api/harness/proposals").json()
    assert listed["enabled"] is False                                   # the setting is off by default
    row = listed["proposals"][0]
    assert row["id"] == pid and row["status"] == "pending" and row["stale"] is False
    assert "before" not in row and "after" not in row                  # list stays light

    one = env.client.get(f"/api/harness/proposals/{pid}").json()
    assert one["proposal"]["before"] == "Answer briefly."
    assert one["proposal"]["after"] == "Answer briefly. Use bullet points when asked."
    assert one["log"][0]["event"] == "proposed"


def test_trivial_session_is_skipped_and_unknown_one_is_404(env):
    _seed(env, "s1", CLEAN)
    skipped = _propose(env).json()
    assert skipped["status"] == "skipped" and skipped["reason"] == "no_signal"
    assert _propose(env, "ghost").status_code == 404
    forced = _propose(env, force=True)
    assert forced.json()["status"] == "proposed"


def test_approve_applies_once_and_is_idempotent(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    ok = env.client.post(f"/api/harness/proposals/{pid}/approve")
    assert ok.status_code == 200 and ok.json()["proposal"]["status"] == "applied"
    assert _instructions(env) == "Answer briefly. Use bullet points when asked."
    again = env.client.post(f"/api/harness/proposals/{pid}/approve")
    assert again.status_code == 200 and again.json()["proposal"]["already_applied"] is True


def test_undo_restores_and_then_refuses_a_second_time(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    env.client.post(f"/api/harness/proposals/{pid}/approve")
    undone = env.client.post(f"/api/harness/proposals/{pid}/undo")
    assert undone.status_code == 200 and undone.json()["undone"] == pid
    assert undone.json()["proposal"]["undo_of"] == pid
    assert _instructions(env) == "Answer briefly."
    again = env.client.post(f"/api/harness/proposals/{pid}/undo")
    assert again.status_code == 409 and again.json()["detail"]["error_class"] == "harness.not_applied"


def test_undo_with_a_clear_message_when_the_target_moved_on(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    env.client.post(f"/api/harness/proposals/{pid}/approve")
    env.projects.update(env.project["id"], {"instructions": "Edited later by hand."})
    refused = env.client.post(f"/api/harness/proposals/{pid}/undo")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["error_class"] == "harness.target_changed_since_apply"
    assert "edited after this proposal was applied" in detail["message"]
    assert _instructions(env) == "Edited later by hand."


def test_a_stale_proposal_is_flagged_and_cannot_be_applied(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    env.projects.update(env.project["id"], {"instructions": "Changed meanwhile."})
    assert env.client.get("/api/harness/proposals").json()["proposals"][0]["stale"] is True
    refused = env.client.post(f"/api/harness/proposals/{pid}/approve")
    assert refused.status_code == 409 and refused.json()["detail"]["error_class"] == "harness.target_changed"
    assert _instructions(env) == "Changed meanwhile."


def test_reject_then_approve_is_refused(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    rejected = env.client.post(f"/api/harness/proposals/{pid}/reject", json={"reason": "not what I meant"})
    assert rejected.status_code == 200 and rejected.json()["proposal"]["status"] == "rejected"
    assert env.client.post(f"/api/harness/proposals/{pid}/approve").status_code == 409
    assert _instructions(env) == "Answer briefly."
    assert env.client.post("/api/harness/proposals/nope/approve").status_code == 404


def test_the_models_loopback_token_cannot_decide_a_proposal(env):
    _seed(env, "s1", CORRECTION)
    asked = env.client.post("/api/harness/proposals/propose", json={"session_id": "s1"}, headers=TOOL_HEADERS)
    assert asked.status_code == 200 and asked.json()["status"] == "proposed"        # asking is allowed
    pid = asked.json()["proposal"]["id"]
    for verb in ("approve", "reject", "undo"):
        refused = env.client.post(f"/api/harness/proposals/{pid}/{verb}", headers=TOOL_HEADERS)
        assert refused.status_code == 403, verb
    assert _instructions(env) == "Answer briefly."
    assert store.get(pid)["status"] == "pending"


def test_status_and_log_endpoints(env):
    _seed(env, "s1", CORRECTION)
    pid = _propose(env).json()["proposal"]["id"]
    env.client.post(f"/api/harness/proposals/{pid}/approve")
    status = env.client.get("/api/harness/proposals/status").json()
    assert status["enabled"] is False and status["counts"]["applied"] == 1
    assert set(status["axes"]) == {"prompt_layer", "skill", "memory", "subagent_spec"}
    log = env.client.get("/api/harness/proposals/log", params={"proposal_id": pid}).json()["log"]
    assert [e["event"] for e in reversed(log)] == ["proposed", "applied"]
    assert log[-1]["trigger"] == "manual"


def test_owner_scope(env):
    _seed(env, "s1", CORRECTION, owner="alice")
    pid = env.client.post("/api/harness/proposals/propose", json={"session_id": "s1"},
                          headers={"X-Test-User": "alice"}).json()["proposal"]["id"]
    bob = {"X-Test-User": "bob"}
    assert env.client.get("/api/harness/proposals", headers=bob).json()["proposals"] == []
    assert env.client.get(f"/api/harness/proposals/{pid}", headers=bob).status_code == 404
    assert env.client.post(f"/api/harness/proposals/{pid}/approve", headers=bob).status_code == 404
    assert env.client.post("/api/harness/proposals/propose", json={"session_id": "s1"}, headers=bob).status_code == 404
    assert env.client.get("/api/harness/proposals", headers={"X-Test-User": "alice"}).json()["proposals"]


def test_route_is_registered_in_the_app_and_the_setting_is_off_by_default():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "setup_harness_proposals_routes" in (root / "app.py").read_text(encoding="utf-8")
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["harness_refinement_enabled"] is False
    from src.agent_settings_schema import schema_fields
    assert "harness_refinement_enabled" in {f["key"] for f in schema_fields()}
