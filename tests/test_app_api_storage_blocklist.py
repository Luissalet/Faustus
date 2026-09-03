"""`app_api` must not allocate, free, or quarantine anything under /api/storage.

Same hole as Local models (tests/test_app_api_local_models_blocklist.py), and
worse in kind. `app_api` loops back with the process-wide internal-tool token,
which `require_admin` accepts with no user session and no approval card. The
Storage writes allocate gigabytes of ballast, release it again, and MOVE the
operator's files into quarantine — on a machine whose disk pressure is the very
thing the feature exists to manage. A model that has just read a web page saying
"free up space on this machine" must not be able to act on it.

GET /api/storage/status stays open on purpose: reading what is filling the disk,
and which candidates were vetoed and why, is exactly what the model should be
doing — so it can tell the user.
"""

import json

import pytest


def _no_loopback(monkeypatch, why):
    import httpx

    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(why)

    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedAsyncClient)


BLOCKED_STORAGE_CALLS = [
    ("POST", "/api/storage/ballast"),
    ("POST", "/api/storage/release"),
    ("POST", "/api/storage/quarantine"),
    ("POST", "/api/storage/undo"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", BLOCKED_STORAGE_CALLS)
async def test_app_api_refuses_storage_mutations_before_loopback(monkeypatch, method, path):
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, f"app_api must block {method} {path} before the loopback")

    result = await do_app_api(
        json.dumps({"action": "call", "method": method, "path": path,
                    "body": {"count": 4, "path": "/anything"}}),
        owner="admin",
    )

    assert result["exit_code"] == 1, result
    assert "blocked" in result["error"].lower()
    # The refusal has to name the surface the user owns, or the model retries.
    assert "Storage" in result["error"]


@pytest.mark.asyncio
async def test_app_api_still_allows_reading_the_storage_status(monkeypatch):
    from src.tool_implementations import do_app_api

    import httpx
    seen = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            seen["url"] = url
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = await do_app_api(
        json.dumps({"action": "call", "method": "GET", "path": "/api/storage/status"}),
        owner="admin",
    )
    assert result["exit_code"] == 0, result.get("error")
    assert seen["url"].endswith("/api/storage/status")
