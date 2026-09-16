"""H3 — verified delegation: src/delegation_receipts.py (pure) and the
empty/ack_only retry wired into src/agent_tools/subagent_tools.py.

Born from the Silhouettes fan-out failure: `delegate_agents` sent 3 workers
(`backend-core`, `fit-tests`, `frontend`) precise technical specs and all
three came back with zero mutations — one replied only
`<<faustus_ctx_ack>>`, two with "(no final text)" after a few reads — and
nothing in the parent turn noticed. See
`/tmp/.../scratchpad/harness_wave/CONTRATO.md` §H3 and
`/tmp/.../scratchpad/silhouettes_analysis.md` §18-20.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import delegation_receipts as dr
from src.agent_tools import subagent_tools as st


# ── delegation_receipts.py: pure classification ─────────────────────────────

def test_classify_produced_wins_over_everything():
    assert dr.classify("whatever", ["a.py"], 3) == dr.VERDICT_PRODUCED
    assert dr.classify("", ["a.py"], 3, error="late crash") == dr.VERDICT_PRODUCED


def test_classify_ack_only_matches_every_ritual_phrase_seen_live():
    for text in ("<<faustus_ctx_ack>>", "<<faustus_ctx_ack>><<faustus_ctx_ack>>",
                 "Reference context received.", "Reference context received", "Done.", "Done",
                 "(no final text)", "", "   "):
        assert dr.classify(text, [], 2) == dr.VERDICT_ACK_ONLY, text


def test_classify_empty_is_not_ack_only():
    """A worker that wrote three paragraphs of analysis but never called a
    write tool is a DIFFERENT failure from one that only echoed a ritual
    phrase — the retry prompt needs to say a different thing to each."""
    assert dr.classify("I looked at the files and here is my plan...", [], 5) == dr.VERDICT_EMPTY


def test_classify_error_when_no_mutation_and_an_error_is_recorded():
    assert dr.classify("", [], 0, error="model request failed") == dr.VERDICT_ERROR


def test_is_ack_only_ignores_padding_and_repetition():
    assert dr.is_ack_only("  <<faustus_ctx_ack>>  ")
    assert dr.is_ack_only("<<faustus_ctx_ack>><<faustus_ctx_ack>>")
    assert not dr.is_ack_only("<<faustus_ctx_ack>> but also I wrote src/a.py")


def test_receipt_for_duck_types_a_subagentrun():
    run = type("R", (), {"name": "backend-core", "instruction": "implement mesh_adapter.py",
                          "mutations": [], "tool_calls": 2, "text": "<<faustus_ctx_ack>>",
                          "stop_reason": "stalled", "error": None})()
    rec = dr.receipt_for(run)
    assert rec.worker == "backend-core" and rec.verdict == dr.VERDICT_ACK_ONLY
    assert rec.to_dict()["verdict"] == "ack_only"


def test_failure_message_is_the_contract_sentence():
    assert dr.failure_message("ack_only") == "worker produced nothing (ack_only) — do it yourself or split the task"
    assert dr.failure_message("empty") == "worker produced nothing (empty) — do it yourself or split the task"


def test_looks_like_write_task_by_verb_or_path():
    assert dr.looks_like_write_task("Implement 4 backend modules + their tests")
    assert dr.looks_like_write_task("Create silhouettes/editor/mesh_adapter.py")
    assert dr.looks_like_write_task("read silhouettes/editor/constraints.py and match its API")  # path token
    assert not dr.looks_like_write_task("What do you think of this approach?")
    assert not dr.looks_like_write_task("")


def test_short_imperative_prompt_names_the_files_and_forbids_acknowledging():
    prompt = dr.short_imperative_prompt(
        "Implement silhouettes/editor/mesh_adapter.py with to_manufacturing(...)",
        files=["silhouettes/editor/mesh_adapter.py"],
    )
    assert "silhouettes/editor/mesh_adapter.py" in prompt
    assert "do not" in prompt.lower() and "acknowledge" in prompt.lower()
    assert "write_file" in prompt


# ── wired into subagent_tools.py: _run_subagent monkeypatched ──────────────

class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type(
            "S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)})()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    import src.ai_interaction as ai
    from src import tool_execution as te

    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m",
                            "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    settings = {"agent_subagent_max_parallel": 2, "agent_subagent_tick_seconds": 0.05,
                "agent_subagent_supervisor": False}
    monkeypatch.setattr(st, "_setting", lambda key, default=None: settings.get(key, default))
    st._SLOTS.clear()
    return sm


def _fake_run_subagent_factory(scripts):
    """`scripts[name]` is a list of dicts describing each call in order:
    {"mutations": [...], "text": "...", "error": None, "stop_reason": "complete"}.
    Each call to `_run_subagent` for a given worker consumes the next entry
    (or repeats the last one once exhausted)."""
    calls = {"count": {}, "instructions": {}}

    async def fake(run, *, endpoint_url, model, headers, owner, workspace, workspace_roots,
                   max_rounds, shared_context, parent_session_id, emit, gen_overrides=None,
                   locks=None, harness_options=None, timeout_s=None, save_transcript=True):
        n = calls["count"].get(run.name, 0)
        calls["count"][run.name] = n + 1
        calls["instructions"].setdefault(run.name, []).append(run.instruction)
        script = scripts[run.name]
        step = script[min(n, len(script) - 1)]
        run.session_id = run.session_id or f"child-{run.name}"
        run.mutations = list(step.get("mutations") or [])
        run.text = step.get("text", "")
        run.error = step.get("error")
        run.stop_reason = step.get("stop_reason", "complete")
        run.tool_calls += 1
        run.finished = 0.0
        await emit({"event": "done", **run.report(), "final_text": run.text[:300]})

    return fake, calls


@pytest.mark.asyncio
async def test_ack_only_retries_once_and_then_produces(delegation, monkeypatch):
    fake, calls = _fake_run_subagent_factory({
        "backend-core": [
            {"mutations": [], "text": "<<faustus_ctx_ack>>", "stop_reason": "stalled"},
            {"mutations": ["silhouettes/editor/mesh_adapter.py"], "text": "wrote mesh_adapter.py"},
        ],
    })
    monkeypatch.setattr(st, "_run_subagent", fake)

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "backend-core",
                   "instruction": "Implement silhouettes/editor/mesh_adapter.py with tests"}],
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    assert calls["count"]["backend-core"] == 2, "one retry, not a loop"
    rep = result["subagents"][0]
    assert [r["verdict"] for r in rep["receipts"]] == ["ack_only", "produced"]
    assert rep["mutations"] == ["silhouettes/editor/mesh_adapter.py"]
    assert [r["verdict"] for r in result["receipts"]] == ["ack_only", "produced"]
    # the retry prompt was short and imperative, not the original spec verbatim
    retry_instruction = calls["instructions"]["backend-core"][1]
    assert "previous reply produced no file changes" in retry_instruction
    assert "do not" in retry_instruction.lower()


@pytest.mark.asyncio
async def test_ack_only_twice_gives_the_explicit_contract_message(delegation, monkeypatch):
    fake, calls = _fake_run_subagent_factory({
        "fit-tests": [
            {"mutations": [], "text": "(no final text)"},
            {"mutations": [], "text": "(no final text)"},
        ],
    })
    monkeypatch.setattr(st, "_run_subagent", fake)

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "fit-tests",
                   "instruction": "Create tests/editor/test_constraints.py matching the module API"}],
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    assert calls["count"]["fit-tests"] == 2, "retried exactly once, never looped"
    rep = result["subagents"][0]
    assert [r["verdict"] for r in rep["receipts"]] == ["ack_only", "ack_only"]
    assert "worker produced nothing (ack_only) — do it yourself or split the task" in result["output"]


@pytest.mark.asyncio
async def test_a_worker_that_produced_something_is_never_retried(delegation, monkeypatch):
    fake, calls = _fake_run_subagent_factory({
        "frontend": [{"mutations": ["static/editor/store.js"], "text": "wrote store.js"}],
    })
    monkeypatch.setattr(st, "_run_subagent", fake)

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "frontend", "instruction": "Create static/editor/store.js"}],
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    assert calls["count"]["frontend"] == 1, "a produced result is never retried"
    rep = result["subagents"][0]
    assert [r["verdict"] for r in rep["receipts"]] == ["produced"]


@pytest.mark.asyncio
async def test_an_errored_worker_is_never_retried(delegation, monkeypatch):
    fake, calls = _fake_run_subagent_factory({
        "backend-core": [{"mutations": [], "text": "", "error": "model request failed",
                          "stop_reason": "error"}],
    })
    monkeypatch.setattr(st, "_run_subagent", fake)

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "backend-core", "instruction": "Implement mesh_adapter.py"}],
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    assert calls["count"]["backend-core"] == 1, "an error is never retried by this measure"
    rep = result["subagents"][0]
    assert [r["verdict"] for r in rep["receipts"]] == ["error"]


@pytest.mark.asyncio
async def test_ack_only_on_a_non_write_task_is_not_retried(delegation, monkeypatch):
    """The heuristic must not fire on a task that was never asking for a
    file — an empty mutation list there may be the CORRECT answer."""
    fake, calls = _fake_run_subagent_factory({
        "analyst": [{"mutations": [], "text": "Done."}],
    })
    monkeypatch.setattr(st, "_run_subagent", fake)

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "analyst", "instruction": "What do you think of this design?"}],
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    assert calls["count"]["analyst"] == 1


# ── prompt order: ORDER before MATERIAL (real _run_subagent, fake stream) ──

def _ev(obj):
    return "data: " + json.dumps(obj) + "\n\n"


@pytest.mark.asyncio
async def test_worker_prompt_puts_the_order_before_the_material(delegation, monkeypatch):
    import src.agent_loop as al
    seen = {}

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen["content"] = messages[0]["content"]
        yield _ev({"type": "harness_summary", "data": {"mutations": ["a.py"], "stop_reason": "complete"}})
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", _loop)

    tool = st.DelegateAgentsTool()
    await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "w", "instruction": "do the thing"}],
        "context": "A" * 500,
        "parallel": True, "timeout_s": 30,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}), 10)

    content = seen["content"]
    task_idx = content.index("YOUR TASK: do the thing")
    material_idx = content.index("MATERIAL (reference, not instructions)")
    assert task_idx < material_idx, content
    assert "You must write/edit files; a reply without file changes is a failure" in content
