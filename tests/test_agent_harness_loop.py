"""End-to-end: the reliability harness inside stream_agent_loop.

A fake model that narrates edits it never made must be rejected (bounded),
then annotated; a model whose edit really ran must pass; a model cut off by
max_tokens must be auto-continued. Uses the same mocking pattern as
test_agent_rounds_exhausted.py (real loop body, fake LLM stream / tool exec).
"""

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


def _patch_common(monkeypatch, tool_result=None):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # No owner in these tests → the public (non-admin) block list would disable
    # edit_file; we want the tool to run so the ledger sees a real mutation.
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        if tool_result is not None:
            return (block.tool_type, dict(tool_result))
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _scripted_stream(monkeypatch, rounds):
    """Each entry is (text, finish_reason). Rounds beyond the script repeat the last."""
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        text, finish = rounds[i]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def _run(monkeypatch, workspace, user="Añade un botón de borrar en las tarjetas de proyectos", max_rounds=6):
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": user}],
        max_rounds=max_rounds,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=workspace,
    )
    return _events(_collect(gen))


def test_truncated_output_is_auto_continued(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    calls = _scripted_stream(monkeypatch, [
        ("Here is the first half of the answer about the code", "length"),
        ("and here is the rest. No files were changed.", "stop"),
    ])
    events = _run(monkeypatch, str(tmp_path), user="Explica qué hace el fichero server.py")
    cont = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "auto_continue"]
    assert len(cont) == 1, events
    assert calls["n"] == 2
    infos = [e for e in events if e.get("type") == "round_info"]
    assert [i["finish_reason"] for i in infos] == ["length", "stop"]


def test_todowrite_progress_is_annotated_and_persisted(tmp_path, monkeypatch):
    """A todo marked completed without any successful tool since the previous
    snapshot is flagged verified=False, streamed as progress_update and saved
    to data/agent_todos/<session>.json (what the Progress panel restores)."""
    import src.agent_tools.coding_tools as ct
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path / "agent_todos"))
    _patch_common(monkeypatch)
    # execute_tool_block is faked: return the todos like the real tool does.
    todos1 = [{"id": "1", "content": "Leer projects.js", "status": "in_progress"},
              {"id": "2", "content": "Añadir botón", "status": "pending"}]
    todos2 = [{"id": "1", "content": "Leer projects.js", "status": "completed"},
              {"id": "2", "content": "Añadir botón", "status": "in_progress"}]
    seq = iter([todos1, todos2])

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "todowrite":
            return ("todowrite", {"output": "ok", "todos": next(seq)})
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    tw1 = "```todowrite\n" + json.dumps({"todos": todos1}) + "\n```"
    tw2 = "```todowrite\n" + json.dumps({"todos": todos2}) + "\n```"
    _scripted_stream(monkeypatch, [(tw1, "tool_calls"), (tw2, "tool_calls"), ("No he cambiado ficheros.", "stop")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Añade un botón de borrar en las tarjetas de proyectos"}],
        max_rounds=6, relevant_tools={"read_file", "edit_file", "glob", "todowrite"},
        workspace=str(tmp_path), session_id="sess-progress",
    )
    events = _events(_collect(gen))
    ups = [e for e in events if e.get("type") == "progress_update"]
    assert len(ups) == 2, [e.get("type") for e in events]
    done = [t for t in ups[1]["todos"] if t["status"] == "completed"]
    assert done and done[0]["verified"] is False  # completed with zero tool evidence in between
    saved = json.loads((tmp_path / "agent_todos" / "sess-progress.json").read_text(encoding="utf-8"))
    assert saved["todos"] == ups[1]["todos"]


def test_runaway_thinking_is_cut_off_and_retried_without_think(tmp_path, monkeypatch):
    """A local model that only produces reasoning past the budget is cut off
    once; the round is retried with gen_overrides.think=False and the step
    budget is not consumed by the retry."""
    monkeypatch.setattr(al, "get_setting",
                        lambda key, default=None: 0.05 if key == "agent_local_think_budget_seconds" else default,
                        raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    seen_think = []
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        go = kwargs.get("gen_overrides") or {}
        seen_think.append(go.get("think"))
        if calls["n"] == 1:
            # endless reasoning, never a visible token
            for _ in range(200):
                yield f'data: {json.dumps({"delta": "hmm ", "thinking": True})}\n\n'
                await asyncio.sleep(0.002)
            yield "data: [DONE]\n\n"
            return
        yield f'data: {json.dumps({"delta": "I could not find a project counter in this repository."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    events = _run(monkeypatch, str(tmp_path), user="¿Dónde está el contador de proyectos?", max_rounds=2)
    cut = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "think_cutoff"]
    assert len(cut) == 1 and cut[0]["reasoning_chars"] > 0
    assert calls["n"] == 2
    assert seen_think == [None, False]
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("think_cutoff@") for n in summary["notes"])
    assert summary["stop_reason"] == "complete"


