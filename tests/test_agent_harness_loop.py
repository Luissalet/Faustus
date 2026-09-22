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
    # Scripted in the user's language on purpose: the harness nudges a reply
    # that comes back in another one, and that nudge costs a round. This test
    # is about `finish_reason: length` being continued, so it must not also
    # be a language-mismatch test by accident.
    calls = _scripted_stream(monkeypatch, [
        ("Esta es la primera mitad de la respuesta sobre el código", "length"),
        ("y este es el resto. No se cambió ningún fichero.", "stop"),
    ])
    events = _run(monkeypatch, str(tmp_path), user="Explica qué hace el fichero server.py")
    cont = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "auto_continue"]
    assert len(cont) == 1, events
    assert calls["n"] == 2
    infos = [e for e in events if e.get("type") == "round_info"]
    assert [i["finish_reason"] for i in infos] == ["length", "stop"]


def test_a_request_to_explain_is_answered_in_prose_without_a_nudge():
    """A bound workspace made every prose-only reply look like a shirked job.
    A greeting was exempted when that turned "hola" into "your original
    request requires work in the active workspace"; a question has exactly
    the same problem, and being told to start the next response with a tool
    call is the wrong answer to it."""
    assert al._explanation_request("Explica qué hace el fichero server.py")
    assert al._explanation_request("explain what this module does")
    assert al._explanation_request("¿Cómo funciona el enrutado de modelos?")
    assert al._explanation_request("What does agent_loop do?")
    # A change is still a change, however it is introduced.
    assert not al._explanation_request("Explica qué hace server.py y luego arregla el bug")
    assert not al._explanation_request("Añade un endpoint /api/stats en server.py")
    assert not al._explanation_request("fix the failing test")
    assert not al._explanation_request("")


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
    retried_with = []
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        go = kwargs.get("gen_overrides") or {}
        seen_think.append(go.get("think"))
        if calls["n"] == 1:
            # endless reasoning, never a visible token
            yield f'data: {json.dumps({"delta": "The counter lives in stats.js, line 40. ", "thinking": True})}\n\n'
            for _ in range(200):
                yield f'data: {json.dumps({"delta": "hmm ", "thinking": True})}\n\n'
                await asyncio.sleep(0.002)
            yield "data: [DONE]\n\n"
            return
        retried_with.append("\n".join(str(m.get("content") or "") for m in messages))
        yield f'data: {json.dumps({"delta": "I could not find a project counter in this repository."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    events = _run(monkeypatch, str(tmp_path), user="¿Dónde está el contador de proyectos?", max_rounds=2)
    cut = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "think_cutoff"]
    assert len(cut) == 1 and cut[0]["reasoning_chars"] > 0
    assert calls["n"] == 2
    assert seen_think == [True, False]
    # the retry starts from what the cut reasoning had already found
    assert "The counter lives in stats.js, line 40." in retried_with[0]
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("think_cutoff@") for n in summary["notes"])
    assert summary["stop_reason"] == "complete"


def test_local_workspace_turn_enables_thinking(tmp_path, monkeypatch):
    """Coding work on a local endpoint thinks by default so the transcript
    can show collapsed Thought Ns between tool groups."""
    _patch_common(monkeypatch)
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append((kwargs.get("gen_overrides") or {}).get("think"))
        yield f'data: {json.dumps({"delta": "No files were changed."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _run(monkeypatch, str(tmp_path), user="¿Dónde está el contador de proyectos?", max_rounds=2)
    assert seen and seen[0] is True


def test_pinned_think_off_is_not_overridden_on_local_workspace(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append((kwargs.get("gen_overrides") or {}).get("think"))
        yield f'data: {json.dumps({"delta": "No files were changed."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "¿Dónde está el contador de proyectos?"}],
        max_rounds=2,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
        gen_overrides={"think": False},
    )))
    assert seen and seen[0] is False
    assert events


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
        # In the user's language: a reply in another one earns a nudge and
        # an extra round, which is a different check from this one.
        "El repositorio tiene server.py en la raíz; no se cambió nada.",
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
        # In the user's language, so the empty-round nudge is the only one.
        "He leído server.py; el endpoint todavía no está y no he cambiado nada.",
    ])
    events = _run(monkeypatch, str(tmp_path), user="Añade un endpoint /api/stats en server.py", max_rounds=6)
    empty = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty) == 1
    assert calls["n"] == 3
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert any(n.startswith("empty_round_nudge@") for n in summary["notes"])


