"""tests/test_media_capabilities.py — MEDIA-01: probed, dated, never assumed.

The rule under test is in the name of the module: a backend is `installed`
only because a real probe just said so, never because a static list claims
it. `ffmpeg`/`ffprobe` are actually on this machine, so the ffmpeg probe is
exercised for real in both directions (present via the real binary, absent by
monkeypatching `shutil.which`); the optional Python packages are not
installed here, so their "present" path is exercised by monkeypatching
`find_spec`/`shutil.which` rather than actually installing anything, which
would be exactly the kind of heavy setup the batch's rules ask this lot to
avoid.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.middleware import require_admin
from src import media_capabilities as caps


def _client(admin=True):
    from routes.local_video_routes import setup_local_video_routes
    app = FastAPI()
    app.include_router(setup_local_video_routes())

    def access():
        if not admin:
            raise HTTPException(403, "Admin required")

    app.dependency_overrides[require_admin] = access
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every probe test wants a fresh probe, not whatever the previous test's
    call cached."""
    caps._cache["manifest"] = None
    caps._cache["at"] = 0.0
    yield
    caps._cache["manifest"] = None
    caps._cache["at"] = 0.0


# ── individual probes ────────────────────────────────────────────────────────

def test_ffmpeg_probe_finds_the_real_binary_on_this_machine():
    result = caps.probe_ffmpeg()
    assert result["installed"] is True
    assert result["version"]
    assert result["probed_at"]


def test_ffmpeg_probe_is_honest_when_the_binary_is_not_on_path(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda *_: None)
    result = caps.probe_ffmpeg()
    assert result["installed"] is False
    assert "not found on PATH" in result["detail"]


def test_ffmpeg_probe_reports_a_hung_binary_as_not_installed(monkeypatch):
    import subprocess as sp

    def hangs(*args, **kwargs):
        raise sp.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 3))

    monkeypatch.setattr(caps.subprocess, "run", hangs)
    result = caps.probe_ffmpeg()
    assert result["installed"] is False
    assert "did not answer" in result["detail"]


def test_stt_probe_is_false_when_faster_whisper_is_not_installed(monkeypatch):
    # Luis's venv has faster_whisper for real: the probe must be asked about
    # a world without it, not the world the test happens to run in.
    monkeypatch.setattr(caps, "_importable", lambda module: False)
    result = caps.probe_stt()
    assert result["installed"] is False
    assert "faster_whisper" in result["detail"]


def test_stt_probe_is_true_when_the_package_is_importable(monkeypatch):
    monkeypatch.setattr(caps, "_importable", lambda module: module == "faster_whisper")
    assert caps.probe_stt()["installed"] is True


def test_tts_kokoro_probe_reflects_import_status(monkeypatch):
    assert caps.probe_tts_kokoro()["installed"] is False
    monkeypatch.setattr(caps, "_importable", lambda module: module == "kokoro")
    assert caps.probe_tts_kokoro()["installed"] is True


def test_tts_piper_probe_checks_both_the_binary_and_the_package():
    result = caps.probe_tts_piper()
    assert result["installed"] is False  # neither is present in this environment
    assert "no piper" in result["detail"]


def test_tts_piper_probe_true_when_the_binary_is_on_path(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda name: "/usr/bin/piper" if name == "piper" else None)
    result = caps.probe_tts_piper()
    assert result["installed"] is True
    assert "/usr/bin/piper" in result["detail"]


def test_comfyui_probe_reuses_the_pool_and_reports_no_engine_when_unconfigured(monkeypatch):
    from src.media_backends import pool
    monkeypatch.setattr(pool, "urls", lambda: ["http://127.0.0.1:1/"])  # nothing listens here
    result = caps.probe_comfyui()
    assert result["installed"] is False
    assert result["engines"] and result["engines"][0]["ok"] is False


def test_comfyui_probe_finds_a_real_running_engine(monkeypatch):
    from tests.test_comfyui_backend import FakeComfy
    fake = FakeComfy()
    url = fake.start()
    try:
        monkeypatch.setenv("COMFYUI_URL", url)
        result = caps.probe_comfyui()
        assert result["installed"] is True
        assert result["engines"] and result["engines"][0]["ok"] is True
        assert result["engines"][0]["url"] == url
    finally:
        fake.stop()


def test_comfyui_probe_never_raises_even_if_the_pool_itself_breaks(monkeypatch):
    from src.media_backends import pool
    def broken(**_):
        raise RuntimeError("boom")
    monkeypatch.setattr(pool, "survey", broken)
    result = caps.probe_comfyui()
    assert result["installed"] is False
    assert "boom" in result["detail"]


def test_every_backend_in_the_manifest_carries_a_fresh_probe_timestamp():
    """Every probe function is called by `capabilities_manifest()`, and every
    one of them stamps `probed_at` freshly — the property MEDIA-01 asks for
    ("nunca disponible sin probe") checked at the manifest level."""
    manifest = caps.capabilities_manifest(force=True)
    for name, backend in manifest["backends"].items():
        assert "installed" in backend
        assert backend.get("probed_at"), f"{name} reported no probe timestamp"


# ── manifest caching ─────────────────────────────────────────────────────────

def test_capabilities_manifest_caches_briefly(monkeypatch):
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"installed": True, "detail": "fake", "probed_at": "now"}

    monkeypatch.setitem(caps._PROBES, "ffmpeg", counted)
    first = caps.capabilities_manifest()
    second = caps.capabilities_manifest()
    assert calls["n"] == 1, "a second call within the cache window must not re-probe"
    assert first is second


def test_force_always_reprobes(monkeypatch):
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"installed": True, "detail": "fake", "probed_at": "now"}

    monkeypatch.setitem(caps._PROBES, "ffmpeg", counted)
    caps.capabilities_manifest()
    caps.capabilities_manifest(force=True)
    assert calls["n"] == 2


def test_the_cache_expires_on_its_own(monkeypatch):
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"installed": True, "detail": "fake", "probed_at": "now"}

    monkeypatch.setitem(caps._PROBES, "ffmpeg", counted)
    monkeypatch.setattr(caps, "_CACHE_SECONDS", 0.01)
    caps.capabilities_manifest()
    time.sleep(0.02)
    caps.capabilities_manifest()
    assert calls["n"] == 2


# ── HTTP surface ─────────────────────────────────────────────────────────────

def test_capabilities_route_is_admin_gated():
    assert _client(admin=False).get("/api/media/capabilities").status_code == 403


def test_capabilities_route_returns_the_manifest():
    body = _client().get("/api/media/capabilities").json()
    assert body["checked_at"]
    assert set(body["backends"]) == {"ffmpeg", "comfyui", "stt_faster_whisper",
                                     "tts_kokoro", "tts_piper"}
    assert body["backends"]["ffmpeg"]["installed"] is True


def test_the_existing_local_video_capabilities_route_is_unchanged():
    """Additive only: the narrower, pre-existing endpoint this lot was told
    not to alter still answers exactly as before."""
    body = _client().get("/api/media/local-video/capabilities").json()
    assert "ffmpeg" in body and "max_seconds" in body
