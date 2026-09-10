"""QA-18 · Rollback fuera de alcance (docs/spec/v2/acceptance_scenarios.json).

Estimulo: entre checkpoint y restore se envia correo y usuario edita otro
archivo.
Resultado exigido (literal): "Explica lo irreversible y conflicto ajeno; no
anuncia reversion universal."

Requisitos: EDIT-04.

Estado: xfail estricto. `src/workspace_checkpoints.py::status()` (probado
sin caso dedicado, EDIT-04 parcial segun MAPA_REUTILIZACION) solo devuelve
`{enabled, git, workspace, shadow_dir, present, size_mb, head, count}` - no
hay campo que liste que se restauraria, que archivos excluidos/binarios
quedarian fuera, ni un aviso de que un correo ya enviado (un efecto externo
irreversible) no se deshace. Sin ese campo, un restore no puede advertir del
conflicto ajeno del enunciado ni evitar anunciar una reversion universal que
no existe.
"""
import pytest

from src.workspace_checkpoints import status

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="workspace_checkpoints.status() no reporta que quedaria fuera del "
           "restore (excluidos/binarios/efectos irreversibles como un correo "
           "ya enviado); no hay aviso de alcance parcial antes de restaurar.",
)
def test_status_reports_what_would_be_out_of_scope_for_a_restore(tmp_path):
    result = status(str(tmp_path))
    assert "irreversible_effects" in result or "out_of_scope" in result
