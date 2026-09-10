"""TASK-06 / PERF-04 (parcial): the autonomy budget (src/autonomy_budget.py)
and its two integration points in src/agent_loop.py — the round-loop /
before-every-tool checks, the checkpoint on exhaustion, and `read_only`
narrowing what is offered.

Unit tests exercise the module directly (no agent loop). The integration
tests drive the REAL `stream_agent_loop` the way
tests/test_agent_rounds_exhausted.py does: only the provider stream and the
tool executor are faked, everything else (tool parsing, the ledger, the
budget checks) is the production code.
"""

import asyncio
import json

import pytest

import src.autonomy_budget as ab
import src.agent_loop as al


# ─────────────────────────── unit: Budget / Ledger ────────────────────────

@pytest.mark.parametrize("kind,add,budget_kwargs", [
    ("tool_calls", lambda l: l.add_tool_call(), {"max_tool_calls": 1}),
    ("tokens", lambda l: l.add_tokens(500), {"max_tokens": 500}),
    ("active_seconds", lambda l: l.add_active_seconds(10), {"max_active_seconds": 10}),
    ("subagents", lambda l: l.add_subagents(2), {"max_subagents": 2}),
    ("remote_spend", lambda l: l.add_remote_spend(100), {"max_remote_spend": 100}),
    ("memory_mb", lambda l: l.set_memory_mb(2048), {"max_memory_mb": 2048}),
])
def test_each_dimension_exhausts_on_its_own(kind, add, budget_kwargs):
    """Every one of the six limits is checked independently: reaching ONE
    reports exactly that kind, with the other five left unlimited."""
    budget = ab.Budget(**budget_kwargs)
    ledger = ab.Ledger()
    assert ledger.check(budget) is None, "nothing spent yet"
    add(ledger)
    exhausted = ledger.check(budget)
    assert exhausted is not None
    assert exhausted.kind == kind
    assert exhausted.used == budget_kwargs[f"max_{kind}"]
    assert exhausted.limit == budget_kwargs[f"max_{kind}"]


def test_dimensions_are_independent_not_summed():
    """Reaching the tool_calls ceiling never trips a completely separate
    tokens ceiling, and vice versa — this is six budgets, not one."""
    budget = ab.Budget(max_tool_calls=1, max_tokens=1000)
    ledger = ab.Ledger()
    ledger.add_tool_call()
    exhausted = ledger.check(budget)
    assert exhausted is not None and exhausted.kind == "tool_calls"
    # Tokens are nowhere near 1000 and must not be reported.
    assert exhausted.kind != "tokens"


def test_zero_or_unset_limit_means_unlimited():
    """Matches the rest of this codebase's own convention (agent_max_tool_calls:
    0 = unlimited)."""
    ledger = ab.Ledger(tool_calls=10_000, tokens=10_000_000, active_seconds=999_999,
                        subagents=999, remote_spend=999_999, memory_mb=999_999)
    assert ledger.check(ab.Budget()) is None
    assert ledger.check(ab.Budget(max_tool_calls=0)) is None


def test_active_seconds_only_charges_what_is_explicitly_added():
    """The ledger has no wall clock of its own: it is exactly the sum of
    what the caller told it happened. A caller that never calls
    add_active_seconds (the shape of every human pause in this codebase —
    ask_user and a pending tool approval both END the turn instead of
    blocking inside a running call, see the module docstring) never moves
    this number, regardless of how much real time passed."""
    import time

    ledger = ab.Ledger()
    ledger.add_active_seconds(2.5)
    time.sleep(0.05)  # simulates a pause: nothing observes this
    ledger.add_active_seconds(1.5)
    assert ledger.active_seconds == 4.0
    # A negative delta (a clock glitch) never reduces the total.
    ledger.add_active_seconds(-100)
    assert ledger.active_seconds == 4.0


def test_check_order_is_fixed_and_deterministic():
    """Two ledgers with the same numbers always report the same cause — the
    order is a fixed tuple, not (accidentally) a dict/set iteration order."""
    budget = ab.Budget(max_tool_calls=1, max_tokens=1, max_active_seconds=1,
                        max_subagents=1, max_remote_spend=1, max_memory_mb=1)
    ledger = ab.Ledger(tool_calls=1, tokens=1, active_seconds=1, subagents=1,
                        remote_spend=1, memory_mb=1)
    assert ledger.check(budget).kind == "tool_calls"


