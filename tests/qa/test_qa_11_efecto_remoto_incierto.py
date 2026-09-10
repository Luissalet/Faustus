"""QA-11 · Efecto remoto incierto (docs/spec/v2/acceptance_scenarios.json).

Estimulo: API acepta correo simulado pero corta respuesta antes del recibo.
Resultado exigido (literal): "outcome_unknown; consultar por clave/ID o
revision humana, nunca reenvio automatico ciego."

Requisitos: CALL-06, CONN-02.

Estado: verde. `src/retry_policy.py::classify_http` (cuyo propio docstring
cita "QA-11") clasifica una conexion cortada a mitad de respuesta como
`RetryClass.OUTCOME_UNKNOWN` en vez de un simple "reintentar". Y
`src/contracts/tool.py::ToolResult` (el contrato de resultado de una tool
call, §34.5) rechaza construir un resultado `status="outcome_unknown"` sin
un `uncertainty` que explique el motivo Y la accion de reconciliacion - un
reenvio ciego no puede representarse sin decir como se resolvera.
"""
import httpx
import pytest

from src.contracts import ContractError
from src.contracts.tool import SPEC_V2_SCHEMA_VERSION, ToolResult
from src.retry_policy import RetryClass, classify_http

pytestmark = pytest.mark.qa_state("green")


def test_a_connection_cut_mid_response_classifies_as_outcome_unknown():
    # The email API accepted the request but the connection died while the
    # response (the receipt) was still streaming back.
    cut_mid_response = httpx.ReadTimeout("timed out waiting for the rest of the body")
    assert classify_http(exc=cut_mid_response) == RetryClass.OUTCOME_UNKNOWN


def test_outcome_unknown_can_never_be_represented_without_a_reconciliation_plan():
    # A blind auto-resend has no way to express itself: the contract refuses
    # an outcome_unknown result that does not say why and what would
    # reconcile it (query by key/id, or a human review).
    with pytest.raises(ContractError, match="uncertainty"):
        ToolResult.from_mapping({
            "schema_version": SPEC_V2_SCHEMA_VERSION,
            "call_id": "call_1", "attempt_id": "attempt_1", "status": "outcome_unknown",
            "output": None, "error": None, "uncertainty": None,
        })

    # The honest shape: the uncertainty names the reconciliation action - a
    # lookup by key/id or a human review - never a silent resend.
    result = ToolResult.from_mapping({
        "schema_version": SPEC_V2_SCHEMA_VERSION,
        "call_id": "call_1", "attempt_id": "attempt_1", "status": "outcome_unknown",
        "output": None, "error": None,
        "uncertainty": {
            "reason": "the connection was cut after the send API accepted the request "
                      "but before the receipt arrived",
            "reconcile_action": "query the mail provider for a message with our "
                                "idempotency key before ever resending",
        },
    })
    assert result.status == "outcome_unknown"
    assert "resending" in result.uncertainty.reconcile_action or "resend" in result.uncertainty.reconcile_action