def test_empty_completion_502_after_tools_is_nudged_not_fatal(tmp_path, monkeypatch):
    """Silhouettes / qwen3.8 on Ollama: a later round sometimes finishes with
    no text and no tool call. stream_llm_with_fallback turns that into
    'All model candidates returned no substantive output' (HTTP 502). The
    harness must treat it as an empty round and continue, not kill the turn.
    """
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield (
                'data: {"type": "tool_calls", "calls": '
                '[{"name": "read_file", "arguments": "{\\"path\\": \\"server.py\\"}"}]}\n\n'
            )
            yield 'data: {"type": "finish", "finish_reason": "tool_calls"}\n\n'
            yield "data: [DONE]\n\n"
            return
        if calls["n"] == 2:
            yield (
                'event: error\ndata: '
                '{"error": "All model candidates returned no substantive output", '
                '"status": 502, "error_class": "generation.empty_completion"}\n\n'
            )
            return
        yield 'data: {"delta": "I read server.py; nothing was changed."}\n\n'
        yield 'data: {"type": "finish", "finish_reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M",
        [{"role": "user", "content": "Añade un endpoint /api/stats en server.py"}],
        max_rounds=6,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
    ))
    events = _events(chunks)
    empty = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty) == 1, [e.get("type") for e in events]
    assert calls["n"] == 3
    assert not any(c.startswith("event: error") for c in chunks)
    assert not any(e.get("type") == "agent_terminal" for e in events)
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete"
    assert any(n.startswith("empty_round_nudge@") for n in summary["notes"])


def test_real_provider_502_still_terminates_the_turn(tmp_path, monkeypatch):
    """A genuine upstream 502 is not an empty round and must still stop."""
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield (
            'event: error\ndata: '
            '{"error": "Bad gateway", "status": 502, '
            '"error_class": "transport.llm_service_error"}\n\n'
        )

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M",
        [{"role": "user", "content": "Añade un endpoint /api/stats en server.py"}],
        max_rounds=4,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
    ))
    assert any(c.startswith("event: error") for c in chunks)
    events = _events(chunks)
    assert not any(e.get("type") == "harness_check" and e.get("status") == "empty_round" for e in events)


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
        workspace=str(tmp_path), session_id="sess-delegate", security_gate_bypass=True,
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


def test_explicit_project_objective_order_cannot_finish_as_prose_only(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    # This test exercises the completion supervisor, not project discovery.
    # Model an already-attached project so preflight keeps the mutation tool.
    import src.tool_preflight as tool_preflight
    monkeypatch.setattr(tool_preflight, "prune_for_turn", lambda *_a, **_k: {})
    objective_call = "```project_objectives\n" + json.dumps({
        "action": "apply",
        "deltas": [{"op": "ADD", "title": "Ship the release", "rationale": "user asked"}],
    }) + "\n```"
    calls = _scripted_stream(monkeypatch, [
        ("Hecho, lo he añadido a objetivos.", "stop"),
        (objective_call, "tool_calls"),
        ("Añadido a los objetivos del proyecto.", "stop"),
    ])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Añade Ship the release a los objetivos del proyecto"}],
        max_rounds=5, relevant_tools={"project_objectives"}, workspace=str(tmp_path),
        session_id="sess-objectives", security_gate_bypass=True,
    )
    events = _events(_collect(gen))
    required = [e for e in events if e.get("type") == "harness_check"
                and e.get("status") == "required_action"]
    assert len(required) == 1
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert any(e["tool"] == "project_objectives" for e in metrics["tool_events"])
    assert calls["n"] == 3