# ─────────────────────────────── unit: presets ────────────────────────────

def test_normalize_preset_defaults_unknown_and_empty_to_supervised():
    assert ab.normalize_preset(None) == "supervised"
    assert ab.normalize_preset("") == "supervised"
    assert ab.normalize_preset("nonsense") == "supervised"
    assert ab.normalize_preset("READ_ONLY") == "read_only"  # case-insensitive


def test_resolve_budget_scales_existing_settings():
    """max_tool_calls/max_tokens derive from the settings that already exist
    (agent_max_tool_calls, agent_input_token_hard_max) — bounded_autonomous
    is strictly more than supervised, read_only strictly less."""
    settings = {"agent_max_tool_calls": 30, "agent_input_token_hard_max": 100_000}
    get_setting = lambda key, default=None: settings.get(key, default)
    supervised = ab.resolve_budget("supervised", get_setting=get_setting)
    bounded = ab.resolve_budget("bounded_autonomous", get_setting=get_setting)
    read_only = ab.resolve_budget("read_only", get_setting=get_setting)
    assert supervised.max_tool_calls == 30
    assert bounded.max_tool_calls > supervised.max_tool_calls
    assert read_only.max_tool_calls < supervised.max_tool_calls
    assert bounded.max_tokens > supervised.max_tokens > read_only.max_tokens


def test_resolve_budget_ignores_the_unlimited_sentinel_as_a_base():
    """agent_max_tool_calls: 0 means unlimited to the rest of the codebase; a
    preset must not become unlimited too, or 'read_only' would offer no
    protection at all on a machine that left the setting at 0 (its default)."""
    get_setting = lambda key, default=None: 0
    budget = ab.resolve_budget("supervised", get_setting=get_setting)
    assert budget.max_tool_calls and budget.max_tool_calls > 0
    assert budget.max_tokens and budget.max_tokens > 0


def test_resolve_budget_overrides_replace_individual_fields():
    budget = ab.resolve_budget("supervised", overrides={"max_subagents": 1})
    assert budget.max_subagents == 1
    default_budget = ab.resolve_budget("supervised")
    assert budget.max_tool_calls == default_budget.max_tool_calls  # untouched


def test_without_tool_calls_clears_only_that_field():
    budget = ab.Budget(max_tool_calls=5, max_tokens=10)
    stripped = budget.without_tool_calls()
    assert stripped.max_tool_calls is None
    assert stripped.max_tokens == 10


# ─────────────────────────── unit: read_only filtering ────────────────────

def test_read_only_keeps_reads_and_drops_effects():
    names = {"read_file", "ls", "grep", "bash", "write_file", "edit_file", "ask_user"}
    disabled = ab.read_only_disabled_names(names)
    assert disabled == {"bash", "write_file", "edit_file"}
    assert ab.is_read_only_tool("read_file")
    assert ab.is_read_only_tool("ask_user")  # user-facing, no external effect
    assert not ab.is_read_only_tool("bash")


def test_read_only_fails_high_on_an_unknown_tool():
    """A tool this process has no classification for (e.g. a freshly added
    MCP tool) is excluded, not allowed — same "fail high" rule
    tool_capabilities.capabilities_for_tool documents for everything else."""
    assert not ab.is_read_only_tool("some_mcp_tool_never_seen_before")
    assert "some_mcp_tool_never_seen_before" in ab.read_only_disabled_names(
        {"some_mcp_tool_never_seen_before"}
    )


# ─────────────────────────── unit: checkpoint / plan ──────────────────────

def test_plan_snapshot_prefers_structured_steps():
    payload = {"plan": "- [ ] a", "steps": [{"id": "s1", "title": "a", "status": "pending"}],
               "revision": 2, "warnings": []}
    snap = ab.plan_snapshot(payload)
    assert snap["revision"] == 2
    assert snap["steps"][0]["title"] == "a"


