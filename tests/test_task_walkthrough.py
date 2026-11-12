import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import task_walkthrough as walk
from routes import task_walkthrough_routes as routes


@pytest.mark.parametrize("value", ["../x", "", "a/b", None])
def test_job_identifier_is_not_a_path(value):
    with pytest.raises(ValueError):
        walk.identifier(value)


def test_start_poll_and_video_use_the_same_job(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content)["steps"][0]["action"] == "click"
            return httpx.Response(200, json={"job": {"id": "job1", "type": "website_walkthrough", "state": "queued"}})
        if request.url.path == "/api/jobs/job1":
            return httpx.Response(200, json={"id": "job1", "type": "website_walkthrough", "state": "done", "outputs": {"asset_id": "asset1", "steps": [{"page_title": "Result"}]}})
        assert request.url.path == "/api/assets/asset1/file"
        assert request.headers["range"] == "bytes=0-3"
        return httpx.Response(206, content=b"mp4!", headers={"content-range": "bytes 0-3/4"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(walk, "connection", lambda owner: ("http://prospero", {}))
    monkeypatch.setattr(walk.httpx, "AsyncClient", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)))
    first = asyncio.run(walk.run("owner", {"url": "http://app", "steps": [{"action": "click", "selector": "a"}]}))
    assert first["state"] == "queued"
    final = asyncio.run(walk.run("owner", {"job_id": first["job_id"]}))
    assert final["video_url"] == "/api/task-walkthrough/job1/video"
    assert final["steps"][0]["page_title"] == "Result"
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "effective_user", lambda request: "owner")
    app = FastAPI()
    app.include_router(routes.setup_task_walkthrough_routes())
    with TestClient(app) as client:
        response = client.get(final["video_url"], headers={"range": "bytes=0-3"})
        assert response.status_code == 206 and response.content == b"mp4!"
        assert response.headers["content-range"] == "bytes 0-3/4"


def test_tool_reports_connection_failure_and_bad_arguments(monkeypatch):
    def unavailable(owner):
        raise ValueError("Connect Prospero")
    monkeypatch.setattr(walk, "connection", unavailable)
    result = asyncio.run(walk.TaskWalkthroughTool().execute('{"url":"http://app"}', {}))
    assert result["exit_code"] == 1 and "Connect Prospero" in result["error"]
    result = asyncio.run(walk.TaskWalkthroughTool().execute('[]', {}))
    assert result["exit_code"] == 1


def test_non_walkthrough_jobs_are_not_accepted(monkeypatch):
    monkeypatch.setattr(walk, "connection", lambda owner: ("http://prospero", {}))
    async def other(*args, **kwargs):
        return {"id": "job1", "type": "other", "state": "done"}
    monkeypatch.setattr(walk, "request", other)
    result = asyncio.run(walk.run("owner", {"job_id": "job1"}))
    assert result["exit_code"] == 1 and "not a website walkthrough" in result["error"]


def test_internal_mcp_selects_the_chat_owner_not_the_pseudo_user(monkeypatch):
    from types import SimpleNamespace
    from src import database

    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def query(self, *args): return self
        def filter(self, *args): return self
        def first(self): return SimpleNamespace(owner="chat-owner")

    monkeypatch.setattr(routes, "effective_user", lambda request: "internal-tool")
    monkeypatch.setattr(database, "SessionLocal", DB)
    monkeypatch.setattr(database, "Session", SimpleNamespace(id="id"), raising=False)
    assert routes.owner_of(None, "chat1") == "chat-owner"
    with pytest.raises(Exception) as error:
        routes.owner_of(None)
    assert error.value.status_code == 400
    monkeypatch.setattr(routes, "effective_user", lambda request: "person")
    assert routes.owner_of(None, "chat1") == "person"
