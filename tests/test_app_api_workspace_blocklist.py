"""`app_api` must not reach /api/workspace.

The `app_api` tool loops back over HTTP with `X-Odysseus-Internal-Token`, which
skips BOTH the auth layer and the per-tool approval gate the user sees before a
write_file or a bash. /api/workspace is the browser UI's file-mutation surface
(revert, checkpoint restore/reset, commit, AGENTS.md, open_editor, reveal), and
its read routes take the folder as a client parameter, so they sidestep the
workspace confinement the agent's own file tools enforce. A hallucinating or
prompt-injected model must not be able to call any of it.
"""

import json

import pytest


def _no_loopback(monkeypatch, why):
    """Make any attempt to actually issue the HTTP call an immediate failure."""
    import httpx

    class UnexpectedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(why)

    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedAsyncClient)


# Every /api/workspace verb that hands the model destructive power without an
# approval prompt. Each must be refused before the request leaves the process.
BLOCKED_WORKSPACE_CALLS = [
    ("POST", "/api/workspace/revert"),
    ("POST", "/api/workspace/checkpoint/restore"),
    ("POST", "/api/workspace/checkpoint/reset"),
    ("POST", "/api/workspace/commit"),
    ("POST", "/api/workspace/instructions/remember"),
    ("POST", "/api/workspace/instructions/draft"),
    ("POST", "/api/workspace/open_editor"),
    ("POST", "/api/workspace/reveal"),
    ("POST", "/api/workspace/review/abc123/decide"),
    # The reads are a confinement bypass, not a convenience: they take the
    # folder from the caller, so the model could enumerate/read any directory
    # on the host instead of the project roots read_file is confined to.
    ("GET", "/api/workspace/browse"),
    ("GET", "/api/workspace/files"),
    ("GET", "/api/workspace/file"),
    ("GET", "/api/workspace/file_diff"),
    ("GET", "/api/workspace/vet"),
    ("GET", "/api/workspace/checkpoint/status"),
    ("GET", "/api/workspace/checkpoint/list"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", BLOCKED_WORKSPACE_CALLS)
async def test_app_api_refuses_workspace_paths_before_loopback(monkeypatch, method, path):
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, f"app_api must block {method} {path} before the loopback")

    result = await do_app_api(
        json.dumps({"action": "call", "method": method, "path": path,
                    "body": {"workspace": "/tmp", "paths": ["a.py"]}}),
        owner="admin",
    )

    assert result["exit_code"] == 1
    assert "blocked" in result["error"].lower()
    # The refusal has to say what to use instead, or the model just retries.
    assert "read_file" in result["error"]


@pytest.mark.asyncio
async def test_app_api_blocks_workspace_without_a_leading_slash(monkeypatch):
    """`path` is normalised before the blocklist runs, so the bare form is
    covered too — otherwise the prefix check is trivially bypassable."""
    from src.tool_implementations import do_app_api

    _no_loopback(monkeypatch, "the leading slash must not decide whether a path is blocked")

    result = await do_app_api(
        json.dumps({"action": "call", "method": "POST", "path": "api/workspace/checkpoint/reset"}),
        owner="admin",
    )
    assert result["exit_code"] == 1 and "blocked" in result["error"].lower()


@pytest.mark.asyncio
async def test_app_api_still_allows_the_ordinary_read_surfaces(monkeypatch):
    """The line is drawn at /api/workspace, not at "reads": everything else the
    tool is documented for must keep working."""
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

    for path in ("/api/cookbook/gpus", "/api/notes/17", "/api/sessions"):
        result = await do_app_api(
            json.dumps({"action": "call", "method": "GET", "path": path}), owner="admin")
        assert result["exit_code"] == 0, f"{path} must not be blocked: {result.get('error')}"
        assert seen["url"].endswith(path)


@pytest.mark.asyncio
async def test_endpoint_discovery_hides_the_workspace_routes(monkeypatch):
    """A blocked path that still shows up in `action: endpoints` just teaches
    the model to try it."""
    import httpx
    from src.tool_implementations import do_app_api

    class FakeResponse:
        def json(self):
            return {
                "paths": {
                    "/api/workspace/browse": {"get": {"summary": "Browse"}},
                    "/api/workspace/revert": {"post": {"summary": "Revert File"}},
                    "/api/workspace/checkpoint/reset": {"post": {"summary": "Reset"}},
                    "/api/cookbook/gpus": {"get": {"summary": "List GPUs"}},
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

    result = await do_app_api(json.dumps({"action": "endpoints"}), owner="admin")

    assert result["exit_code"] == 0
    paths = {(e["method"], e["path"]) for e in result["endpoints"]}
    assert ("GET", "/api/cookbook/gpus") in paths
    assert all(not e["path"].startswith("/api/workspace") for e in result["endpoints"]), paths


def test_the_blocklist_documents_why_the_whole_prefix_is_blocked():
    """The prefix, not a hand-maintained list of verbs: a new POST added to
    routes/workspace_routes.py must be closed by default, not by remembering."""
    from src.tools.system import _APP_API_BLOCKLIST_PREFIXES

    assert "/api/workspace" in _APP_API_BLOCKLIST_PREFIXES
    for prefix in ("/api/workspace/revert", "/api/workspace/checkpoint/restore",
                   "/api/workspace/checkpoint/reset", "/api/workspace/commit",
                   "/api/workspace/instructions/remember",
                   "/api/workspace/open_editor", "/api/workspace/reveal"):
        assert any(prefix.startswith(p) for p in _APP_API_BLOCKLIST_PREFIXES), prefix
