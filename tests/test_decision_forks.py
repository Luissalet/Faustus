"""Typed decisions at three forks of the agent loop (src/decision_forks.py):
after a tool error, on a tie between the top tools, and in extractive
compaction. Every fork is off by default and writes a receipt.

The decisions themselves are faked at `typed_decision.decide` / `decide_sync`
(the single-token logprob call is covered in tests/test_typed_decision*.py).
The loop tests drive `stream_agent_loop` with a scripted provider like
tests/test_advisor.py does.
"""
import asyncio
import json

import pytest

import src.agent_loop as al
from src import decision_forks as forks
from src import typed_decision as td


@pytest.fixture(autouse=True)
def _clean():
    forks._reset_stats()
    yield
    forks._reset_stats()


def _switches(monkeypatch, **values):
    base = dict(forks.DEFAULTS)
    base.update(values)
    monkeypatch.setattr(forks, "_setting", lambda key: base.get(key, forks.DEFAULTS.get(key)))
    monkeypatch.setattr(td, "enabled", lambda: True)


def _dec(field, value, confidence=0.95, mass=0.99, reason=""):
    return td.Decision(field=field, value=value, confidence=confidence, mass=mass,
                       method="logprobs", ms=12.0, reason=reason, best=value or "x")


def _fake_decide(monkeypatch, answers, seen=None):
    """answers: {field_name: value | None}; a callable gets the field."""
    async def _decide(context, fields, **kwargs):
        if seen is not None:
            seen.append({"context": context, "fields": list(fields), **kwargs})
        out = {}
        for f in fields:
            v = answers(f) if callable(answers) else answers.get(f.name)
            out[f.name] = _dec(f.name, v) if v is not None else _dec(f.name, None, 0.4, 0.9, "low_confidence")
        return out

    def _decide_sync(context, fields, **kwargs):
        return asyncio.run(_decide(context, fields, **kwargs))
    monkeypatch.setattr(td, "decide", _decide)
    monkeypatch.setattr(td, "decide_sync", _decide_sync)


# ----------------------------------------------------------------- basics --

def test_every_fork_is_off_by_default_and_needs_typed_decisions(monkeypatch):
    assert not any(forks.DEFAULTS[k] for k in ("typed_decision_error_fork",
                                                "typed_decision_tool_tie",
                                                "typed_decision_compaction_keep"))
    from src.settings import DEFAULT_SETTINGS
    for key, value in forks.DEFAULTS.items():
        assert DEFAULT_SETTINGS[key] == value
    _switches(monkeypatch, typed_decision_error_fork=True)
    assert forks.fork_enabled(forks.FORK_TOOL_ERROR)
    assert not forks.fork_enabled(forks.FORK_TOOL_TIE)
    monkeypatch.setattr(td, "enabled", lambda: False)
    assert not forks.fork_enabled(forks.FORK_TOOL_ERROR)


def test_receipt_carries_options_choice_confidence_mass_fallback_outcome():
    r = forks.make_receipt("tool_error", forks.ERROR_OPTIONS, _dec("next_step", "stop", 0.91, 0.97),
                           fallback=False, outcome="hint_injected", round_num=2, extra={"tool": "bash"})
    assert r["options"] == list(forks.ERROR_OPTIONS)
    assert (r["choice"], r["confidence"], r["mass"], r["fallback"], r["outcome"], r["round"]) == \
        ("stop", 0.91, 0.97, False, "hint_injected", 2)
    none = forks.make_receipt("tool_error", forks.ERROR_OPTIONS, None, fallback=True, outcome="x")
    assert none["choice"] is None and none["method"] == "unavailable" and none["fallback"] is True


def test_trace_nests_and_restores():
    outer, restore_outer = forks.begin_turn()
    inner, restore_inner = forks.begin_turn()
    assert forks.current_trace() is inner
    restore_inner()
    assert forks.current_trace() is outer
    forks.record({"fork": "tool_tie", "outcome": "a"})
    assert len(outer.receipts) == 1 and not inner.receipts
    restore_outer()
    assert forks.current_trace() is None


