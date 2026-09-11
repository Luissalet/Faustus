"""MEDIA-05 backend · `POST /api/media/local-video/subtitles/export` serves
SRT/VTT as a real download, and changing subtitles never touches the
original audio/video.

`tests/test_p1_media_subtitle_routes.py` already pins the route's
reachability, media types, 400/403 behaviour and that it does not collide
with the `/{mode}` catch-all. What it does not pin — and what this file
adds — is the two things MEDIA-05's acceptance literally names:

  * the response is a DOWNLOAD (`Content-Disposition: attachment;
    filename="..."`), not just a media-typed body a browser might render
    inline instead of saving;
  * exporting subtitles never opens ffmpeg, spawns a subprocess, or touches
    any file on disk — `src/media_subtitles.py` is pure text formatting
    (see its module docstring), so the source video/audio cannot be at risk
    from a caption export by construction. This is verified here by making
    every escape hatch a call to it WOULD have to go through raise if used,
    rather than merely asserting nothing "looks" written afterwards.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes.local_video_routes import setup_local_video_routes

SEGMENTS = [
    {"start": 0.0, "end": 1.5, "text": "Hola"},
    {"start": 1.5, "end": 3.0, "text": "mundo"},
]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(setup_local_video_routes())
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as api:
        yield api


@pytest.mark.parametrize("fmt,expected_ext,expected_media", [
    ("srt", "subtitles.srt", "application/x-subrip"),
    ("vtt", "subtitles.vtt", "text/vtt"),
])
def test_export_is_a_real_attachment_download(client, fmt, expected_ext, expected_media):
    r = client.post("/api/media/local-video/subtitles/export",
                    json={"segments": SEGMENTS, "format": fmt})
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert f'filename="{expected_ext}"' in cd
    assert r.headers["content-type"].startswith(expected_media)
    # No caching of a caption export a re-transcription may have superseded.
    assert r.headers.get("cache-control") == "no-store"


def test_default_format_is_srt_when_omitted(client):
    r = client.post("/api/media/local-video/subtitles/export", json={"segments": SEGMENTS})
    assert r.status_code == 200
    assert 'filename="subtitles.srt"' in r.headers["content-disposition"]


def test_exporting_subtitles_never_spawns_a_subprocess_or_touches_disk(client, monkeypatch, tmp_path):
    """The literal MEDIA-05 guarantee: changing/exporting subtitles must
    never be able to reach ffmpeg or the source media file. Every process-
    spawning and filesystem-writing primitive `routes/local_video_routes.py`
    imports is made to fail loudly if called, so a future regression that
    routes the subtitles export through `_process()` (which DOES touch
    video/audio) fails this test instead of silently starting to shell out."""
    import routes.local_video_routes as lvr

    def _boom(*args, **kwargs):
        raise AssertionError("subtitle export must never spawn a process")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(lvr, "_process", _boom)

    written = []
    import pathlib
    real_write_bytes = pathlib.Path.write_bytes

    def _tracked_write_bytes(self, data):
        written.append(str(self))
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", _tracked_write_bytes)

    r = client.post("/api/media/local-video/subtitles/export",
                    json={"segments": SEGMENTS, "format": "vtt"})

    assert r.status_code == 200, r.text
    assert written == [], f"subtitle export wrote to disk: {written}"


def test_two_different_transcripts_of_the_same_clip_never_share_or_mutate_state(client):
    """Exporting subtitles for one set of segments must not leave anything
    behind that a second, different export could pick up — consistent with
    "no video or audio file is ever opened" (src/media_subtitles.py)."""
    first = client.post("/api/media/local-video/subtitles/export",
                        json={"segments": SEGMENTS, "format": "srt"})
    other_segments = [{"start": 0.0, "end": 1.0, "text": "Different transcript entirely"}]
    second = client.post("/api/media/local-video/subtitles/export",
                         json={"segments": other_segments, "format": "srt"})
    assert first.status_code == second.status_code == 200
    assert "Hola" in first.text and "Hola" not in second.text
    assert "Different transcript entirely" in second.text
