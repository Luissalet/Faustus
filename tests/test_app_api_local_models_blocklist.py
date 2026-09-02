"""`app_api` must not mutate /api/local-models.

The `app_api` tool loops back over HTTP with the process-wide internal-tool
token, which `require_admin` accepts without a user session. The Local models
routes delete models from the Ollama server, start multi-GB pulls, load and
unload models from VRAM and rewrite the per-model load options every request
inherits — an admin-only surface the model was reaching with no admin cookie
and no approval card (audited: DELETE /api/local-models/{name} with just the
internal token deleted the model). Reads (the list, the pull board) stay open.
"""

import json

import pytest


def _no_loopback(monkeypatch, why):
    import httpx

    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(why)

    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedAsyncClient)


BLOCKED_LOCAL_MODEL_CALLS = [
    ("DELETE", "/api/local-models/qwen3.5:9b"),
    ("DELETE", "/api/local-models/pulls/abc123"),
    ("POST", "/api/local-models/pull"),
    ("POST", "/api/local-models/load"),
    ("POST", "/api/local-models/unload"),
    ("PUT", "/api/local-models/qwen3.5:9b/options"),
    ("PUT", "/api/local-models/library/qwen3.5:9b/options"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", BLOCKED_LOCAL_MODEL_CALLS)
async def test_app_api_refuses_local_model_mutations_before_loopback(monkeypatch, method, path):
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, f"app_api must block {method} {path} before the loopback")

    result = await do_app_api(
        json.dumps({"action": "call", "method": method, "path": path,
                    "body": {"name": "qwen3.5:9b", "num_ctx": 4096}}),
        owner="admin",
    )

    assert result["exit_code"] == 1, result
    assert "blocked" in result["error"].lower()
    # The refusal names the surface the user owns, or the model just retries.
    assert "Local models" in result["error"]


@pytest.mark.asyncio
async def test_app_api_still_allows_the_local_model_reads(monkeypatch):
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

    for path in ("/api/local-models", "/api/local-models/pulls", "/api/local-models/discover",
                 "/api/local-models/qwen3.5:9b/options"):
        result = await do_app_api(
            json.dumps({"action": "call", "method": "GET", "path": path}), owner="admin")
        assert result["exit_code"] == 0, f"{path} must not be blocked: {result.get('error')}"
        assert seen["url"].endswith(path)


@pytest.mark.asyncio
async def test_endpoint_discovery_hides_the_local_model_mutations(monkeypatch):
    import httpx
    from src.tool_implementations import do_app_api

    class FakeResponse:
        def json(self):
            return {
                "paths": {
                    "/api/local-models": {"get": {"summary": "List"}},
                    "/api/local-models/pull": {"post": {"summary": "Pull"}},
                    "/api/local-models/load": {"post": {"summary": "Load"}},
                    "/api/local-models/unload": {"post": {"summary": "Unload"}},
                    "/api/local-models/pulls/{job_id}": {"delete": {"summary": "Cancel"}},
                    "/api/local-models/{name}/options": {"get": {"summary": "Options"},
                                                         "put": {"summary": "Set options"}},
                    "/api/local-models/{name}": {"delete": {"summary": "Delete"}},
                }
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = await do_app_api(json.dumps({"action": "endpoints", "filter": "local-models"}), owner="admin")

    assert result["exit_code"] == 0
    paths = {(e["method"], e["path"]) for e in result["endpoints"]}
    assert ("GET", "/api/local-models") in paths
    assert ("GET", "/api/local-models/{name}/options") in paths
    assert not any(m in ("POST", "PUT", "DELETE") for m, _ in paths), paths
