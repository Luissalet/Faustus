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


def test_fabricated_edit_claims_are_rejected_then_annotated(tmp_path, monkeypatch):
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "projects.js").write_text("x", encoding="utf-8")
    _patch_common(monkeypatch)
    lie = ("He creado ProjectCard.vue y añadido el botón de eliminación con confirmación. "
           "Todo está listo para funcionar.")
    calls = _scripted_stream(monkeypatch, [(lie, "stop"), (lie, "stop"), (lie, "stop")])
    events = _run(monkeypatch, str(tmp_path))

    checks = [e for e in events if e.get("type") == "harness_check"]
    statuses = [c["status"] for c in checks]
    assert statuses[:2] == ["rejected", "rejected"], statuses
    assert "unverified" in statuses, statuses
    assert any("ProjectCard.vue" in (c.get("bad_paths") or []) for c in checks)
    assert calls["n"] == 3  # two rejections → three model rounds, then accepted
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["stop_reason"] == "complete_unverified"
    assert summary["mutations"] == []
    # The visible answer carries the localized warning.
    text = "".join(e["delta"] for e in events if "delta" in e and not e.get("type"))
    assert "Verificación del harness" in text and "ninguno" in text


def test_real_edit_passes_verification(tmp_path, monkeypatch):
    (tmp_path / "static" / "js").mkdir(parents=True)
    target = tmp_path / "static" / "js" / "projects.js"
    target.write_text("export const x = 1;\n", encoding="utf-8")
    _patch_common(monkeypatch, tool_result={"output": "Edited static/js/projects.js (1 replacement)", "exit_code": 0})
    call = '```edit_file\n{"path": "static/js/projects.js", "old_string": "1", "new_string": "2"}\n```'
    _scripted_stream(monkeypatch, [
        (call, "tool_calls"),
        ("He añadido el botón en static/js/projects.js.", "stop"),
    ])
    events = _run(monkeypatch, str(tmp_path))
    statuses = [e["status"] for e in events if e.get("type") == "harness_check"]
    assert "rejected" not in statuses and "unverified" not in statuses, statuses
    assert "verified" in statuses, statuses
    summary = next(e for e in events if e.get("type") == "harness_summary")["data"]
    assert summary["mutations"] == ["static/js/projects.js"]
    assert summary["stop_reason"] == "complete"


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
