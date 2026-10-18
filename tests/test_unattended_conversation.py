"""Headless runs nobody is watching: a conversation task whose replies only
reach the user through a tool, and a model server that drops a connection."""

import asyncio
import json

import pytest

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


def test_an_announced_step_that_never_ran_is_sent_back_to_the_audit(tmp_path, monkeypatch):
    """A terminal task ended on "Now writing the filter." after one todo list:
    no file written, the turn closed as answered. The audit catches it too."""
    _patch_common(monkeypatch)
    seen = _scripted(monkeypatch, [
        _glob("*.html"),
        _text("Now writing the filter."),
        _text("Wrote /app/filter.py and checked it against the sample pages."),
    ])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Write /app/filter.py that strips JavaScript from HTML files"}],
        max_rounds=8, relevant_tools={"read_file", "glob"}, workspace=str(tmp_path), unattended=True,
    )
    events = _events(_collect(gen))
    assert [e for e in events if e.get("reason") == "completion_audit"], [e.get("type") for e in events]
    assert len(seen) >= 3


def test_back_to_back_loops_before_any_tool_do_not_end_an_unattended_task(monkeypatch):
    """Two polyglot runs died at rounds 3 and 7: the model looped again within
    four rounds before running any tool, and the recovery ladder ended the turn."""
    import src.llm_core as llm_core
    _patch_common(monkeypatch)
    loop = llm_core._degenerate_output_error_chunk(
        llm_core.DegenerateOutput("repeated phrase 'edge cases edge cases' 10 times", "glm", "http://x"))
    seen = _scripted(monkeypatch, [[loop], [loop], [loop], _text("The answer is 4.")])
    events = _run(user="What is 2 + 2?")
    assert len(seen) == 4, [e.get("type") for e in events]
    assert not [e for e in events if e.get("type") == "error"]


@pytest.mark.parametrize("initial_length", [False, True])
def test_answer_only_data_with_a_workspace_does_not_reopen_after_stop(tmp_path, monkeypatch, initial_length):
    _patch_common(monkeypatch)
    request = (
        "Documentos de una auditoría sintética. Aplica el cambio de moneda. "
        "Convert using integer rate numerator/denominator. "
        "INVOICE cents=12; rate=3/2. Devuelve SOLO JSON con el neto."
    )
    answer = '{"net":18}'
    unexpected = {"error": "unexpected extra provider call", "status": 400}
    rounds = ([_text("", "length")] if initial_length else []) + [
        _text(answer), [f'event: error\ndata: {json.dumps(unexpected)}\n\n'],
    ]
    seen = _scripted(monkeypatch, rounds)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": request}], workspace=str(tmp_path),
        unattended=True, max_rounds=6, relevant_tools={"read_file", "glob"},
    )))
    assert len(seen) == 1 + int(initial_length), events
    assert not [e for e in events if e.get("reason") == "completion_audit"
                or e.get("status") == "no_action" or e.get("type") == "error"], events
    assert "".join(e.get("delta", "") for e in events).strip() == answer
    terminal = next(e for e in reversed(events) if e.get("type") == "metrics")
    assert terminal["data"]["harness"]["stop_reason"] == "complete"


@pytest.mark.parametrize("user_text", [
    "Aplica el cambio usando ratio a/b y responde con JSON.",
    "Read the ratio numerator/denominator and return JSON.",
    "El cambio de moneda usa el ratio foo/bar.",
])
def test_data_ratios_are_not_workspace_paths(user_text):
    assert not al._looks_like_workspace_coding_request(user_text)


@pytest.mark.parametrize("user_text", [
    "Read /etc/hosts", "Edit ./config/settings", "Rename ../old ../new",
    "Open ~/work/README", r"Edit C:\work\LICENSE", "Edit C:/work/LICENSE",
    r"Open \\server\share\LICENSE", "Fix cart.py", "Edit the file foo/bar",
    "Lee la carpeta foo/bar", "Edit config/settings", "Rename logs/old logs/new",
    "Lee src/config", "Open foo/bar",
])
def test_real_workspace_paths_still_require_work(user_text):
    assert al._looks_like_workspace_coding_request(user_text)


@pytest.mark.parametrize("user_text", [
    "Write /app/result.json and return only JSON.",
    "Guarda el resultado en result.json y devuelve solo JSON.",
])
def test_json_summary_does_not_exempt_a_file_job(tmp_path, monkeypatch, user_text):
    _patch_common(monkeypatch)
    seen = _scripted(monkeypatch, [_text('{"status":"done"}'), _text('{"status":"checked"}')])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user_text}], workspace=str(tmp_path),
        unattended=True, max_rounds=2, relevant_tools={"read_file", "glob"},
    )))
    assert len([e for e in events if e.get("reason") == "completion_audit"]) == 1, events
    assert len(seen) == 2


