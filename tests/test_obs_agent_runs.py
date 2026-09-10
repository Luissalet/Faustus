"""Lote 21 — OBS-01 (trace_id/step_id/sequence/stream_id on every SSE event),
OBS-02 (the eight-phase canonical vocabulary + honest percent) and QA-09
(cursor-based replay) as implemented in src/agent_runs.py.

`_publish()` is the single choke point every SSE event of a detached run
passes through — from agent_loop.py's tool_start/tool_progress/tool_output/
delta/etc., from routes/chat_routes.py's vram_admission events, and from
this module's own queue_status — so these tests drive it directly rather
than standing up the whole agent loop, the same pattern
test_agent_run_log_replacement.py already uses for this module.
"""
import asyncio
import json

import pytest

from src import agent_runs


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: {"agent_runs_persist": False}.get(key, default), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()


async def _never():
    """A run body that never finishes on its own — the test drives _publish
    directly and tears the task down itself."""
    await asyncio.sleep(3600)
    yield "data: [DONE]\n\n"  # pragma: no cover - never reached


async def _quiesce(run):
    for t in (run.task, run.evict_task):
        if t is not None and not t.done():
            t.cancel()
    await asyncio.sleep(0.01)


def _tool_start(tool="bash", round_=1):
    return "data: " + json.dumps({"type": "tool_start", "tool": tool, "command": "x", "round": round_}) + "\n\n"


def _tool_progress(tool="bash", round_=1, tail="."):
    return "data: " + json.dumps({"type": "tool_progress", "tool": tool, "round": round_, "tail": tail}) + "\n\n"


def _delta(text):
    return "data: " + json.dumps({"delta": text}) + "\n\n"


def _decode(ev: str):
    assert ev.startswith("data: ")
    return json.loads(ev[6:].rstrip("\n"))


# ── OBS-01: trace_id / step_id / sequence / stream_id on every event ───────

@pytest.mark.asyncio
async def test_every_published_event_carries_the_observability_fields():
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start(round_=1))
    agent_runs._publish(run, _delta("hello"))

    d0, d1 = (_decode(e) for e in run.buffer)
    assert d0["trace_id"] == run.run_id
    assert d1["trace_id"] == run.run_id
    assert d0["stream_id"] == run.run_id
    assert d0["step_id"] == f"{run.run_id}:1"
    assert d0["schema_version"] == "2.0"
    # 1-based and growing, matching contracts.task.EventEnvelope.sequence's
    # own `minimum=1` contract.
    assert d0["sequence"] == 1
    assert d1["sequence"] == 2
    await _quiesce(run)


@pytest.mark.asyncio
async def test_step_id_advances_with_the_round_the_module_already_tracks():
    """step_id must FOLLOW the existing `round` authority (_observe_activity),
    not a second counter this change invents."""
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start(round_=1))
    agent_runs._publish(run, _tool_start(round_=2))
    assert run.round == 2

    d0, d1 = (_decode(e) for e in run.buffer)
    assert d0["step_id"] == f"{run.run_id}:1"
    assert d1["step_id"] == f"{run.run_id}:2"
    await _quiesce(run)


@pytest.mark.asyncio
async def test_an_already_present_field_is_never_overridden():
    """Additive means additive: a payload that (however unlikely) already
    carries one of these keys keeps its own value."""
    run = agent_runs.start("sid", _never())
    ev = "data: " + json.dumps({"type": "tool_start", "tool": "x", "sequence": 999}) + "\n\n"
    agent_runs._publish(run, ev)
    assert _decode(run.buffer[0])["sequence"] == 999
    await _quiesce(run)


def test_done_sentinel_is_never_augmented():
    assert agent_runs._augment_sse_fields("data: [DONE]\n\n", {"trace_id": "x"}) == "data: [DONE]\n\n"


def test_a_non_object_payload_passes_through_unchanged():
    assert agent_runs._augment_sse_fields("data: [1,2,3]\n\n", {"trace_id": "x"}) == "data: [1,2,3]\n\n"


def test_augmentation_preserves_the_compaction_prefix_check():
    """_compact_key's fast path string-matches on the raw payload; the
    augmented event must still start with it, or every long-running tool's
    progress compaction silently stops working."""
    ev = agent_runs._augment_sse_fields(_tool_progress(), {"trace_id": "abc"})
    assert ev.startswith(agent_runs._PROGRESS_PREFIX)
    assert agent_runs._compact_key(ev) is not None


# ── QA-09: sequence survives compaction, and it is what a client dedupes on ─

@pytest.mark.asyncio
async def test_a_compacted_progress_tick_keeps_its_slots_sequence():
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_progress(tail="1"))
    agent_runs._publish(run, _tool_progress(tail="2"))   # compacts into the same slot
    agent_runs._publish(run, _tool_start(tool="other"))  # a fresh, non-compactable event

    assert len(run.buffer) == 2, "the two progress ticks must still occupy ONE slot"
    d0, d1 = (_decode(e) for e in run.buffer)
    assert d0["tail"] == "2", "the slot holds the LATEST tick's content"
    assert d0["sequence"] == 1, "but the SAME sequence as the tick it replaced"
    assert d1["sequence"] == 2
    await _quiesce(run)


# ── QA-09: resume with a cursor replays only what is newer ─────────────────

@pytest.mark.asyncio
async def test_subscribe_with_a_cursor_skips_everything_up_to_it():
    run = agent_runs.start("sid", _never())
    for i in range(5):
        agent_runs._publish(run, _delta(str(i)))
    run.status = "done"   # replay-only path, no live tail to wait on

    replayed = [ev async for ev in agent_runs.subscribe("sid", run, from_sequence=3)]
    assert [_decode(e)["delta"] for e in replayed] == ["3", "4"]
    await _quiesce(run)