def test_plan_snapshot_falls_back_to_markdown():
    snap = ab.plan_snapshot({"plan": "- [x] done step\n- [ ] open step"})
    assert snap is not None
    titles = [s["title"] for s in snap["steps"]]
    assert "done step" in titles and "open step" in titles
    done = next(s for s in snap["steps"] if s["title"] == "done step")
    assert done["status"] == "done"


def test_plan_snapshot_is_none_without_a_plan():
    assert ab.plan_snapshot(None) is None
    assert ab.plan_snapshot({}) is None
    assert ab.plan_snapshot("not a mapping") is None


def test_build_checkpoint_never_discards_touched_files():
    checkpoint = ab.build_checkpoint(
        plan_update={"plan": "- [ ] a"},
        touched_files=["b.py", "a.py", "a.py"],  # dedup, order-independent
        note="stopped early",
    )
    assert checkpoint["touched_files"] == ["a.py", "b.py"]
    assert checkpoint["note"] == "stopped early"
    assert checkpoint["plan"]["steps"][0]["title"] == "a"


def test_remote_spend_units_only_charges_cost_tracked_endpoints():
    assert ab.remote_spend_units(endpoint_cost_tracked=True, input_tokens=100, output_tokens=50) == 150
    assert ab.remote_spend_units(endpoint_cost_tracked=False, input_tokens=100, output_tokens=50) == 0
    assert ab.remote_spend_units(endpoint_cost_tracked=None, input_tokens=100, output_tokens=50) == 0


# ═══════════════════ integration: the real stream_agent_loop ══════════════

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
    # Not the thing under test: an owner with the public/non-admin denylist
    # applied would lose bash/delegate_agents regardless of the autonomy
    # preset, which is exactly the ambiguity tests/test_agent_loop_tool_preflight.py
    # avoids the same way.
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "update_plan":
            from src.plan_state import from_markdown
            import json as _json
            try:
                plan_md = _json.loads(block.content or "{}").get("plan", "")
            except Exception:
                plan_md = ""
            parsed = from_markdown(plan_md)
            payload = {"plan": plan_md}
            payload.update(parsed.to_dict())
            return (block.tool_type, {"plan_update": payload, "output": "Plan updated.", "exit_code": 0})
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_texts, *, max_rounds=4, relevant_tools=None,
              autonomy_preset=None, capture_tools=None):
    texts = list(round_texts)

    async def _fake_stream(_candidates, messages, **kwargs):
        if capture_tools is not None:
            capture_tools.append(kwargs.get("tools") or [])
        body = texts.pop(0) if texts else "All done."
        yield f'data: {json.dumps({"delta": body})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools=relevant_tools,
        autonomy_preset=autonomy_preset,
        owner="admin",
    )
    return _events(_collect(gen))


def test_read_only_does_not_offer_bash_or_write(monkeypatch):
    """Requirement: read_only no ofrece bash/write. Offered == executable is
    already guaranteed generically by
    tests/test_agent_loop_offer_execute_coherence.py; this pins the
    read_only-specific narrowing itself."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True), raising=False)
    captured = []
    _run_loop(
        monkeypatch, ["All done."],
        relevant_tools={"bash", "write_file", "edit_file", "read_file", "ls", "ask_user"},
        autonomy_preset="read_only",
        capture_tools=captured,
    )
    names = {s.get("function", {}).get("name") for s in captured[0]}
    assert "bash" not in names
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "read_file" in names
    assert "ls" in names
    assert "ask_user" in names  # user interaction is not an "effect"


def test_read_only_refuses_bash_even_if_the_model_calls_it_anyway(monkeypatch):
    """Offered == executable, the read_only-specific case: even a model that
    ignores the schema list and emits the fence anyway gets refused, never
    executed."""
    _patch_common(monkeypatch)
    events = _run_loop(
        monkeypatch,
        ['```bash\necho hi\n```'],
        relevant_tools={"bash"},
        autonomy_preset="read_only",
    )
    outputs = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == "bash"]
    assert outputs, events
    assert outputs[0].get("error") or outputs[0].get("blocked")


def test_supervised_still_offers_bash(monkeypatch):
    """The default preset changes nothing about what was offered before this
    feature existed."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True), raising=False)
    captured = []
    _run_loop(monkeypatch, ["All done."], relevant_tools={"bash"}, capture_tools=captured)
    names = {s.get("function", {}).get("name") for s in captured[0]}
    assert "bash" in names


