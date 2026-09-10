"""QA-02 · Proyecto grande (docs/spec/v2/acceptance_scenarios.json).

Estimulo: repositorio con vendor, binarios y codigo propio; pedir un bug
localizado.
Resultado exigido (literal): "Indice respeta exclusiones y devuelve
definicion/callers/tests sin cargar el repositorio entero."

Requisitos: IDX-02, IDX-03, CTX-01.

Estado: xfail estricto. No existe en el repo un indice de simbolos que
resuelva "definicion" ni "callers" por nombre (0 resultados para
`def find_definition`/`def find_callers`/`ctags`/symbol index en `src/`).
`src/read_plan.py` pagina un archivo dado (offset/limit) y
`src/context_engine/code_index.py` no expone busqueda por simbolo tampoco -
ambos asumen que ya se sabe que archivo mirar. Sin un indice, no hay forma de
pedir "quien llama a esta funcion" sin leer archivos uno a uno, lo que
contradice literalmente "sin cargar el repositorio entero". IDX-02/IDX-03 ni
siquiera aparecen en docs/spec/v2/MAPA_REUTILIZACION.md (no forman parte de
los 100 P0 auditados), lo que confirma que no se llego a valorar su estado.
"""
import importlib

import pytest

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="No hay indice de simbolos (definicion/callers) en el repo; "
           "src/read_plan.py y src/context_engine/code_index.py exigen ya "
           "conocer el archivo. IDX-02/IDX-03 no estan implementados.",
)
def test_a_symbol_index_can_answer_definition_and_callers_without_a_full_scan():
    # A real symbol index would expose something like:
    #     from src.code_index import find_definition, find_callers
    #     find_definition("some_function")   # -> file:line, without reading
    #     find_callers("some_function")      # -> [file:line, ...]
    # Neither exists; even the module fails to provide the entry point.
    src_read_plan = importlib.import_module("src.read_plan")
    assert hasattr(src_read_plan, "find_definition")
    assert hasattr(src_read_plan, "find_callers")
