"""Dictate-anywhere routes (routes/dictation_routes.py): admin + loopback
gating, the STT pipeline (transcribe-and-paste) with fakes, and a clean
error when STT isn't configured. No real Windows call is ever made — the
underlying paste/capture is monkeypatched at module level.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import routes.dictation_routes as dictation_routes
from src import dictation_paste as dp


def make_app(*, admin: bool = True, user: str | None = "admin"):
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request: Request, call_next):
        request.state.current_user = user
        return await call_next(request)

    app.include_router(dictation_routes.setup_dictation_routes(fake_stt()))
    app.state.current_user = user
    return app, admin


def fake_stt(available=True, text="hola mundo"):
    return SimpleNamespace(available=available, transcribe=lambda *a, **kw: text)


@pytest.fixture(autouse=True)
def _admin(monkeypatch, request):
    # Default every test to admin=True; individual tests override via
    # monkeypatch again after client() is built, or by passing admin=False.
    monkeypatch.setattr(dictation_routes, "owner_is_admin_or_single_user", lambda owner: True)
    yield


def client(app):
    return TestClient(app, base_url="http://testclient")


# ── Auth / locality gate ────────────────────────────────────────────────

def test_non_admin_gets_403(monkeypatch):
    monkeypatch.setattr(dictation_routes, "owner_is_admin_or_single_user", lambda owner: False)
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "hi"})
    assert resp.status_code == 403


def test_admin_over_non_loopback_host_is_rejected(monkeypatch):
    monkeypatch.setattr(dictation_routes, "_LOOPBACK_HOSTS", frozenset({"127.0.0.1"}))
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "hi"})
    # TestClient's default client host is "testclient", now excluded.
    assert resp.status_code == 403


def test_cross_origin_request_is_rejected(monkeypatch):
    app, _ = make_app()
    with client(app) as c:
        resp = c.post(
            "/api/dictation/paste",
            json={"text": "hi"},
            headers={"sec-fetch-site": "cross-site"},
        )
    assert resp.status_code == 403


# ── /paste ───────────────────────────────────────────────────────────────

def test_paste_delegates_to_dictation_paste(monkeypatch):
    calls = {}

    def fake_paste_text(text, *, target=None, method="clipboard"):
        calls["args"] = (text, target, method)
        return {"method": method, "chars": len(text), "refocused": False, "clipboard_restored": True, "note": None}

    monkeypatch.setattr(dp, "paste_text", fake_paste_text)
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "hola", "method": "type"})
    assert resp.status_code == 200
    assert resp.json()["method"] == "type"
    assert calls["args"][0] == "hola"
    assert calls["args"][2] == "type"


def test_paste_with_target_round_trips(monkeypatch):
    seen = {}

    def fake_paste_text(text, *, target=None, method="clipboard"):
        seen["target"] = target
        return {"method": method, "chars": len(text), "refocused": True, "clipboard_restored": True, "note": None}

    monkeypatch.setattr(dp, "paste_text", fake_paste_text)
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "hola", "target": {"handle": 5, "title": "Notepad"}})
    assert resp.status_code == 200
    assert seen["target"] == dp.ForegroundTarget(handle=5, title="Notepad")


def test_paste_rejects_empty_text():
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "   "})
    assert resp.status_code == 400


def test_paste_surfaces_unsupported_platform_as_501(monkeypatch):
    def fake_paste_text(*a, **kw):
        raise dp.DictationUnsupported()

    monkeypatch.setattr(dp, "paste_text", fake_paste_text)
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/paste", json={"text": "hola"})
    assert resp.status_code == 501


# ── /recorder (hidden-window page for the desktop shell) ───────────────

def test_recorder_page_served_to_local_admin():
    app, _ = make_app()
    with client(app) as c:
        resp = c.get("/api/dictation/recorder")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "dictationRecorder" in resp.text
    assert "/api/dictation/transcribe-and-paste" in resp.text


def test_recorder_page_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(dictation_routes, "owner_is_admin_or_single_user", lambda owner: False)
    app, _ = make_app()
    with client(app) as c:
        resp = c.get("/api/dictation/recorder")
    assert resp.status_code == 403


# ── /capture-target ──────────────────────────────────────────────────────

def test_capture_target_returns_handle_and_title(monkeypatch):
    monkeypatch.setattr(dp, "capture_foreground", lambda: dp.ForegroundTarget(handle=9, title="Terminal"))
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/capture-target")
    assert resp.status_code == 200
    assert resp.json() == {"handle": 9, "title": "Terminal"}


def test_capture_target_unsupported_on_non_windows(monkeypatch):
    def raiser():
        raise dp.DictationUnsupported()

    monkeypatch.setattr(dp, "capture_foreground", raiser)
    app, _ = make_app()
    with client(app) as c:
        resp = c.post("/api/dictation/capture-target")
    assert resp.status_code == 501


# ── /transcribe-and-paste ────────────────────────────────────────────────

def test_transcribe_and_paste_full_pipeline(monkeypatch):
    monkeypatch.setattr(dp, "IS_WINDOWS", True)
    pasted = {}

    def fake_paste_text(text, *, target=None, method="clipboard"):
        pasted["text"] = text
        pasted["target"] = target
        return {"method": method, "chars": len(text), "refocused": bool(target), "clipboard_restored": True, "note": None}

    monkeypatch.setattr(dp, "paste_text", fake_paste_text)
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request: Request, call_next):
        request.state.current_user = "admin"
        return await call_next(request)

    app.include_router(dictation_routes.setup_dictation_routes(fake_stt(text="buenas tardes")))
    with client(app) as c:
        resp = c.post(
            "/api/dictation/transcribe-and-paste",
            files={"file": ("a.wav", b"RIFFfakeaudio", "audio/wav")},
            data={"method": "clipboard", "target_handle": "5", "target_title": "Notepad"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "buenas tardes"
    assert body["pasted"] is True
    assert pasted["text"] == "buenas tardes"
    assert pasted["target"] == dp.ForegroundTarget(handle=5, title="Notepad")


def test_transcribe_and_paste_without_stt_configured_is_clean_error():
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request: Request, call_next):
        request.state.current_user = "admin"
        return await call_next(request)

    app.include_router(dictation_routes.setup_dictation_routes(fake_stt(available=False)))
    with client(app) as c:
        # Even off Windows this must fail with a clean STT/platform error,
        # never a traceback.
        resp = c.post(
            "/api/dictation/transcribe-and-paste",
            files={"file": ("a.wav", b"RIFFfakeaudio", "audio/wav")},
        )
    assert resp.status_code in (501, 503)
    assert "detail" in resp.json()


def test_transcribe_and_paste_rejects_empty_audio(monkeypatch):
    monkeypatch.setattr(dp, "IS_WINDOWS", True)
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request: Request, call_next):
        request.state.current_user = "admin"
        return await call_next(request)

    app.include_router(dictation_routes.setup_dictation_routes(fake_stt()))
    with client(app) as c:
        resp = c.post("/api/dictation/transcribe-and-paste", files={"file": ("a.wav", b"", "audio/wav")})
    assert resp.status_code == 400


def test_transcribe_and_paste_empty_transcript_does_not_paste(monkeypatch):
    monkeypatch.setattr(dp, "IS_WINDOWS", True)
    calls = []
    monkeypatch.setattr(dp, "paste_text", lambda *a, **kw: calls.append(1))
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request: Request, call_next):
        request.state.current_user = "admin"
        return await call_next(request)

    app.include_router(dictation_routes.setup_dictation_routes(fake_stt(text="")))
    with client(app) as c:
        resp = c.post("/api/dictation/transcribe-and-paste", files={"file": ("a.wav", b"RIFFdata", "audio/wav")})
    assert resp.status_code == 200
    assert resp.json() == {"text": "", "pasted": False}
    assert calls == []
