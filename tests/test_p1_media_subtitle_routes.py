"""MEDIA-05 over HTTP — `POST /api/media/local-video/subtitles/export`.

A DIFFERENT path shape (two segments) than the existing `POST /{mode}`
catch-all one level up, checked here explicitly: this route must be
reachable, and the existing `/{mode}` route must still take everything it
took before (`transcribe`/`dub`) — "subtitles" is never treated as a `mode`.
"""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.local_video_routes import setup_local_video_routes


def client(admin=True):
    app = FastAPI()
    app.include_router(setup_local_video_routes())
    def access():
        if not admin:
            raise HTTPException(403, "Admin required")
    app.dependency_overrides[require_admin] = access
    return TestClient(app)


SEGMENTS = [{"start": 0, "end": 1.5, "text": "Hola"}, {"start": 1.5, "end": 3, "text": "mundo"}]


def test_export_srt_over_http():
    with client() as api:
        resp = api.post("/api/media/local-video/subtitles/export",
                        json={"segments": SEGMENTS, "format": "srt"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-subrip")
    assert "Hola" in resp.text and "-->" in resp.text


def test_export_vtt_over_http():
    with client() as api:
        resp = api.post("/api/media/local-video/subtitles/export",
                        json={"segments": SEGMENTS, "format": "vtt"})
    assert resp.status_code == 200
    assert resp.text.startswith("WEBVTT")


def test_bad_segments_are_refused_not_500(monkeypatch):
    with client() as api:
        resp = api.post("/api/media/local-video/subtitles/export",
                        json={"segments": [{"start": 5, "end": 1, "text": "backwards"}], "format": "srt"})
    assert resp.status_code == 400


def test_admin_gate_applies_to_the_new_route_too():
    with client(False) as api:
        resp = api.post("/api/media/local-video/subtitles/export",
                        json={"segments": SEGMENTS, "format": "srt"})
    assert resp.status_code == 403


def test_subtitles_export_does_not_collide_with_the_mode_catch_all(monkeypatch):
    """`/{mode}` still refuses everything it always refused (no video file,
    no valid mode) — "subtitles" being a real path now must not have
    swallowed requests aimed at the transcribe/dub endpoint."""
    with client() as api:
        resp = api.post("/api/media/local-video/transcribe",
                        data={"language": "en", "segments": "[]"})
    assert resp.status_code in (400, 422)  # missing file — never 404/mode confusion
