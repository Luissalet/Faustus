"""QA-29 · Privacidad transitiva (docs/spec/v2/acceptance_scenarios.json).

Estimulo: modo local con reranker o resumen remoto configurado.
Resultado exigido (literal): "Bloquea salida o pide cambio de politica; no
filtra por componente auxiliar."

Requisitos: SEC-04, MOD-05.

Estado: verde. `src/privacy_policy.py` (nuevo) es el perfil unico
(`local_only|local_preferred|cloud_allowed`) que TODO auxiliar consulta antes
de mandar contenido fuera via `assert_outbound(component, destination)`. Bajo
`local_only`, un componente apuntado a un endpoint no local se bloquea con el
codigo `privacy.blocked_outbound` (taxonomia OBS-03, `src/contracts/errors.py`)
antes de la llamada de red, sea cual sea el auxiliar — no hay un camino que
filtre "por componente": `src/embedding_lanes.py::_build_custom_client`,
`src/chroma_client.py::get_chroma_client` y
`src/context_compactor.py::maybe_compact` (su resumidor remoto) consultan la
misma funcion. Ver tests/test_privacy_policy.py y
tests/test_privacy_policy_auxiliaries.py para la prueba unitaria y de
integracion de cada uno; este test reproduce el escenario QA-29 literal
directamente sobre `assert_outbound` para varios auxiliares a la vez, sin
mocks del componente completo.

Pendiente fuera de este lote (ver el informe del lote 41): el reranker real
vive en `src/rerank.py` (no en `services/search/*`, donde el mapa de
reutilizacion lo situaba) y no es un fichero PROPIO de este lote; el hueco
exacto (linea y codigo) queda documentado en el informe. Tampoco hay OCR ni
telemetria de salida en el repo para cablear.
"""
import pytest

from src import privacy_policy

pytestmark = pytest.mark.qa_state("green")


def test_local_only_blocks_every_auxiliary_regardless_of_which_one_is_configured():
    auxiliaries = (
        ("embeddings", "https://embeddings.example.com/v1"),
        ("context_compactor", "https://api.openai.com/v1/chat/completions"),
        ("chroma", "chroma.example.com:8100"),
    )
    for component, destination in auxiliaries:
        with pytest.raises(privacy_policy.PrivacyPolicyError) as exc_info:
            privacy_policy.assert_outbound(
                component, destination, profile=privacy_policy.PROFILE_LOCAL_ONLY,
            )
        assert exc_info.value.error_info.code == "privacy.blocked_outbound"
        assert exc_info.value.component == component


def test_local_only_does_not_block_a_locally_configured_auxiliary():
    privacy_policy.assert_outbound(
        "embeddings", "http://127.0.0.1:11434/v1", profile=privacy_policy.PROFILE_LOCAL_ONLY,
    )


def test_local_preferred_and_cloud_allowed_never_block_here():
    for profile in (privacy_policy.PROFILE_LOCAL_PREFERRED, privacy_policy.PROFILE_CLOUD_ALLOWED):
        privacy_policy.assert_outbound(
            "context_compactor", "https://api.openai.com/v1/chat/completions", profile=profile,
        )
