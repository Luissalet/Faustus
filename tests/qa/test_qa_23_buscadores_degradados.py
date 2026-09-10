"""QA-23 · Buscadores degradados (docs/spec/v2/acceptance_scenarios.json).

Estimulo: solo responde un motor; despues ninguno.
Resultado exigido (literal): "Aviso de cobertura y fallo diagnosticado; no
inventar fuentes ni completed sin evidencia."

Requisitos: WEB-01, OBS-03.

Estado: verde. `GET /api/search/health` (WEB-01, existente) expone
`services.search.providers.searxng_engine_health()`, que registra quien
respondio, quien fue suspendido/con timeout y quien se quedo callado (ver
tests/test_search_engine_health.py, nacido del incidente real del
09-09-2026: "every query returned 10 results and all ten came from bing").
Este test reproduce las dos fases del escenario - un unico motor
respondiendo, y luego ninguno - contra la ruta real y comprueba que
`single_engine` se activa y que el diagnostico nombra la causa, sin que la
ruta finja una lista de fuentes que no existe.
"""
from unittest.mock import MagicMock

import pytest

from routes.search import search_routes
from services.search import providers

pytestmark = pytest.mark.qa_state("green")


def _health_endpoint():
    router = search_routes.setup_search_routes(MagicMock())
    return next(r.endpoint for r in router.routes if r.path == "/api/search/health")


def test_single_engine_degradation_is_flagged_with_a_diagnosed_cause(monkeypatch):
    providers._LAST_ENGINE_WARNING = ""
    only_bing = {
        "results": [{"url": f"https://x/{i}", "title": "t", "content": "c", "engines": ["bing"]}
                    for i in range(10)],
        "unresponsive_engines": [["mojeek", "Suspended: access denied"]],
    }
    providers._record_engine_health("q", "bing,mojeek", only_bing)
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "bing,mojeek")

    import asyncio
    endpoint = _health_endpoint()
    body = asyncio.run(endpoint())

    assert body["single_engine"] is True
    assert body["last_call"]["answered"] == {"bing": 10}
    assert body["last_call"]["unresponsive"] == [{"engine": "mojeek", "reason": "Suspended: access denied"}]
    # No source was fabricated for the silent engine.
    assert "mojeek" not in body["last_call"]["answered"]


def test_no_engine_answering_is_diagnosed_not_hidden_behind_a_result_count(monkeypatch):
    providers._LAST_ENGINE_WARNING = ""
    nothing = {
        "results": [],
        "unresponsive_engines": [["bing", "timeout"], ["mojeek", "Suspended: access denied"]],
    }
    providers._record_engine_health("q2", "bing,mojeek", nothing)
    monkeypatch.setattr(providers, "_GENERAL_ENGINES", "bing,mojeek")

    import asyncio
    endpoint = _health_endpoint()
    body = asyncio.run(endpoint())

    assert body["last_call"]["answered"] == {}
    assert body["last_call"]["results"] == 0
    assert {u["engine"] for u in body["last_call"]["unresponsive"]} == {"bing", "mojeek"}
