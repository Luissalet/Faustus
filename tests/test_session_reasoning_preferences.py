from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from core.database import Base, Session as DbSession


def test_session_effort_storage_is_owner_scoped_and_preserves_profile(tmp_path, monkeypatch):
    import core.database as db_mod

    engine = create_engine("sqlite:///" + (tmp_path / "sessions.db").as_posix())
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    with factory() as db:
        db.add(DbSession(id="sid-a", name="A", endpoint_url="http://endpoint", model="test", owner="alice",
                         chat_profile=json.dumps({"v": 1, "mode": "lean", "pinned": {"k": "v"}})))
        db.add(DbSession(id="sid-b", name="B", endpoint_url="http://endpoint", model="test", owner="bob"))
        db.commit()

    assert db_mod.set_session_reasoning_effort("sid-a", "high", "alice")
    assert db_mod.get_session_reasoning_effort("sid-a", "alice") == "high"
    assert db_mod.get_session_reasoning_effort("sid-a", "bob") is None
    assert not db_mod.set_session_reasoning_effort("sid-a", "low", "bob")
    with factory() as db:
        stored = json.loads(db.query(DbSession.chat_profile).filter_by(id="sid-a").scalar())
    assert stored == {"v": 1, "mode": "lean", "pinned": {"k": "v"}, "reasoning_effort": "high"}
    assert db_mod.set_session_chat_profile("sid-a", {"v": 1, "mode": "normal", "pinned": {}})
    assert db_mod.get_session_reasoning_effort("sid-a", "alice") == "high"
    assert db_mod.set_session_reasoning_effort("sid-a", None, "alice")
    assert db_mod.get_session_reasoning_effort("sid-a", "alice") is None
    engine.dispose()


def test_request_precedence_is_explicit_then_session_then_project(tmp_path, monkeypatch):
    import core.database as db_mod
    from src.first_token_budget import request_budget

    engine = create_engine("sqlite:///" + (tmp_path / "turns.db").as_posix())
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    with factory() as db:
        db.add(DbSession(id="session", name="A", endpoint_url="http://endpoint", model="test", owner="alice",
                         chat_profile=json.dumps({"reasoning_effort": "medium"})))
        db.commit()
    monkeypatch.setattr("src.project_execution_defaults.for_session", lambda *a, **k: {
        "project_id": "project", "defaults": {"reasoning_effort": "low"}, "revision": 2})
    monkeypatch.setattr("src.auth_helpers.effective_user", lambda request: "alice")
    app = FastAPI()

    @app.post("/turn")
    @request_budget
    async def turn(request: Request):
        async def stream():
            yield json.dumps(request.state.execution_defaults)
        return StreamingResponse(stream())

    with TestClient(app) as client:
        inherited = client.post("/turn", json={"session": "session", "message": "x"})
        explicit = client.post("/turn", json={"session": "session", "message": "x", "reasoning_effort": "xhigh"})
    assert inherited.json()["reasoning_effort"] == "medium"
    assert explicit.json()["reasoning_effort"] == "xhigh"
    engine.dispose()


def test_native_harness_resolves_session_and_keeps_explicit_overrides(monkeypatch):
    import inspect
    import src.agent_loop as agent_loop

    seen = []
    signature = inspect.signature(agent_loop._stream_agent_loop_body)

    async def scripted_body(*args, **kwargs):
        call = signature.bind(*args, **kwargs)
        seen.append(call.arguments.get("gen_overrides"))
        yield "ok"

    scripted_body.__signature__ = signature
    monkeypatch.setattr(agent_loop, "_stream_agent_loop_body", scripted_body)
    monkeypatch.setattr("services.projects.project_for_session", lambda *a: {
        "execution_defaults": {"reasoning_effort": "low"}})
    monkeypatch.setattr("core.database.get_session_reasoning_effort", lambda *a: "high")

    async def run(overrides=None):
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            endpoint_url="http://synthetic", model="scripted", messages=[], session_id="session",
            owner="alice", gen_overrides=overrides)]

    import asyncio
    assert asyncio.run(run()) == ["ok"]
    assert seen[-1]["reasoning_effort"] == "high"
    assert asyncio.run(run({"reasoning_effort": "low"})) == ["ok"]
    assert seen[-1]["reasoning_effort"] == "low"


def test_session_mcp_schema_exposes_read_and_set_action():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(item["function"] for item in FUNCTION_TOOL_SCHEMAS if item.get("function", {}).get("name") == "manage_session")
    assert "reasoning_effort" in schema["parameters"]["properties"]["action"]["enum"]
    assert "auto to clear" in schema["parameters"]["properties"]["value"]["description"]


def test_owner_api_and_mcp_read_patch_the_same_session(tmp_path, monkeypatch):
    import asyncio
    import core.database as db_mod
    import core.session_manager as sm_mod
    import routes.session_routes as routes_mod
    import src.agent_tools.session_tools as tools_mod
    import src.database as compatibility_db
    from core.session_manager import SessionManager

    engine = create_engine("sqlite:///" + (tmp_path / "api-mcp.db").as_posix())
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(sm_mod, "SessionLocal", factory)
    monkeypatch.setattr(routes_mod, "SessionLocal", factory)
    monkeypatch.setattr(compatibility_db, "SessionLocal", factory, raising=False)
    monkeypatch.setattr(compatibility_db, "Session", DbSession, raising=False)
    monkeypatch.setattr(routes_mod, "effective_user", lambda request: request.headers.get("x-owner", "alice"))
    monkeypatch.setattr(routes_mod, "_auth_disabled", lambda: False)
    monkeypatch.setattr("src.reasoning_levels.levels_for", lambda *a: {"levels": ["low", "high"], "source": "fixture"})
    manager = SessionManager()
    manager.create_session(session_id="owned", name="Owned", endpoint_url="http://fixture", model="model", owner="alice")
    manager.create_session(session_id="foreign", name="Foreign", endpoint_url="http://fixture", model="model", owner="bob")
    monkeypatch.setattr(tools_mod, "get_session_manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes_mod.setup_session_routes(manager, {}))

    with TestClient(app) as client:
        assert client.get("/api/session/owned/reasoning-effort", headers={"x-owner": "alice"}).json() == {
            "session_id": "owned", "reasoning_effort": None,
            "available_levels": ["low", "high"], "levels_source": "fixture"}
        patched = client.patch("/api/session/owned", data={"reasoning_effort": "high"}, headers={"x-owner": "alice"})
        assert patched.status_code == 200 and patched.json()["reasoning_effort"] == "high"
        assert client.get("/api/session/foreign/reasoning-effort", headers={"x-owner": "alice"}).status_code == 404
    mcp = asyncio.run(tools_mod.manage_session(json.dumps({
        "action": "reasoning_effort", "session_id": "owned"}), session_id="active", owner="alice"))
    assert mcp["reasoning_effort"] == "high" and mcp["available_levels"] == ["low", "high"]
    mcp = asyncio.run(tools_mod.manage_session(json.dumps({
        "action": "reasoning_effort", "session_id": "owned", "value": "low"}), session_id="active", owner="alice"))
    assert mcp["reasoning_effort"] == "low"
    assert db_mod.get_session_reasoning_effort("owned", "alice") == "low"
    engine.dispose()