def test_missing_project_cannot_be_reported_as_a_successful_objective_change(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    calls = _scripted_stream(monkeypatch, [
        ("Hecho, ya está en los objetivos del proyecto.", "stop"),
        ("No se ha cambiado nada: este chat no está vinculado a un proyecto.", "stop"),
    ])
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Añade Ship the release a los objetivos del proyecto"}],
        max_rounds=4, relevant_tools={"project_objectives"}, workspace=str(tmp_path),
        session_id="sess-without-project", security_gate_bypass=True,
    )
    events = _events(_collect(gen))
    unavailable = [e for e in events if e.get("type") == "harness_check"
                   and e.get("status") == "required_action_unavailable"]
    assert len(unavailable) == 1
    assert calls["n"] == 2


def test_looks_like_continue_turn():
    assert al._looks_like_continue_turn("Continue")
    assert al._looks_like_continue_turn("continuar")
    assert al._looks_like_continue_turn("Continue with task 03")
    assert not al._looks_like_continue_turn("Añade un botón de borrar")
    assert not al._looks_like_continue_turn("")


def test_stale_completed_progress_is_refreshed_after_more_tools(tmp_path, monkeypatch):
    """After a 5/5 completed list, more tool work must prompt a new todowrite
    so the Progress panel does not freeze on the previous batch."""
    import src.agent_tools.coding_tools as ct
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path / "agent_todos"))
    _patch_common(monkeypatch)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    done = [{"content": "Task 02", "status": "completed"}]
    tw = "```todowrite\n" + json.dumps({"todos": done}) + "\n```"
    steps = [
        tw,
        "```get_workspace\n{}\n```",
        '```read_file\n{"path": "a.py"}\n```',
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        '```read_file\n{"path": "b.py"}\n```',
        "No files were changed.",
    ]
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append("\n".join(
            str(m.get("content") or "") for m in messages if isinstance(m.get("content"), str)
        ))
        i = min(len(seen) - 1, len(steps) - 1)
        text = steps[i]
        finish = "stop" if i >= len(steps) - 1 else "tool_calls"
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "todowrite":
            return ("todowrite", {"output": "ok", "todos": done})
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Añade un botón de borrar en las tarjetas de proyectos"}],
        max_rounds=8, relevant_tools={"read_file", "get_workspace", "update_plan", "todowrite"},
        workspace=str(tmp_path), session_id="sess-stale-progress",
    )))
    assert any("Progress list is fully completed" in blob for blob in seen), seen[-1] if seen else seen


def test_continue_turn_refreshes_completed_progress_immediately(tmp_path, monkeypatch):
    """Clicking Continue after a finished batch must ask for a new todowrite
    on the first model call, not after four more silent tools."""
    import src.agent_tools.coding_tools as ct
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path / "agent_todos"))
    ct.save_todos("sess-cont", [{"content": "Task 02", "status": "completed"}])
    _patch_common(monkeypatch)
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append("\n".join(
            str(m.get("content") or "") for m in messages if isinstance(m.get("content"), str)
        ))
        yield f'data: {json.dumps({"delta": "No files were changed."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Continue"}],
        max_rounds=2, relevant_tools={"read_file", "todowrite"},
        workspace=str(tmp_path), session_id="sess-cont",
    )))
    assert seen and "Progress list is fully completed" in seen[0]