def test_stats_aggregate_choices_outcomes_and_outcome_changes():
    a = forks.record(forks.make_receipt("tool_error", forks.ERROR_OPTIONS, _dec("n", "stop", 0.9),
                                        fallback=False, outcome="hint_injected"))
    forks.record(forks.make_receipt("tool_error", forks.ERROR_OPTIONS, _dec("n", None, 0.4),
                                    fallback=True, outcome="below_threshold"))
    forks.update_outcome(a, "followed")
    row = forks.stats()["forks"]["tool_error"]
    assert row["asked"] == 2 and row["decided"] == 1 and row["fallback"] == 1
    assert row["choices"] == {"stop": 1}
    assert row["outcomes"] == {"followed": 1, "below_threshold": 1}
    assert row["mean_confidence"] == round((0.9 + 0.4) / 2, 4)


# --------------------------------------------------------------- tool error --

def test_failed_result_classification():
    assert forks.is_failed_result({"error": "boom"})
    assert forks.is_failed_result({"output": "x", "exit_code": 2})
    assert not forks.is_failed_result({"output": "x", "exit_code": 0})
    assert not forks.is_failed_result({"error": "blocked", "blocked": True})
    assert not forks.is_failed_result({"error": "needs approval", "approval_required": True})
    assert not forks.is_failed_result({"ask_user": {"q": 1}, "error": "x"})
    assert not forks.is_failed_result("text")
    assert forks.skips_error_fork("ask_user") and not forks.skips_error_fork("bash")


def test_error_fork_returns_a_hint_above_the_threshold_and_records(monkeypatch):
    seen = []
    _fake_decide(monkeypatch, {"next_step": "change_arguments"}, seen)
    hint, receipt = asyncio.run(forks.decide_after_error(
        user_request="fix it", tool="read_file", args='{"path": "nope"}', error="No such file"))
    assert hint and "advisory" in hint and "arguments" in hint
    assert receipt["choice"] == "change_arguments" and receipt["outcome"] == "hint_injected"
    assert receipt["tool"] == "read_file" and receipt["options"] == list(forks.ERROR_OPTIONS)
    assert seen[0]["caller"] == "fork_tool_error"
    assert "No such file" in seen[0]["context"] and "read_file" in seen[0]["context"]
    assert forks.stats()["forks"]["tool_error"]["asked"] == 1


def test_error_fork_below_threshold_does_nothing_new(monkeypatch):
    _fake_decide(monkeypatch, {"next_step": None})
    hint, receipt = asyncio.run(forks.decide_after_error(
        user_request="x", tool="bash", args="ls", error="boom"))
    assert hint is None and receipt["fallback"] is True and receipt["outcome"] == "below_threshold"


@pytest.mark.parametrize("choice,tool,args,ok", [
    ("retry_same", "bash", "ls", True), ("retry_same", "bash", "ls -l", False),
    ("change_arguments", "bash", "ls -l", True), ("change_arguments", "bash", "ls", False),
    ("change_arguments", "read_file", "ls -l", False),
    ("other_tool", "read_file", "ls", True), ("other_tool", "bash", "ls -l", False),
    ("stop", "read_file", "x", False),
])
def test_followed_choice(choice, tool, args, ok):
    assert forks.followed_choice(choice, "bash", "ls", tool, args) is ok


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_loop(monkeypatch, tool_output):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, tool_output)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _drive(monkeypatch, rounds, *, tools, max_rounds=5):
    seen = []

    async def _fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        text = rounds[min(len(seen) - 1, len(rounds) - 1)]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _go():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "read the config please"}],
            max_rounds=max_rounds, relevant_tools=tools, security_gate_bypass=True)]
    return seen, _events(asyncio.run(_go()))


READ_A = '```read_file\na.py\n```'
READ_B = '```read_file\nb.py\n```'


def _hints(seen):
    return [str(m.get("content")) for r in seen for m in r if "Runtime hint" in str(m.get("content"))]


def test_loop_error_fork_injects_a_hint_records_receipt_and_outcome(monkeypatch):
    _patch_loop(monkeypatch, {"error": "No such file: a.py"})
    _switches(monkeypatch, typed_decision_error_fork=True)
    asked = []
    _fake_decide(monkeypatch, {"next_step": "other_tool"}, asked)
    seen, events = _drive(monkeypatch, [READ_A, READ_B, "I could not read it."], tools={"read_file"})
    assert len(asked) >= 1 and asked[0]["fields"][0].name == "next_step"
    assert _hints(seen), "the hint reaches the next model call"
    assert "different tool" in _hints(seen)[0]
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    receipts = metrics["decision_receipts"]
    assert receipts[0]["fork"] == "tool_error" and receipts[0]["choice"] == "other_tool"
    # next call was the same tool, so the hint ("other_tool") was not followed
    assert receipts[0]["outcome"] == "ignored"


