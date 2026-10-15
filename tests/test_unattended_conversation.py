"""Headless runs nobody is watching: a conversation task whose replies only
reach the user through a tool, and a model server that drops a connection."""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _scripted(monkeypatch, rounds):
    """Each entry is a list of raw SSE chunks for one model call."""
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        i = min(len(seen), len(rounds) - 1)
        seen.append([dict(m) for m in messages])
        for chunk in rounds[i]:
            yield chunk
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return seen


def _text(text, finish="stop"):
    return [
        f'data: {json.dumps({"delta": text})}\n\n',
        f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n',
        "data: [DONE]\n\n",
    ]


def _run(unattended=True, user="Help the customer with their order."):
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user}],
        max_rounds=6, relevant_tools={"read_file", "glob"}, unattended=unattended,
    )
    return _events(_collect(gen))


def test_the_relay_tool_is_the_one_that_talks_to_the_user():
    names = ["bash", "mcp__ab12__get_user_details", "mcp__ab12__send_message_to_user", "mcp__ab12__end_conversation"]
    assert al._unattended_relay_tool(names, []) == "mcp__ab12__send_message_to_user"
    assert al._unattended_relay_tool(["reply_to_customer"], []) == "reply_to_customer"
    # an email tool is not a conversation with the user
    assert al._unattended_relay_tool(["send_email", "reply_to_email", "bash"], []) is None
    # once the conversation was ended there is nobody left to talk to
    ended = [{"tool": "mcp__ab12__end_conversation", "ok": True}]
    assert al._unattended_relay_tool(names, ended) is None


def test_only_server_hiccups_are_waited_out():
    assert al._unattended_transport_retryable({"error_class": "transport.connection_reset"}, 502)
    assert al._unattended_transport_retryable({}, 503)
    assert al._unattended_transport_retryable({}, 429)
    assert not al._unattended_transport_retryable({"error_class": "bad_request"}, 400)
    assert not al._unattended_transport_retryable({}, None)
    waits = [al._unattended_transport_wait(n) for n in range(1, 7)]
    assert waits == sorted(waits) and waits[0] == 15 and waits[-1] == 120


def test_a_plain_text_reply_in_a_headless_conversation_is_sent_back_through_the_tool(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_unattended_relay_tool", lambda names, events: "send_message_to_user")
    seen = _scripted(monkeypatch, [
        _text("Could you please give me the email on your account?"),
        _text("The case is resolved. ###STOP###"),
    ])
    events = _run()
    nudges = [e for e in events if e.get("type") == "harness_check" and e.get("reason") == "undelivered_message"]
    assert len(nudges) == 1, events
    assert len(seen) == 2
    last = json.dumps(seen[1])
    assert "NOT delivered" in last and "email on your account" in last and "send_message_to_user" in last


def test_an_attended_turn_ends_on_its_plain_text_answer(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_unattended_relay_tool", lambda names, events: "send_message_to_user")
    seen = _scripted(monkeypatch, [_text("Could you please give me the email on your account?")])
    events = _run(unattended=False)
    assert not [e for e in events if e.get("reason") == "undelivered_message"]
    assert len(seen) == 1


def test_a_dropped_connection_is_waited_out_and_the_round_redone(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_unattended_transport_wait", lambda attempt: 0.0)
    err = {"error": "Upstream protocol error: Server disconnected", "status": 502,
           "error_class": "transport.connection_reset", "retryable": False}
    seen = _scripted(monkeypatch, [
        [f"event: error\ndata: {json.dumps(err)}\n\n"],
        [f"event: error\ndata: {json.dumps(err)}\n\n"],
        _text("All done: the answer is 4."),
    ])
    events = _run(user="What is 2 + 2?")
    retries = [e for e in events if e.get("reason") == "transport_retry"]
    assert [r["attempt"] for r in retries] == [1, 2], events
    assert len(seen) == 3
    assert not [e for e in events if e.get("type") == "error"]


def test_an_attended_turn_still_reports_the_dropped_connection(monkeypatch):
    _patch_common(monkeypatch)
    err = {"error": "Upstream protocol error", "status": 502, "error_class": "transport.connection_reset"}
    seen = _scripted(monkeypatch, [[f"event: error\ndata: {json.dumps(err)}\n\n"], _text("never reached")])
    events = _run(unattended=False, user="What is 2 + 2?")
    assert not [e for e in events if e.get("reason") == "transport_retry"]
    assert len(seen) == 1

def _empty_completion():
    err = {"error": "All model candidates returned no substantive output", "status": 502,
           "error_class": "generation.empty_completion", "fallback_eligible": False}
    return [f"event: error\ndata: {json.dumps(err)}\n\n"]


def test_empty_replies_do_not_end_an_unattended_task(tmp_path, monkeypatch):
    """A terminal task ended at round 30 of a two-hour budget on two empty
    replies from the model; unattended, a few more are waited out."""
    _patch_common(monkeypatch)
    seen = _scripted(monkeypatch, [_empty_completion(), _empty_completion(), _empty_completion(),
                                   _text("Finished: the solver is in /app/solve.py and eval.py passes.")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Make /app/solve.py pass eval.py"}],
        max_rounds=8, relevant_tools={"read_file", "glob"}, workspace=str(tmp_path), unattended=True,
    )
    events = _events(_collect(gen))
    assert len(seen) >= 4, [e.get("type") for e in events]
    nudges = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert nudges
    last = json.dumps(seen[-1])
    assert "Nobody can answer a question in this run" in last


def _glob(pattern):
    return _text("```glob\n" + json.dumps({"pattern": pattern}) + "\n```", "tool_calls")


def _audit_run(monkeypatch, tmp_path, unattended):
    _patch_common(monkeypatch)
    seen = _scripted(monkeypatch, [
        _glob("*.py"), _glob("src/*.py"), _glob("tests/*.py"),
        _text("Done: solve.py now passes eval.py."),
        _text("Checked every requirement: eval.py passes, output format matches."),
    ])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Make /app/solve.py pass eval.py"}],
        max_rounds=10, relevant_tools={"read_file", "glob"}, workspace=str(tmp_path), unattended=unattended,
    )
    return seen, _events(_collect(gen))


def test_an_unattended_task_audits_its_work_once_before_the_run_ends(tmp_path, monkeypatch):
    seen, events = _audit_run(monkeypatch, tmp_path, unattended=True)
    audits = [e for e in events if e.get("reason") == "completion_audit"]
    assert len(audits) == 1, [e.get("type") for e in events]
    assert len(seen) == 5
    assert "audit the work" in json.dumps(seen[4])


def test_an_attended_task_is_not_audited(tmp_path, monkeypatch):
    seen, events = _audit_run(monkeypatch, tmp_path, unattended=False)
    assert not [e for e in events if e.get("reason") == "completion_audit"]
    assert len(seen) == 4
