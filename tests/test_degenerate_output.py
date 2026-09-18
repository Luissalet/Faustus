"""Coverage for the token-repeat-collapse guard ("0000000000…" until Ollama
itself aborted with "prediction aborted, token repeat limit reached", HTTP
400 mid-stream — seen live with a local 27B model on Ollama's native
/api/chat, think=false).

- `_DegenerateStreamGuard.check` raises `DegenerateOutput` on a canned run of
  repeated characters/tokens, and does NOT on legitimate long content such as
  a hash.
- The two sampler-floor settings (`local_repeat_penalty_default`,
  `local_min_p_default`) exist with the documented values, and an explicit
  saved/per-request override still wins over them.
- `stream_agent_loop` retries a degenerate round exactly once (repeat_penalty
  bumped, temperature raised, untrusted-context blocks dropped) and, if it
  degenerates again, ends the turn with a plain-language message instead of
  a bare HTTP-400.
"""
import asyncio
import json

import pytest

import src.llm_core as llm_core
import src.agent_loop as al
from src.prompt_security import UNTRUSTED_CONTEXT_HEADER
from src.settings import DEFAULT_SETTINGS


# ── guard detection on canned chunks ────────────────────────────────────────

def test_guard_raises_on_a_long_run_of_a_single_repeated_character():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    with pytest.raises(llm_core.DegenerateOutput):
        guard.check("0" * 130)


def test_guard_raises_on_a_long_run_of_zeros_fed_in_small_chunks():
    # The real failure mode: many small SSE deltas, not one giant string.
    guard = llm_core._DegenerateStreamGuard("looping-model")
    with pytest.raises(llm_core.DegenerateOutput):
        for _ in range(130):
            guard.check("0")


def test_guard_raises_on_repeated_short_word():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    with pytest.raises(llm_core.DegenerateOutput):
        guard.check("the " * 40)


def test_guard_does_not_raise_on_a_legitimate_long_hash():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    # A sha256 hex digest repeated a few times to exceed the 120-char floor —
    # random-looking, no small unit repeats across the whole span.
    hash_like = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    guard.check(hash_like + hash_like)  # 130 chars, no exception


def test_guard_does_not_raise_on_ordinary_prose():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    guard.check(
        "This is a perfectly ordinary answer that happens to be long enough "
        "to cross the guard's window but contains no repeated unit at all."
    )


def test_guard_does_not_raise_below_the_120_char_floor():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    guard.check("0" * 90)  # short burst — not yet degenerate


def test_degenerate_output_error_chunk_is_tagged_and_detectable():
    guard = llm_core._DegenerateStreamGuard("looping-model")
    with pytest.raises(llm_core.DegenerateOutput) as excinfo:
        guard.check("0" * 130)
    chunk = llm_core._degenerate_output_error_chunk(excinfo.value)
    assert chunk.startswith("event: error")
    payload = json.loads(chunk.split("data: ", 1)[1])
    assert llm_core.is_degenerate_output_error(payload)


def test_is_degenerate_output_error_matches_ollamas_own_abort_message():
    # Ollama's native abort text, not this runtime's own error_class.
    assert llm_core.is_degenerate_output_error(
        {"error": "prediction aborted, token repeat limit reached", "status": 400}
    )
    assert not llm_core.is_degenerate_output_error({"error": "rate limited", "status": 429})
    assert not llm_core.is_degenerate_output_error(None)


# ── settings defaults ───────────────────────────────────────────────────────

def test_local_sampler_defaults_are_registered():
    assert DEFAULT_SETTINGS["local_repeat_penalty_default"] == 1.05
    assert DEFAULT_SETTINGS["local_min_p_default"] == 0.05


# ── override precedence ─────────────────────────────────────────────────────

def test_defaults_apply_when_nothing_saved_or_requested():
    payload = {}
    llm_core._apply_gen_overrides_ollama(payload, {})
    assert payload["options"]["repeat_penalty"] == 1.05
    assert payload["options"]["min_p"] == 0.05


def test_explicit_override_wins_over_the_default():
    payload = {}
    llm_core._apply_gen_overrides_ollama(payload, {"repeat_penalty": 1.3, "min_p": 0.2})
    assert payload["options"]["repeat_penalty"] == 1.3
    assert payload["options"]["min_p"] == 0.2


def test_saved_setting_value_is_honoured_when_no_override_present(monkeypatch):
    from src import settings as settings_mod

    def _fake_get_setting(key, default=None):
        if key == "local_repeat_penalty_default":
            return 1.2
        if key == "local_min_p_default":
            return 0.1
        return default
    monkeypatch.setattr(settings_mod, "get_setting", _fake_get_setting)
    payload = {}
    llm_core._apply_gen_overrides_ollama(payload, {})
    assert payload["options"]["repeat_penalty"] == 1.2
    assert payload["options"]["min_p"] == 0.1


def test_min_p_is_a_recognized_gen_override_key():
    assert "min_p" in llm_core.GEN_OVERRIDE_KEYS
    assert "min_p" in llm_core._OLLAMA_NATIVE_ONLY_KEYS
    assert llm_core._clean_gen_overrides({"min_p": "0.08"}) == {"min_p": 0.08}


# ── agent-loop retry ─────────────────────────────────────────────────────────

def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
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

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _degenerate_error_chunk():
    return (
        "event: error\n"
        f"data: {json.dumps({'status': 502, 'error': 'looping-model started repeating tokens', 'error_class': 'degenerate_output', 'fallback_eligible': False})}\n\n"
    )


