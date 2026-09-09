# -*- coding: utf-8 -*-
"""A brief written as an outline keeps its headings as the report's sections.

Luis's whiplash guide (09-09-2026): 44 bullets under "Tratamiento",
"Ejercicio y progresión", "Pronóstico y seguimiento", "Qué evitar",
"Algoritmo clínico" and "Tabla final de consulta rápida". Flattened and
capped at 12, the report covered the first twelve bullets and stopped at
"Movilidad cervical". Grouped by heading it is 15 sections, each heading one
## with its bullets covered inside.
"""
from src.deep_research import DeepResearcher, _outline_sections

WAD_BRIEF = """\
Soy fisioterapeuta y quiero elaborar una guía práctica sobre el whiplash (WAD).

Quiero que abarque:

- Qué es el whiplash y qué mecanismos pueden estar implicados.
- Clasificación de los WAD y características clínicas de cada grado.
- Signos de alarma y criterios claros de derivación médica.

Tratamiento

Desarrolla el tratamiento basado en la evidencia:

- Educación y explicación al paciente.
- Ejercicio terapéutico.
- Terapia manual.

Para cada intervención quiero saber qué utilidad tiene, cuándo utilizarla, cómo dosificarla y qué calidad tiene la evidencia disponible.

Ejercicio y progresión

Este apartado es especialmente importante.

Organiza la rehabilitación por fases:

- Fase aguda.
- Fase subaguda.

Para cada fase especifica:

- Objetivos.
- Ejercicios recomendados.

Qué evitar

Incluye un apartado sobre errores frecuentes:

- Reposo excesivo.
- Uso prolongado de collarín.

Algoritmo clínico

Al final crea un algoritmo sencillo de valoración y toma de decisiones:

Paciente tras whiplash → clasificación/gravedad → valoración → tratamiento → retorno a actividad.

Tabla final de consulta rápida

Finaliza con una tabla que permita consultar rápidamente:

| Situación/fase | Qué valorar | Objetivos | Tratamiento |

Quiero que el documento sea muy práctico para un fisioterapeuta, evitando extenderse demasiado en biomecánica o fisiopatología que no tenga implicaciones clínicas.

Me interesa especialmente poder responder a:

¿Qué tengo que valorar?, ¿cómo sé si este paciente necesita derivación?, ¿qué ejercicios le mando?

Prioriza guías de práctica clínica, revisiones sistemáticas y metaanálisis recientes.
"""


def _dr():
    return object.__new__(DeepResearcher)


def test_the_headings_become_sections_and_their_bullets_ride_inside():
    subs = _dr()._extract_subquestions(WAD_BRIEF)
    titles = [s.split(":")[0] for s in subs]
    assert titles == [
        "Qué es el whiplash y qué mecanismos pueden estar implicados.",
        "Clasificación de los WAD y características clínicas de cada grado.",
        "Signos de alarma y criterios claros de derivación médica.",
        "Tratamiento",
        "Ejercicio y progresión",
        "Qué evitar",
        "Algoritmo clínico",
        "Tabla final de consulta rápida",
        "Me interesa especialmente poder responder a",
    ]
    treatment = subs[3]
    assert treatment == "Tratamiento: Educación y explicación al paciente; Ejercicio terapéutico; Terapia manual"
    # the long "Para cada intervención…" sentence is prose, not an item, and it ends the group
    assert "Para cada intervención" not in treatment


def test_lead_ins_inside_a_group_are_skipped_but_their_items_kept():
    subs = _dr()._extract_subquestions(WAD_BRIEF)
    exercise = subs[4]
    assert exercise.startswith("Ejercicio y progresión: ")
    assert "Organiza la rehabilitación por fases" not in exercise
    assert "Fase aguda; Fase subaguda; Objetivos; Ejercicios recomendados" in exercise


def test_an_arrow_chain_and_a_table_row_are_items_of_their_heading():
    subs = _dr()._extract_subquestions(WAD_BRIEF)
    assert subs[6].startswith("Algoritmo clínico: Paciente tras whiplash → clasificación/gravedad")
    assert subs[7] == "Tabla final de consulta rápida: | Situación/fase | Qué valorar | Objetivos | Tratamiento |"


def test_a_lead_in_followed_by_packed_questions_is_one_section_not_three():
    subs = _dr()._extract_subquestions(WAD_BRIEF)
    last = subs[-1]
    assert last.startswith("Me interesa especialmente poder responder a: ¿Qué tengo que valorar?")
    assert last.count("?") == 3
    assert not any(s.startswith("¿cómo sé") for s in subs)


def test_a_blank_line_between_every_line_changes_nothing():
    spaced = "\n\n".join(l for l in WAD_BRIEF.splitlines() if l.strip())
    assert _dr()._extract_subquestions(spaced) == _dr()._extract_subquestions(WAD_BRIEF)


def test_a_plain_bulleted_list_is_not_an_outline():
    flat = "Compare the options:\n- dosage of eccentric loading\n- expected recovery timeline\n"
    assert _outline_sections(flat) is None
    assert _dr()._extract_subquestions(flat) == ["dosage of eccentric loading", "expected recovery timeline"]


def test_one_heading_alone_is_not_an_outline_either():
    text = "Whiplash rehab\n\n- dosage\n- timeline\n\nThanks a lot for the detailed answer here."
    assert _outline_sections(text) is None


def test_the_sections_block_explains_grouped_items():
    r = _dr()
    r.subquestions = _dr()._extract_subquestions(WAD_BRIEF)
    r.category = ""
    block = r._sections_block()
    assert "4. Tratamiento: Educación" in block
    assert "ONE section headed" in block



def test_grouped_headings_lose_their_pasted_lists():
    """The 10-09 run wrote "## Tratamiento: Educación…; Ejercicio…" and
    "## Algoritmo clínico: Paciente tras whiplash → …" although told not to."""
    subs = _dr()._extract_subquestions(WAD_BRIEF)
    text = (
        "# Guía\n\n"
        "## Tratamiento: Educación y explicación al paciente; Ejercicio terapéutico; Terapia manual\n\ntexto\n\n"
        "## Algoritmo clínico: Paciente tras whiplash → clasificación/gravedad → valoración → tratamiento → retorno a actividad\n\nmás\n\n"
        "## Tabla final de consulta rápida: | Situación/fase | Qué valorar | Objetivos | Tratamiento |\n\n| a | b |\n\n"
        "## Me interesa especialmente poder responder a: ¿Qué tengo que valorar?, ¿cómo sé si este paciente necesita derivación?, ¿qué ejercicios le mando?\n\nfin\n\n"
        "## Síntomas habituales y síntomas asociados: dolor cervical, rigidez\n\nse queda"
    )
    tidy = DeepResearcher._tidy_grouped_headings(text, subs)
    heads = [l for l in tidy.splitlines() if l.startswith("## ")]
    assert heads == [
        "## Tratamiento",
        "## Algoritmo clínico",
        "## Tabla final de consulta rápida",
        "## Me interesa especialmente poder responder a",
        "## Síntomas habituales y síntomas asociados: dolor cervical, rigidez",   # not a group: the user's own colon
    ]
    assert "| a | b |" in tidy
