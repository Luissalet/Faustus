"""Lot 40 (QA-28/MOD-06), point 4 — recomputing capabilities on a mid-task
model switch.

`src/model_calibration.py::diff` is the new shared authority behind both
`routes/session_routes.py`'s existing between-turn `PATCH /session/{sid}`
(MOD-06's already-closed half, MAPA_REUTILIZACION) and this lot's new
`src/agent_loop.py::recompute_capabilities_on_model_switch`, wired into the
SAME `"fallback"` branch `test_toolless_multi_round_agent_persists_round_route_provenance`
(tests/test_foreground_model_routing.py, not owned by this lot) already
exercises for round/route bookkeeping — this file adds the capability-loss
angle only.

Revert proof (COMUN.md rule 5): with the `_cap_switch`/`capabilities_changed`
block this lot added to that "fallback" branch removed (`cp`-backed, never
git), `test_fallback_mid_task_emits_capabilities_changed_with_lost` fails —
no `capabilities_changed` event is yielded at all.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_loop as agent_loop
import src.model_calibration as mcal
from src.foreground_model_routing import FOREGROUND_AVAILABILITY_STATUSES


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


# ── model_calibration.diff ───────────────────────────────────────────────

def test_diff_reports_capabilities_the_new_model_does_not_announce():
    previous = {"announced": {"capabilities": {"vision": True, "tools": True}}}
    new = {"announced": {"capabilities": {"vision": False, "tools": False}}}
    assert mcal.diff(previous, new) == ["vision", "native tool calling"]


def test_diff_never_invents_a_loss_for_an_uncalibrated_model():
    """Neither side ever announced anything — honest empty diff, not a guess
    from the model's name (this module's own contract, see its docstring)."""
    assert mcal.diff({}, {}) == []


def test_diff_reports_nothing_when_the_new_model_keeps_every_capability():
    previous = {"announced": {"capabilities": {"vision": True}}}
    new = {"announced": {"capabilities": {"vision": True, "tools": True}}}
    assert mcal.diff(previous, new) == []


# ── agent_loop.recompute_capabilities_on_model_switch ────────────────────

def test_recompute_reads_the_manifest_store_and_reports_the_loss(monkeypatch, tmp_path):
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    prev_key = mcal.manifest_key(vendor="openai", model_id="rich-model", endpoint_id="ep-a")
    new_key = mcal.manifest_key(vendor="openai", model_id="plain-model", endpoint_id="ep-b")
    mcal.save_announced(prev_key, {"capabilities": {"vision": True, "tools": True}})
    mcal.save_announced(new_key, {"capabilities": {"vision": False, "tools": False}})

    result = agent_loop.recompute_capabilities_on_model_switch(
        previous_model="rich-model",
        previous_endpoint_url="https://api.openai.com/v1",
        new_model="plain-model",
        new_endpoint_url="https://api.openai.com/v1",
        previous_endpoint_id="ep-a",
        new_endpoint_id="ep-b",
    )

    assert result["lost"] == ["vision", "native tool calling"]
    assert result["capabilities"]["announced"]["capabilities"]["tools"] is False


def test_recompute_reports_nothing_lost_for_two_uncalibrated_models(monkeypatch, tmp_path):
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    result = agent_loop.recompute_capabilities_on_model_switch(
        previous_model="never-seen-a", previous_endpoint_url="https://api.openai.com/v1",
        new_model="never-seen-b", new_endpoint_url="https://api.openai.com/v1",
    )
    assert result == {"capabilities": result["capabilities"], "lost": []}


# ── stream_agent_loop wiring: a real mid-task fallback ───────────────────

def test_fallback_mid_task_emits_capabilities_changed_with_lost(monkeypatch, tmp_path):
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    prev_key = mcal.manifest_key(vendor="openai", model_id="selected-model", endpoint_id="selected-ep")
    new_key = mcal.manifest_key(vendor="openai", model_id="backup-model", endpoint_id="backup-ep")
    mcal.save_announced(prev_key, {"capabilities": {"vision": True, "tools": True}})
    mcal.save_announced(new_key, {"capabilities": {"vision": False, "tools": False}})

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)

    calls = 0

    async def fake_stream(candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta": "Let me check that now"}\n\n'
        else:
            yield (
                'data: {"type": "fallback", "selected_model": "selected-model", '
                '"answered_by": "backup-model", "candidate_index": 1, '
                '"selected_endpoint_id": "selected-ep", "selected_endpoint_label": "Selected", '
                '"selected_endpoint_cost_tracked": false, "answered_by_endpoint_id": "backup-ep", '
                '"answered_by_endpoint_label": "Backup", "answered_by_endpoint_cost_tracked": true}\n\n'
            )
            yield 'data: {"delta": "final answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "selected-model",
        [{"role": "user", "content": "Please investigate."}],
        max_rounds=3, relevant_tools=set(),
        fallbacks=[("https://api.openai.com/v1", "backup-model", {})],
        route_descriptors=[
            {"endpoint_id": "selected-ep", "endpoint_label": "Selected", "endpoint_cost_tracked": False},
            {"endpoint_id": "backup-ep", "endpoint_label": "Backup", "endpoint_cost_tracked": True},
        ],
        fallback_statuses=FOREGROUND_AVAILABILITY_STATUSES,
        fallback_on_empty=False,
        _is_teacher_run=True,
    ))

    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass

    cap_events = [e for e in events if e.get("type") == "capabilities_changed"]
    assert len(cap_events) == 1
    data = cap_events[0]["data"]
    assert data["from_model"] == "selected-model"
    assert data["to_model"] == "backup-model"
    assert data["lost"] == ["vision", "native tool calling"]
    assert cap_events[0]["round"] == 2


def test_fallback_mid_task_with_no_capability_loss_stays_quiet(monkeypatch, tmp_path):
    """Both models fully calibrated and equal (or neither calibrated at all,
    the honest-empty case): no `capabilities_changed` noise."""
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)

    calls = 0

    async def fake_stream(candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta": "Let me check that now"}\n\n'
        else:
            yield (
                'data: {"type": "fallback", "selected_model": "selected-model", '
                '"answered_by": "backup-model", "candidate_index": 1, '
                '"selected_endpoint_id": "selected-ep", "selected_endpoint_label": "Selected", '
                '"selected_endpoint_cost_tracked": false, "answered_by_endpoint_id": "backup-ep", '
                '"answered_by_endpoint_label": "Backup", "answered_by_endpoint_cost_tracked": true}\n\n'
            )
            yield 'data: {"delta": "final answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "selected-model",
        [{"role": "user", "content": "Please investigate."}],
        max_rounds=3, relevant_tools=set(),
        fallbacks=[("https://api.openai.com/v1", "backup-model", {})],
        route_descriptors=[
            {"endpoint_id": "selected-ep", "endpoint_label": "Selected", "endpoint_cost_tracked": False},
            {"endpoint_id": "backup-ep", "endpoint_label": "Backup", "endpoint_cost_tracked": True},
        ],
        fallback_statuses=FOREGROUND_AVAILABILITY_STATUSES,
        fallback_on_empty=False,
        _is_teacher_run=True,
    ))

    assert not any('"type": "capabilities_changed"' in chunk for chunk in chunks)
