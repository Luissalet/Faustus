"""PENDIENTES 23-09 noche — interim narration in the wrong language.

Live: a long agent turn with a local model wrote interim text like
"Continuing with the analysis..." in English while the user wrote Spanish
(the FINAL answer came back in Spanish, only the interim narration drifted).

The real source: every harness/loop-recovery note Faustus injects BETWEEN
rounds (`_harness_note`, plus a handful of `role: system` runtime nudges --
verifier failure, intent-without-action, completion-engine continue, plan
coverage gap, approval/budget-stop echo) is hard-coded English at its call
site. `reply_language.refresh_continuation` already keeps a standing
reply-language reminder in the message list, but a harness note appended
AFTER it becomes the new last message before the next round -- the freshest
language cue a local model reads is then the runtime's own English text, not
the user's language. This covers the fix: every such note now carries a
one-line reminder in the user's own language via `reply_language.
localize_runtime_note`, wired through `agent_loop._stream_agent_loop_body`'s
`_lang_note` helper.
"""

import asyncio
import json

import src.agent_loop as al
from src.reply_language import localize_runtime_note, runtime_note_language_reminder


# ---------------------------------------------------------------------------
# Unit: the reply_language.py helpers
# ---------------------------------------------------------------------------

def test_localize_runtime_note_appends_a_spanish_reminder():
    out = localize_runtime_note(
        "[Harness check — automatic message from the runtime, not from the user] "
        "Your last message was EMPTY.",
        "es",
    )
    assert out.startswith("[Harness check")
    assert "español" in out


def test_localize_runtime_note_appends_an_english_reminder():
    out = localize_runtime_note("[Harness check] do it now.", "en")
    assert "English" in out


def test_localize_runtime_note_is_a_noop_without_a_settled_language():
    assert localize_runtime_note("text unchanged", None) == "text unchanged"


def test_localize_runtime_note_is_a_noop_for_an_unrecognised_code():
    assert localize_runtime_note("text unchanged", "xx") == "text unchanged"


def test_localize_runtime_note_is_a_noop_for_non_string_content():
    # Some notes are built and only conditionally used; a non-string content
    # (e.g. None from a helper that found nothing to say) must pass through.
    assert localize_runtime_note(None, "es") is None


def test_runtime_note_language_reminder_covers_every_reply_language_directive():
    from src.reply_language import _DIRECTIVE
    for code in _DIRECTIVE:
        assert runtime_note_language_reminder(code), code


# ---------------------------------------------------------------------------
# Integration: a fake stream through the real loop body
# ---------------------------------------------------------------------------

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


def _scripted_stream(monkeypatch, rounds, snapshots=None):
    """Each entry is text (empty string = silent give-up).

    `_stream_agent_loop_body` reassigns its local `messages` name (e.g. via
    `_insert_before_latest_user`, which returns a new list) rather than
    always mutating the caller's list object in place, so a harness note
    appended mid-turn does not necessarily show up on the list object the
    caller originally passed in. `stream_llm_with_fallback` is always called
    with whatever the CURRENT internal list is, so recording it here (a
    shallow copy, taken at call time) is what actually observes injected
    notes.
    """
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        if snapshots is not None:
            snapshots.append(list(messages))
        i = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        text = rounds[i]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def test_the_empty_round_nudge_carries_the_spanish_reminder(tmp_path, monkeypatch):
    """Reproduces the live bug's mechanism directly: the runtime's own
    injected note must argue for Spanish, not sit there in plain English for
    a local model to echo."""
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    snapshots = []
    _scripted_stream(monkeypatch, [
        "",  # silent give-up -> triggers the empty_round nudge
        "He revisado server.py; el endpoint todavía no existe.",
    ], snapshots=snapshots)
    messages = [{"role": "user", "content": "Añade un endpoint /api/stats en server.py"}]
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        messages,
        max_rounds=6,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    empty = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty) == 1, events

    notes = [
        m.get("content") for snap in snapshots for m in snap
        if isinstance(m, dict) and m.get("_harness_note") and isinstance(m.get("content"), str)
    ]
    assert notes, "expected at least one injected harness note"
    assert any("EMPTY" in n for n in notes)
    # The runtime's own note now argues for the user's language too.
    assert any("español" in n for n in notes), notes


def test_the_unknown_tool_nudge_carries_the_spanish_reminder(tmp_path, monkeypatch):
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    calls = {"n": 0}
    snapshots = []

    async def _fake_stream(_candidates, messages, **kwargs):
        snapshots.append(list(messages))
        calls["n"] += 1
        if calls["n"] == 1:
            yield 'data: ' + json.dumps({
                "type": "tool_calls",
                "calls": [{"name": "list", "arguments": json.dumps({"path": "."})}],
            }) + "\n\n"
            yield 'data: ' + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield 'data: ' + json.dumps({
                "delta": "El repositorio tiene server.py en la raíz; no se cambió nada.",
            }) + "\n\n"
            yield 'data: ' + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    messages = [{"role": "user", "content": "Añade un endpoint /api/stats en server.py"}]
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        messages,
        max_rounds=6,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    unk = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "unknown_tool"]
    assert len(unk) == 1, events

    notes = [
        m.get("content") for snap in snapshots for m in snap
        if isinstance(m, dict) and m.get("_harness_note") and isinstance(m.get("content"), str)
        and "does not exist" in m.get("content")
    ]
    assert notes and any("español" in n for n in notes), notes


def test_an_english_turn_still_gets_an_english_reminder_not_a_regression(tmp_path, monkeypatch):
    (tmp_path / "server.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch)
    snapshots = []
    _scripted_stream(monkeypatch, [
        "",
        "I checked server.py; the endpoint does not exist yet.",
    ], snapshots=snapshots)
    messages = [{"role": "user", "content": "Add an /api/stats endpoint in server.py"}]
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        messages,
        max_rounds=6,
        relevant_tools={"read_file", "edit_file", "glob"},
        workspace=str(tmp_path),
    )
    events = _events(_collect(gen))
    empty = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "empty_round"]
    assert len(empty) == 1, events
    notes = [
        m.get("content") for snap in snapshots for m in snap
        if isinstance(m, dict) and m.get("_harness_note") and isinstance(m.get("content"), str)
    ]
    assert any("EMPTY" in n for n in notes)
    assert any("English" in n for n in notes), notes
