"""RES-01 — no brief with nested headings or a long bullet list loses items
when it is turned into report sections.

Regression found while implementing this: `_outline_sections` grouped every
bullet under a heading into ONE string ("Heading: a; b; c…") and
`_clean_subquestions` then hard-truncated any entry longer than
`_MAX_SUBQUESTION_CHARS` (700) — silently dropping the tail of a long list.
Luis's real whiplash guide has headings with more than 700 characters of
bullets under them (docs/spec/v2/MAPA_REUTILIZACION.md, RES-01 row). The fix
(`_group_chunks`) splits an overlong group across "Heading (cont.)" chunks
instead of truncating.
"""
from src.deep_research import (
    DeepResearcher,
    MAX_GROUPED_SUBQUESTIONS,
    _MAX_SUBQUESTION_CHARS,
    _group_chunks,
    _outline_sections,
)


def _all_text(sections):
    return " ".join(sections)


def test_a_long_bullet_list_under_one_heading_keeps_every_bullet():
    # `_outline_sections` needs >= 2 groups to engage at all (one incidental
    # heading-like line in an otherwise flat prompt must not misfire) -- a
    # short second heading is enough to make this a realistic outline.
    items = [f"punto {i} sobre estiramientos cervicales y su tecnica de aplicacion"
             for i in range(1, 41)]
    text = "Tratamiento\n" + "\n".join(f"- {it}" for it in items) + "\n\nOtro tema\n- algo breve\n- algo mas"

    sections = _outline_sections(text)
    assert sections is not None

    joined = _all_text(sections)
    missing = [it for it in items if it not in joined]
    assert missing == [], f"{len(missing)} bullet(s) silently dropped: {missing[:3]}"
    # Every chunk stays within the cap `_clean_subquestions` enforces, so the
    # later truncation pass never has to cut a chunk (which would be a second
    # way to lose text even after grouping).
    assert all(len(s) <= _MAX_SUBQUESTION_CHARS for s in sections)
    # And it did need to split -- otherwise this test would not exercise the
    # bug at all.
    assert len([s for s in sections if s.startswith("Tratamiento")]) > 1


def test_the_same_list_through_clean_subquestions_still_keeps_every_bullet():
    """The full pipeline `_extract_subquestions` uses: outline grouping THEN
    the cap/dedup pass. This is what a real research run calls."""
    items = [f"punto {i} sobre estiramientos cervicales y su tecnica de aplicacion"
             for i in range(1, 41)]
    text = "Tratamiento\n" + "\n".join(f"- {it}" for it in items) + "\n\nOtro tema\n- algo breve\n- algo mas"
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")

    subs = r._extract_subquestions(text)

    joined = " ".join(subs)
    missing = [it for it in items if it not in joined]
    assert missing == []


def test_a_brief_with_more_than_24_real_headings_keeps_every_section():
    """RES-01 (MAPA_REUTILIZACION.md): "sigue siendo un tope numérico" even
    after grouping by heading -- a brief with more than MAX_SUBQUESTIONS (24)
    real headings used to lose the headings past the 24th, not just their
    bullets. Each heading here is a real, distinct section the user wrote."""
    n_headings = 30
    text = "\n\n".join(f"Tema {i}\n- punto unico del tema {i}" for i in range(1, n_headings + 1))
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")

    subs = r._extract_subquestions(text)

    for i in range(1, n_headings + 1):
        assert any(f"Tema {i}:" == s[:len(f"Tema {i}:")] or s == f"Tema {i}" for s in subs), \
            f"Tema {i} missing from sections"
    assert len(subs) <= MAX_GROUPED_SUBQUESTIONS


def test_nested_headings_lose_no_bullets_either():
    text = """Guia clinica
Diagnostico
Se evalua con las siguientes pruebas
- prueba de rango cervical activo
- prueba de flexion craneocervical
- prueba de resistencia isometrica del cuello

Tratamiento
Fase aguda
- reposo relativo no mas de 48 horas
- collarin blando solo si hay dolor severo
- analgesia segun pauta medica

Fase subaguda
- movilidad activa progresiva
- ejercicios isometricos de baja intensidad
- educacion sobre pronostico favorable

Que evitar
- inmovilizacion prolongada
- reposo estricto en cama
- collarin rigido mas de una semana
"""
    bullets = [
        "prueba de rango cervical activo", "prueba de flexion craneocervical",
        "prueba de resistencia isometrica del cuello", "reposo relativo no mas de 48 horas",
        "collarin blando solo si hay dolor severo", "analgesia segun pauta medica",
        "movilidad activa progresiva", "ejercicios isometricos de baja intensidad",
        "educacion sobre pronostico favorable", "inmovilizacion prolongada",
        "reposo estricto en cama", "collarin rigido mas de una semana",
    ]

    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    subs = r._extract_subquestions(text)
    joined = " ".join(subs)
    missing = [b for b in bullets if b not in joined]
    assert missing == []


def test_group_chunks_never_exceeds_the_cap_for_many_small_items():
    heading = "Tratamiento"
    items = [f"item {i} corto" for i in range(1, 200)]
    chunks = _group_chunks(heading, items)
    assert all(len(c) <= _MAX_SUBQUESTION_CHARS for c in chunks)
    joined = " ".join(chunks)
    assert all(it in joined for it in items)


def test_a_single_oversized_item_is_its_own_chunk_not_lost_alongside_others():
    """One bullet alone longer than the cap cannot be rescued by splitting --
    but it must not take its neighbours down with it."""
    huge = "x" * (_MAX_SUBQUESTION_CHARS + 50)
    chunks = _group_chunks("Heading", ["short one", huge, "short two"])
    joined = " ".join(chunks)
    assert "short one" in joined
    assert "short two" in joined


def test_without_group_chunks_the_old_truncation_really_did_drop_items():
    """Documents the bug this file guards against: reproduces the OLD
    single-string-then-truncate behaviour standalone (not calling any
    production code) and shows it loses most of a 40-item list. This is the
    "fails without the fix" proof for RES-01 -- `_group_chunks` is what
    keeps `test_a_long_bullet_list_under_one_heading_keeps_every_bullet`
    above green instead of failing the same way.
    """
    items = [f"punto {i} sobre estiramientos cervicales y su tecnica de aplicacion"
             for i in range(1, 41)]
    old_style = "Tratamiento: " + "; ".join(items)
    truncated = old_style[:_MAX_SUBQUESTION_CHARS]
    missing = [it for it in items if it not in truncated]
    assert len(missing) > 20, "the old join-then-truncate shape should have lost most items"
