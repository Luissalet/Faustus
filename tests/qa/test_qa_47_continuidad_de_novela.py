"""QA-47 · Continuidad de novela (docs/spec/v2/acceptance_scenarios.json).

Estimulo: crear final alternativo, descartarlo y escribir capitulo
siguiente.
Resultado exigido (literal): "Canon original conservado; alternativa no se
recupera como hecho confirmado."

Requisitos: WRITE-02, WRITE-04.

Estado: xfail estricto. No existe en el repo ningun concepto de "canon" de
una obra narrativa distinto de "borrador descartado": ni `src/memory_engine.py`
ni `src/context_engine/` tienen un campo que marque un fragmento como
"alternativa explorada y descartada" frente a "hecho establecido de la
historia" (0 resultados para "canon" en `src/` fuera de dependencias de
terceros). Sin esa distincion, nada impide que un resumen/memoria posterior
recupere el final alternativo descartado como si fuera parte de la trama
confirmada.
"""
import subprocess

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO_ROOT = __file__.rsplit("/tests/", 1)[0]


@pytest.mark.xfail(
    strict=True,
    reason="No hay ningun mecanismo de 'canon narrativo' vs 'alternativa "
           "descartada' en src/memory_engine.py ni src/context_engine/; un "
           "final alternativo descartado no esta marcado para no resucitar "
           "como hecho confirmado en un resumen posterior.",
)
def test_a_discarded_alternate_ending_is_marked_and_never_resurfaces_as_canon():
    hits = subprocess.run(
        ["grep", "-rl", "canon", "src/memory_engine.py", "src/context_engine"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert hits.stdout.strip(), "no canon-vs-discarded-alternative marker found"
