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


# ── gibberish-script guard (pure gibberish that never repeats one unit) ────

def test_guard_raises_on_sustained_cyrillic_run_that_never_repeats_a_unit():
    # The live failure: "lis the lis the" degrading into a DIFFERENT
    # Cyrillic word each token — no small repeated unit, so the repeat-based
    # checks above would never fire, yet it is still garbage.
    guard = llm_core._DegenerateStreamGuard("qwen3.8:27b-q8_0")
    cyrillic_words = [
        "привет", "мир", "собака", "кошка", "дерево", "солнце", "вода",
        "гора", "река", "облако", "звезда", "дорога", "город", "человек",
    ]
    with pytest.raises(llm_core.DegenerateOutput) as excinfo:
        for i in range(60):
            guard.check(cyrillic_words[i % len(cyrillic_words)] + " ")
    assert "non-Latin" in excinfo.value.reason


def test_guard_does_not_raise_on_normal_spanish_with_accents_and_emoji():
    guard = llm_core._DegenerateStreamGuard("model")
    text = (
        "¡Hola! Aquí tienes un resumen del estado del proyecto 🎉. "
        "La migración a la nueva versión terminó sin incidencias y el "
        "equipo revisó los cambios en la reunión de ayer. Todo salió bien "
        "y quedamos en continuar mañana con la siguiente fase del trabajo. "
        "Gracias por la paciencia y hasta luego 👋."
    )
    guard.check(text)  # no exception


def test_guard_does_not_raise_on_a_short_russian_quote_inside_english_text():
    guard = llm_core._DegenerateStreamGuard("model")
    text = (
        "The document explains the concept clearly. In Russian, the word "
        "for hello is 'привет', which is a common, friendly greeting used "
        "casually among friends and family across most of the country. "
        "The rest of this answer continues entirely in English, as "
        "requested, so the short quoted phrase above should not trip any "
        "kind of language-mismatch detector at all."
    )
    guard.check(text)  # no exception — short quote, mostly Latin overall


def test_unexpected_script_fraction_ignores_digits_punctuation_and_emoji():
    assert llm_core._unexpected_script_fraction("123 456 !!! ... 🎉🎉🎉") == 0.0
    assert llm_core._unexpected_script_fraction("") == 0.0


def test_gibberish_threshold_and_window_are_registered_settings():
    assert DEFAULT_SETTINGS["local_gibberish_script_threshold"] == 0.40
    assert DEFAULT_SETTINGS["local_gibberish_window_chars"] == 300


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


