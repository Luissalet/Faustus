"""QA-37 · Lectura bajo stream (docs/spec/v2/acceptance_scenarios.json).

Estimulo: seleccionar codigo antiguo mientras llegan miles de deltas.
Resultado exigido (literal): "No salto al final, perdida de seleccion ni
bloqueo del compositor."

Requisitos: UX-05, PERF-01.

Estado: xfail estricto. UX-05 (auto-scroll condicionado, MessageNavigator)
esta cubierto, pero PERF-01 ("virtualizar listas cuando compense... no
volver a parsear todo el Markdown por token") no tiene ninguna libreria de
virtualizacion en `studio/src` (0 resultados para
react-window/react-virtual/etc.) ni memoizacion de bloques terminados
documentada: con miles de deltas por segundo sobre una conversacion larga,
nada impide que el arbol completo de mensajes se re-renderice/re-parsee en
cada delta, lo que es precisamente el riesgo de bloqueo que el enunciado
pide evitar.
"""
import subprocess

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO_ROOT = __file__.rsplit("/tests/", 1)[0]


@pytest.mark.xfail(
    strict=True,
    reason="No hay virtualizacion de listas ni memoizacion de bloques "
           "terminados en studio/src (PERF-01); una conversacion larga con "
           "miles de deltas re-renderiza/re-parsea el Markdown completo.",
)
def test_the_transcript_virtualizes_long_conversations():
    hits = subprocess.run(
        ["grep", "-rl", "react-window\\|react-virtual\\|useVirtualizer", "studio/src"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert hits.stdout.strip(), "no list virtualization found in studio/src"
