"""tests/test_rest_mcp_bridge.py — bridges/rest_mcp/server.py, the generic
REST -> MCP stdio adapter (lot C of the "Apps" wave).

Unit tests exercise the manifest/OpenAPI -> tool-spec builders directly
(`bridges.rest_mcp.server`, importable without touching the network or a
subprocess). The end-to-end smoke tests spawn the real script as a stdio
child (exactly how `src/mcp_manager.py::_connect_stdio` would) against a
tiny local `http.server` app, and drive it with the `mcp` client SDK the
same way `tests/test_mcp_env_and_stderr.py` documents the server side of.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

from bridges.rest_mcp import server as rest_mcp

pytestmark = pytest.mark.asyncio

SERVER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bridges", "rest_mcp", "server.py")

BIG_BODY = json.dumps({"data": "x" * 30_000}).encode()


class _AppHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401 - keep pytest output clean
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/health"):
            self._send_json({"status": "ok"})
        elif self.path.startswith("/api/thing/"):
            from urllib.parse import urlsplit, parse_qs
            parts = urlsplit(self.path)
            item_id = parts.path.rsplit("/", 1)[-1]
            q = parse_qs(parts.query).get("q", [None])[0]
            self._send_json({"id": item_id, "q": q})
        elif self.path.startswith("/api/secure"):
            if self.headers.get("Authorization") != "Bearer sekret":
                self.send_response(401)
                self.end_headers()
                return
            self._send_json({"ok": True})
        elif self.path.startswith("/api/big"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(BIG_BODY)
        elif self.path.startswith("/api/binary"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"\x00\x01\x02\x03binary")
        elif self.path.startswith("/openapi.json"):
            self._send_json({
                "openapi": "3.0.0",
                "info": {"title": "Fake app", "version": "1.0"},
                "paths": {
                    "/api/thing/{id}": {
                        "get": {
                            "operationId": "get_thing",
                            "summary": "Get a thing.",
                            "parameters": [
                                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                                {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                            ],
                        }
                    },
                    "/api/items": {
                        "post": {
                            "operationId": "create_item",
                            "summary": "Create an item.",
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": {
                                    "type": "object",
                                    "required": ["name"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "qty": {"type": "integer"},
                                    },
                                }}},
                            },
                        }
                    },
                    "/api/upload": {
                        "post": {
                            "operationId": "upload_file",
                            "requestBody": {
                                "content": {"multipart/form-data": {"schema": {"type": "object"}}},
                            },
                        }
                    },
                },
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        if self.path.startswith("/api/items"):
            self._send_json({"created": payload}, status=201)
        else:
            self._send_json({"received": payload})


@pytest.fixture
def app_url():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _AppHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _manifest_path(tmp_path, tools, name="Fake App", instructions=None):
    data = {"name": name, "tools": tools}
    if instructions:
        data["instructions"] = instructions
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return str(path)


# ── unit tests: builders (no subprocess) ───────────────────────────────────

def test_loopback_check_accepts_loopback_and_rejects_others():
    assert rest_mcp.is_loopback_base_url("http://127.0.0.1:8767") is True
    assert rest_mcp.is_loopback_base_url("http://localhost:5000") is True
    assert rest_mcp.is_loopback_base_url("http://[::1]:9000") is True
    assert rest_mcp.is_loopback_base_url("http://10.0.0.5:8080") is False
    assert rest_mcp.is_loopback_base_url("http://example.com") is False
    assert rest_mcp.is_loopback_base_url("") is False


def test_manifest_to_tools_builds_canonical_specs():
    manifest = {
        "name": "X",
        "tools": [
            {"name": "get_thing", "method": "GET", "path": "/api/thing/{id}",
             "params": {"id": {"in": "path", "type": "string", "required": True}}},
            {"name": "write_thing", "method": "POST", "path": "/api/thing/{id}", "readonly": False,
             "params": {"id": {"in": "path", "type": "string", "required": True},
                        "body": {"in": "body", "type": "object", "required": True}}},
            {"name": "bad name!", "method": "GET", "path": "/x", "params": {}},
        ],
    }
    specs, notes = rest_mcp.manifest_to_tools(manifest)
    names = {s["name"] for s in specs}
    assert names == {"get_thing", "write_thing"}
    by_name = {s["name"]: s for s in specs}
    assert by_name["get_thing"]["readonly"] is True   # defaulted from method
    assert by_name["write_thing"]["readonly"] is False
    assert notes and "bad name!" in notes[0]


def test_openapi_to_tools_defaults_to_get_only():
    spec = {"paths": {
        "/a": {"get": {"operationId": "get_a"}},
        "/b": {"post": {"operationId": "post_b"}},
    }}
    specs, notes = rest_mcp.openapi_to_tools(spec, None)
    assert {s["name"] for s in specs} == {"get_a"}


def test_openapi_to_tools_allow_regex_widens_the_filter():
    spec = {"paths": {
        "/a": {"get": {"operationId": "get_a"}},
        "/b": {"post": {"operationId": "post_b"}},
    }}
    specs, notes = rest_mcp.openapi_to_tools(spec, r"^(GET|POST) ")
    assert {s["name"] for s in specs} == {"get_a", "post_b"}


def test_openapi_body_flattens_a_small_plain_object():
    spec = {"paths": {"/items": {"post": {
        "operationId": "create_item",
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
        }}}},
    }}}}
    specs, _ = rest_mcp.openapi_to_tools(spec, r"^POST ")
    spec_out = specs[0]
    kinds = {m["orig"]: m["in"] for m in spec_out["params"].values()}
    assert kinds == {"name": "body_field", "qty": "body_field"}


def test_openapi_body_falls_back_to_a_single_body_argument_when_large():
    props = {f"f{i}": {"type": "string"} for i in range(15)}
    spec = {"paths": {"/items": {"post": {
        "operationId": "create_item",
        "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": props}}}},
    }}}}
    specs, _ = rest_mcp.openapi_to_tools(spec, r"^POST ")
    kinds = [m["in"] for m in specs[0]["params"].values()]
    assert kinds == ["body"]


def test_openapi_multipart_body_is_skipped_with_a_note():
    spec = {"paths": {"/upload": {"post": {
        "operationId": "upload_file",
        "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
    }}}}
    specs, notes = rest_mcp.openapi_to_tools(spec, r"^POST ")
    assert specs == []
    assert notes and "multipart" in notes[0]


def test_response_truncated_at_20kb():
    big = json.dumps({"x": "y" * 40_000})
    rendered = rest_mcp._render_response(200, "application/json", big.encode())
    assert len(rendered.encode("utf-8")) < len(big) + 200
    assert "truncated" in rendered


def test_binary_response_is_never_returned_as_bytes():
    rendered = rest_mcp._render_response(200, "application/octet-stream", b"\x00\x01\x02")
    assert "binary response" in rendered
    assert "application/octet-stream" in rendered
    assert "\x00" not in rendered


# ── smoke tests: the real script as a stdio subprocess ─────────────────────

def test_non_loopback_base_url_refused_at_startup():
    env = {**os.environ, "REST_BASE_URL": "http://example.com"}
    result = subprocess.run([sys.executable, SERVER_PATH], env=env,
                             capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "loopback" in result.stderr.lower()


async def test_manifest_tools_appear_over_stdio(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manifest = _manifest_path(tmp_path, tools=[
        {"name": "get_thing", "description": "Get a thing.", "method": "GET", "path": "/api/thing/{id}",
         "params": {"id": {"in": "path", "type": "string", "required": True}}},
    ])
    env = {**os.environ, "REST_BASE_URL": app_url, "REST_MANIFEST": manifest, "REST_NAME": "Fake App"}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {"get_thing", "app_status"}

            result = await session.call_tool("get_thing", {"id": "42"})
            text = result.content[0].text
            assert '"id": "42"' in text

            status = await session.call_tool("app_status", {})
            assert "ok" in status.content[0].text


async def test_openapi_tools_path_query_and_body(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "REST_BASE_URL": app_url,
           "REST_OPENAPI_URL": app_url + "/openapi.json", "REST_ALLOW": r"^(GET|POST) "}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "get_thing" in names
            assert "create_item" in names
            assert "upload_file" not in names  # multipart, skipped
            get_thing = next(t for t in tools.tools if t.name == "get_thing")
            assert get_thing.annotations is not None and get_thing.annotations.readOnlyHint is True
            props = get_thing.inputSchema["properties"]
            assert "id" in props and "q" in props
            assert get_thing.inputSchema.get("required") == ["id"]

            result = await session.call_tool("get_thing", {"id": "7", "q": "hi"})
            text = result.content[0].text
            assert '"id": "7"' in text and '"q": "hi"' in text

            created = await session.call_tool("create_item", {"name": "widget", "qty": 3})
            body = created.content[0].text
            assert '"name": "widget"' in body and '"qty": 3' in body


async def test_openapi_default_allow_hides_post(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "REST_BASE_URL": app_url, "REST_OPENAPI_URL": app_url + "/openapi.json"}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "get_thing" in names
            assert "create_item" not in names  # POST, default REST_ALLOW is GET-only


async def test_manifest_survives_even_when_openapi_adds_write_tools(tmp_path, app_url):
    """The contract's own example: OpenAPI supplies the read-only tools,
    a manifest adds the write ones a `^GET ` allow would otherwise hide."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manifest = _manifest_path(tmp_path, tools=[
        {"name": "create_item", "description": "Create an item (manifest).", "method": "POST", "path": "/api/items",
         "readonly": False, "params": {"body": {"in": "body", "type": "object", "required": True}}},
    ])
    env = {**os.environ, "REST_BASE_URL": app_url, "REST_OPENAPI_URL": app_url + "/openapi.json",
           "REST_MANIFEST": manifest}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"get_thing", "create_item", "app_status"} <= names