def test_exhaustion_ends_the_turn_partial_with_a_checkpoint(monkeypatch):
    """A tiny forced budget (tool_calls=2): the third tool call attempt is
    refused, the turn ends via the SAME mechanism rounds_exhausted already
    uses (stop_reason != 'complete' -> harness_summary is emitted), and the
    new `budget_exhausted` event carries a checkpoint built from what the
    turn's own ledgers already knew — the plan from update_plan, and the
    (possibly empty) list of touched files."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        al.autonomy_budget, "resolve_budget",
        lambda *a, **k: al.autonomy_budget.Budget(max_tool_calls=2),
        raising=False,
    )
    events = _run_loop(
        monkeypatch,
        [
            '```update_plan\n{"plan":"- [ ] step one"}\n```',
            '```bash\necho one\n```',
            '```bash\necho two\n```',
        ],
        max_rounds=4,
        relevant_tools={"bash"},
    )
    exhausted = [e for e in events if e.get("type") == "budget_exhausted"]
    assert exhausted, events
    assert exhausted[0]["kind"] == "tool_calls"
    assert exhausted[0]["used"] == 2
    assert exhausted[0]["limit"] == 2
    checkpoint = exhausted[0]["checkpoint"]
    assert checkpoint["plan"]["steps"][0]["title"] == "step one"
    assert isinstance(checkpoint["touched_files"], list)
    assert checkpoint["note"]

    summary = next(e for e in events if e.get("type") == "harness_summary")
    assert summary["data"]["stop_reason"] == "budget_exceeded", (
        "compat: the pre-existing stop_reason string is unchanged — "
        "src/tool_outcome.py, src/scorecard.py, routes/chat_routes.py and "
        "src/agent_tools/subagent_tools.py all classify a turn by it"
    )


def test_legacy_budget_exceeded_event_is_unchanged(monkeypatch):
    """Compat: a caller that never passes autonomy_preset (every existing
    caller, until routes/chat_routes.py starts forwarding it) still gets the
    exact `budget_exceeded` event it always got, from the exact same
    max_tool_calls/total_tool_calls mechanism — this module only ADDS the
    richer `budget_exhausted` alongside it."""
    _patch_common(monkeypatch)
    captured = []

    async def _fake_stream(_candidates, messages, **kwargs):
        captured.append(1)
        texts = ['```bash\necho one\n```', '```bash\necho two\n```', 'All done.']
        body = texts[len(captured) - 1] if len(captured) <= len(texts) else 'All done.'
        yield f'data: {json.dumps({"delta": body})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do work"}],
        max_rounds=4,
        relevant_tools={"bash"},
        max_tool_calls=1,
        owner="admin",
    )
    events = _events(_collect(gen))
    legacy = [e for e in events if e.get("type") == "budget_exceeded"]
    assert legacy == [{"type": "budget_exceeded", "limit": 1, "used": 1}]


def test_subagents_are_counted_from_delegate_agents_results(monkeypatch):
    """max_subagents charges from the same `subagents` list delegate_agents
    already returns (no change to delegate_agents' own behaviour)."""
    _patch_common(monkeypatch)

    async def _fake_exec(block, *a, **k):
        if block.tool_type == "delegate_agents":
            return (block.tool_type, {
                "output": "2 workers done", "exit_code": 0,
                "subagents": [{"name": "w1", "status": "done"}, {"name": "w2", "status": "done"}],
            })
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(
        al.autonomy_budget, "resolve_budget",
        lambda *a, **k: al.autonomy_budget.Budget(max_subagents=1),
        raising=False,
    )

    events = _run_loop(
        monkeypatch,
        ['```delegate_agents\n{"tasks":[]}\n```', '```bash\necho x\n```'],
        max_rounds=3,
        relevant_tools={"bash", "delegate_agents"},
    )
    exhausted = [e for e in events if e.get("type") == "budget_exhausted"]
    assert exhausted, events
    assert exhausted[0]["kind"] == "subagents"
    assert exhausted[0]["used"] == 2
