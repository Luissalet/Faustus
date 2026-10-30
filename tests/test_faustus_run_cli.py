"""scripts/faustus_run.py: a headless turn against a stand-in server."""
import io
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.server import ThreadingHTTPServer

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("faustus_run", Path(__file__).resolve().parents[1] / "scripts" / "faustus_run.py")
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)


def _sse(events):
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"


class _H(BaseHTTPRequestHandler):
    forms = []
    legs = []
    sessions = []
    gets = {}
    refuse = None
    puts = []

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        _H.puts.append((self.path, body))
        data = json.dumps({"binding": {**body, "project_id": "p1"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        payload = _H.gets.get(self.path.split("?")[0])
        if payload is None:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path == "/api/chat_stream" and _H.refuse:
            data = json.dumps({"detail": _H.refuse}).encode()
            self.send_response(400); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if self.path == "/api/session":
            _H.sessions.append(dict(urllib.parse.parse_qsl(body)))
            out, ctype = json.dumps({"id": "s1"}), "application/json"
        else:
            form = dict(urllib.parse.parse_qsl(body))
            _H.forms.append(form)
            out, ctype = _sse(_H.legs.pop(0)), "text/event-stream"
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(legs, gets=None, refuse=None):
    _H.forms, _H.legs, _H.sessions, _H.gets, _H.refuse = [], list(legs), [], dict(gets or {}), refuse
    _H.puts = []
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


CARD = {"type": "ask_user", "data": {"kind": "tool_approval", "approval_id": "A1"}}
LEG_DONE = [{"type": "tool_start", "tool": "edit_file", "round": 2},
            {"delta": "Fixed."},
            {"type": "usage", "data": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 80}}]


def test_json_run_with_auto_approval(monkeypatch, capsys):
    srv, url = _serve([[{"type": "tool_start", "tool": "read_file", "round": 1},
                        {"delta": "Allow this task to continue?"}, CARD], LEG_DONE])
    try:
        code = fr.main(["-p", "fix it", "--url", url, "--json", "--approve", "--workspace", "."])
    finally:
        srv.shutdown()
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines[0]["type"] == "run_start" and lines[-1]["type"] == "run_summary"
    s = lines[-1]
    assert s["stop"] == "answered" and s["text"] == "Fixed." and s["tools"] == ["read_file", "edit_file"]
    assert s["usage"]["cached_tokens"] == 80 and s["rounds"] == 2
    assert _H.forms[1]["tool_approval_id"] == "A1" and _H.forms[1]["tool_approval_decision"] == "approve_task"


def test_json_run_uses_utf8_even_with_windows_code_page(monkeypatch):
    srv, url = _serve([[{"delta": "A → B"}]])
    data = io.BytesIO()
    out = io.TextIOWrapper(data, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    try:
        code = fr.main(["-p", "test", "--url", url, "--json", "--model", "test-model"])
    finally:
        srv.shutdown()
    lines = [json.loads(line) for line in data.getvalue().decode("utf-8").splitlines()]
    assert code == 0
    assert lines[-1]["text"] == "A → B"


def test_stops_on_a_card_without_approve(capsys):
    srv, url = _serve([[CARD]])
    try:
        code = fr.main(["-p", "delete build", "--url", url])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == fr.EXIT_APPROVAL
    assert "approval A1" in captured.err


def test_prompt_from_file(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("¿Qué hay?", encoding="utf-8")
    assert fr.read_prompt("@" + str(p)) == "¿Qué hay?"


def test_a_run_without_model_opens_the_chat_on_the_default_model(capsys):
    gets = {"/api/auth/settings": {"default_model": "big-27b", "default_endpoint_id": "e2"},
            "/api/models": {"items": [
                {"endpoint_id": "e1", "url": "http://127.0.0.1:9/v1", "models": ["big-27b"], "model_type": "llm"},
                {"endpoint_id": "e2", "url": "http://127.0.0.1:8081/v1", "models": ["big-27b"], "model_type": "llm"}]}}
    srv, url = _serve([LEG_DONE], gets=gets)
    try:
        code = fr.main(["-p", "hola", "--url", url])
    finally:
        srv.shutdown()
    assert code == 0
    assert _H.sessions[0]["model"] == "big-27b"
    assert _H.sessions[0]["endpoint_url"] == "http://127.0.0.1:8081/v1"
    assert _H.forms[0]["model"] == "big-27b"


def test_unattended_marks_every_leg_of_the_turn():
    srv, url = _serve([LEG_DONE])
    try:
        code = fr.main(["-p", "hola", "--url", url, "--model", "m", "--unattended"])
    finally:
        srv.shutdown()
    assert code == 0
    assert _H.forms[0]["unattended"] == "true"


def test_a_refused_turn_says_why(capsys):
    srv, url = _serve([], refuse="No model selected for this chat.")
    try:
        code = fr.main(["-p", "hola", "--url", url, "--json"])
    finally:
        srv.shutdown()
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == fr.EXIT_ERROR
    assert "No model selected for this chat." in summary["error"]


def test_explicit_project_is_created_atomically_and_preferences_are_not_overridden(capsys):
    p={'id':'p1','workspace':'D:/project','enabled':True}
    srv,url=_serve([LEG_DONE],gets={'/api/projects/p1':p,'/api/projects/resolve/session/s1':{'project':p}})
    try:code=fr.main(['-p','do it','--url',url,'--project-id','p1','--json'])
    finally:srv.shutdown()
    assert code==0 and _H.sessions[0]['project_id']=='p1'
    assert not {'autonomy_preset','reasoning_effort','thinking_enabled','initial_response_timeout_s'} & _H.forms[0].keys()
    assert json.loads(capsys.readouterr().out.splitlines()[0])['project_id']=='p1'


def test_exact_unique_workspace_inherits_project(tmp_path,capsys):
    p={'id':'p1','workspace':str(tmp_path),'enabled':True}
    srv,url=_serve([LEG_DONE],gets={'/api/projects':[p],'/api/projects/resolve/session/s1':{'project':p}})
    try:code=fr.main(['-p','do it','--url',url,'--workspace',str(tmp_path)])
    finally:srv.shutdown()
    assert code==0 and _H.sessions[0]['project_id']=='p1'


def test_ambiguous_workspace_warns_and_continues_without_inventing_a_project(tmp_path,capsys):
    projects=[{'id':pid,'workspace':str(tmp_path),'enabled':True} for pid in ('p1','p2')]
    srv,url=_serve([LEG_DONE],gets={'/api/projects':projects})
    try:code=fr.main(['-p','do it','--url',url,'--workspace',str(tmp_path),'--json'])
    finally:srv.shutdown()
    assert code==0 and 'project_id' not in _H.sessions[0]
    captured=capsys.readouterr();assert 'Several projects' in captured.err
    assert json.loads(captured.out.splitlines()[0])['project_warnings']


def test_disabled_explicit_project_does_not_create_or_infer(capsys):
    srv,url=_serve([],gets={'/api/projects/p1':{'id':'p1','enabled':False}})
    try:code=fr.main(['-p','do it','--url',url,'--project-id','p1'])
    finally:srv.shutdown()
    assert code==fr.EXIT_ERROR and not _H.sessions and not _H.forms


def test_resumed_chat_is_never_moved_to_the_selected_project(capsys):
    srv,url=_serve([],gets={'/api/projects/resolve/session/old':{'project':{'id':'p2'}}})
    try:code=fr.main(['-p','continue','--url',url,'--session','old','--project-id','p1'])
    finally:srv.shutdown()
    assert code==fr.EXIT_ERROR and not _H.sessions and not _H.forms and not _H.puts


def test_resuming_same_project_preserves_identity_and_explicit_effort(capsys):
    p={'id':'p1'};binding={'actor':'faustus-reviewer'}
    srv,url=_serve([LEG_DONE],gets={'/api/projects/resolve/session/old':{'project':p},'/api/coordination/old':{'binding':binding}})
    try:code=fr.main(['-p','continue','--url',url,'--session','old','--project-id','p1','--agent-id','faustus-reviewer','--effort','low'])
    finally:srv.shutdown()
    assert code==0 and not _H.sessions and not _H.puts
    assert _H.forms[0]['reasoning_effort']=='low' and 'actor' not in _H.forms[0]


def test_new_explicit_actor_uses_native_owner_api_and_exact_saved_scope(capsys):
    p={'id':'p1'};template={'connector_id':'c1','operations':['poll','heartbeat'],'resources':[],'repos':['Fixture'],'thread_ids':[1],'active':True}
    srv,url=_serve([LEG_DONE],gets={'/api/projects/p1':p,'/api/projects/resolve/session/s1':{'project':p},'/api/coordination/s1':{'binding':None,'template':template}})
    try:code=fr.main(['-p','do it','--url',url,'--project-id','p1','--agent-id','faustus-reviewer'])
    finally:srv.shutdown()
    assert code==0 and _H.puts==[('/api/coordination/s1',{**template,'actor':'faustus-reviewer','space_id':'ecosystem'})]
    assert not {'actor','agent_id','coordination_binding'} & _H.forms[0].keys()


def test_explicit_human_actor_is_rejected_before_creating_a_chat(capsys):
    srv,url=_serve([],gets={'/api/projects/p1':{'id':'p1'}})
    try:code=fr.main(['-p','do it','--url',url,'--project-id','p1','--agent-id','luis'])
    finally:srv.shutdown()
    assert code==fr.EXIT_ERROR and not _H.sessions and not _H.puts and not _H.forms


def test_resumed_automatic_actor_is_not_rebound(capsys):
    srv,url=_serve([],gets={'/api/projects/resolve/session/old':{'project':{'id':'p1'}},'/api/coordination/old':{'binding':None,'template':{'active':True}}})
    try:code=fr.main(['-p','continue','--url',url,'--session','old','--agent-id','faustus'])
    finally:srv.shutdown()
    assert code==fr.EXIT_ERROR and not _H.puts and not _H.forms


def test_mcp_servers_are_registered_before_the_turn(tmp_path):
    calls = []

    class _C:
        def get_json(self, path):
            assert path == "/api/mcp/servers"
            return []

        def post_form(self, path, form, timeout=30):
            calls.append((path, form))
            return {"id": "x"}

    spec = tmp_path / "mcp.json"
    spec.write_text(json.dumps([
        {"name": "shop", "transport": "streamable-http", "url": "http://svc:8000/mcp"},
        {"name": "local", "transport": "stdio", "command": "tool", "args": ["--x", 1]},
        {"transport": "sse", "url": "http://nameless"},
    ]), encoding="utf-8")
    assert fr.register_mcp_servers(_C(), "@" + str(spec)) == ["shop", "local"]
    assert calls[0] == ("/api/mcp/servers", {"name": "shop", "transport": "http", "url": "http://svc:8000/mcp", "args": "[]"})
    assert calls[1][1]["command"] == "tool" and json.loads(calls[1][1]["args"]) == ["--x", "1"]
    assert fr.register_mcp_servers(_C(), "") == []

def test_input_budget_is_sent_through_real_http_and_approval_legs(monkeypatch, capsys):
    monkeypatch.delenv("FAUSTUS_RUN_INPUT_TOKEN_BUDGET", raising=False)
    srv, url = _serve([[CARD], LEG_DONE])
    try:
        code = fr.main(["-p", "readonly fixture", "--url", url, "--session", "owned",
                        "--model", "fixture", "--json", "--approve",
                        "--input-token-budget", "48000"])
    finally:
        srv.shutdown(); srv.server_close()
    assert code == 0 and len(_H.forms) == 2
    assert all(form["input_token_budget"] == "48000" for form in _H.forms)
    assert _H.forms[1]["tool_approval_id"] == "A1"
    assert not _H.sessions and not _H.puts
    assert json.loads(capsys.readouterr().out.splitlines()[0])["input_token_budget"] == 48000


def test_input_budget_environment_precedence_and_omission(monkeypatch, capsys):
    for environment, flag, expected in (("48000", [], "48000"),
                                         ("invalid", ["--input-token-budget", "64000"], "64000"),
                                         ("", [], None)):
        monkeypatch.setenv("FAUSTUS_RUN_INPUT_TOKEN_BUDGET", environment)
        srv, url = _serve([LEG_DONE])
        try:
            code = fr.main(["-p", "readonly", "--url", url, "--session", "owned",
                            "--model", "fixture", *flag])
        finally:
            srv.shutdown(); srv.server_close()
        assert code == 0 and _H.forms[0].get("input_token_budget") == expected
        assert not _H.sessions and not _H.puts
    capsys.readouterr()


def test_invalid_input_budget_rejects_before_client_creation(monkeypatch):
    import pytest
    def no_client(*args, **kwargs):
        raise AssertionError("invalid input budget must fail before any client side effect")
    monkeypatch.setattr(fr, "Client", no_client)
    for value in ("4095", "200001", "0", "-1", "word", "48000.0", "+48000"):
        monkeypatch.setenv("FAUSTUS_RUN_INPUT_TOKEN_BUDGET", value)
        with pytest.raises(SystemExit) as exc:
            fr.main(["-p", "readonly"])
        assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        fr.main(["-p", "readonly", "--input-token-budget", ""])
    assert exc.value.code == 2


class _AndroidCatalogue:
    def __init__(self, servers=None):
        self.servers = list(servers or [])
        self.posts = []

    def get_json(self, path):
        assert path == "/api/mcp/servers"
        return json.loads(json.dumps(self.servers))

    def post_form(self, path, form, timeout=30):
        assert path == "/api/mcp/servers"
        self.posts.append(dict(form))
        row = {**form, "args": json.loads(form["args"]), "id": "android-owned", "status": "connected", "env": {}, "is_enabled": True}
        self.servers.append(row)
        return row


ANDROID_SPEC = [{"name": "Android QA", "transport": "stdio", "command": "npx", "args": ["-y", "mobile-fixture@1"]}]
ANDROID_CONNECTED = {**ANDROID_SPEC[0], "id": "existing", "status": "connected", "env": "{}", "is_enabled": True, "inherit_env": False}


def test_android_registration_retry_reuses_exact_healthy_connection():
    client = _AndroidCatalogue()
    for _ in range(2):
        assert fr.register_mcp_servers(client, json.dumps(ANDROID_SPEC)) == ["Android QA"]
    assert len(client.posts) == 1
    assert len(client.servers) == 1


def test_android_registration_does_not_overwrite_a_same_name_different_service():
    import pytest
    client = _AndroidCatalogue([{**ANDROID_CONNECTED, "command": "another-service"}])
    with pytest.raises(RuntimeError):
        fr.register_mcp_servers(client, json.dumps(ANDROID_SPEC))
    assert client.posts == []


def test_android_registration_refuses_ambiguous_duplicate_connections():
    import pytest
    client = _AndroidCatalogue([ANDROID_CONNECTED, {**ANDROID_CONNECTED, "id": "other"}])
    with pytest.raises(RuntimeError):
        fr.register_mcp_servers(client, json.dumps(ANDROID_SPEC))
    assert client.posts == []


def test_android_registration_does_not_treat_disabled_or_pending_servers_as_healthy():
    import pytest
    for changed in [{"status": "disconnected"}, {"is_enabled": False}, {"manifest_pending_approval": True}, {"security_scan_pending_approval": True}, {"has_oauth": True}, {"env": {"SERVICE_SETTING": "different"}}, {"inherit_env": True}]:
        client = _AndroidCatalogue([{**ANDROID_CONNECTED, **changed}])
        with pytest.raises(RuntimeError):
            fr.register_mcp_servers(client, json.dumps(ANDROID_SPEC))
        assert client.posts == []


def test_nondefault_project_space_is_copied_into_new_actor_binding(capsys):
    p={"id":"p1"};template={"connector_id":"c1","space_id":"hoarddex","operations":["poll"],"resources":["app:emulator-5554"],"repos":["fielddex"],"thread_ids":[1988],"active":True}
    srv,url=_serve([LEG_DONE],gets={"/api/projects/p1":p,"/api/projects/resolve/session/s1":{"project":p},"/api/coordination/s1":{"binding":None,"template":template}})
    try: code=fr.main(["-p","Android smoke","--url",url,"--project-id","p1","--agent-id","faustus"])
    finally: srv.shutdown()
    assert code==0
    assert _H.puts[0][1]["space_id"]=="hoarddex"
    assert not {"actor","space_id"} & _H.forms[0].keys()


def test_binding_confirmation_with_the_wrong_space_does_not_start_inference():
    import argparse
    import pytest
    template={"connector_id":"c1","space_id":"hoarddex","operations":["poll"],"resources":[],"repos":[],"thread_ids":[],"active":True}
    class C:
        def get_json(self, path): return {"binding":None,"template":template}
        def put_json(self, path, body): return {"binding":{**body,"project_id":"p1","space_id":"ecosystem"}}
    with pytest.raises(RuntimeError):
        fr.select_agent_identity(C(),argparse.Namespace(agent_id="faustus",session=None),"new-owned",{"id":"p1"})


def test_setup_failure_retains_the_created_session_for_reconciliation(capsys):
    p={"id":"p1"}
    srv,url=_serve([],gets={"/api/projects/p1":p,"/api/projects/resolve/session/s1":{"project":p},"/api/coordination/s1":{"binding":None,"template":None}})
    try: code=fr.main(["-p","Android smoke","--url",url,"--project-id","p1","--agent-id","faustus","--json"])
    finally: srv.shutdown()
    summary=json.loads(capsys.readouterr().out.strip())
    assert code==fr.EXIT_ERROR and summary["stop"]=="error"
    assert summary["session"]=="s1"
    assert not _H.forms


def test_whole_registration_batch_is_validated_before_any_create():
    import pytest
    client=_AndroidCatalogue([ANDROID_CONNECTED])
    batch=[{"name":"New first service","transport":"stdio","command":"new-service","args":[]},{**ANDROID_SPEC[0],"command":"conflicting-service"}]
    with pytest.raises(RuntimeError):
        fr.register_mcp_servers(client,json.dumps(batch))
    assert client.posts==[]
    assert client.servers==[ANDROID_CONNECTED]


def test_same_batch_duplicate_names_are_checked_before_any_create():
    import pytest
    for second in [ANDROID_SPEC[0],{**ANDROID_SPEC[0],"command":"conflicting-service"}]:
        client=_AndroidCatalogue()
        if second==ANDROID_SPEC[0]:
            assert fr.register_mcp_servers(client,json.dumps([ANDROID_SPEC[0],second]))==["Android QA","Android QA"]
            assert len(client.posts)==1
        else:
            with pytest.raises(RuntimeError):
                fr.register_mcp_servers(client,json.dumps([ANDROID_SPEC[0],second]))
            assert client.posts==[]


# --------------------------------------------------------------------------- #
# The run deadline: a live stream (data, heartbeats, or silence) must not keep
# the CLI alive past --timeout, and expiry must cancel only the run this CLI
# was actually observing, by its own X-Odysseus-Run-Id.
#
# `_Live` answers on a thread per connection (the stop request arrives while
# the stream handler is still writing) and writes each frame as it is
# produced: HTTP/1.0 without a Content-Length, so a read returns as soon as a
# line arrives, exactly like the app's stream.
# --------------------------------------------------------------------------- #
class _Live(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    #: one entry per /api/chat_stream request: a list of frames that then ends
    #: with [DONE], or None for a stream that keeps sending heartbeats.
    script = []
    #: the run id handed to each chat_stream caller, in order ("" = no header).
    run_ids = ["run-live"]
    interval = 0.02
    #: a busy stream ends on its own here even if nothing abandons it, so a
    #: regression fails an assertion instead of hanging the suite.
    busy_seconds = 4.0
    stop_response = {"stopped": True}
    stops = []
    forms = []
    streams = 0
    closed = threading.Event()

    def log_message(self, *a):
        pass

    def _answer(self, status, data, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        # No project/coordination catalogue here: the CLI continues without
        # inheriting one, which is the "owned chat, no project" shape.
        self._answer(404, b'{"detail": "not found"}')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path.startswith("/api/chat/stop/"):
            _Live.stops.append({"path": self.path, "run_id": self.headers.get("X-Odysseus-Run-Id"),
                               "body": body})
            self._answer(200, json.dumps(_Live.stop_response).encode())
            return
        if self.path == "/api/session":
            self._answer(200, b'{"id": "s1"}')
            return
        _Live.forms.append(dict(urllib.parse.parse_qsl(body)))
        index = _Live.streams
        _Live.streams += 1
        run_id = _Live.run_ids[min(index, len(_Live.run_ids) - 1)] if _Live.run_ids else ""
        frames = _Live.script[index] if index < len(_Live.script) else None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if run_id:
            self.send_header("X-Odysseus-Run-Id", run_id)
        self.end_headers()
        self.close_connection = True
        try:
            if frames is not None:
                self.wfile.write(_sse(frames).encode())
                self.wfile.flush()
                return
            until = time.monotonic() + _Live.busy_seconds
            while not _Live.closed.is_set() and time.monotonic() < until:
                self.wfile.write(b'data: {"delta": "."}\n\n')
                self.wfile.flush()
                time.sleep(_Live.interval)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:  # the CLI went away first (the expected path)
            pass


def _serve_live(script, run_ids=("run-live",), stop_response=None):
    _Live.script, _Live.run_ids, _Live.stop_response = list(script), list(run_ids), stop_response or {"stopped": True}
    _Live.stops, _Live.forms, _Live.streams = [], [], 0
    _Live.closed = threading.Event()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Live)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _close_live(srv):
    _Live.closed.set()
    srv.shutdown()
    srv.server_close()


def _summary(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_busy_stream_cannot_outlive_the_run_budget_and_cancels_its_own_run(capsys):
    srv, url = _serve_live([None], run_ids=["run-busy"])
    started = time.monotonic()
    try:
        code = fr.main(["-p", "keep working", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--timeout", "0.4"])
    finally:
        _close_live(srv)
    assert code == fr.EXIT_TIMEOUT
    assert time.monotonic() - started < 3
    summary = _summary(capsys)
    assert summary["stop"] == "timeout" and summary["session"] == "owned"
    assert summary["elapsed_s"] < 3
    assert [stop["path"] for stop in _Live.stops] == ["/api/chat/stop/owned"]
    assert _Live.stops[0]["run_id"] == "run-busy"
    assert "scope" not in (_Live.stops[0]["body"] or "{}")
    assert summary["cancel"] == {"requested": "run-busy", "stopped": True, "reason": "cancelled"}


def test_a_silent_stream_is_also_bounded_and_cancels_the_observed_run(capsys):
    srv, url = _serve_live([None], run_ids=["run-silent"], stop_response={"stopped": True})
    started = time.monotonic()
    try:
        code = fr.main(["-p", "wait", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--timeout", "0.4"])
    finally:
        _close_live(srv)
    assert code == fr.EXIT_TIMEOUT and time.monotonic() - started < 3
    summary = _summary(capsys)
    assert summary["stop"] == "timeout"
    assert _Live.stops and _Live.stops[0]["run_id"] == "run-silent"
    assert summary["cancel"]["stopped"] is True


def test_no_run_identity_means_no_stop_and_an_uncertain_report(capsys):
    srv, url = _serve_live([None], run_ids=[""])
    try:
        code = fr.main(["-p", "keep working", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--timeout", "0.3"])
    finally:
        _close_live(srv)
    assert code == fr.EXIT_TIMEOUT
    summary = _summary(capsys)
    assert summary["stop"] == "timeout"
    assert _Live.stops == []
    assert summary["cancel"] == {"requested": None, "stopped": False, "reason": "run_id_unknown"}


def test_a_refused_stale_run_id_is_reported_and_never_claimed_stopped(capsys):
    srv, url = _serve_live([None], run_ids=["run-stale"],
                           stop_response={"stopped": False, "reason": "run_id_mismatch"})
    try:
        code = fr.main(["-p", "keep working", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--timeout", "0.3"])
    finally:
        _close_live(srv)
    assert code == fr.EXIT_TIMEOUT
    summary = _summary(capsys)
    assert _Live.stops[0]["run_id"] == "run-stale"
    assert summary["cancel"] == {"requested": "run-stale", "stopped": False, "reason": "run_id_mismatch"}


def test_a_normal_answer_never_asks_for_a_stop(capsys):
    srv, url = _serve_live([[{"type": "tool_start", "tool": "read_file", "round": 1}, {"delta": "Done."}]],
                           run_ids=["run-done"])
    try:
        code = fr.main(["-p", "read it", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--timeout", "5"])
    finally:
        _close_live(srv)
    assert code == 0
    summary = _summary(capsys)
    assert summary["stop"] == "answered" and summary["text"] == "Done."
    assert summary["cancel"] is None
    assert _Live.stops == []


def test_an_approval_resume_leg_is_cancelled_by_its_own_run_id(capsys):
    srv, url = _serve_live([[CARD], None], run_ids=["run-leg1", "run-leg2"])
    try:
        code = fr.main(["-p", "fix it", "--url", url, "--session", "owned", "--model", "fixture",
                        "--json", "--approve", "--timeout", "0.6"])
    finally:
        _close_live(srv)
    assert code == fr.EXIT_TIMEOUT
    summary = _summary(capsys)
    assert len(_Live.forms) == 2 and _Live.forms[1]["tool_approval_id"] == "A1"
    assert summary["session"] == "owned" and summary["stop"] == "timeout"
    assert [stop["run_id"] for stop in _Live.stops] == ["run-leg2"]
    assert summary["cancel"] == {"requested": "run-leg2", "stopped": True, "reason": "cancelled"}