def test_degenerate_round_is_retried_once_with_bumped_sampler(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    round_no = 0
    seen_overrides = []

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        seen_overrides.append(kwargs.get("gen_overrides"))
        if round_no == 1:
            yield f'data: {json.dumps({"delta": "0000000000"})}\n\n'
            yield _degenerate_error_chunk()
        else:
            yield f'data: {json.dumps({"delta": "Here is the real answer."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "looping-model",
        [{"role": "user", "content": "Summarize the project status in detail"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    )))

    assert round_no == 2
    assert any(
        e.get("type") == "harness_check" and e.get("reason") == "degenerate_output_retry"
        for e in events
    ), events
    # The retry round asked for a higher repeat_penalty.
    assert seen_overrides[1] is not None
    assert seen_overrides[1].get("repeat_penalty") == 1.15


def test_degenerate_on_step1_retry_then_ladder_step2_answers(monkeypatch, tmp_path):
    """Initial round degenerates, the existing single retry (ladder step 1)
    degenerates AGAIN, and ladder step 2 (no tools, minimal prompt, same
    endpoint) finally answers. The turn must end with that answer, never an
    error — the owner's "never end with an error while a model is loaded".
    """
    _patch_common(monkeypatch)
    call_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no <= 2:
            yield f'data: {json.dumps({"delta": "0000000000"})}\n\n'
            yield _degenerate_error_chunk()
        else:
            yield f'data: {json.dumps({"delta": "Real answer from the ladder."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "looping-model",
        [{"role": "user", "content": "Summarize the project status in detail"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    # call 1: initial round degenerates. call 2: step-1 retry degenerates
    # too. call 3: ladder step 2 (fresh, tools-off, minimal-prompt
    # completion) answers.
    assert call_no == 3
    assert any(
        e.get("type") == "harness_check" and e.get("status") == "recovery" and e.get("step") == 2
        for e in events
    ), events
    joined = "".join(raw_chunks)
    assert "Real answer from the ladder." in joined
    assert not any(c.startswith("event: error") for c in raw_chunks), raw_chunks
    terminal_events = [e for e in events if e.get("type") == "agent_terminal"]
    assert not terminal_events, terminal_events  # a real answer is not a terminal failure


def test_ctx_ack_marker_only_through_ladder_step2_then_step3_answers(monkeypatch, tmp_path):
    """Echo on the initial round and the clean retry (ladder step 1), echo
    AGAIN on ladder step 2 (same model, tools off), and ladder step 3 (the
    utility endpoint — a different model) finally answers with the required
    one-line note prepended.
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(
        al, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, True),
        raising=False,
    )

    def _fake_resolve_endpoint(prefix, *args, **kwargs):
        if prefix == "utility":
            return ("http://127.0.0.1:11434/v1", "utility-model", {})
        return (None, None, None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", _fake_resolve_endpoint)

    call_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal call_no
        call_no += 1
        cand_model = _candidates[0][1] if _candidates else None
        if cand_model == "utility-model":
            yield f'data: {json.dumps({"delta": "Answer from the utility model."})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "<<faustus_ctx_ack>>"})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q4_K_M",
        [
            {"role": "user", "content": "Implement the zip plan for the project"},
            {
                "role": "user",
                "content": UNTRUSTED_CONTEXT_HEADER + "\nblah\n",
                "metadata": {"trusted": False, "source": "test"},
            },
            {"role": "user", "content": "Continua"},
        ],
        workspace=str(tmp_path),
        max_rounds=5,
        relevant_tools={"read_file", "apply_patch", "python"},
    ))
    events = _types(raw_chunks)

    step2 = next(
        (e for e in events if e.get("type") == "harness_check" and e.get("status") == "recovery" and e.get("step") == 2),
        None,
    )
    step3 = next(
        (e for e in events if e.get("type") == "harness_check" and e.get("status") == "recovery" and e.get("step") == 3),
        None,
    )
    assert step2 is not None, events
    assert step3 is not None, events
    joined = "".join(raw_chunks)
    assert "Answer from the utility model." in joined
    assert "answered by utility-model after the default model looped" in joined
    assert not any(c.startswith("event: error") for c in raw_chunks), raw_chunks
    assert "<<faustus_ctx_ack>>" not in joined.split('"type": "response_replace"')[-1]


def test_degenerate_round_ends_the_turn_with_plain_message_after_the_whole_ladder(monkeypatch, tmp_path):
    """When EVERY rung of the ladder degenerates (no utility endpoint is
    configured in this test env, so step 3 can't even be tried), the turn
    still ends with the plain-language message — never a bare HTTP-400 —
    and the collapsed output is never what gets persisted.
    """
    _patch_common(monkeypatch)
    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        yield f'data: {json.dumps({"delta": "0000000000"})}\n\n'
        yield _degenerate_error_chunk()

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "looping-model",
        [{"role": "user", "content": "Summarize the project status in detail"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))

    # round 1 (initial) -> step 1 retry (round 2) -> ladder step 2 (round 3,
    # same endpoint/model) -> step 3 has no utility endpoint in this test
    # environment, so it can't even be tried -> step 4 gives up.
    assert round_no == 3
    joined = "".join(chunks)
    assert "looped" in joined.lower()
    assert any(
        json.loads(c[6:]).get("step") == 2
        for c in chunks
        if c.startswith("data: ") and '"status": "recovery"' in c
    ), chunks
    # The raw delta chunk that already reached the "wire" before the error
    # fired can't be unsent (inherent to streaming) — but the PERSISTED
    # terminal text must never carry the collapsed output.
    terminal = next(
        json.loads(c[6:])
        for c in chunks
        if c.startswith("data: ") and '"type": "agent_terminal"' in c
    )
    persisted_text = " ".join(terminal["data"].get("round_texts") or [])
    assert "0000000000" not in persisted_text
