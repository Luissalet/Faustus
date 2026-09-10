"""QA-27 · Caida de nodo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: nodo remoto desaparece a mitad de generacion o render.
Resultado exigido (literal): "Diagnostico, liberacion/reconciliacion y
resultado parcial/incierto; no exito falso."

Requisitos: HW-06, MEDIA-04.

Estado: xfail estricto. `src/capability_registry.py` declara `remote_worker`
con `implemented: False` (ver tests/test_contracts_mcp_and_routes.py, donde
la propia aseveracion dice "one that genuinely has no code says so
instead") - no hay ningun nodo remoto real que ejecute inferencia o render y
cuya caida a mitad de trabajo haya que diagnosticar/reconciliar. La
reconciliacion que SI existe (`src/media_runs.py::poll`, MEDIA-04) es para
el backend LOCAL de ComfyUI perdiendo su job, no para un nodo remoto que
desaparece de la red.
"""
import pytest

from src import capability_registry as registry

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="capability_registry declara remote_worker con implemented=False: "
           "no hay ningun nodo de computo remoto real que reconciliar cuando "
           "desaparece a mitad de generacion o render.",
)
def test_a_remote_worker_node_is_a_real_implemented_backend():
    catalogue = registry.declared_backends() if hasattr(registry, "declared_backends") else {}
    remote = next((b for b in catalogue.get("backends", []) if b.get("id") == "remote_worker"), None)
    assert remote is not None and remote.get("implemented") is True