async def test_bearer_token_is_sent(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    token_file = tmp_path / "token"
    token_file.write_text("sekret\n")
    manifest = _manifest_path(tmp_path, tools=[
        {"name": "get_secure", "description": "Secure endpoint.", "method": "GET", "path": "/api/secure", "params": {}},
    ])
    env = {**os.environ, "REST_BASE_URL": app_url, "REST_MANIFEST": manifest,
           "REST_TOKEN_FILE": str(token_file)}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_secure", {})
            assert '"ok": true' in result.content[0].text


async def test_response_is_truncated_over_stdio(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manifest = _manifest_path(tmp_path, tools=[
        {"name": "get_big", "description": "Big response.", "method": "GET", "path": "/api/big", "params": {}},
    ])
    env = {**os.environ, "REST_BASE_URL": app_url, "REST_MANIFEST": manifest}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_big", {})
            text = result.content[0].text
            assert "truncated" in text
            assert len(text.encode("utf-8")) < len(BIG_BODY)


async def test_binary_response_is_a_note_not_bytes_over_stdio(tmp_path, app_url):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manifest = _manifest_path(tmp_path, tools=[
        {"name": "get_binary", "description": "Binary response.", "method": "GET", "path": "/api/binary", "params": {}},
    ])
    env = {**os.environ, "REST_BASE_URL": app_url, "REST_MANIFEST": manifest}
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_binary", {})
            text = result.content[0].text
            assert "binary response" in text
            assert "application/octet-stream" in text