@pytest.mark.asyncio
async def test_subscribe_without_a_cursor_replays_everything_unchanged():
    """Compatibility: an old caller that never passes from_sequence gets
    EXACTLY today's full-replay behaviour."""
    run = agent_runs.start("sid", _never())
    for i in range(3):
        agent_runs._publish(run, _delta(str(i)))
    run.status = "done"

    replayed = [ev async for ev in agent_runs.subscribe("sid", run)]
    assert [_decode(e)["delta"] for e in replayed] == ["0", "1", "2"]
    await _quiesce(run)


@pytest.mark.asyncio
async def test_a_cursor_past_the_buffer_is_clamped_not_rejected():
    """The "snapshot+cursor" case: a client miles ahead of what the buffer
    holds gets a superset (nothing it hasn't seen, no error), not a 500 or
    an empty replay that silently drops what comes next."""
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _delta("0"))
    run.status = "done"

    replayed = [ev async for ev in agent_runs.subscribe("sid", run, from_sequence=999)]
    assert replayed == []   # nothing new to replay — not an error
    await _quiesce(run)


@pytest.mark.asyncio
async def test_a_negative_cursor_is_clamped_to_a_full_replay():
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _delta("0"))
    run.status = "done"

    replayed = [ev async for ev in agent_runs.subscribe("sid", run, from_sequence=-5)]
    assert len(replayed) == 1
    await _quiesce(run)


# ── OBS-02: the canonical phase vocabulary, without losing the old one ─────

@pytest.mark.asyncio
async def test_phase_keeps_its_old_values_while_phase_canonical_is_new():
    run = agent_runs.start("sid", _never())
    thinking_ev = "data: " + json.dumps({"thinking": True, "delta": "reasoning"}) + "\n\n"
    agent_runs._publish(run, thinking_ev)
    snap = agent_runs.activity_snapshot("sid")
    # `phase` is UNCHANGED from before this lot: still this module's own
    # word, not the new 8-value vocabulary — an old client reading `phase`
    # sees nothing it has never seen before.
    assert snap["phase"] == "thinking"
    assert snap["phase_raw"] == "thinking"
    # `phase_canonical` is the NEW, additive field.
    assert snap["phase_canonical"] == "generating"
    assert snap["phase_canonical"] in agent_runs.CANONICAL_PHASES
    await _quiesce(run)


@pytest.mark.asyncio
async def test_vram_admission_refines_the_canonical_phase_without_touching_phase():
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start())     # phase="tool" -> canonical="tool"
    assert agent_runs.activity_snapshot("sid")["phase_canonical"] == "tool"

    vram_ev = "data: " + json.dumps({
        "type": "vram_admission",
        "data": {"phase": "vram_blocked", "message": "does not fit"},
    }) + "\n\n"
    agent_runs._publish(run, vram_ev)

    snap = agent_runs.activity_snapshot("sid")
    assert snap["phase"] == "tool", "the legacy field must not be touched by a VRAM event"
    assert snap["phase_canonical"] == "admission"
    await _quiesce(run)


def test_every_known_ad_hoc_phase_maps_into_the_closed_canonical_set():
    for raw in ("starting", "queued", "waiting_model", "thinking", "writing", "tool",
                "research", "awaiting_user", "finishing", "vram_blocked",
                "unloading_model", "loading_model", "probing"):
        assert agent_runs._canonical_phase(raw) in agent_runs.CANONICAL_PHASES, raw


def test_an_unmapped_phase_stays_at_the_previous_bucket_rather_than_guessing():
    assert agent_runs._canonical_phase("some_future_phase_nobody_mapped_yet", previous="tool") == "tool"


# ── OBS-02: a % only when there is a measurable total, never invented ──────

@pytest.mark.asyncio
async def test_no_percent_is_ever_invented_without_a_measurable_total():
    """FAILS if some future change starts deriving a percentage from elapsed
    time or round count — the acceptance bar (OBS-02) is that a heartbeat
    never shows a number with nothing measurable behind it."""
    run = agent_runs.start("sid", _never())
    agent_runs._publish(run, _tool_start())
    agent_runs._publish(run, _delta("some text"))
    agent_runs._publish(run, "data: " + json.dumps({"type": "agent_step", "round": 3}) + "\n\n")

    snap = agent_runs.activity_snapshot("sid")
    assert "percent" not in snap
    await _quiesce(run)


@pytest.mark.asyncio
async def test_percent_reflects_the_real_done_over_total_todo_count():
    run = agent_runs.start("sid", _never())
    todos = [{"status": "done"}, {"status": "pending"}, {"status": "pending"}, {"status": "pending"}]
    ev = "data: " + json.dumps({"type": "progress_update", "round": 1, "todos": todos}) + "\n\n"
    agent_runs._publish(run, ev)

    assert agent_runs.activity_snapshot("sid")["percent"] == 25.0
    await _quiesce(run)


@pytest.mark.asyncio
async def test_percent_updates_as_todos_complete():
    run = agent_runs.start("sid", _never())
    def _ev(done):
        todos = [{"status": "done"}] * done + [{"status": "pending"}] * (4 - done)
        return "data: " + json.dumps({"type": "progress_update", "round": 1, "todos": todos}) + "\n\n"
    agent_runs._publish(run, _ev(1))
    assert agent_runs.activity_snapshot("sid")["percent"] == 25.0
    agent_runs._publish(run, _ev(4))
    assert agent_runs.activity_snapshot("sid")["percent"] == 100.0
    await _quiesce(run)
