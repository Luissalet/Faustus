"""B-003: the HF GGUF listing's error handler used an undefined name.

`except Exception: logger.exception(..., repo)` — the parameter is `repo_id`,
so every network hiccup turned the controlled `{"ok": false}` answer into a
NameError and an HTTP 500. These tests drive each failure shape through the
real endpoint and pin the typed answers.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from routes.cookbook_routes import setup_cookbook_routes

REPO = "TheBloke/Llama-2-7B-GGUF"


def _endpoint():
    router = setup_cookbook_routes()
    matches = [
        r for r in router.routes
        if getattr(r, "path", "") == "/api/cookbook/hf-gguf-files"
    ]
    assert matches, "the hf-gguf-files route disappeared"
    return matches[-1].endpoint  # last wins: factories can accumulate (B-006)


class _Resp:
    def __init__(self, status=200, payload=None, bad_body=None):
        self.status_code = status
        self._payload = payload
        self._bad_body = bad_body

    def json(self):
        if self._bad_body is not None:
            raise json.JSONDecodeError("Expecting value", self._bad_body, 0)
        return self._payload


def _install_client(monkeypatch, behaviour):
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return behaviour(url, headers)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _call(monkeypatch, behaviour, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HF_TOKEN", raising=False)
    _install_client(monkeypatch, behaviour)
    return asyncio.run(_endpoint()(repo_id=REPO, owner="tester"))


def test_network_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(url, headers):
        raise httpx.ConnectError("no route to host")

    out = _call(monkeypatch, boom, tmp_path)
    assert out == {"ok": False, "files": [], "error": "HF API unreachable"}


def test_timeout_is_its_own_answer(tmp_path, monkeypatch):
    def slow(url, headers):
        raise httpx.ReadTimeout("too slow")

    out = _call(monkeypatch, slow, tmp_path)
    assert out == {"ok": False, "files": [], "error": "HF API request timed out"}


def test_two_hundred_with_a_body_that_is_not_json(tmp_path, monkeypatch):
    out = _call(monkeypatch, lambda u, h: _Resp(200, bad_body="<html>nope</html>"), tmp_path)
    assert out == {"ok": False, "files": [], "error": "HF API returned invalid JSON"}


def test_valid_json_of_the_wrong_shape(tmp_path, monkeypatch):
    """A list where an object was expected used to be an AttributeError."""
    out = _call(monkeypatch, lambda u, h: _Resp(200, payload=["not", "a", "repo"]), tmp_path)
    assert out["ok"] is False
    assert "unexpected payload" in out["error"]


def test_http_error_status_keeps_its_code(tmp_path, monkeypatch):
    out = _call(monkeypatch, lambda u, h: _Resp(404, payload={}), tmp_path)
    assert out == {"ok": False, "files": [], "error": "HF API HTTP 404"}


def test_happy_path_lists_only_gguf_files(tmp_path, monkeypatch):
    payload = {
        "siblings": [
            {"rfilename": "README.md"},
            {"rfilename": "llama-2-7b.Q4_K_M.gguf"},
            {"rfilename": "llama-2-7b.Q8_0.GGUF"},
            {"nope": True},
            "a bare string",
        ]
    }
    out = _call(monkeypatch, lambda u, h: _Resp(200, payload=payload), tmp_path)
    assert out["ok"] is True
    assert out["repo_id"] == REPO
    assert out["files"] == ["llama-2-7b.Q4_K_M.gguf", "llama-2-7b.Q8_0.GGUF"]


def test_the_undefined_name_is_gone():
    source = Path("routes/cookbook_routes.py").read_text(encoding="utf-8")
    assert 'logger.exception("HF GGUF file scan failed for %s", repo)' not in source
