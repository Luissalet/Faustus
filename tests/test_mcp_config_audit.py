"""MCP configuration audit (src/mcp_config_audit.py, radar #503): each check on a
small server list, the masking of secrets, and the harness MCP tool."""
import asyncio
import json

import pytest

from src.mcp_config_audit import audit_servers, normalize_server

HOME = r"C:\Users\ana"


def _codes(report, server_id=None):
    return sorted(f["code"] for f in report["findings"] if server_id is None or f["server_id"] == server_id)


def _srv(i, **kw):
    base = {"id": i, "name": i, "transport": "stdio", "command": "node", "args": [], "env": {},
            "url": None, "is_enabled": True, "inherit_env": False}
    base.update(kw)
    return base


def test_a_clean_configuration_has_no_findings():
    rep = audit_servers([
        _srv("files", command="npx", args=["-y", "@modelcontextprotocol/server-filesystem@2025.8.21", r"D:\Proyectos\app"]),
        _srv("py", command="uvx", args=["mcp-server-git==0.6.2", "--repository", r"D:\Proyectos\app"]),
        _srv("remote", transport="sse", command=None, url="https://mcp.example.com/sse"),
        _srv("local", transport="sse", command=None, url="http://127.0.0.1:8810/mcp"),
        _srv("keyref", command="python", args=["server.py"], env={"GITHUB_TOKEN": "${GITHUB_TOKEN}", "MAX_TOKENS": "100000"}),
    ], home=HOME)
    assert rep["findings"] == [], rep["findings"]
    assert rep["servers"] == 5 and rep["with_findings"] == 0
    assert rep["counts"] == {"high": 0, "medium": 0, "low": 0}


def test_plain_text_secrets_in_env_args_and_url_are_found_and_masked():
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    rep = audit_servers([
        _srv("gh", env={"GITHUB_PERSONAL_ACCESS_TOKEN": secret}),
        _srv("cli", args=["serve", "--api-key", "sk-live-0123456789abcdef"]),
        _srv("web", transport="sse", command=None, url="https://x.example.com/mcp?token=abcdef123456"),
    ], home=HOME)
    assert set(_codes(rep, "gh")) == {"plaintext_secret"}
    assert "plaintext_secret" in _codes(rep, "cli")
    assert _codes(rep, "web") == ["plaintext_secret"]
    blob = json.dumps(rep)
    assert secret not in blob and "sk-live-0123456789abcdef" not in blob and "abcdef123456" not in blob
    assert all(f["severity"] == "high" for f in rep["findings"])


def test_placeholders_references_and_numbers_are_not_secrets():
    rep = audit_servers([_srv("p", env={"API_KEY": "your-api-key", "TOKEN": "%TOKEN%", "SECRET": "<secret>",
                                        "PASSWORD": "op://vault/item/pw", "TOKEN_LIMIT": "400000",
                                        "AUTHOR": "Ana Martínez García",
                                        # the Hoard servers name the file the token lives in (seen live 11-10)
                                        "LINKS_TOKEN_FILE": r"C:\Users\ana\AppData\Roaming\links\mcp-token",
                                        "SECRET_STORE": r"D:\vault\secrets.json"})], home=HOME)
    assert rep["findings"] == []


@pytest.mark.parametrize("command,args,flag", [
    ("npx", ["-y", "@modelcontextprotocol/server-filesystem"], True),
    ("npx.cmd", ["-y", "some-mcp@latest"], True),
    ("npx", ["-y", "some-mcp@1.4.0"], False),
    ("npx", ["-y", "@scope/pkg@2.0.1"], False),
    ("uvx", ["mcp-server-fetch"], True),
    ("uvx", ["--from", "mcp-server-fetch==0.6.3", "mcp-server-fetch"], False),
    ("pnpm", ["dlx", "thing-mcp"], True),
    ("pipx", ["run", "thing-mcp==1.0"], False),
    ("npx", ["-y", "./local/server.js"], False),
    ("node", ["server.js"], False),
])
def test_package_runners_without_a_fixed_version(command, args, flag):
    rep = audit_servers([_srv("s", command=command, args=args)], home=HOME)
    assert ("unpinned_package" in _codes(rep)) is flag