def test_degenerate_on_step1_retry_skips_same_model_and_answers_via_utility(monkeypatch, tmp_path):
    """Initial round degenerates, the existing single retry (ladder step 1)
    degenerates AGAIN — two degenerate aborts of the SAME model this turn.
    The ladder must not spend a third same-model attempt (step 2 is
    skipped): it goes straight to the utility endpoint (step 3), which
    answers. The turn must end with that answer, never an error — the
    owner's "never end with an error while a model is loaded".
    """
    _patch_common(monkeypatch)

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
            yield f'data: {json.dumps({"delta": "Real answer from the utility model."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"
        else:
            yield f'data: {json.dumps({"delta": "0000000000"})}\n\n'
            yield _degenerate_error_chunk()

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    raw_chunks = _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "looping-model",
        [{"role": "user", "content": "Summarize the project status in detail"}],
        max_rounds=3,
        relevant_tools={"read_file"},
    ))
    events = _types(raw_chunks)

    # call 1: initial round degenerates. call 2: step-1 retry degenerates
    # too (same model, second abort). call 3: ladder step 3 — the utility
    # model, reached WITHOUT a third same-model attempt — answers.
    assert call_no == 3
    assert not any(
        e.get("type") == "harness_check" and e.get("status") == "recovery" and e.get("step") == 2
        for e in events
    ), events
    step3 = next(
        (e for e in events if e.get("type") == "harness_check" and e.get("status") == "recovery" and e.get("step") == 3),
        None,
    )
    assert step3 is not None, events
    joined = "".join(raw_chunks)
    assert "Real answer from the utility model." in joined
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
    and the collapsed output is never what gets persisted. Step 2 (a third
    same-model attempt) is skipped outright: the model has already
    degenerated twice by the time the ladder runs.
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

    # round 1 (initial) -> step 1 retry (round 2, still the same model) ->
    # ladder: step 2 is skipped (second same-model degenerate abort already
    # happened) -> step 3 has no utility endpoint in this test environment,
    # so it can't even be tried -> step 4 gives up. Only 2 actual model
    # calls happen.
    assert round_no == 2
    joined = "".join(chunks)
    assert "looped" in joined.lower()
    assert not any(
        c.startswith("data: ") and '"status": "recovery"' in c and json.loads(c[6:]).get("step") == 2
        for c in chunks
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


# ── recovery ladder: skip_same_model_retry and the hard output cap ─────────

def test_recovery_ladder_skip_same_model_retry_omits_step2(monkeypatch, tmp_path):
    """`skip_same_model_retry=True` never runs step 2 (no step=2 harness_check
    event, and `stream_llm_with_fallback` is never called for the original
    model) and goes straight to step 3 against the utility endpoint."""
    _patch_common(monkeypatch)
    calls = []

    def _fake_resolve_endpoint(prefix, *args, **kwargs):
        if prefix == "utility":
            return ("http://127.0.0.1:11434/v1", "utility-model", {})
        return (None, None, None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", _fake_resolve_endpoint)

    async def _fake_stream(_candidates, messages, **kwargs):
        model = _candidates[0][1] if _candidates else None
        calls.append(model)
        yield f'data: {json.dumps({"delta": "Utility answer."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    events = []
    result = None
    async def _run():
        nonlocal result
        async for kind, payload in al._recovery_ladder(
            reason="degenerate", endpoint_url="http://127.0.0.1:11434/v1",
            model="looping-model", headers={}, messages=[{"role": "user", "content": "hi"}],
            temperature=0.7, max_tokens=512, gen_overrides=None, session_id="s1",
            owner=None, agent_stream_timeout=60, skip_same_model_retry=True,
        ):
            if kind == "event":
                events.append(json.loads(payload.split("data: ", 1)[1]))
            else:
                result = payload

    asyncio.run(_run())
    assert calls == ["utility-model"]  # never called for "looping-model" (step 2 skipped)
    assert not any(e.get("step") == 2 for e in events)
    assert any(e.get("step") == 3 for e in events)
    assert result["ok"] is True
    assert result["model"] == "utility-model"
    assert result["note"] == "(answered by utility-model after the default model looped)"
    assert result["text"] == f"{result['note']}\n\nUtility answer."


def test_recovery_ladder_step2_caps_output_tokens(monkeypatch, tmp_path):
    """Every recovery step (2 and 3) caps num_predict/max_tokens at
    `_RECOVERY_STEP2_MAX_TOKENS` regardless of the turn's own max_tokens, so
    a broken model cannot burn minutes of GPU time before the guard/cap ends
    it."""
    _patch_common(monkeypatch)
    seen_max_tokens = []

    async def _fake_stream(_candidates, messages, *, max_tokens=None, **kwargs):
        seen_max_tokens.append(max_tokens)
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _run():
        async for _kind, _payload in al._recovery_ladder(
            reason="degenerate", endpoint_url="http://127.0.0.1:11434/v1",
            model="looping-model", headers={}, messages=[{"role": "user", "content": "hi"}],
            temperature=0.7, max_tokens=8000, gen_overrides=None, session_id="s1",
            owner=None, agent_stream_timeout=60, skip_same_model_retry=False,
        ):
            pass

    asyncio.run(_run())
    assert seen_max_tokens
    assert all(mt <= al._RECOVERY_STEP2_MAX_TOKENS for mt in seen_max_tokens)



# --- reasoning paragraph loops (20-09-2026) --------------------------------

_LOOP_PARAGRAPH = (
    "Now I'm working through the naming logic: if there's a single Pokemon, use its name. "
    "For tags, I'll start with each identifier in lowercase, then add the group or line tag. "
    "I'm planning to process the remaining folders in batches of about 10 at a time.\n\n"
)


def _feed_in_chunks(fn, text, size=7):
    for i in range(0, len(text), size):
        fn(text[i:i + size])


def test_reasoning_paragraph_loop_is_caught_across_small_chunks():
    import pytest
    from src.llm_core import DegenerateOutput, _DegenerateStreamGuard

    guard = _DegenerateStreamGuard("qwen3.8:27b-q4_K_M")
    _feed_in_chunks(guard.check_reasoning, _LOOP_PARAGRAPH * 2)  # twice: still thinking
    with pytest.raises(DegenerateOutput) as info:
        _feed_in_chunks(guard.check_reasoning, _LOOP_PARAGRAPH)
    assert "reasoning loop" in str(info.value)


def test_content_may_repeat_long_lines_without_tripping():
    from src.llm_core import _DegenerateStreamGuard

    guard = _DegenerateStreamGuard("m")
    row = '{"title": "Venusaur Line - Pokemon Silhouette Frame | Normal, Inverse & Scaled STL"}\n'
    _feed_in_chunks(guard.check, row * 5)  # content channel: no paragraph-loop rule


def test_varied_reasoning_is_left_alone():
    from src.llm_core import _DegenerateStreamGuard

    guard = _DegenerateStreamGuard("m")
    text = "".join(
        f"Step {i}: read folder {i}-{i + 2}, look the ids up in the csv and write its listing file.\n"
        for i in range(40)
    )
    _feed_in_chunks(guard.check_reasoning, text)
