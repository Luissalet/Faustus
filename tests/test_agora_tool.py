"""The Agora for a Faustus conversation (src/agent_tools/agora_tool.py): the hub's own Agora tools, reached the way the
page reaches the hub, split into what only looks and what acts, always speaking as Faustus."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.agent_tools import agora_tool
from src.agent_tools.agora_tool import AgoraTool


def _tool(tool, /, read=False, **props):
    return {"name": "hub_agora_" + tool, "description": f"Does {tool}. More about it.", "annotations": {"readOnlyHint": read},
            "inputSchema": {"type": "object", "properties": {"agent": {"type": "string"}, "space_id": {"type": "string"}, **props},
                            "required": ["agent"]}}


CATALOGUE = [
    _tool("servers", read=True),
    _tool("inbox", read=True, peek={"type": "boolean"}, wait_s={"type": "number"}),
    _tool("channel_read", channel={"type": "string", "description": "Channel name."}, peek={"type": "boolean"},
          limit={"type": "integer"}),
    _tool("channel_post", channel={"type": "string"}, body={"type": "string"}, kind={"type": "string", "enum": ["comment", "agree"]}),
    _tool("member_add", id={"type": "string"}),
    _tool("file_add", name={"type": "string"}, content_base64={"type": "string"}),
    _tool("thread", read=True, thread_id={"type": "integer"}, peek={"type": "boolean"}),
    _tool("task", read=True, task_id={"type": "integer"}),            # as a hub that cannot peek here would offer it
    {"name": "hub_agora_pulse", "description": "The change token.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"since": {"type": "string"}}}},
    {"name": "hub_apps_list", "description": "Not the Agora.", "inputSchema": {"type": "object", "properties": {}}},
]


@pytest.fixture
def hub(monkeypatch):
    calls = []
    answers = {}

    def fake(method, path, body=None, timeout=45.0):
        calls.append({"method": method, "path": path, "body": body})
        if path == "/api/agent/tools":
            return answers.get("tools", (200, {"tools": CATALOGUE}))
        return answers.get("call", (200, {"ok": True, "tool": body["tool"], "result": {"ok": True, "echo": body["arguments"]}}))

    monkeypatch.setattr(agora_tool, "_hub", fake)
    monkeypatch.setattr(agora_tool, "_cache", {"at": 0.0, "url": "", "tools": {}})
    return calls, answers


def run(tool, args, ctx=None):
    return asyncio.run(tool.execute(json.dumps(args), ctx or {}))


def test_help_lists_what_each_tool_does_as_the_hub_offers_it(hub):
    read = run(AgoraTool(write=False), {"action": "help"})
    assert read["exit_code"] == 0
    listed = [line.split(":")[0][2:] for line in read["output"].splitlines()[1:]]
    assert listed == ["servers", "inbox", "channel_read", "thread", "task", "pulse"]   # a channel is read as a peek; nothing outside the Agora
    assert "- servers: Does servers." in read["output"] and "you are faustus" in read["output"]
    write = run(AgoraTool(write=True), {"action": "help"})
    assert [line.split(":")[0][2:] for line in write["output"].splitlines()[1:]] == ["channel_post", "member_add", "file_add"]

    about = run(AgoraTool(write=True), {"action": "help", "args": {"about": "channel_post"}})["output"]
    assert "- body: string" in about and "- kind: string (comment, agree)" in about
    assert "agent" not in about.split("args:")[1]                            # who speaks is not the model's to say
    assert run(AgoraTool(write=False), {"action": "help", "args": {"about": "channel_post"}})["error"] == "channel_post is an action of agora_write."


def test_an_action_goes_to_the_hub_under_its_name_as_faustus(hub):
    calls, _ = hub
    out = run(AgoraTool(write=True), {"action": "channel_post", "args": {"channel": "general", "body": "hola", "agent": "luis"}})
    assert out["exit_code"] == 0 and out["untrusted_content"] is True
    sent = calls[-1]
    assert sent["path"] == "/api/agent/call" and sent["body"]["tool"] == "hub_agora_channel_post"
    assert sent["body"]["arguments"] == {"channel": "general", "body": "hola", "agent": "faustus"}

    # arguments beside the action, and the hub's full name, are understood
    run(AgoraTool(write=True), {"action": "hub_agora_member_add", "id": "codex-docs", "space_id": "atlas"})
    assert calls[-1]["body"] == {"tool": "hub_agora_member_add", "arguments": {"id": "codex-docs", "space_id": "atlas", "agent": "faustus"}}

    # a tool with no agent argument is not given one
    run(AgoraTool(write=False), {"action": "pulse", "args": {"since": "1-2", "agent": "x"}})
    assert calls[-1]["body"]["arguments"] == {"since": "1-2"}


def test_reading_never_moves_a_read_mark_nor_waits_long(hub):
    calls, _ = hub
    run(AgoraTool(write=False), {"action": "channel_read", "args": {"channel": "general", "peek": False}})
    assert calls[-1]["body"]["arguments"] == {"channel": "general", "peek": True, "agent": "faustus"}
    run(AgoraTool(write=False), {"action": "inbox", "args": {"wait_s": 120}})
    assert calls[-1]["body"]["arguments"] == {"wait_s": 20, "peek": True, "agent": "faustus"}
    # a thread and a task count as read by whoever reads them: as a peek where the hub has one, as nobody where not
    run(AgoraTool(write=False), {"action": "thread", "args": {"thread_id": 5}})
    assert calls[-1]["body"]["arguments"] == {"thread_id": 5, "agent": "faustus", "peek": True}
    run(AgoraTool(write=False), {"action": "task", "args": {"task_id": 3}})
    assert calls[-1]["body"]["arguments"] == {"task_id": 3}


def test_a_file_is_sent_as_its_bytes_never_as_a_path_on_the_hubs_machine(hub):
    calls, _ = hub
    run(AgoraTool(write=True), {"action": "file_add", "args": {"name": "a.txt", "content_base64": "aG9sYQ==", "path": "C:/secreto.txt"}})
    assert calls[-1]["body"]["arguments"] == {"name": "a.txt", "content_base64": "aG9sYQ==", "agent": "faustus"}


def test_the_chat_of_a_bound_project_speaks_as_its_actor_in_its_space(hub):
    calls, _ = hub
    ctx = {"security_context": SimpleNamespace(coordination_grant=SimpleNamespace(binding=SimpleNamespace(actor="faustus-atlas", space_id="atlas")))}
    run(AgoraTool(write=True), {"action": "channel_post", "args": {"body": "hola"}}, ctx)
    assert calls[-1]["body"]["arguments"] == {"body": "hola", "agent": "faustus-atlas", "space_id": "atlas"}
    run(AgoraTool(write=True), {"action": "channel_post", "args": {"body": "hola", "space_id": "vulcan"}}, ctx)
    assert calls[-1]["body"]["arguments"]["space_id"] == "vulcan"


def test_what_is_not_there_is_said_plainly(hub):
    calls, answers = hub
    assert run(AgoraTool(write=False), {"action": "channel_post", "args": {}})["error"] == "channel_post is an action of agora_write."
    unknown = run(AgoraTool(write=True), {"action": "chanel_post"})
    assert unknown["exit_code"] == 1 and "Close: channel_post" in unknown["error"]
    assert all(call["path"] == "/api/agent/tools" for call in calls)         # nothing was called on the hub

    read_only = run(AgoraTool(write=True), {"action": "channel_post", "args": {"body": "x"}}, {"tool_policy": SimpleNamespace(read_only=True)})
    assert read_only["blocked"] is True and calls[-1]["path"] == "/api/agent/tools"

    answers["call"] = (400, {"ok": False, "tool": "hub_agora_channel_post", "result": {"ok": False, "error": "say something"}})
    assert run(AgoraTool(write=True), {"action": "channel_post"})["error"] == "channel_post: say something"
    answers["call"] = (None, None)
    lost = run(AgoraTool(write=True), {"action": "channel_post", "args": {"body": "x"}})
    assert lost["exit_code"] == 1 and lost["outcome_unknown"] is True
    assert "outcome_unknown" not in run(AgoraTool(write=False), {"action": "servers"})

    assert run(AgoraTool(write=False), "not json")["exit_code"] == 1
    assert run(AgoraTool(write=False), [1])["exit_code"] == 1


def test_a_hub_that_is_off_is_an_answer_and_the_catalogue_is_asked_once(hub, monkeypatch):
    calls, answers = hub
    answers["tools"] = (None, None)
    off = run(AgoraTool(write=False), {"action": "servers"})
    assert off["exit_code"] == 1 and "not answering" in off["error"]
    answers["tools"] = (500, None)
    assert "HTTP 500" in run(AgoraTool(write=False), {"action": "servers"})["error"]
    answers.pop("tools")
    for _ in range(3):
        assert run(AgoraTool(write=False), {"action": "servers"})["exit_code"] == 0
    assert sum(call["path"] == "/api/agent/tools" for call in calls) == 3     # two failures, then once for the three calls


def test_a_long_answer_is_cut_and_says_so(hub):
    _, answers = hub
    answers["call"] = (200, {"ok": True, "result": {"messages": ["x" * 500] * 100}})
    out = run(AgoraTool(write=False), {"action": "channel_read"})["output"]
    assert len(out) < agora_tool.MAX_OUTPUT + 100 and out.endswith("one thing at a time]")


def test_the_request_carries_the_hubs_token_wherever_the_hub_is(monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(path=self.path, auth=self.headers.get("Authorization"), person=self.headers.get("X-Hoard-Person"),
                        body=json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            blob = json.dumps({"ok": True, "result": {"ok": True}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(blob))); self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = tmp_path / "token"
        token.write_text("s3cret", encoding="utf-8")
        link = tmp_path / "hub-link.json"
        link.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "token_file": str(token)}), encoding="utf-8")
        monkeypatch.setenv("FAUSTUS_HUB_LINK", str(link))
        status, payload = agora_tool._hub("POST", "/api/agent/call", {"tool": "hub_agora_servers", "arguments": {}})
        assert status == 200 and payload["ok"] is True
        assert seen["path"] == "/api/agent/call" and seen["auth"] == "Bearer s3cret" and seen["body"]["tool"] == "hub_agora_servers"
        assert seen["person"] is None                                          # an agent is never the person's page
    finally:
        server.shutdown()
    monkeypatch.setenv("FAUSTUS_HUB_LINK", str(tmp_path / "nowhere.json"))
    monkeypatch.setattr(agora_tool.hub_link.hoard_hub, "hub_location", lambda: {"url": "http://127.0.0.1:9", "token_file": ""})
    assert agora_tool._hub("GET", "/api/agent/tools", timeout=2.0) == (None, None)


def test_the_tools_are_registered_like_their_neighbours():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_action
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import _PLAN_MODE_KNOWN_MUTATORS

    for name in ("agora_read", "agora_write"):
        assert name in TOOL_HANDLERS and name in TOOL_TAGS
        assert name in BUILTIN_TOOL_DESCRIPTIONS and len(EXAMPLES[name]) >= 4
        schema = next(s["function"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == name)
        assert schema["parameters"]["required"] == ["action"]
        assert capabilities_for_action(name, "{}").result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED   # other agents' text
    read, write = capabilities_for_action("agora_read", "{}"), capabilities_for_action("agora_write", "{}")
    assert read.known and ToolEffect.READ_PRIVATE in read.effects and ToolEffect.WRITE_PRIVATE not in read.effects
    assert write.known and ToolEffect.WRITE_PRIVATE in write.effects
    assert "agora_write" in _PLAN_MODE_KNOWN_MUTATORS and "agora_read" not in _PLAN_MODE_KNOWN_MUTATORS
