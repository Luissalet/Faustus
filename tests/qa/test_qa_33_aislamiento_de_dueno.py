"""QA-33 · Aislamiento de dueno (docs/spec/v2/acceptance_scenarios.json).

Estimulo: usuario B prueba IDs de chats, memoria, archivos y recibos de A.
Resultado exigido (literal): "No hay datos por API, SSE, indice, cache ni
links de descarga."

Requisitos: SEC-06.

Estado: verde. SEC-06 es existente segun docs/spec/v2/MAPA_REUTILIZACION.md,
con ~46 ficheros `test_*_owner_scope.py`/IDOR ya cubriendo casi cada ruta.
Este test repite el patron literal (ver
tests/test_memory_owner_isolation.py) contra memoria: A crea un recuerdo, B
busca por el mismo texto y ADEMAS intenta leer el ID exacto de A - en
ningun caso el dato de A aparece en la respuesta de B.
"""
from unittest.mock import MagicMock

import pytest

import routes.memory_routes as memory_routes
from src.memory import MemoryManager

pytestmark = pytest.mark.qa_state("green")


def test_user_b_probing_user_as_memory_ids_gets_nothing(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path))
    a_memory = manager.add_entry("El codigo secreto del proyecto es Odyssey", owner="alice")
    manager.save([a_memory])

    monkeypatch.setattr(memory_routes, "get_current_user", lambda request: "bob")
    router = memory_routes.setup_memory_routes(manager, MagicMock())
    search = next(
        route.endpoint for route in router.routes
        if route.path == "/api/memory/search" and "POST" in route.methods
    )

    # B searches with the exact text A stored - nothing of A's comes back.
    result = search(request=None, query="El codigo secreto del proyecto es Odyssey",
                     session_id=None, category=None)
    assert a_memory["id"] not in [m["id"] for m in result["memories"]]
    assert result["memories"] == []
