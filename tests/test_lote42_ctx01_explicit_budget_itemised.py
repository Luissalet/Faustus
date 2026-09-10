"""CTX-01 (lote 42): an explicit `agent_input_token_budget` must never exceed
the REAL per-request budget (`src.context_budget.budget_for`'s itemised
window-minus-reserves figure), not just the raw model window.

Before this lote, `agent_loop.py::_trim_route_request_messages` only ran the
explicit cap through `compute_input_token_budget`, which for an explicit
setting just does ``min(configured, context_length)`` -- it has no idea a
response reservation and a tool-schema reservation also have to fit inside
that same window. A user who pins a cap close to the whole context window
(a very common thing to do -- "give the agent almost everything") could
therefore get an effective budget that, once the response and tool schemas
are added back on top, overflows the model's real window. `budget_for`
already computes the itemised, reserve-aware number (CTX-01,
`src/context_budget.py`); this only had to be wired into the one function
this lote owns.

Reverting the `if budget_is_explicit: ...` block added in
`src/agent_loop.py::_trim_route_request_messages` (lote 20 -> lote 42) makes
this test fail: the captured budget goes back to the naive 99000 instead of
the itemised 87952.
"""
import json

import src.agent_loop as agent_loop
import src.context_budget as context_budget
import src.context_compactor as context_compactor
import src.model_context as model_context


def _collect(gen):
    import asyncio

    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def test_explicit_budget_is_tightened_by_itemised_reserves(monkeypatch):
    trim_budgets = []
    context_length = 100_000  # a real, "known" window (see budget_context_for_model)

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda messages: len(messages) * 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, False)
    )

    def fake_build(messages, model, *args, **kwargs):
        return (
            [{"role": "system", "content": "route prompt", "_agent_injected": "prompt"}]
            + list(messages),
            [],
        )

    monkeypatch.setattr(agent_loop, "_build_system_prompt", fake_build)
    monkeypatch.setattr(
        model_context, "budget_context_for_model", lambda *a, **kw: context_length
    )

    def spying_trim(messages, effective_budget, reserve_tokens=0):
        trim_budgets.append(effective_budget)
        return list(messages)

    monkeypatch.setattr(context_compactor, "trim_for_context", spying_trim)
    # compute_input_token_budget and budget_for are deliberately left REAL
    # here -- that is exactly the integration this test protects.

    async def fake_stream(candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    _collect(
        agent_loop.stream_agent_loop(
            "https://selected.example/v1",
            "selected-model",
            [{"role": "user", "content": "hello"}],
            max_rounds=1,
            relevant_tools={"bash"},
            fallbacks=[],
            harness_options={"input_token_budget": "99000"},
            _is_teacher_run=True,
        )
    )

    naive_cap = context_budget.compute_input_token_budget(
        99000, context_length, True, hard_max=context_budget.DEFAULT_HARD_MAX,
    )
    itemised = context_budget.budget_for(
        {"context_length": context_length, "context_known": True, "context_measured": False},
        "text",
        response_reserve=2048,  # reserve_tokens = min(max(max_tokens=4096, 512), 2048)
    )["input_budget"]
    assert naive_cap == 99000
    assert itemised == 87_952
    assert trim_budgets, "trim_for_context was never called"
    assert trim_budgets[0] == itemised
    assert trim_budgets[0] < naive_cap
