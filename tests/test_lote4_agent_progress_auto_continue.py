"""FAUSTUS §115 — "keep going while there is progress" (item 2) and the
empty-round exhaustion path (item 3) in `src/agent_loop.py`'s round-budget
check (`if round_num > _rounds_budget:`).

Uses the same fake-model harness as tests/test_agent_rounds_exhausted.py:
`stream_llm_with_fallback` and `execute_tool_block` are patched so the loop
body, tool parsing and round bookkeeping all run for real.
"""

import asyncio
import json

import src.agent_loop as al


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


def _settings(overrides):
    def _get(key, default=None):
        if key in overrides:
            return overrides[key]
        return default
    return _get


def _patch_common(monkeypatch, settings_overrides=None):
    monkeypatch.setattr(
        al, "get_setting", _settings(settings_overrides or {}), raising=False,
    )
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def test_progress_auto_continue_extends_past_the_round_cap(monkeypatch):
    """Each round runs a DIFFERENT bash command (real progress via the
    ledger). With the round cap hit but progress present, the loop must
    extend automatically (a harness_check/auto_continue event with
    reason=progress_continue) instead of ending with rounds_exhausted."""
    _patch_common(monkeypatch, {
        "agent_auto_continue_on_progress": True,
        "agent_auto_continue_max_rounds": 10,
        "agent_auto_continue_cycles": 0,   # force: only the progress gate can extend
    })
    counter = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        counter["n"] += 1
        cmd = "echo unit-" + str(counter["n"])
        block = "```bash\n" + cmd + "\n```"
        yield "data: " + json.dumps({"delta": block}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "create one folder per item, one at a time"}],
        max_rounds=2,
        relevant_tools={"bash"},
    )
    events = _types(_collect(gen))

    assert any(
        e.get("type") == "harness_check" and e.get("reason") == "progress_continue"
        for e in events
    ), events
    # The turn kept going past the original 2-round cap instead of stopping cold.
    assert counter["n"] > 2, counter


def test_no_progress_streak_ends_with_a_question_not_silence(monkeypatch):
    """Once the progress gate has extended the turn at least once, a run of
    identical (non-progressing) rounds must eventually end with an ask_user
    event rather than a bare rounds_exhausted placeholder."""
    _patch_common(monkeypatch, {
        "agent_auto_continue_on_progress": True,
        "agent_auto_continue_max_rounds": 30,
        "agent_auto_continue_cycles": 0,
    })
    state = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        state["n"] += 1
        if state["n"] <= 2:
            cmd = "echo unit-" + str(state["n"])
            block = "```bash\n" + cmd + "\n```"
        else:
            # From round 3 on, repeat the SAME call over and over -> no
            # ledger growth once the loop-breaker's own dedupe kicks in,
            # and no new information either way.
            block = "```bash\necho stuck\n```"
        yield "data: " + json.dumps({"delta": block}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "create one folder per item, one at a time"}],
        max_rounds=2,
        relevant_tools={"bash"},
    )
    events = _types(_collect(gen))

    # Never allowed to end in bare silence: either it is still finishing
    # normally, or if it stops short it must carry an explicit ask_user.
    saw_ask = any(e.get("type") == "ask_user" for e in events)
    saw_rounds_exhausted_bare = (
        any(e.get("type") == "rounds_exhausted" for e in events) and not saw_ask
    )
    assert not saw_rounds_exhausted_bare, events


def test_empty_round_nudges_then_asks_a_question(monkeypatch, tmp_path):
    """A model that returns neither text nor a tool call gets nudged up to
    `agent_empty_round_max_nudges` times, then the turn ends with a concrete
    question (ask_user) instead of silently stopping.

    The "silent give-up" guard only engages from round 2 onward and inside
    an active harness scope (`workspace` set is the simplest way to get
    there in a unit test) — see `_empty_give_up` in src/agent_loop.py."""
    _patch_common(monkeypatch, {
        "agent_empty_round_max_nudges": 2,
    })

    async def _fake_stream(_candidates, messages, **kwargs):
        # Completely empty completion: no content, no tool call, every round.
        yield 'data: ' + json.dumps({"delta": ""}) + '\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do something ambiguous"}],
        max_rounds=10,
        relevant_tools={"bash"},
        workspace=str(tmp_path),
    )
    events = _types(_collect(gen))

    assert any(e.get("type") == "ask_user" for e in events), events