def test_loop_error_fork_off_by_default_asks_nothing(monkeypatch):
    _patch_loop(monkeypatch, {"error": "boom"})
    asked = []
    _fake_decide(monkeypatch, {"next_step": "stop"}, asked)
    seen, events = _drive(monkeypatch, [READ_A, "done"], tools={"read_file"})
    assert asked == [] and _hints(seen) == []
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert "decision_receipts" not in metrics


def test_loop_error_fork_below_threshold_injects_nothing_but_records(monkeypatch):
    _patch_loop(monkeypatch, {"error": "boom"})
    _switches(monkeypatch, typed_decision_error_fork=True)
    _fake_decide(monkeypatch, {"next_step": None})
    seen, events = _drive(monkeypatch, [READ_A, "done"], tools={"read_file"})
    assert _hints(seen) == []
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert metrics["decision_receipts"][0]["outcome"] == "below_threshold"


def test_loop_error_fork_is_bounded_per_turn(monkeypatch):
    _patch_loop(monkeypatch, {"error": "boom"})
    _switches(monkeypatch, typed_decision_error_fork=True)
    asked = []
    _fake_decide(monkeypatch, {"next_step": None}, asked)
    rounds = [f'```read_file\nf{i}.py\n```' for i in range(10)]
    _drive(monkeypatch, rounds, tools={"read_file"}, max_rounds=9)
    assert len(asked) <= forks.MAX_ERROR_FORKS_PER_TURN


def test_loop_error_fork_ignores_policy_blocks_and_successes(monkeypatch):
    _patch_loop(monkeypatch, {"output": "fine", "exit_code": 0})
    _switches(monkeypatch, typed_decision_error_fork=True)
    asked = []
    _fake_decide(monkeypatch, {"next_step": "stop"}, asked)
    _drive(monkeypatch, [READ_A, "done"], tools={"read_file"})
    assert asked == []


def test_a_failing_fork_never_costs_the_turn(monkeypatch):
    _patch_loop(monkeypatch, {"error": "boom"})
    _switches(monkeypatch, typed_decision_error_fork=True)

    async def _boom(*a, **k):
        raise RuntimeError("classifier down")
    monkeypatch.setattr(td, "decide", _boom)
    seen, events = _drive(monkeypatch, [READ_A, "done"], tools={"read_file"})
    assert any(e.get("type") == "metrics" for e in events)
    assert _hints(seen) == []


# ----------------------------------------------------------------- tool tie --

def test_tie_group_needs_two_within_tolerance_and_a_nonzero_best():
    assert forks.tie_group([("a", 1.0), ("b", 0.99), ("c", 0.5)], tolerance=0.03) == ["a", "b"]
    assert forks.tie_group([("a", 1.0), ("b", 0.8), ("c", 0.5)], tolerance=0.03) == []
    assert forks.tie_group([("a", 1.0), ("b", 1.0), ("c", 1.0)], tolerance=0.0) == ["a", "b", "c"]
    assert forks.tie_group([("a", 0.0), ("b", 0.0)]) == []
    assert forks.tie_group([("a", 1.0)]) == []
    # only the top three are looked at
    assert forks.tie_group([("a", 1.0), ("b", 0.5), ("c", 0.4), ("d", 1.0)], top=3, tolerance=0.03) == []


def test_apply_tie_choice_moves_only_the_chosen_one_to_the_front_of_the_tie():
    order = ["x", "a", "b", "c", "y"]
    assert forks.apply_tie_choice(order, ["a", "b", "c"], "c") == ["x", "c", "a", "b", "y"]
    assert forks.apply_tie_choice(order, ["a", "b"], "a") == order
    assert forks.apply_tie_choice(order, ["a", "b"], None) == order
    assert forks.apply_tie_choice(order, ["a", "b"], "zzz") == order