@pytest.mark.parametrize("broken", [False, True])
def test_real_file_effect_keeps_audit_and_syntax_check_with_json_summary(tmp_path, monkeypatch, broken):
    _patch_common(monkeypatch)
    path = tmp_path / "result.py"
    content = "def broken(:\n" if broken else "value = 18\n"
    writes = []
    checked = []

    def check_syntax(paths, workspace):
        # Keep the closing-stage check live without launching py_compile.
        assert paths == ["result.py"]
        checked.extend(paths)
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as error:
            return [{"path": str(path), "ok": False, "error": str(error)}]
        return [{"path": str(path), "ok": True}]

    monkeypatch.setattr(al._harness, "static_check_files", check_syntax)

    async def write(block, *args, **kwargs):
        assert block.tool_type == "write_file"
        payload = json.loads(block.content)
        assert payload["path"] == str(path)
        path.write_text(payload["content"], encoding="utf-8")
        writes.append(str(path))
        return (block.tool_type, {"output": "Written " + str(path), "exit_code": 0})

    monkeypatch.setattr(al, "execute_tool_block", write)
    tool = _text("```write_file\n" + json.dumps({"path": str(path), "content": content}) + "\n```", "tool_calls")
    seen = _scripted(monkeypatch, [tool, _text('{"status":"done"}'), _text('{"status":"checked"}')])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Write result.py and return only JSON."}],
        workspace=str(tmp_path), unattended=True, max_rounds=4,
        relevant_tools={"write_file", "read_file"},
    )))
    assert writes == [str(path)]
    assert path.read_text(encoding="utf-8") == content
    assert len([e for e in events if e.get("reason") == "completion_audit"]) == 1, events
    assert bool([e for e in events if e.get("status") == "syntax_error"]) == broken, events
    assert checked == ["result.py"]
    assert len(seen) >= 3


def test_completion_work_uses_real_effects_and_workspace_reads_not_tool_availability(tmp_path):
    intent = {"workspace_work": False}
    ledger = al._harness.TurnLedger(str(tmp_path), "Return the data as JSON.")
    assert not al._workspace_completion_work(intent, [], ledger)
    ledger.record("web_search", "weather", {"output": "sunny"})
    assert not al._workspace_completion_work(intent, [], ledger)
    ledger.record("read_file", json.dumps({"path": "result.txt"}), {"output": "contents"})
    assert al._workspace_completion_work(intent, [], ledger)
    effects = al._harness.TurnLedger(str(tmp_path), "Return only JSON.")
    effects.record("write_file", json.dumps({"path": "result.txt"}), {"output": "written"})
    assert effects.effects
    assert al._workspace_completion_work(intent, [], effects)


def test_completion_work_preserves_requested_files_and_pending_progress(tmp_path):
    ledger = al._harness.TurnLedger(str(tmp_path), "Write the result.")
    assert al._workspace_completion_work({}, ["result.json"], ledger)
    ledger.progress = [{"id": "1", "content": "Write result", "status": "pending"}]
    assert al._workspace_completion_work({}, [], ledger)


def test_prepared_workspace_intent_survives_an_explicit_continuation():
    messages = [{"role": "user", "content": "Fix /app/cart.py"},
                {"role": "assistant", "content": "I will inspect the file."},
                {"role": "user", "content": "continue"}]
    assert al._classify_agent_request(messages, "continue")["workspace_work"]
    text = "Read the ratio a/b and return JSON."
    assert not al._classify_agent_request([{"role": "user", "content": text}], text)["workspace_work"]


@pytest.mark.parametrize("operation", ["Fix", "Refactor", "Update", "Review", "Inspect"])
def test_relative_package_operation_keeps_audit_without_tools(operation, tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    package = tmp_path / "src" / "widgets"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("value = 1\n", encoding="utf-8")
    user_text = operation + " src/widgets"
    seen = _scripted(monkeypatch, [_text('{"status":"done"}'), _text('{"status":"checked"}')])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user_text}], workspace=str(tmp_path),
        unattended=True, max_rounds=2, relevant_tools={"read_file", "glob"},
    )))
    assert al._looks_like_workspace_coding_request(user_text)
    assert len([event for event in events if event.get("reason") == "completion_audit"]) == 1, events
    assert len(seen) == 2
    assert source.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("operation", [
    "Debug", "Implement", "Build", "Test", "Compile", "Run", "Change", "Convert",
    "Remove", "Delete", "Corrige", "Actualiza", "Revisa", "Refactoriza", "Implementa",
])
def test_relative_operations_share_the_existing_action_vocabulary(operation):
    assert al._looks_like_workspace_coding_request(operation + " config/settings")


@pytest.mark.parametrize("operation", ["Update", "Inspect", "Change", "Convert"])
def test_action_on_a_ratio_is_not_an_action_on_a_relative_path(operation):
    assert not al._looks_like_workspace_coding_request(operation + " the ratio numerator/denominator")


def test_relative_delete_support_does_not_change_explanation_classification():
    assert al._looks_like_workspace_coding_request("Delete config/settings")
    assert al._explanation_request("Explain how to delete records")