def test_auto_continue_asks_to_refresh_a_completed_progress_list(tmp_path, monkeypatch):
    import src.agent_tools.coding_tools as ct
    monkeypatch.setattr(ct, "_TODO_DIR", str(tmp_path / "agent_todos"))
    _patch_common(monkeypatch)
    done = [{"content": "Task 02", "status": "completed"}]
    tw = "```todowrite\n" + json.dumps({"todos": done}) + "\n```"
    grep_block = '```grep\n{"pattern": "x"}\n```'
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append("\n".join(
            str(m.get("content") or "") for m in messages if isinstance(m.get("content"), str)
        ))
        n = len(seen)
        if n == 1:
            text, finish = tw, "tool_calls"
        elif n == 2:
            text, finish = grep_block, "tool_calls"
        else:
            text, finish = "No files were changed.", "stop"
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "todowrite":
            return ("todowrite", {"output": "ok", "todos": done})
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Añade un botón de borrar en las tarjetas de proyectos"}],
        max_rounds=2, relevant_tools={"grep", "todowrite"},
        workspace=str(tmp_path), session_id="sess-auto-progress",
    )))
    autos = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "auto_continue"]
    assert autos, [e.get("type") for e in events]
    assert any("Progress list is fully completed" in blob for blob in seen)


def test_permission_to_continue_is_rejected_and_the_model_is_told_to_keep_working(tmp_path, monkeypatch):
    """Continue already given; after real edits the model asks for a green
    light. That must not end the turn — seen live as 'Say the word and I'll
    continue' after task 03, with the star IoU question still open."""
    _patch_common(monkeypatch)
    (tmp_path / "x.py").write_text("a = 1\n", encoding="utf-8")
    edit = (
        "```edit_file\n"
        + json.dumps({"path": "x.py", "old_string": "a = 1", "new_string": "a = 2"})
        + "\n```"
    )
    stall = (
        "I updated x.py. The star IoU question is still open.\n\n"
        "Say the word and I'll continue with task 04."
    )
    calls = _scripted_stream(monkeypatch, [
        (edit, "tool_calls"),
        (stall, "stop"),
        ("No files were changed. Task 04 is next.", "stop"),
    ])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        # Unambiguously English: "Continue" settles no language of its own,
        # so the installation's configured default decided it, and on a
        # Spanish one the English script earned a language nudge -- which
        # costs the round this test is watching for.
        [{"role": "user", "content": "Continue with the next task, please."}],
        max_rounds=6, relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
        harness_options={"trusted_workspace": str(tmp_path)},
    )))
    rejected = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "rejected"]
    assert rejected, [e.get("type") for e in events]
    assert "asked_instead_of_continuing" in (rejected[0].get("reasons") or [])
    assert calls["n"] >= 3


def test_backed_work_is_kept_when_a_permission_stall_is_exhausted(tmp_path, monkeypatch):
    """Seen live: tasks 04–06 wrote 17 files, then the model asked for a green
    light. After two rejections the harness replaced the whole summary with
    'the task remains unfinished'. The writes were real; the summary must stay."""
    _patch_common(monkeypatch)
    (tmp_path / "x.py").write_text("a = 1\n", encoding="utf-8")
    edit = (
        "```edit_file\n"
        + json.dumps({"path": "x.py", "old_string": "a = 1", "new_string": "a = 2"})
        + "\n```"
    )
    stall = (
        "I updated x.py and finished the remaining editor tasks.\n\n"
        "Say the word and I'll continue with the next one."
    )
    _scripted_stream(monkeypatch, [
        (edit, "tool_calls"),
        (stall, "stop"),
        (stall, "stop"),
        (stall, "stop"),
    ])
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Continue"}],
        max_rounds=8, relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
        harness_options={"trusted_workspace": str(tmp_path), "run_tests": False},
    )))
    checks = [e for e in events if e.get("type") == "harness_check"]
    assert any(c.get("status") == "rejected" for c in checks)
    assert any(c.get("status") == "stall_exhausted" for c in checks), [
        (c.get("status"), c.get("reasons"), c.get("reason")) for c in checks
    ]
    assert not any(c.get("status") == "unverified" for c in checks)
    assert not any(c.get("reason") == "execution_recovery" for c in checks)
    replace = [e for e in events if e.get("type") == "response_replace"]
    assert not any("task remains unfinished" in (e.get("text") or "") for e in replace)
    assert not any("I did not complete or verify" in (e.get("text") or "") for e in replace)
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete"
    assert "x.py" in summary["mutations"]
    assert any(str(n).startswith("stall_exhausted:") for n in (summary.get("notes") or []))