def test_choose_first_records_a_receipt_and_keeps_order_when_unsure(monkeypatch):
    seen = []
    _fake_decide(monkeypatch, {"promote_first": "b"}, seen)
    assert forks.choose_first_sync("send mail", ["a", "b"], {"a": "x", "b": "y"}) == "b"
    assert seen[0]["caller"] == "fork_tool_tie" and seen[0]["fields"][0].options() == ["a", "b"]
    _fake_decide(monkeypatch, {"promote_first": None})
    assert forks.choose_first_sync("send mail", ["a", "b"], {}) is None
    row = forks.stats()["forks"]["tool_tie"]
    assert row["asked"] == 2 and row["outcomes"] == {"promoted_first": 1, "kept_order": 1}


def test_tool_index_lexical_scores_lists_ties_in_name_order():
    from src.tool_index import ToolIndex
    idx = ToolIndex.__new__(ToolIndex)
    idx._corpus = {"builtin": {"zeta": "Tool: zeta\nsend a message", "alpha": "Tool: alpha\nsend a message",
                               "other": "Tool: other\ncalculate taxes"}, "mcp": {}}
    scores = idx.lexical_scores("send a message", k=3)
    assert [n for n, _ in scores][:2] == ["alpha", "zeta"]
    assert scores[0][1] == scores[1][1] > 0
    assert idx.lexical_scores("", k=3) == []


def _catalog_with_tie(monkeypatch, scored):
    import src.tool_serve as ts
    import src.tool_index as ti

    class _Idx:
        def retrieve(self, q, k=8, **kw):
            return ["a", "b", "c"]

        def lexical_scores(self, q, k=3, **kw):
            return scored
    monkeypatch.setattr(ti, "get_tool_index", lambda: _Idx())
    monkeypatch.setattr(ts, "_keyword_hits", lambda q: [])
    monkeypatch.setattr(ts, "_explicit_mcp_hits", lambda q: [])
    return ts


def test_search_catalog_reorders_a_tie_when_the_fork_is_on(monkeypatch):
    ts = _catalog_with_tie(monkeypatch, [("a", 1.0), ("b", 1.0), ("c", 0.2)])
    _switches(monkeypatch, typed_decision_tool_tie=True)
    _fake_decide(monkeypatch, {"promote_first": "b"})
    assert ts.search_catalog("do the thing", k=3)[:2] == ["b", "a"]


def test_search_catalog_untouched_when_off_or_not_a_tie_or_unsure(monkeypatch):
    ts = _catalog_with_tie(monkeypatch, [("a", 1.0), ("b", 1.0), ("c", 0.2)])
    asked = []
    _fake_decide(monkeypatch, {"promote_first": "b"}, asked)
    _switches(monkeypatch)                                   # fork off
    assert ts.search_catalog("do the thing", k=3) == ["a", "b", "c"] and asked == []
    _switches(monkeypatch, typed_decision_tool_tie=True)
    ts2 = _catalog_with_tie(monkeypatch, [("a", 1.0), ("b", 0.5), ("c", 0.2)])   # not a tie
    assert ts2.search_catalog("do the thing", k=3) == ["a", "b", "c"] and asked == []
    _catalog_with_tie(monkeypatch, [("a", 1.0), ("b", 1.0), ("c", 0.2)])
    _fake_decide(monkeypatch, {"promote_first": None})       # unsure
    assert ts.search_catalog("do the thing", k=3) == ["a", "b", "c"]


# --------------------------------------------------------------- compaction --

def _rows(n_old=6):
    rows = [{"role": "user", "content": "Migrate the billing module and keep the invoice ids."}]
    for i in range(n_old):
        rows.append({"role": "assistant", "content": f"calling tool {i}",
                     "tool_calls": [{"id": str(i), "function": {"name": "read_file", "arguments": "{}"}}]})
        rows.append({"role": "tool", "name": "read_file", "tool_call_id": str(i),
                     "content": f"RESULT-{i} " + "line of file content " * 30})
    return rows


def test_compaction_candidates_are_the_old_long_tool_results_newest_first():
    text_of = lambda c: str(c or "")
    rows = _rows(3) + [{"role": "tool", "name": "x", "content": "short"}]
    found = forks.compaction_candidates(rows, text_of)
    assert found == [6, 4, 2] and len(forks.compaction_candidates(_rows(20), text_of)) == forks.MAX_COMPACTION_CANDIDATES


