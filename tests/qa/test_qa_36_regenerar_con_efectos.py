"""QA-36 · Regenerar con efectos (docs/spec/v2/acceptance_scenarios.json).

Estimulo: regenerar resumen de una accion externa completada.
Resultado exigido (literal): "Nueva redaccion usa evidencia, no repite
accion."

Requisitos: UX-03.

Estado: green (lote 40). `POST /api/chat/regenerate/{sid}`
(routes/chat_routes.py, nuevo) reescribe la ultima respuesta del asistente
reinyectando como contexto la evidencia (`tool_events`) del turno anterior
en vez de reejecutar la ronda: el turno regenerado NUNCA vuelve a llamar una
herramienta con efecto de la ronda que reescribe (se bloquea la aplicacion
entera de herramientas no probadamente de solo lectura para esa llamada,
reusando `src.tool_security.PLAN_MODE_READONLY_TOOLS` — la misma autoridad
que ya usa el modo plan, no un segundo mecanismo), mientras que las lecturas
siguen disponibles. La respuesta guardada lleva `regenerated_from` apuntando
al mensaje original.

Mismo patron de llamada directa al endpoint que
`tests/test_ask_user_answer_route.py` (extraer la funcion real del router y
invocarla con un `Request` sintetico) — cruza el codigo real de la ruta sin
un servidor vivo; el modelo se simula como en el resto de la suite.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import routes.chat_routes as chat_routes
from core.models import ChatMessage

pytestmark = pytest.mark.qa_state("green")


class _RouteRequest:
    def __init__(self):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")


def _regenerate_endpoint(monkeypatch, session, saved):
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(
        chat_routes, "save_assistant_response",
        lambda *a, **k: saved.append({"args": a, "kwargs": k}) or "msg-db-id",
    )
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(get_session=lambda sid: session, save_sessions=lambda: None),
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    return next(r.endpoint for r in router.routes if r.path == "/api/chat/regenerate/{sid}")


async def _drain(response):
    async for _ in response.body_iterator:
        pass


@pytest.mark.asyncio
async def test_regenerated_summary_reuses_evidence_and_never_resends_the_email(monkeypatch):
    """UX-03's own worked example: an already-sent email must not go out
    again just because the user wants a better-written recap of it."""
    history = [
        ChatMessage("user", "Send Bob the Q3 numbers by email and confirm."),
        ChatMessage("assistant", "Sent.", metadata={
            "_db_id": "orig-summary-1",
            "tool_events": [{
                "tool": "send_email",
                "desc": "send_email: to bob@example.com",
                "command": json.dumps({"to": "bob@example.com", "subject": "Q3 numbers", "body": "..."}),
                "output": "Email sent to bob@example.com (message id msg-42)",
                "exit_code": 0,
            }],
        }),
    ]
    session = SimpleNamespace(
        endpoint_url="https://selected.example/v1", model="selected-model", headers={},
        name="test", history=history, add_message=lambda m: None,
    )
    saved = []
    endpoint = _regenerate_endpoint(monkeypatch, session, saved)

    email_spy_calls = []

    async def fake_agent_stream(endpoint_url, model, messages, **kwargs):
        if "send_email" not in (kwargs.get("disabled_tools") or set()):
            email_spy_calls.append("would have re-sent the email")
        yield 'data: {"delta": "I already emailed Bob the Q3 numbers earlier; here is a clearer summary of what was sent."}\n\n'
        yield 'data: {"type": "metrics", "data": {"model": "selected-model"}}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", fake_agent_stream)

    response = await endpoint(_RouteRequest(), sid="qa36-sess")
    await _drain(response)

    # No repeated effect: the spy never fires.
    assert email_spy_calls == [], "regenerating must never repeat an already-completed external action"

    assert len(saved) == 1
    saved_text = saved[0]["args"][3]
    saved_metrics = saved[0]["args"][4]
    assert "already emailed" in saved_text  # new wording, informed by the evidence
    assert saved_metrics["regenerated_from"] == "orig-summary-1"


@pytest.mark.asyncio
async def test_reads_stay_available_while_regenerating(monkeypatch):
    """"las lecturas sí pueden repetirse": only non-read-proven tools are
    blocked, never a blanket "no tools at all"."""
    history = [
        ChatMessage("user", "Read report.txt and summarize it."),
        ChatMessage("assistant", "Summary: ...", metadata={
            "_db_id": "orig-summary-2",
            "tool_events": [{
                "tool": "read_file", "desc": "read_file: report.txt",
                "command": json.dumps({"path": "report.txt"}),
                "output": "report contents", "exit_code": 0,
            }],
        }),
    ]
    session = SimpleNamespace(
        endpoint_url="https://selected.example/v1", model="selected-model", headers={},
        name="test", history=history, add_message=lambda m: None,
    )
    saved = []
    endpoint = _regenerate_endpoint(monkeypatch, session, saved)

    captured = {}

    async def fake_agent_stream(endpoint_url, model, messages, **kwargs):
        captured["disabled_tools"] = kwargs.get("disabled_tools")
        yield 'data: {"delta": "A clearer summary of report.txt."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", fake_agent_stream)

    response = await endpoint(_RouteRequest(), sid="qa36-sess-2")
    await _drain(response)

    assert "read_file" not in captured["disabled_tools"]
    assert "write_file" in captured["disabled_tools"]  # never-used-before effect tools stay blocked too
