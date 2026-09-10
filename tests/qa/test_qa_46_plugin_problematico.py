"""QA-46 · Plugin problematico (docs/spec/v2/acceptance_scenarios.json).

Estimulo: extension se cuelga al iniciar o solicita permisos nuevos.
Resultado exigido (literal): "Modo seguro, desactivacion y revision; nucleo
sigue usable."

Requisitos: OPS-05, TOOL-04.

Estado: xfail estricto. `src/crash_recovery.py::boot_scan` detecta runs
interrumpidos tras un crash y los tools/MCP se pueden desactivar
MANUALMENTE via `disabled_tools`, pero no hay un arranque automatico en
"modo seguro" cuando un servidor MCP se cuelga repetidamente al iniciar, ni
una desactivacion automatica del plugin/MCP responsable (OPS-05 parcial
segun docs/spec/v2/MAPA_REUTILIZACION.md: "no hay arranque en 'modo seguro'
automatico tras crashes repetidos ni desactivacion automatica de un
plugin/MCP corrupto").
"""
import inspect

import pytest

from src import mcp_manager

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="mcp_manager no auto-desactiva ni entra en modo seguro cuando un "
           "servidor MCP se cuelga repetidamente al iniciar; disabled_tools "
           "es solo una desactivacion manual del usuario.",
)
def test_a_repeatedly_crashing_mcp_server_is_automatically_disabled():
    source = inspect.getsource(mcp_manager)
    assert "safe_mode" in source or "auto_disable" in source