@pytest.mark.parametrize("arg,flag", [
    ("C:\\", True), ("D:", True), ("/", True), (HOME, True), (HOME + "\\", True), (r"C:\Users", True),
    (r"C:\Users\ana\Proyectos\app", False), (r"D:\LocalAI", False), ("--root=C:\\", True),
])
def test_whole_drives_and_the_profile_as_roots(arg, flag):
    rep = audit_servers([_srv("fs", args=["serve", arg])], home=HOME)
    assert ("broad_root" in _codes(rep)) is flag


def test_insecure_remote_url_but_not_loopback():
    rep = audit_servers([
        _srv("lan", transport="sse", command=None, url="http://192.168.1.20:9000/sse"),
        _srv("lo", transport="streamable_http", command=None, url="http://localhost:3000/mcp"),
    ], home=HOME)
    assert _codes(rep, "lan") == ["insecure_url"] and _codes(rep, "lo") == []


def test_inherited_environment_shell_wrapper_and_duplicates():
    rep = audit_servers([
        _srv("a", command="cmd", args=["/c", "npx", "-y", "x-mcp@1.0.0"], inherit_env=True),
        _srv("b", command="node", args=["srv.js"]),
        _srv("c", command="node.exe", args=["srv.js"], is_enabled=False),
    ], home=HOME)
    assert _codes(rep, "a") == ["inherits_env", "shell_wrapper"]
    dup = [f for f in rep["findings"] if f["code"] == "duplicate"]
    assert len(dup) == 1 and dup[0]["server_id"] == "c" and dup[0]["detail"] == "b" and dup[0]["enabled"] is False
    assert audit_servers([_srv("b", command="node", args=["srv.js"]),
                          _srv("c", command="node", args=["srv.js"], is_enabled=False)],
                         home=HOME, include_disabled=False)["findings"] == []


def test_one_bridge_serving_two_apps_through_env_is_not_a_duplicate():
    bridge = dict(command="python", args=[r"D:\LocalAI\odysseus/bridges/rest_mcp/server.py"])
    rep = audit_servers([_srv("plato", env={"REST_BASE_URL": "http://127.0.0.1:5000"}, **bridge),
                         _srv("gepetto", env={"REST_BASE_URL": "http://127.0.0.1:8767"}, **bridge),
                         _srv("plato2", env={"REST_BASE_URL": "http://127.0.0.1:5000"}, **bridge)], home=HOME)
    assert [(f["code"], f["server_id"], f["detail"]) for f in rep["findings"]] == [("duplicate", "plato2", "plato")]


def test_findings_are_sorted_by_severity_and_summarised_per_server():
    rep = audit_servers([
        _srv("low", command="bash", args=["-c", "node srv.js"]),
        _srv("high", env={"API_KEY": "abcdef0123456789"}),
    ], home=HOME)
    assert [f["severity"] for f in rep["findings"]] == ["high", "low"]
    per = {p["id"]: p for p in rep["per_server"]}
    assert per["high"]["worst"] == "high" and per["low"]["worst"] == "low"


def test_db_rows_with_json_text_columns_are_normalized():
    class Row:
        id, name, transport, command = "r", "Row", "stdio", "npx"
        args = json.dumps(["-y", "pkg"])
        env = "not json"
        url, is_enabled, inherit_env = None, True, None

    n = normalize_server(Row())
    assert n["args"] == ["-y", "pkg"] and n["env"] == {}
    assert _codes(audit_servers([Row()], home=HOME)) == ["unpinned_package"]


def test_harness_tool_audits_a_supplied_list_and_rejects_bad_input():
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv

    def payload(args):
        return json.loads(asyncio.run(srv.call_tool("mcp_config_audit", args))[0].text)

    out = payload({"servers": [_srv("x", command="npx", args=["-y", "pkg"])]})
    assert out["configuration"] == "supplied" and _codes(out) == ["unpinned_package"]
    assert "must be a list" in payload({"servers": "nope"})["error"]
