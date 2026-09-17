"""Tests for routes/favicon_routes.py — GET /api/favicon?domain=<host>.

Covers: disk cache hit (no outbound fetch), a neutral SVG placeholder when
every fetch attempt fails, and that a private/loopback-looking "domain" is
refused before any fetch is attempted (SSRF guard at the route boundary,
independent of src.outbound_fetch's own guard).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import routes.favicon_routes as fav


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(fav, "FAVICON_CACHE_DIR", tmp_path / "favicons")
    app = FastAPI()
    app.include_router(fav.setup_favicon_routes())
    app.dependency_overrides[fav.require_user] = lambda: "tester"
    return TestClient(app, raise_server_exceptions=False)


def test_invalid_domain_returns_placeholder_without_fetching(client, monkeypatch):
    called = []
    monkeypatch.setattr(fav, "_fetch_favicon_bytes", lambda d: called.append(d) or None)
    resp = client.get("/api/favicon", params={"domain": "localhost"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.content == fav._PLACEHOLDER_SVG
    assert called == []  # never reached the fetch layer


def test_invalid_domain_with_path_is_rejected(client, monkeypatch):
    called = []
    monkeypatch.setattr(fav, "_fetch_favicon_bytes", lambda d: called.append(d) or None)
    resp = client.get("/api/favicon", params={"domain": "example.com/../../etc/passwd"})
    assert resp.status_code == 200
    assert resp.content == fav._PLACEHOLDER_SVG
    assert called == []


def test_placeholder_on_fetch_failure(client, monkeypatch):
    monkeypatch.setattr(fav, "_fetch_favicon_bytes", lambda d: None)
    resp = client.get("/api/favicon", params={"domain": "example.com"})
    assert resp.status_code == 200
    assert resp.content == fav._PLACEHOLDER_SVG


def test_successful_fetch_is_served_and_cached(client, monkeypatch, tmp_path):
    calls = []

    def fake_fetch(domain):
        calls.append(domain)
        return "image/png", b"\x89PNG-fake-bytes"

    monkeypatch.setattr(fav, "_fetch_favicon_bytes", fake_fetch)
    resp = client.get("/api/favicon", params={"domain": "example.com"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNG-fake-bytes"
    assert calls == ["example.com"]
    assert (tmp_path / "favicons" / "example.com.png").exists()


def test_cache_hit_skips_outbound_fetch(client, monkeypatch, tmp_path):
    cache_dir = tmp_path / "favicons"
    cache_dir.mkdir(parents=True)
    (cache_dir / "cached.example.png").write_bytes(b"cached-bytes")

    def fail_if_called(domain):
        raise AssertionError("should not fetch when cache is warm")

    monkeypatch.setattr(fav, "_fetch_favicon_bytes", fail_if_called)
    resp = client.get("/api/favicon", params={"domain": "cached.example"})
    assert resp.status_code == 200
    assert resp.content == b"cached-bytes"
    assert resp.headers["content-type"] == "image/png"


def test_stale_cache_entry_is_refetched(client, monkeypatch, tmp_path):
    import time

    cache_dir = tmp_path / "favicons"
    cache_dir.mkdir(parents=True)
    stale = cache_dir / "stale.example.ico"
    stale.write_bytes(b"old-bytes")
    old_time = time.time() - fav._CACHE_TTL_SECONDS - 3600
    import os
    os.utime(stale, (old_time, old_time))

    monkeypatch.setattr(fav, "_fetch_favicon_bytes", lambda d: ("image/x-icon", b"fresh-bytes"))
    resp = client.get("/api/favicon", params={"domain": "stale.example"})
    assert resp.status_code == 200
    assert resp.content == b"fresh-bytes"


@pytest.mark.parametrize("domain", [
    "",
    "localhost",
    "not a domain",
    "a" * 300,
    "example.com/evil",
    "user@example.com",
])
def test_safe_domain_rejects_malformed_input(domain):
    # _safe_domain is the syntax gate: scheme/path/credential smuggling and
    # obviously-malformed strings are refused before any fetch is attempted.
    # Refusing an actual private/loopback *hostname* (metadata.google.internal,
    # an internal.local suffix, a bare IP) is the outbound-fetch SSRF guard's
    # job (src.outbound_fetch.classify_destination), exercised below via the
    # real route instead of re-implemented here.
    assert fav._safe_domain(domain) is None


def test_private_hostname_is_refused_by_the_real_ssrf_guard(client):
    # No mocking of _fetch_favicon_bytes here: this exercises the real
    # src.outbound_fetch.fetch() SSRF guard end to end. A hostname that
    # resolves/points to a private or loopback destination must never be
    # followed -- the route falls back to the placeholder instead.
    resp = client.get("/api/favicon", params={"domain": "metadata.google.internal"})
    assert resp.status_code == 200
    assert resp.content == fav._PLACEHOLDER_SVG


def test_safe_domain_accepts_plain_hostnames():
    assert fav._safe_domain("example.com") == "example.com"
    assert fav._safe_domain("Sub.Example.CO.UK") == "sub.example.co.uk"


def test_fetch_favicon_bytes_uses_public_untrusted_profile(monkeypatch):
    seen_profiles = []

    def fake_fetch(url, *, profile, timeout, max_bytes, allowed_mime):
        seen_profiles.append(profile)
        return SimpleNamespace(status_code=200, content=b"icon-bytes", headers={"content-type": "image/x-icon"}, text="")

    monkeypatch.setattr(fav, "fetch", fake_fetch)
    result = fav._fetch_favicon_bytes("example.com")
    assert result == ("image/x-icon", b"icon-bytes")
    assert seen_profiles == [fav.PUBLIC_UNTRUSTED]


def test_fetch_favicon_bytes_falls_back_to_homepage_link(monkeypatch):
    def fake_fetch(url, *, profile, timeout, max_bytes, allowed_mime):
        if url.endswith("/favicon.ico"):
            return SimpleNamespace(status_code=404, content=b"", headers={}, text="")
        if url.endswith("example.com/"):
            html = '<html><head><link rel="icon" href="/static/icon.png"></head></html>'
            return SimpleNamespace(status_code=200, content=html.encode(), headers={"content-type": "text/html"}, text=html)
        return SimpleNamespace(status_code=200, content=b"png-bytes", headers={"content-type": "image/png"}, text="")

    monkeypatch.setattr(fav, "fetch", fake_fetch)
    result = fav._fetch_favicon_bytes("example.com")
    assert result == ("image/png", b"png-bytes")


def test_fetch_favicon_bytes_returns_none_on_every_failure(monkeypatch):
    def always_fail(url, *, profile, timeout, max_bytes, allowed_mime):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(fav, "fetch", always_fail)
    assert fav._fetch_favicon_bytes("example.com") is None
