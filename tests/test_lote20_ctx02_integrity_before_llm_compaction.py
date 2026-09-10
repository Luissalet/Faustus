"""L20 (integrates L18, item 4): `stream_agent_loop`'s route-request builder
tried an LLM-summarized compaction (`maybe_compact`, lossy — paraphrases
older turns away) whenever a route was near its context budget, with no
attempt at the deterministic, identifier-preserving fold
(`compact_with_integrity`, CTX-02) first. `compact_with_integrity` is tried
first now, gated on the exact same over-threshold condition `maybe_compact`
itself checks, so a turn nowhere near its budget is unaffected either way;
`maybe_compact` remains the fallback when the deterministic fold is a no-op
(few turns, everything protected) or errors.

Mirrors `tests/test_foreground_model_routing.py::
test_agent_fallback_request_uses_candidate_context_budget`'s minimal
`stream_agent_loop` harness (that file is not owned by this lote and is
left untouched).
"""
import asyncio

import pytest

import src.agent_loop as agent_loop


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _base_mocks(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, False),
    )

    def fake_build(messages, model, *args, **kwargs):
        return ([{
            "role": "system", "content": f"route prompt for {model}", "_agent_injected": "prompt",
        }] + list(messages), [])

    monkeypatch.setattr(agent_loop, "_build_system_prompt", fake_build)

    import src.model_context as model_context

    # A tiny window: any non-trivial history is over COMPACT_THRESHOLD of it.
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 200)
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *a, fallback=0, **k: 200)

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        if factory is not None:
            await factory(0, *candidates[0])
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)


def _history(n=20):
    h = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"history-{i}"}
        for i in range(n)
    ]
    h.append({"role": "user", "content": "LATEST USER TURN"})
    return h


def test_integrity_compaction_is_tried_first_and_skips_the_llm_summary(monkeypatch):
    calls = []

    def fake_integrity(messages, *, owner_id="system", session_id="", keep_recent=4):
        calls.append(("integrity", owner_id, session_id))
        marker = {"role": "system", "content": "[compactado: N mensajes]"}
        return [marker] + messages[-2:], [{"evidence_id": "evi_fake"}]

    async def fake_maybe_compact(*args, **kwargs):
        calls.append(("maybe_compact",))
        return args[3] if len(args) > 3 else kwargs.get("messages"), 200, True

    monkeypatch.setattr(agent_loop, "compact_with_integrity", fake_integrity)
    monkeypatch.setattr(agent_loop, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda messages: len(messages) * 1000)
    _base_mocks(monkeypatch)

    primary = ("https://selected.example/v1", "selected-model", {})
    _collect(agent_loop.stream_agent_loop(
        primary[0], primary[1], _history(),
        headers=primary[2], max_rounds=1, relevant_tools=set(),
        defer_context_shaping=True, context_length=200,
        owner="alice", session_id="sess-ctx02",
    ))

    assert calls, "compact_with_integrity was never tried"
    assert calls[0][0] == "integrity"
    assert calls[0][1] == "alice"
    assert calls[0][2] == "sess-ctx02"
    assert not any(c[0] == "maybe_compact" for c in calls), (
        "maybe_compact ran even though compact_with_integrity already reduced the prompt"
    )


def test_llm_summary_still_runs_when_integrity_compaction_is_a_no_op(monkeypatch):
    """compact_with_integrity legitimately returns no evidence for a short
    conversation (nothing old enough to fold) — maybe_compact must still be
    reachable as the fallback in that case, so no capability is lost."""
    calls = []

    def fake_integrity(messages, *, owner_id="system", session_id="", keep_recent=4):
        calls.append(("integrity",))
        return list(messages), []  # no-op: nothing folded

    async def fake_maybe_compact(*args, **kwargs):
        calls.append(("maybe_compact",))
        messages = args[3] if len(args) > 3 else kwargs.get("messages")
        return messages, 200, False

    monkeypatch.setattr(agent_loop, "compact_with_integrity", fake_integrity)
    monkeypatch.setattr(agent_loop, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda messages: len(messages) * 1000)
    _base_mocks(monkeypatch)

    primary = ("https://selected.example/v1", "selected-model", {})
    _collect(agent_loop.stream_agent_loop(
        primary[0], primary[1], _history(),
        headers=primary[2], max_rounds=1, relevant_tools=set(),
        defer_context_shaping=True, context_length=200,
    ))

    assert [c[0] for c in calls] == ["integrity", "maybe_compact"]


def test_integrity_compaction_is_skipped_under_threshold_maybe_compact_unaffected(monkeypatch):
    """A turn nowhere near its budget must never have compact_with_integrity
    fold anything away. `maybe_compact` is still reached exactly as it was
    BEFORE this change (unconditionally, whenever defer_context_shaping or
    fallbacks is set) — it always did its own internal threshold check and
    no-ops itself; that pre-existing contract must not change either."""
    calls = []

    def fake_integrity(*args, **kwargs):
        calls.append("integrity")
        return list(args[0]), []

    async def fake_maybe_compact(*args, **kwargs):
        calls.append("maybe_compact")
        messages = args[3] if len(args) > 3 else kwargs.get("messages")
        return messages, 200, False

    monkeypatch.setattr(agent_loop, "compact_with_integrity", fake_integrity)
    monkeypatch.setattr(agent_loop, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda messages: 1)  # tiny: well under threshold
    _base_mocks(monkeypatch)

    primary = ("https://selected.example/v1", "selected-model", {})
    _collect(agent_loop.stream_agent_loop(
        primary[0], primary[1], _history(4),
        headers=primary[2], max_rounds=1, relevant_tools=set(),
        defer_context_shaping=True, context_length=200,
    ))

    assert calls == ["maybe_compact"]
