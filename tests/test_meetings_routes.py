"""Route-level tests for routes/meetings_routes.py — owner scoping on the
list/detail/job endpoints, and that an upload creates a job (direct endpoint
calls, same pattern as tests/test_research_owner_scope_routes.py)."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from routes.meetings_routes import setup_meetings_routes


def _request(user: str):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace()),
        client=None,
    )


def _route(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


@pytest.fixture(autouse=True)
def _redirect_meetings_dir(tmp_path, monkeypatch):
    from src import meetings
    meetings_dir = tmp_path / "meetings"
    monkeypatch.setattr(meetings, "MEETINGS_DIR", str(meetings_dir))
    monkeypatch.setattr(meetings, "JOBS_FILE", str(meetings_dir / "jobs.json"))
    monkeypatch.setattr(meetings, "_TMP_DIR", str(meetings_dir / "_jobs"))
    return meetings_dir


def _seed_meeting(owner: str, meeting_id: str, title: str = "T"):
    from src import meetings
    import json
    from pathlib import Path

    Path(meetings.MEETINGS_DIR).mkdir(parents=True, exist_ok=True)
    (Path(meetings.MEETINGS_DIR) / f"{meeting_id}.md").write_text(f"# {title}\n\nbody\n", encoding="utf-8")
    (Path(meetings.MEETINGS_DIR) / f"{meeting_id}.json").write_text(
        json.dumps({"id": meeting_id, "title": title, "owner": owner, "date": "2026-01-01"}),
        encoding="utf-8",
    )


def test_list_meetings_is_owner_scoped_via_route():
    _seed_meeting("alice", "2026-01-01-standup")
    _seed_meeting("bob", "2026-01-01-budget")

    router = setup_meetings_routes()
    target = _route(router, "/api/meetings", "GET")

    out = asyncio.run(target(request=_request("alice"), limit=100))
    assert [m["id"] for m in out["meetings"]] == ["2026-01-01-standup"]


def test_get_meeting_404s_for_a_different_owner():
    _seed_meeting("alice", "2026-01-01-standup")
    router = setup_meetings_routes()
    target = _route(router, "/api/meetings/{meeting_id}", "GET")

    out = asyncio.run(target(meeting_id="2026-01-01-standup", request=_request("alice")))
    assert out["id"] == "2026-01-01-standup"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(meeting_id="2026-01-01-standup", request=_request("bob")))
    assert exc.value.status_code == 404


def test_job_status_is_owner_scoped():
    from src import meetings
    meetings._update_job("job-x", owner="alice", status="running")

    router = setup_meetings_routes()
    target = _route(router, "/api/meetings/jobs/{job_id}", "GET")

    out = asyncio.run(target(job_id="job-x", request=_request("alice")))
    assert out["status"] == "running"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(job_id="job-x", request=_request("bob")))
    assert exc.value.status_code == 404


def test_upload_creates_a_job(monkeypatch):
    from src import meetings

    started = {}

    def fake_create_job(audio_bytes, filename, *, title, language, project_id, owner):
        started["args"] = (audio_bytes, filename, title, language, project_id, owner)
        return "job-created"

    monkeypatch.setattr(meetings, "create_job", fake_create_job)

    router = setup_meetings_routes()
    target = _route(router, "/api/meetings", "POST")

    upload = UploadFile(file=io.BytesIO(b"RIFF....WAVEfmt "), filename="call.wav")
    out = asyncio.run(target(
        request=_request("alice"), file=upload, title="Weekly", language="es", project_id="",
    ))
    assert out == {"job_id": "job-created", "status": "queued"}
    assert started["args"][1] == "call.wav"
    assert started["args"][5] == "alice"


def test_upload_requires_authentication(monkeypatch):
    router = setup_meetings_routes()
    target = _route(router, "/api/meetings", "POST")
    upload = UploadFile(file=io.BytesIO(b"data"), filename="call.wav")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(request=_request(""), file=upload, title="", language="", project_id=""))
    assert exc.value.status_code == 401
