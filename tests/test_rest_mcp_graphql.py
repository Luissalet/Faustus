"""The REST -> MCP bridge also adapts a GraphQL endpoint (introspection -> one tool per first-level field)."""
import asyncio
import http.server
import json
import threading

import pytest

from bridges.rest_mcp import server as bridge


def T(kind, name=None, of=None):
    return {"kind": kind, "name": name, "ofType": of}


STRING = T("SCALAR", "String")
INT = T("SCALAR", "Int")
ID_REQ = T("NON_NULL", None, T("SCALAR", "ID"))

SCHEMA = {"data": {"__schema": {
    "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "user", "description": "One user by id.", "args": [
                {"name": "id", "type": ID_REQ, "defaultValue": None}], "type": T("OBJECT", "User")},
            {"name": "users", "description": "", "args": [
                {"name": "limit", "type": INT, "defaultValue": None},
                {"name": "role", "type": T("ENUM", "Role"), "defaultValue": None}],
             "type": T("NON_NULL", None, T("LIST", None, T("OBJECT", "User")))},
            {"name": "version", "description": "", "args": [], "type": STRING},
            {"name": "__typename", "args": [], "type": STRING}]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [
            {"name": "deleteUser", "description": "", "args": [{"name": "id", "type": ID_REQ, "defaultValue": None}],
             "type": T("OBJECT", "User")}]},
        {"kind": "OBJECT", "name": "User", "fields": [
            {"name": "id", "args": [], "type": ID_REQ},
            {"name": "name", "args": [], "type": STRING},
            {"name": "role", "args": [], "type": T("ENUM", "Role")},
            {"name": "friends", "args": [{"name": "first", "type": T("NON_NULL", None, INT), "defaultValue": None}],
             "type": T("LIST", None, T("OBJECT", "User"))},
            {"name": "team", "args": [], "type": T("OBJECT", "Team")}]},
        {"kind": "OBJECT", "name": "Team", "fields": [{"name": "title", "args": [], "type": STRING}]},
        {"kind": "ENUM", "name": "Role", "enumValues": [{"name": "ADMIN"}, {"name": "USER"}]},
    ]}}}


def test_one_read_only_tool_per_query_field_and_no_mutations_by_default():
    specs, notes = bridge.graphql_to_tools(SCHEMA, "/graphql")
    assert [s["name"] for s in specs] == ["query_user", "query_users", "query_version"]
    assert all(s["readonly"] for s in specs) and notes == []
    user = specs[0]
    assert user["description"] == "One user by id."
    assert user["params"]["id"]["required"] is True and user["params"]["id"]["gql_type"] == "ID!"
    assert specs[1]["params"]["limit"]["type"] == "integer" and specs[1]["params"]["limit"]["required"] is False


def test_mutations_only_when_asked_and_not_read_only():
    specs, _ = bridge.graphql_to_tools(SCHEMA, "/graphql", include_mutations=True)
    mut = [s for s in specs if s["name"].startswith("mutation_")]
    assert [s["name"] for s in mut] == ["mutation_deleteUser"] and mut[0]["readonly"] is False


def test_selection_set_comes_from_the_schema():
    specs, _ = bridge.graphql_to_tools(SCHEMA, "/graphql")
    # scalar and enum leaves, one nested level; `friends` needs a required argument so it is left out
    assert specs[0]["graphql"]["selection"] == "{ id name role team { title } }"
    assert specs[2]["graphql"]["selection"] == ""  # a scalar needs none


def test_request_declares_only_the_variables_given():
    specs, _ = bridge.graphql_to_tools(SCHEMA, "/graphql")
    users = specs[1]
    body = bridge.graphql_request(users, {"limit": 3, "role": None})
    assert body["variables"] == {"limit": 3}
    assert body["query"] == "query Faustus($limit: Int) { users(limit: $limit) { id name role team { title } } }"
    assert bridge.graphql_request(specs[2], {})["query"] == "query Faustus { version }"


class _Gql(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if "IntrospectionQuery" in body["query"]:
            out = SCHEMA
        else:
            _Gql.seen.append(body)
            out = {"data": {"user": {"id": "7", "name": "Ada", "role": "ADMIN", "team": {"title": "Core"}}}}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def endpoint():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Gql)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Gql.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_server_exposes_graphql_tools_and_calls_the_endpoint(endpoint):
    mcp = bridge.build_server({"REST_BASE_URL": endpoint, "REST_GRAPHQL_PATH": "/graphql"})
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"query_user", "query_users", "query_version", "app_status"} <= names
    assert not any(n.startswith("mutation_") for n in names)
    result = asyncio.run(mcp.call_tool("query_user", {"id": "7"}))
    text = json.dumps(result, default=str)
    assert "Ada" in text and "Core" in text
    assert _Gql.seen[-1]["variables"] == {"id": "7"}
    assert _Gql.seen[-1]["query"].startswith("query Faustus($id: ID!) { user(id: $id)")


def test_mutations_exposed_with_the_flag(endpoint):
    mcp = bridge.build_server({"REST_BASE_URL": endpoint, "REST_GRAPHQL_PATH": "/graphql",
                               "REST_GRAPHQL_MUTATIONS": "1"})
    assert "mutation_deleteUser" in {t.name for t in asyncio.run(mcp.list_tools())}


def test_saved_schema_file_works_without_the_endpoint(tmp_path):
    f = tmp_path / "schema.json"
    f.write_text(json.dumps(SCHEMA), encoding="utf-8")
    mcp = bridge.build_server({"REST_BASE_URL": "http://127.0.0.1:1", "REST_GRAPHQL_SCHEMA": str(f)})
    assert "query_user" in {t.name for t in asyncio.run(mcp.list_tools())}


def test_unreachable_endpoint_is_not_fatal():
    mcp = bridge.build_server({"REST_BASE_URL": "http://127.0.0.1:1", "REST_GRAPHQL_PATH": "/graphql"})
    assert {t.name for t in asyncio.run(mcp.list_tools())} == {"app_status"}
