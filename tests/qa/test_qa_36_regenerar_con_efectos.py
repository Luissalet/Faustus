"""QA-36 · Regenerar con efectos (docs/spec/v2/acceptance_scenarios.json).

Estimulo: regenerar resumen de una accion externa completada.
Resultado exigido (literal): "Nueva redaccion usa evidencia, no repite
accion."

Requisitos: UX-03.

Estado: xfail estricto. `docs/spec/v2/Faustus_Especificacion_Integral_v2.md`
describe UX-03 ("regenerar una respuesta que informo de un correo enviado
no envia ese correo de nuevo") como P1, pero no hay ninguna ruta
`/api/.../regenerate` ni funcion equivalente en `routes/chat_routes.py` o
`src/agent_loop.py` (0 resultados para "regenerate"): el boton "Regenerar"
de Transcript.tsx (`IconButton RefreshCw`, ver MAPA_REUTILIZACION UX-08) solo
reintenta el turno, lo que en un turno con tool calls con efectos re-envia
la ronda del modelo entera - sin garantia de que un correo ya enviado no se
repita.
"""
import subprocess

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO_ROOT_MARKER = __file__.rsplit("/tests/", 1)[0]


@pytest.mark.xfail(
    strict=True,
    reason="No existe un endpoint/funcion de 'regenerar' dedicada que "
           "reutilice la evidencia de una accion externa ya completada sin "
           "repetirla; el boton Regenerar en Transcript.tsx solo reintenta "
           "el turno completo.",
)
def test_a_dedicated_regenerate_path_reuses_evidence_without_repeating_effects():
    # Scoped to the chat turn/agent loop specifically - routes/task/task_routes.py
    # has an unrelated "webhook-regenerate" (a token rotation), which a looser
    # grep would wrongly match.
    hits = subprocess.run(
        ["grep", "-rlE", r"def regenerate_turn|regenerate_response_reusing_evidence",
         "routes/chat_routes.py", "src/agent_loop.py"],
        cwd=REPO_ROOT_MARKER, capture_output=True, text=True,
    )
    assert hits.stdout.strip(), "no dedicated regenerate-with-evidence path found"