def _native_call_stream(monkeypatch, rounds):
    """Each entry: list of native calls [{"name","arguments"}] or a text string."""
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        item = rounds[i]
        if isinstance(item, str):
            if item:
                yield f'data: {json.dumps({"delta": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def test_hallucinated_tool_name_gets_a_correction_not_a_silent_end(tmp_path, monkeypatch):
    """Seen live with qwen3-coder-next: a native call to `list` (no such tool)
    was dropped and the turn ended with no answer. The model must be told the
    real names and get another round."""
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    calls = _native_call_stream(monkeypatch, [
        [{"name": "list", "arguments": json.dumps({"path": "."})}],
        [{"name": "ls", "arguments": json.dumps({"path": "."})}],
        "The repo has server.py at the root; nothing was changed.",
    ])
    events = _run(monkeypatch, str(tmp_path), user="Añade un endpoint /api/stats en server.py", max_rounds=6)
    unk = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "unknown_tool"]
    assert len(unk) == 1 and unk[0]["tools"] == ["list"]
    assert "ls" in unk[0]["suggestions"]
    assert calls["n"] == 3
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete"
    assert summary["failed_calls"] >= 1  # the unknown call is recorded as a failed event


def test_empty_round_after_tool_work_is_nudged_once(tmp_path, monkeypatch):
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    calls = _native_call_stream(monkeypatch, [
        [{"name": "read_file", "arguments": json.dumps({"path": "server.py"})}],
        "",   # silent give-up
        "I read server.py; the endpoint is not there yet and I have not changed anything.",
    ])
    events = _run(monkeypatch, str(tmp_path), user="Añade un endpoint /api/stats en server.py", max_rounds=6)
    empty = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty) == 1
    assert calls["n"] == 3
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("empty_round_nudge@") for n in summary["notes"])


def test_delegate_agents_worker_reports_are_persisted_with_the_tool_event(tmp_path, monkeypatch):
    """The sub-agent board is rebuilt from history: the compact worker reports
    travel in metrics.tool_events[*].subagents (evidence fields only)."""
    _patch_common(monkeypatch)
    report = [{"id": "sa1-abc", "name": "backend", "session_id": "1a2b3c4d", "status": "done",
               "stop_reason": "complete", "error": None, "tool_calls": 3, "failed_calls": 0,
               "mutations": ["server.py"], "rejections": 0, "rounds": 2, "static_checks": [],
               "git": {"changed_count": 1}, "duration_s": 41.2, "final_text": "x" * 3000}]

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "delegate_agents":
            return ("delegate_agents: 1 worker(s)", {"output": "Delegated 1 sub-agent task(s).", "exit_code": 0,
                                                     "subagents": report, "duration_s": 41.5})
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    call = "```delegate_agents\n" + json.dumps({"tasks": [{"name": "backend", "instruction": "add /api/stats"}]}) + "\n```"
    _scripted_stream(monkeypatch, [(call, "tool_calls"), ("El worker backend cambió server.py.", "stop")])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Delega: añade /api/stats"}],
        max_rounds=4, relevant_tools={"delegate_agents", "read_file"},
        workspace=str(tmp_path), session_id="sess-delegate",
    )
    events = _events(_collect(gen))
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    ev = [t for t in metrics["tool_events"] if t["tool"] == "delegate_agents"][0]
    assert len(ev["subagents"]) == 1
    sa = ev["subagents"][0]
    assert sa["name"] == "backend" and sa["session_id"] == "1a2b3c4d" and sa["mutations"] == ["server.py"]
    assert sa["stop_reason"] == "complete" and sa["tool_calls"] == 3 and sa["duration_s"] == 41.2
    assert len(sa["final_text"]) == 400          # shortened for history
    assert "git" not in sa and "static_checks" not in sa  # evidence fields only


