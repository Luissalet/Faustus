"""QA-17 · Lote parcialmente aplicado (docs/spec/v2/acceptance_scenarios.json).

Estimulo: fallo de disco durante modificacion multiarquivo.
Resultado exigido (literal): "Journal y rollback/compensacion claros;
alcance aplicado registrado."

Requisitos: EDIT-02.

Estado: GREEN (lote 20, integrando el trabajo real del lote 19).
`src/edit_journal.py::EditJournal`/`apply_batch` registra la intencion de un
lote multiarchivo (ruta y bytes previos de cada objetivo) ANTES de escribir
nada, aplica cada operacion en orden y, ante el primer `OSError`, compensa
cada archivo ya aplicado devolviendolo a sus bytes previos (o borrandolo, si
no existia antes del lote). `ApplyPatchTool`
(src/agent_tools/filesystem_tools.py) ejecuta su fase de escritura a traves
de este journal, asi que un fallo de disco a mitad de un patch multiarchivo
deja un recibo con exactamente que se aplico, donde se detuvo y si el propio
rollback tuvo exito — nunca una escritura parcial silenciosa. La suite real
esta en tests/test_edit_journal.py (`EditJournal.apply_batch` en aislado, mas
el escenario end-to-end de `ApplyPatchTool` reproduciendo justo el estimulo
de este QA); este test reutiliza ese mismo escenario end-to-end para no
duplicar la logica de fallo simulado.
"""
import pytest

from src.agent_tools import filesystem_tools as ft
from tests.test_edit_journal import _PATCH_3_FILES, _write3

pytestmark = pytest.mark.qa_state("green")


@pytest.mark.asyncio
async def test_apply_patch_tool_journals_and_compensates_a_mid_batch_disk_failure(tmp_path, monkeypatch):
    """QA-17's literal stimulus: a disk failure partway through a multi-file
    write. The receipt must say what landed (`applied`), where it stopped
    (`failed_at`), and whether the compensation itself succeeded
    (`rolled_back`) — EDIT-02's required shape."""
    monkeypatch.chdir(tmp_path)
    files = _write3(tmp_path)

    calls = {"n": 0}
    orig_write = ft._write_text_lf

    def failing_write(path, text, crlf):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated disk failure on third file")
        return orig_write(path, text, crlf)

    monkeypatch.setattr(ft, "_write_text_lf", failing_write)

    res = await ft.ApplyPatchTool().execute(_PATCH_3_FILES, {})

    assert res["exit_code"] == 1
    assert res["status"] == "partial"
    journal = res["journal"]
    # "alcance aplicado registrado": the receipt says exactly what happened,
    # not a generic failure.
    assert set(journal.keys()) >= {"applied", "failed_at", "rolled_back"}
    assert journal["failed_at"] == str(files["c.txt"])
    # "rollback/compensacion claros": the two files already written before
    # the failure were put back to their pre-batch content.
    assert journal["rolled_back"] is True
    assert journal["applied"] == []
    assert files["a.txt"].read_text() == "A1\n"
    assert files["b.txt"].read_text() == "B1\n"
