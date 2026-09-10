"""QA-17 · Lote parcialmente aplicado (docs/spec/v2/acceptance_scenarios.json).

Estimulo: fallo de disco durante modificacion multiarquivo.
Resultado exigido (literal): "Journal y rollback/compensacion claros;
alcance aplicado registrado."

Requisitos: EDIT-02.

Estado: xfail estricto. `src/agent_tools/filesystem_tools.py::ApplyPatchTool`
escribe archivo a archivo sin ningun journal ni compensacion automatica: si
el disco falla a mitad de un lote multiarchivo, los archivos ya escritos se
quedan escritos y los que faltan no, sin que nada registre "que alcance se
aplico" ni dispare una compensacion. `src/workspace_checkpoints.py` solo
ofrece `checkpoint()`/`restore()` de todo el turno (invocado por
`src/dispatch.py` antes del job) - una restauracion manual posterior, no una
compensacion automatica por lote (EDIT-02 parcial segun
docs/spec/v2/MAPA_REUTILIZACION.md: "no hay journal/compensacion por archivo
dentro de un lote... solo checkpoint/restore de todo el turno").
"""
import pytest

from src.agent_tools.filesystem_tools import ApplyPatchTool

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="ApplyPatchTool no lleva journal ni compensacion por archivo dentro "
           "de un lote multiarchivo; un fallo de disco a mitad de la escritura "
           "no deja registro del alcance aplicado ni dispara rollback automatico.",
)
def test_apply_patch_tool_journals_partial_scope_on_a_multi_file_failure():
    assert hasattr(ApplyPatchTool, "journal") or hasattr(ApplyPatchTool, "applied_scope")
