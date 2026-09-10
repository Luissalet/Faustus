"""QA-45 · Actualizacion fallida (docs/spec/v2/acceptance_scenarios.json).

Estimulo: error a mitad de migracion de schema y corte de luz simulado.
Resultado exigido (literal): "Recuperacion consistente con backup, no base
vacia ni datos mezclados."

Requisitos: OPS-02, OPS-03.

Estado: xfail estricto. `scripts/update_database.py` aplica `ALTER TABLE`
condicionales SIN backup previo ni transaccion con rollback (OPS-02 parcial
segun docs/spec/v2/MAPA_REUTILIZACION.md: "no hay preflight de version,
backup antes de migrar, transaccion con rollback ni modo mantenimiento").
`src/backup_service.py` (OPS-03, existente) SI sabe hacer copias seguras de
sqlite y verificarlas, pero `update_database.py` no lo invoca: un corte de
luz a mitad de la migracion puede dejar el schema a medias sin que exista
ningun backup automatico anterior al intento con el que recuperar un estado
consistente.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="scripts/update_database.py no toma un backup (via "
           "src/backup_service.py) antes de migrar el schema ni envuelve la "
           "migracion en una transaccion con rollback explicito.",
)
def test_the_migration_script_backs_up_before_altering_the_schema():
    script = Path(__file__).resolve().parents[2] / "scripts" / "update_database.py"
    source = script.read_text(encoding="utf-8")
    assert "backup_service" in source
