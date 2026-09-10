"""QA-22 · Brief largo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: 44 vinetas agrupadas con tablas y preguntas finales.
Resultado exigido (literal): "Cobertura conserva todas; export tiene
secciones y tabla final, sin recorte arbitrario."

Requisitos: RES-01, RES-04.

Estado: verde. `src/deep_research.py::DeepResearcher._extract_subquestions`
(via `_outline_sections`, RES-01 parcial) es precisamente el mecanismo que
arreglo este incidente real (ver PENDIENTES.md #13 y
tests/test_deep_research_outline_sections.py::test_luis_s_real_whiplash_brief_is_fifteen_sections,
"used to come out as the first 12 bullets"). Este test usa el mismo brief
real (tests/fixtures/wad_brief_es.txt: 8 vinetas sueltas + 6 grupos con
cabecera + una tabla + un cierre de 7 preguntas) y comprueba que NINGUN
grupo se pierde y que la tabla y las preguntas finales sobreviven intactas -
el "sin recorte arbitrario" literal del enunciado. `RES-04`
(`_final_report_in_parts`, existente) ya esta cubierto end-to-end por
tests/test_deep_research_report_in_parts.py, que prueba que una seccion
fallida se sustituye por su encabezado + nota en vez de borrar el resto del
informe.
"""
from pathlib import Path

import pytest

from src.deep_research import DeepResearcher

pytestmark = pytest.mark.qa_state("green")


def _dr():
    return object.__new__(DeepResearcher)


def test_a_long_headed_brief_keeps_every_group_table_and_closing_questions():
    brief = (Path(__file__).parent.parent / "fixtures" / "wad_brief_es.txt").read_text(encoding="utf-8")
    sections = _dr()._extract_subquestions(brief)

    # Nothing was dropped: every heading the brief declared survives as its
    # own section, in order - not clipped at some fixed bullet count.
    titles = [s.split(":", 1)[0] for s in sections]
    assert titles[8:] == [
        "Tratamiento", "Ejercicio y progresión", "Pronóstico y seguimiento", "Qué evitar",
        "Algoritmo clínico", "Tabla final de consulta rápida",
        "Me interesa especialmente poder responder a",
    ]
    # The table survives inside its section...
    assert "| Situación/fase |" in sections[13]
    # ...and the closing block still carries all 7 questions, none merged
    # away or dropped.
    assert sections[-1].count("?") == 7
