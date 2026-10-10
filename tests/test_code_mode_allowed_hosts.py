"""#255: per-run ``allowed_hosts`` for Code Mode.

Three groups. Validation and the proxy itself need nothing but loopback; the
host-runtime group runs real guest processes against local HTTP servers; the
container group needs a Docker daemon with a Python image (skipped otherwise,
and a skip there is not evidence of the container boundary).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.code_mode import confined, net_allowlist
from src.code_mode.net_allowlist import AllowlistProxy, parse_allowed_hosts, parse_entry
from src.code_mode.runner import run_code_mode


# ── local servers ──────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = f"{self.server.label}|host={self.headers.get('Host')}|conn={self.headers.get('Connection')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(label: str, bind: str = "127.0.0.1"):
    server = ThreadingHTTPServer((bind, 0), _Handler)
    server.label = label
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers():
    a, b = _serve("A"), _serve("B")
    yield a.server_address[1], b.server_address[1]
    a.shutdown()
    b.shutdown()


async def _via_proxy(proxy_port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    data = b""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
            if not chunk:
                break
            data += chunk
    finally:
        writer.close()
    return data


def _get(url: str, host_header: str | None = None) -> bytes:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.netloc
    return (f"GET {url} HTTP/1.1\r\nHost: {host_header or host}\r\nProxy-Connection: keep-alive\r\n\r\n").encode()


# ── validation ─────────────────────────────────────────────────────────────

def test_absent_list_is_not_a_list():
    assert parse_allowed_hosts(None) == (None, [])


def test_empty_list_means_no_network():
    rules, errors = parse_allowed_hosts([])
    assert rules == [] and errors == []


def test_valid_entries_are_normalised_and_deduplicated():
    rules, errors = parse_allowed_hosts([" API.GitHub.com ", "pypi.org:443", "*.Example.org", "api.github.com",
                                         "203.0.113.7", "203.0.113.7:8080", "[2001:db8::1]:9000", "xn--bcher-kva.example"])
    assert errors == []
    assert [r.text for r in rules] == ["api.github.com", "pypi.org:443", "*.example.org", "203.0.113.7",
                                       "203.0.113.7:8080", "[2001:db8::1]:9000", "xn--bcher-kva.example"]


def test_idn_names_become_punycode():
    assert parse_entry("bücher.example").text == "xn--bcher-kva.example"


@pytest.mark.parametrize("bad", [
    "", "   ", "*", "*.com", "*.*.example.com", "ex*mple.com", "api.*.com", "example.*", "https://pypi.org",
    "pypi.org/simple", "pypi.org?x=1", "user@pypi.org", "pypi.org:0", "pypi.org:99999", "pypi.org:abc",
    "localhost", "api.localhost", "intranet", "1.2.3", "0x7f.1", "127.1", "[127.0.0.1]", "[::1", "a b.com",
    "-bad.example.com", "bad-.example.com", "under_score.example.com", "a..b.com", "example.com;rm", 42, None,
])
def test_invalid_entries_are_refused(bad):
    rules, errors = parse_allowed_hosts([bad])
    assert rules is None and len(errors) == 1


def test_string_form_is_split_and_non_list_refused():
    rules, errors = parse_allowed_hosts("pypi.org, api.github.com")
    assert [r.text for r in rules] == ["pypi.org", "api.github.com"]
    assert parse_allowed_hosts({"a": 1})[0] is None


def test_too_many_entries_refused():
    rules, errors = parse_allowed_hosts([f"h{i}.example.com" for i in range(net_allowlist.MAX_HOSTS + 1)])
    assert rules is None and "limit" in errors[0]


def test_wildcard_matches_subdomains_not_the_apex():
    rule = parse_entry("*.example.org")
    assert rule.matches("a.example.org", 443) and rule.matches("a.b.example.org", 80)
    assert not rule.matches("example.org", 443) and not rule.matches("evilexample.org", 443)


def test_port_restricts():
    rule = parse_entry("pypi.org:443")
    assert rule.matches("pypi.org", 443) and not rule.matches("pypi.org", 80)
    assert parse_entry("pypi.org").matches("pypi.org", 8443)


def test_approval_note_names_the_hosts_and_the_strength():
    absent = net_allowlist.approval_note(json.dumps({"code": "x"}))
    assert absent == ""
    listed = net_allowlist.approval_note(json.dumps({"code": "x", "allowed_hosts": ["pypi.org", "*.github.com"]}),
                                         "confined")
    assert "pypi.org, *.github.com" in listed and "hard boundary" in listed
    host = net_allowlist.approval_note(json.dumps({"code": "x", "allowed_hosts": ["pypi.org"]}), "host")
    assert "not a security boundary" in host
    assert "none" in net_allowlist.approval_note(json.dumps({"code": "x", "allowed_hosts": []}), "confined")
    assert "invalid" in net_allowlist.approval_note(json.dumps({"code": "x", "allowed_hosts": ["http://x"]}))


def test_approval_card_carries_the_note():
    from src.tool_approvals import PendingToolApproval

    def card(content, tool="run_code"):
        return PendingToolApproval(
            approval_id="a", owner="u", session_id="s", origin_run_id="r", tool_name=tool, content=content,
            workspace="", document_id="", document_version=None, document_digest="",
            external_untrusted_context_seen=False, effects=("execute_code",), result_integrity="x",
            digest="d" * 64, created_at=0.0, expires_at=1.0).public_payload(reason="Needs approval.")

    listed = card(json.dumps({"code": "print(1)", "allowed_hosts": ["pypi.org"]}))
    assert "Needs approval." in listed["description"] and "Network: only pypi.org" in listed["description"]
    assert json.loads(listed["action"]["content"])["allowed_hosts"] == ["pypi.org"]
    plain = card(json.dumps({"code": "print(1)"}))
    assert plain["description"] == "Needs approval."
    other = card(json.dumps({"code": "x", "allowed_hosts": ["pypi.org"]}), tool="bash")
    assert other["description"] == "Needs approval."


# ── the proxy ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allowed_ip_entry_is_reached_and_other_port_refused(servers):
    port_a, port_b = servers
    rules, _ = parse_allowed_hosts([f"127.0.0.1:{port_a}"])
    proxy = AllowlistProxy(rules)
    await proxy.start()
    try:
        ok = await _via_proxy(proxy.port, _get(f"http://127.0.0.1:{port_a}/"))
        assert ok.startswith(b"HTTP/1.1 200") and b"A|host=127.0.0.1:%d|conn=close" % port_a in ok
        denied = await _via_proxy(proxy.port, _get(f"http://127.0.0.1:{port_b}/"))
        assert denied.startswith(b"HTTP/1.1 403") and b"not in allowed_hosts" in denied
        stats = proxy.stats()
        assert stats["requests_allowed"] == 1 and stats["requests_denied"] == 1
        assert stats["denied_targets"] == [f"127.0.0.1:{port_b}"]
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_names_resolve_through_the_proxy_and_host_header_is_the_target(servers):
    port_a, port_b = servers

    async def resolver(host, port):
        return {"allowed.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}[host]

    rules, _ = parse_allowed_hosts(["allowed.test"])
    proxy = AllowlistProxy(rules, resolver=resolver, public_only=False)
    await proxy.start()
    try:
        ok = await _via_proxy(proxy.port, _get(f"http://allowed.test:{port_a}/x", host_header="evil.test"))
        assert b"A|host=allowed.test:%d" % port_a in ok          # the client's Host header is not trusted
        refused = await _via_proxy(proxy.port, _get(f"http://other.test:{port_b}/"))
        assert refused.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_a_name_resolving_to_a_private_address_is_refused(servers):
    port_a, _ = servers

    async def resolver(host, port):
        return ["127.0.0.1"]

    rules, _ = parse_allowed_hosts(["rebind.example.com"])
    proxy = AllowlistProxy(rules, resolver=resolver)            # public_only is the default
    await proxy.start()
    try:
        out = await _via_proxy(proxy.port, _get(f"http://rebind.example.com:{port_a}/"))
        assert out.startswith(b"HTTP/1.1 403") and b"loopback/private" in out
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_an_ip_url_is_not_allowed_by_a_name_entry(servers):
    port_a, _ = servers

    async def resolver(host, port):
        return ["127.0.0.1"]

    rules, _ = parse_allowed_hosts(["allowed.test"])
    proxy = AllowlistProxy(rules, resolver=resolver, public_only=False)
    await proxy.start()
    try:
        out = await _via_proxy(proxy.port, _get(f"http://127.0.0.1:{port_a}/"))
        assert out.startswith(b"HTTP/1.1 403")
        for spelling in ("2130706433", "0x7f.0.0.1", "[::ffff:7f00:1]"):
            out = await _via_proxy(proxy.port, _get(f"http://{spelling}:{port_a}/"))
            assert out.startswith(b"HTTP/1.1 4"), spelling
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_connect_tunnels_only_to_allowed_targets(servers):
    port_a, port_b = servers
    rules, _ = parse_allowed_hosts([f"127.0.0.1:{port_a}"])
    proxy = AllowlistProxy(rules)
    await proxy.start()
    try:
        ok = await _via_proxy(proxy.port, (f"CONNECT 127.0.0.1:{port_a} HTTP/1.1\r\nHost: 127.0.0.1:{port_a}\r\n\r\n"
                                           f"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode())
        assert ok.startswith(b"HTTP/1.1 200 Connection established") and b"A|host=x" in ok
        denied = await _via_proxy(proxy.port, f"CONNECT 127.0.0.1:{port_b} HTTP/1.1\r\n\r\n".encode())
        assert denied.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_keep_alive_cannot_carry_a_second_host(servers):
    port_a, port_b = servers
    rules, _ = parse_allowed_hosts([f"127.0.0.1:{port_a}"])
    proxy = AllowlistProxy(rules)
    await proxy.start()
    try:
        both = _get(f"http://127.0.0.1:{port_a}/") + _get(f"http://127.0.0.1:{port_b}/")
        out = await _via_proxy(proxy.port, both)
        assert b"A|host" in out and b"B|host" not in out
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_malformed_and_origin_form_requests_are_rejected():
    rules, _ = parse_allowed_hosts(["pypi.org"])
    proxy = AllowlistProxy(rules)
    await proxy.start()
    try:
        assert (await _via_proxy(proxy.port, b"GET / HTTP/1.1\r\nHost: pypi.org\r\n\r\n")).startswith(b"HTTP/1.1 400")
        assert (await _via_proxy(proxy.port, b"nonsense\r\n\r\n")).startswith(b"HTTP/1.1 400")
        assert (await _via_proxy(proxy.port, b"GET ftp://pypi.org/ HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 400")
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_empty_allowlist_refuses_everything(servers):
    port_a, _ = servers
    proxy = AllowlistProxy([])
    await proxy.start()
    try:
        out = await _via_proxy(proxy.port, _get(f"http://127.0.0.1:{port_a}/"))
        assert out.startswith(b"HTTP/1.1 403") and b"Allowed: nothing" in out
    finally:
        await proxy.stop()


# ── host runtime ───────────────────────────────────────────────────────────

_FETCH = ("import urllib.request, urllib.error\n"
          "def fetch(url):\n"
          "    try:\n"
          "        return 'GOT ' + urllib.request.urlopen(url, timeout=8).read().decode().split('|')[0]\n"
          "    except urllib.error.HTTPError as e:\n"
          "        return 'HTTP %d' % e.code\n"
          "    except Exception as e:\n"
          "        return 'ERR ' + type(e).__name__\n")


@pytest.mark.usefixtures("code_mode_host_runtime")
class TestHostRuntime:
    @pytest.mark.asyncio
    async def test_allowed_host_reachable_and_other_refused_with_report(self, servers):
        port_a, port_b = servers
        code = _FETCH + f"print(fetch('http://127.0.0.1:{port_a}/'))\nprint(fetch('http://127.0.0.1:{port_b}/'))\n"
        result = await run_code_mode(code, allowed_hosts=[f"127.0.0.1:{port_a}"])
        assert result["exit_code"] == 0, result
        assert result["output"].splitlines() == ["GOT A", "HTTP 403"]
        network = result["network"]
        assert network["enforcement"] == net_allowlist.KIND_HOST_PROXY
        assert network["hard_boundary"] is False and network["bypassable_by"]
        assert network["allowed_hosts"] == [f"127.0.0.1:{port_a}"]
        assert network["proxy"]["requests_allowed"] == 1 and network["proxy"]["requests_denied"] == 1
        assert network["proxy"]["denied_targets"] == [f"127.0.0.1:{port_b}"]
        guarantees = result["runtime_guarantees"]
        assert guarantees["network_scope"] == "allowlist" and guarantees["network_isolated"] is False
        assert guarantees["network_enforcement"] == net_allowlist.KIND_HOST_PROXY

    @pytest.mark.asyncio
    async def test_direct_sockets_name_lookups_and_children_are_refused(self, servers):
        port_a, _ = servers
        code = (
            "import socket, subprocess\n"
            "def attempt(label, fn):\n"
            "    try:\n"
            "        fn(); print(label, 'OPEN')\n"
            "    except PermissionError as e:\n"
            "        print(label, 'BLOCKED')\n"
            "    except Exception as e:\n"
            "        print(label, 'OTHER', type(e).__name__)\n"
            f"attempt('direct', lambda: socket.create_connection(('127.0.0.1', {port_a}), timeout=3))\n"
            "attempt('udp', lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('203.0.113.9', 53)))\n"
            "attempt('dns', lambda: socket.getaddrinfo('example.com', 80))\n"
            "attempt('child', lambda: subprocess.run(['python', '-c', 'print(1)']))\n"
        )
        result = await run_code_mode(code, allowed_hosts=[f"127.0.0.1:{port_a}"])
        assert result["output"].splitlines() == ["direct BLOCKED", "udp BLOCKED", "dns BLOCKED", "child BLOCKED"], result

    @pytest.mark.asyncio
    async def test_a_listener_the_code_made_itself_still_works(self):
        code = (
            "import socket\n"
            "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen()\n"
            "cli = socket.create_connection(srv.getsockname(), timeout=3)\n"
            "conn, _ = srv.accept(); cli.sendall(b'hi'); print(conn.recv(2).decode())\n"
        )
        result = await run_code_mode(code, allowed_hosts=["pypi.org"])
        assert result["exit_code"] == 0 and result["output"].strip() == "hi", result

    @pytest.mark.asyncio
    async def test_empty_list_blocks_even_the_proxyless_path(self, servers):
        port_a, _ = servers
        code = (_FETCH + f"print(fetch('http://127.0.0.1:{port_a}/'))\n"
                f"import socket\n"
                f"try:\n    socket.create_connection(('127.0.0.1', {port_a}), timeout=3); print('OPEN')\n"
                f"except PermissionError:\n    print('BLOCKED')\n")
        result = await run_code_mode(code, allowed_hosts=[])
        assert result["exit_code"] == 0, result
        assert result["output"].splitlines() == ["ERR URLError", "BLOCKED"]
        assert result["network"]["enforcement"] == net_allowlist.KIND_HOST_NONE
        assert result["network"]["allowed_hosts"] == [] and result["network"]["hard_boundary"] is False

    @pytest.mark.asyncio
    async def test_absent_list_is_exactly_todays_behaviour(self, servers):
        port_a, port_b = servers
        code = _FETCH + (f"import socket\n"
                         f"print(fetch('http://127.0.0.1:{port_a}/'))\n"
                         f"socket.create_connection(('127.0.0.1', {port_b}), timeout=3); print('direct ok')\n"
                         f"import subprocess; print(subprocess.run(['python','-c','print(7)'],capture_output=True,text=True).stdout.strip())\n")
        result = await run_code_mode(code)
        assert result["exit_code"] == 0, result
        assert result["output"].splitlines() == ["GOT A", "direct ok", "7"]
        assert "network" not in result
        assert result["runtime_guarantees"] == {
            "mode": "host_process", "filesystem_isolated": False,
            "network_isolated": False, "tool_policy_scope": "tools.call_only"}

    @pytest.mark.asyncio
    async def test_proxy_environment_is_only_set_for_a_list(self):
        code = "import os\nprint('P=%s|N=%r' % (os.environ.get('HTTP_PROXY'), os.environ.get('NO_PROXY')))\n"
        plain = await run_code_mode(code)
        assert plain["output"].strip() == "P=None|N=None"
        listed = await run_code_mode(code, allowed_hosts=["pypi.org"])
        assert listed["output"].strip().startswith("P=http://127.0.0.1:") and listed["output"].strip().endswith("|N=''")

    @pytest.mark.asyncio
    async def test_invalid_list_is_refused_and_nothing_runs(self, tmp_path):
        marker = tmp_path / "ran.txt"
        result = await run_code_mode(f"open({str(marker)!r}, 'w').write('x')", allowed_hosts=["https://pypi.org/"])
        assert result["exit_code"] == 1 and result["refused"] is True
        assert "allowed_hosts" in result["error"] and "NOT run" in result["error"]
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_proxy_is_stopped_after_the_run(self, servers):
        port_a, _ = servers
        seen = {}
        real = net_allowlist.start_host_egress

        async def spy(rules):
            egress = await real(rules)
            seen["proxy"] = egress.proxy
            return egress

        import src.code_mode.runner as runner_mod
        runner_mod.net_allowlist.start_host_egress = spy
        try:
            await run_code_mode("print(1)", allowed_hosts=[f"127.0.0.1:{port_a}"])
        finally:
            runner_mod.net_allowlist.start_host_egress = real
        proxy_port = seen["proxy"].port
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", proxy_port), timeout=2).close()

    @pytest.mark.asyncio
    async def test_tool_call_validates_and_forwards_the_list(self, monkeypatch, servers):
        port_a, port_b = servers
        from src.agent_tools.code_mode_tool import RunCodeTool
        monkeypatch.setattr("src.settings.get_setting",
                            lambda key, default=None: True if key == "agent_code_mode" else default)
        code = _FETCH + f"print(fetch('http://127.0.0.1:{port_a}/'))\nprint(fetch('http://127.0.0.1:{port_b}/'))\n"
        result = await RunCodeTool().execute(json.dumps({"code": code, "allowed_hosts": [f"127.0.0.1:{port_a}"]}), {})
        assert result["output"].splitlines() == ["GOT A", "HTTP 403"], result
        bad = await RunCodeTool().execute(json.dumps({"code": "print(1)", "allowed_hosts": "*"}), {})
        assert bad["exit_code"] == 1 and bad["refused"] is True


def test_wire_summary_is_the_small_form_the_transcript_reads():
    report = net_allowlist.build_report(
        net_allowlist.KIND_HOST_PROXY, parse_allowed_hosts(["pypi.org"])[0], hard=False,
        stats={"requests_allowed": 2, "requests_denied": 1, "allowed_targets": ["pypi.org:443"],
               "denied_targets": ["evil.example:443"]})
    assert net_allowlist.wire_summary(report) == {
        "allowed_hosts": ["pypi.org"], "enforcement": net_allowlist.KIND_HOST_PROXY, "hard_boundary": False,
        "requests_allowed": 2, "requests_denied": 1, "denied_targets": ["evil.example:443"]}
    none = net_allowlist.wire_summary(net_allowlist.build_report(net_allowlist.KIND_CONTAINER_NONE, [], hard=True))
    assert none["allowed_hosts"] == [] and none["hard_boundary"] is True and none["requests_allowed"] == 0
    assert net_allowlist.wire_summary({"anything": 1}) is None and net_allowlist.wire_summary("x") is None


# ── schema / MCP surface ───────────────────────────────────────────────────

def test_run_code_schema_declares_allowed_hosts():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    fn = next(e["function"] for e in FUNCTION_TOOL_SCHEMAS if e["function"]["name"] == "run_code")
    prop = fn["parameters"]["properties"]["allowed_hosts"]
    assert prop["type"] == "array" and prop["items"] == {"type": "string"}
    assert "allowed_hosts" in fn["description"] and "hard boundary" in fn["description"]
    assert fn["parameters"]["required"] == ["code"]


def test_execution_mcp_server_exposes_run_code_with_the_list():
    from mcp_servers import execution_server
    description, props, required = execution_server._TOOLS["run_code"]
    assert "allowed_hosts" in props and required == ["code"] and "allowed_hosts" in description


@pytest.mark.asyncio
async def test_execution_mcp_server_runs_code_with_the_list(monkeypatch, servers):
    port_a, port_b = servers
    from mcp_servers import execution_server
    from src.code_mode import confined as confined_mod
    real = confined_mod.resolve_runtime
    monkeypatch.setattr(confined_mod, "resolve_runtime", lambda explicit=None, default=None: real(explicit or "host"))
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: True if key == "agent_code_mode" else default)
    code = _FETCH + f"print(fetch('http://127.0.0.1:{port_a}/'))\nprint(fetch('http://127.0.0.1:{port_b}/'))\n"
    out = await execution_server.call_tool("run_code", {"code": code, "allowed_hosts": [f"127.0.0.1:{port_a}"]})
    payload = json.loads(out[0].text)
    assert payload["output"].splitlines() == ["GOT A", "HTTP 403"]
    assert payload["network"]["enforcement"] == net_allowlist.KIND_HOST_PROXY


# ── container plan (no Docker needed) ──────────────────────────────────────

def test_plan_with_egress_joins_the_internal_network_and_passes_only_proxy_variables(tmp_path):
    egress = confined.EgressProxy(network="faustus-codemode-net-x", proxy="faustus-codemode-egress-x", rules=[])
    plan = confined.build_plan(workspace=str(tmp_path), timeout_seconds=30, image="img:1", egress=egress,
                               network=True)  # even a network grant cannot override the proxy network
    argv = plan.argv
    assert argv[argv.index("--network") + 1] == "faustus-codemode-net-x"
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "HTTP_PROXY=http://egress:3128" in env and "HTTPS_PROXY=http://egress:3128" in env
    assert all(item.split("=")[0].upper().endswith("PROXY") for item in env)
    assert "--env-file" not in argv
    assert plan.guarantees["network_isolated"] is False and plan.guarantees["host_environment_passed"] is False


@pytest.mark.asyncio
async def test_confined_list_cannot_open_a_closed_network_unless_the_operator_allows_it(monkeypatch, tmp_path):
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {"ok": True})
    started = []

    async def fake_start(image, rules, docker="docker"):
        started.append(rules)
        return confined.refusal("stop here (test)")

    monkeypatch.setattr(confined, "start_egress", fake_start)
    refused = await run_code_mode("print(1)", runtime="confined", allowed_hosts=["pypi.org"], workspace=str(tmp_path))
    assert refused["refused"] is True and "agent_code_mode_host_lists" in refused["error"]
    assert "NOT run" in refused["error"] and started == []
    assert refused["network"]["enforcement"] == "not_executed"

    # the operator allows run_code to name its own hosts -> the proxy path is taken
    monkeypatch.setattr(confined, "_setting", lambda key, default=None: True if key == "agent_code_mode_host_lists" else default)
    await run_code_mode("print(1)", runtime="confined", allowed_hosts=["pypi.org"], workspace=str(tmp_path))
    assert len(started) == 1

    # ... or the network is already granted (then the list narrows it)
    started.clear()
    monkeypatch.setattr(confined, "_setting", lambda key, default=None: True if key == "agent_code_mode_network" else default)
    await run_code_mode("print(1)", runtime="confined", allowed_hosts=["pypi.org"], workspace=str(tmp_path))
    assert len(started) == 1


def test_plan_without_egress_is_unchanged(tmp_path):
    plan = confined.build_plan(workspace=str(tmp_path), timeout_seconds=30, image="img:1")
    assert "-e" not in plan.argv and plan.argv[plan.argv.index("--network") + 1] == "none"


def test_sidecar_command_fits_a_windows_command_line_and_round_trips():
    rules, _ = parse_allowed_hosts(["pypi.org", "*.github.com:443"])
    cmd = confined.sidecar_command(rules)
    assert sum(len(part) + 1 for part in cmd) < 30_000
    assert cmd[:3] == ["python3", "-I", "-c"] and cmd[4] == "--serve"
    import base64
    assert json.loads(base64.b64decode(cmd[6])) == ["pypi.org", "*.github.com:443"]


@pytest.mark.asyncio
async def test_confined_empty_list_overrides_a_network_grant(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {"ok": True})
    real_build = confined.build_plan

    def build(**kwargs):
        seen.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(confined, "build_plan", build)

    async def fake_impl(code, **kwargs):
        return {"output": "", "exit_code": 0, "runtime_guarantees": dict(kwargs["_confined"].guarantees)}

    import src.code_mode.runner as runner_mod
    monkeypatch.setattr(runner_mod, "_run_code_mode_impl", fake_impl)

    async def noop(self):
        return None

    monkeypatch.setattr(confined.ConfinedPlan, "cleanup", noop)
    result = await run_code_mode("print(1)", runtime="confined", network=True, allowed_hosts=[],
                                 workspace=str(tmp_path))
    assert seen["network"] is False and seen["egress"] is None
    assert result["network"]["enforcement"] == net_allowlist.KIND_CONTAINER_NONE
    assert result["network"]["hard_boundary"] is True
    assert result["runtime_guarantees"]["network_isolated"] is True
    assert result["runtime_guarantees"]["network_scope"] == "none"


@pytest.mark.asyncio
async def test_confined_proxy_start_failure_is_a_refusal_not_a_host_run(monkeypatch, tmp_path):
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {"ok": True})

    async def failing(image, rules, docker="docker"):
        return confined.refusal("the allowed_hosts filtering proxy could not start (test)")

    monkeypatch.setattr(confined, "start_egress", failing)
    result = await run_code_mode("print(1)", runtime="confined", allowed_hosts=["pypi.org"], workspace=str(tmp_path))
    assert result["refused"] is True and "NOT run" in result["error"]


# ── real containers ────────────────────────────────────────────────────────

@pytest.fixture
def own_container_namespace(monkeypatch):
    monkeypatch.setattr(confined, "_CONTAINER_PREFIX", f"faustus-codemode-test-{uuid.uuid4().hex[:12]}-")
    return confined._CONTAINER_PREFIX


def _docker_left(prefix: str) -> list[str]:
    names = subprocess.run(["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=30).stdout.split()
    nets = subprocess.run(["docker", "network", "ls", "--filter", f"name={prefix}", "--format", "{{.Name}}"],
                          capture_output=True, text=True, timeout=30).stdout.split()
    return names + nets


def _host_ip_seen_from_container(image: str) -> str:
    done = subprocess.run(["docker", "run", "--rm", "--pull", "never", image, "python3", "-c",
                           "import socket;print(socket.gethostbyname('host.docker.internal'))"],
                          capture_output=True, text=True, timeout=60)
    ip = done.stdout.strip()
    if done.returncode != 0 or not ip:
        done = subprocess.run(["docker", "network", "inspect", "bridge", "-f",
                               "{{(index .IPAM.Config 0).Gateway}}"], capture_output=True, text=True, timeout=30)
        ip = done.stdout.strip()
    return ip


@pytest.fixture
def host_servers():
    a, b = _serve("A", "0.0.0.0"), _serve("B", "0.0.0.0")
    yield a.server_address[1], b.server_address[1]
    a.shutdown()
    b.shutdown()


@pytest.mark.asyncio
async def test_container_run_reaches_only_the_allowed_host(own_container_namespace, container_test_image,
                                                           monkeypatch, host_servers, tmp_path):
    monkeypatch.setattr(confined, "_sandbox_image", lambda: container_test_image)
    monkeypatch.setattr(confined, "_setting", lambda key, default=None: True if key == "agent_code_mode_host_lists" else default)
    port_a, port_b = host_servers
    ip = _host_ip_seen_from_container(container_test_image)
    code = (_FETCH + f"print(fetch('http://{ip}:{port_a}/'))\nprint(fetch('http://{ip}:{port_b}/'))\n"
            "import socket\n"
            "def raw(addr):\n"
            "    try:\n"
            "        socket.create_connection(addr, timeout=4).close(); return 'OPEN'\n"
            "    except Exception as e:\n"
            "        return 'CLOSED ' + type(e).__name__\n"
            f"print('raw-allowed-host', raw(('{ip}', {port_a})))\n"
            "print('raw-internet', raw(('1.1.1.1', 443)))\n")
    result = await run_code_mode(code, runtime="confined", allowed_hosts=[f"{ip}:{port_a}"],
                                 workspace=str(tmp_path))
    assert result["exit_code"] == 0, result
    lines = result["output"].splitlines()
    assert lines[0] == "GOT A" and lines[1] == "HTTP 403", result
    assert lines[2].startswith("raw-allowed-host CLOSED") and lines[3].startswith("raw-internet CLOSED"), result
    network = result["network"]
    assert network["enforcement"] == net_allowlist.KIND_CONTAINER_PROXY and network["hard_boundary"] is True
    assert network["proxy"]["requests_allowed"] == 1 and network["proxy"]["requests_denied"] == 1
    assert network["proxy"]["denied_targets"] == [f"{ip}:{port_b}"]
    assert result["runtime_guarantees"]["network_scope"] == "allowlist"
    assert _docker_left(own_container_namespace) == []


@pytest.mark.asyncio
async def test_container_empty_list_has_no_network_and_absent_list_follows_the_setting(
        own_container_namespace, container_test_image, monkeypatch, host_servers, tmp_path):
    monkeypatch.setattr(confined, "_sandbox_image", lambda: container_test_image)
    port_a, _ = host_servers
    ip = _host_ip_seen_from_container(container_test_image)
    code = _FETCH + f"print(fetch('http://{ip}:{port_a}/'))\n"
    empty = await run_code_mode(code, runtime="confined", network=True, allowed_hosts=[], workspace=str(tmp_path))
    assert empty["output"].strip() == "ERR URLError", empty
    assert empty["network"]["enforcement"] == net_allowlist.KIND_CONTAINER_NONE
    granted = await run_code_mode(code, runtime="confined", network=True, workspace=str(tmp_path))
    assert granted["output"].strip() == "GOT A" and "network" not in granted, granted
    assert _docker_left(own_container_namespace) == []