def test_choose_verbatim_keeps_the_yes_rows_and_records_a_receipt_each(monkeypatch):
    rows = _rows(3)
    _fake_decide(monkeypatch, lambda f: "yes" if f.name == "keep_4" else ("no" if f.name == "keep_6" else None))
    kept = asyncio.run(forks.choose_verbatim(rows, lambda c: str(c or ""), goal="migrate billing"))
    assert kept == {4}
    row = forks.stats()["forks"]["compaction_keep"]
    assert row["asked"] == 3 and row["outcomes"] == {"kept_verbatim": 1, "digested": 1, "digested_default": 1}
    block = forks.verbatim_block(rows, kept, lambda c: str(c or ""))
    assert "RESULT-1" in block and "RESULT-0" not in block and block.startswith("Kept verbatim")


def test_verbatim_block_is_capped():
    rows = [{"role": "tool", "name": "t", "content": "z" * 9000} for _ in range(5)]
    block = forks.verbatim_block(rows, {0, 1, 2, 3, 4}, lambda c: str(c))
    assert len(block) < forks.VERBATIM_CHARS_TOTAL + forks.VERBATIM_CHARS_PER_RESULT + 400


def _compact(monkeypatch, rows, *, fork_on, answers):
    import src.context_compactor as cc
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "extract")
    _switches(monkeypatch, typed_decision_compaction_keep=fork_on)
    _fake_decide(monkeypatch, answers)
    state = {}
    result = asyncio.run(cc.maybe_compact(None, "http://x/v1", "m", rows, persist=False,
                                          compaction_state=state))
    return result, state


def test_extractive_compaction_keeps_chosen_results_and_pins_the_last_six(monkeypatch):
    rows = _rows(4)            # 9 messages
    (messages, _ctx, did), state = _compact(monkeypatch, rows, fork_on=True,
                                            answers=lambda f: "yes" if f.name == "keep_2" else "no")
    assert did
    summary = next(m["content"] for m in messages if m.get("role") == "system" and "summary" in m["content"].lower())
    assert "Kept verbatim" in summary and "RESULT-0" in summary
    # the last six conversation messages are untouched and in order
    assert messages[-6:] == rows[-6:]
    assert [r["fork"] for r in state["decision_receipts"]] == ["compaction_keep"] * len(state["decision_receipts"])


def test_extractive_compaction_unchanged_when_the_fork_is_off(monkeypatch):
    rows = _rows(4)
    asked = []
    import src.context_compactor as cc
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "extract")
    _switches(monkeypatch)
    _fake_decide(monkeypatch, {}, asked)
    messages, _c, did = asyncio.run(cc.maybe_compact(None, "http://x/v1", "m", rows, persist=False))
    assert did and asked == []
    summary = next(m["content"] for m in messages if m.get("role") == "system")
    assert "Kept verbatim" not in summary


def test_extractive_compaction_survives_a_broken_fork(monkeypatch):
    import src.context_compactor as cc
    monkeypatch.setattr(cc, "get_context_length", lambda *a: 100)
    monkeypatch.setattr(cc, "compaction_summary_mode", lambda: "extract")
    _switches(monkeypatch, typed_decision_compaction_keep=True)

    async def _boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(td, "decide", _boom)
    messages, _c, did = asyncio.run(cc.maybe_compact(None, "http://x/v1", "m", _rows(4), persist=False))
    assert did


# ------------------------------------------------------------------- route --

def test_stats_route_lists_the_three_forks(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import agent_loop_stats_routes
    # the object the routes depend on: a test elsewhere may reload src.auth_helpers
    require_user = agent_loop_stats_routes.require_user
    forks.record(forks.make_receipt("tool_tie", ["a", "b"], _dec("p", "a"), fallback=False, outcome="promoted_first"))
    app = FastAPI()
    app.include_router(agent_loop_stats_routes.setup_agent_loop_stats_routes())
    app.dependency_overrides[require_user] = lambda: "ada"
    body = TestClient(app).get("/api/agent/decision-forks/stats").json()
    assert body["forks"]["tool_tie"]["asked"] == 1 and set(body["enabled"]) == set(forks.FORKS)
