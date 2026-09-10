"""QA-29 · Privacidad transitiva (docs/spec/v2/acceptance_scenarios.json).

Estimulo: modo local con reranker o resumen remoto configurado.
Resultado exigido (literal): "Bloquea salida o pide cambio de politica; no
filtra por componente auxiliar."

Requisitos: SEC-04, MOD-05.

Estado: xfail estricto. `src/outbound_fetch.py`/`src/url_safety.py` bloquean
SSRF por-fetch (SEC-04 parcial), pero no existe un perfil "solo local"
unico que tambien cubra `src/rerank.py` o un resumidor remoto: activar
"modo local" en la configuracion del chat principal no impide que un
componente auxiliar (reranker, resumen) siga llamando a un endpoint remoto,
porque no hay una politica global auditada que los incluya a todos.
"""
import pytest

from src import endpoint_resolver

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="No existe un perfil 'local-only' unico que cubra tambien "
           "reranker/resumen remoto; el control de egress es por-fetch "
           "(outbound_fetch/url_safety), no una politica global auditable "
           "que bloquee componentes auxiliares cuando el modo local esta "
           "activo.",
)
def test_local_only_mode_blocks_every_auxiliary_remote_component():
    assert hasattr(endpoint_resolver, "local_only_policy_covers_auxiliaries")
