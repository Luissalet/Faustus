"""QA-28 · Cambio de modelo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: cambiar a modelo sin vision/tool nativo durante una tarea.
Resultado exigido (literal): "Recalcula capacidades, reconstruye estado y no
simula capacidades perdidas."

Requisitos: MOD-06, MEDIA-01.

Estado: green (lote 40). `src/agent_loop.py::recompute_capabilities_on_model_switch`
(nuevo) + `src/model_calibration.py::diff` (nuevo) leen el manifiesto
tested/announced de MOD-01/MOD-02 — nunca una probe en vivo ni una
heurística por nombre — y `stream_agent_loop`'s propia rama `"fallback"`
(la que ya recalculaba `_is_api_model`/`_resolve_tool_blocks` por-ronda para
la transición nativa->fence, y `_render_tool_result_content`'s
`vision_capable` para no fingir imágenes — ambos mecanismos preexistentes,
verificados aquí, no reescritos) ahora además emite un evento
`capabilities_changed` con `lost` cuando el modelo que de verdad responde
cambia a mitad de tarea. La otra mitad de MOD-06 — el cambio de modelo ENTRE
turnos — ya estaba cerrada por `routes/session_routes.py`'s
`PATCH /session/{sid}` (tests/test_session_model_switch_capabilities.py,
lote 20); este archivo prueba únicamente la mitad DENTRO de una llamada en
curso, la que MAPA_REUTILIZACION.md señalaba como pendiente para MOD-06.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import agent_loop
from src import model_calibration as mcal
from src.foreground_model_routing import FOREGROUND_AVAILABILITY_STATUSES

pytestmark = pytest.mark.qa_state("green")


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def test_recompute_capabilities_on_model_switch_exists_and_is_manifest_only(monkeypatch, tmp_path):
    assert hasattr(agent_loop, "recompute_capabilities_on_model_switch")
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    # Neither model has ever been announced/calibrated: honest empty result,
    # never a guess from the model's name (the old `_agent_route_tool_mode`
    # keyword heuristic this recompute deliberately does NOT reuse).
    result = agent_loop.recompute_capabilities_on_model_switch(
        previous_model="rich-model", previous_endpoint_url="https://api.openai.com/v1",
        new_model="plain-model", new_endpoint_url="https://api.openai.com/v1",
    )
    assert result["lost"] == []


def test_switching_to_a_model_without_vision_or_native_tools_mid_task_recomputes_and_tells_the_client(
    monkeypatch, tmp_path,
):
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
            # Mid-task: round 1 answers normally, no tool call yet. (No
            # trailing period on purpose: it reads as unfinished, so the
            # reliability harness continues to round 2 instead of treating
            # a bare sentence as a final answer — see MAPA_REUTILIZACION's
            # own tests/test_foreground_model_routing.py fixtures for the
            # same convention.)
            yield 'data: {"delta": "Let me check that now"}\n\n'
        else:
            # The model actually answering the task just changed.
            yield (
                'data: {"type": "fallback", "selected_model": "selected-model", '
                '"answered_by": "backup-model", "candidate_index": 1, '
                '"selected_endpoint_id": "selected-ep", "selected_endpoint_label": "Selected", '
                '"selected_endpoint_cost_tracked": false, "answered_by_endpoint_id": "backup-ep", '
                '"answered_by_endpoint_label": "Backup", "answered_by_endpoint_cost_tracked": true}\n\n'
            )
            yield 'data: {"delta": "Done without vision or native tools."}\n\n'
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
    assert len(cap_events) == 1, "the client is never left to notice a lost capability by itself"
    data = cap_events[0]["data"]
    assert data["from_model"] == "selected-model"
    assert data["to_model"] == "backup-model"
    # "no simula capacidades perdidas": named explicitly, never silently assumed.
    assert set(data["lost"]) == {"vision", "native tool calling"}
    # Recompute is a manifest read, not a guess: the reported new capabilities
    # match exactly what was announced for the model actually answering now.
    assert data["capabilities"]["announced"]["capabilities"] == {"vision": False, "tools": False}
