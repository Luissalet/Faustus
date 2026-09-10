"""PLAN-01 — `plan_state.coverage(plan, requirements)`: requirement -> the
steps that cover it, and `uncovered` for what has none, so a plan is never
accepted as complete just because every step is checked off while the
checklist never named half the brief. For research, `requirements` is the
brief's own bullets (the same ones `DeepResearcher._extract_subquestions`
turns into report sections).
"""
from src.plan_state import Plan, PlanStep, coverage


def _plan(*titles: str) -> Plan:
    return Plan(steps=[PlanStep(id=f"s{i}", title=t) for i, t in enumerate(titles, 1)])


def test_a_requirement_with_a_matching_step_title_is_covered():
    plan = _plan("Buscar evidencia sobre tratamiento con collarin cervical")
    result = coverage(plan, ["¿Qué evidencia hay sobre el tratamiento con collarín cervical?"])
    assert result["uncovered"] == []
    assert result["complete"] is True
    assert len(result["covered"]) == 1
    (steps,) = result["covered"].values()
    assert steps == ["s1"]


def test_a_requirement_with_no_matching_step_is_uncovered_and_plan_incomplete():
    plan = _plan("Buscar evidencia sobre tratamiento con collarin cervical")
    result = coverage(plan, [
        "¿Qué evidencia hay sobre el tratamiento con collarín cervical?",
        "¿Qué efectos secundarios tiene la relajación muscular?",
    ])
    assert result["uncovered"] == ["¿Qué efectos secundarios tiene la relajación muscular?"]
    assert result["complete"] is False
    assert result["covered_requirements"] == 1
    assert result["total_requirements"] == 2


def test_a_requirement_can_be_covered_by_more_than_one_step():
    plan = _plan(
        "Revisar ensayos clinicos sobre collarin cervical rigido",
        "Revisar guias clinicas sobre collarin cervical blando",
    )
    result = coverage(plan, ["Evidencia sobre collarín cervical"])
    (req,) = result["covered"].keys()
    assert set(result["covered"][req]) == {"s1", "s2"}


def test_an_empty_plan_leaves_every_requirement_uncovered():
    plan = _plan()  # no steps at all
    result = coverage(plan, ["Analizar la evidencia sobre reposo en cama"])
    assert result["uncovered"] == ["Analizar la evidencia sobre reposo en cama"]
    assert result["complete"] is False


def test_no_requirements_at_all_is_trivially_complete():
    """coverage() judges coverage of what was asked, not whether the plan has
    any steps -- an empty requirement list has nothing left to cover."""
    plan = _plan("Some step")
    result = coverage(plan, [])
    assert result["uncovered"] == []
    assert result["complete"] is True
    assert result["total_requirements"] == 0


def test_a_blank_or_punctuation_only_requirement_is_skipped_not_reported():
    plan = _plan("Some real step about cervical treatment")
    result = coverage(plan, ["", "   ", "???"])
    assert result["uncovered"] == []
    assert result["covered"] == {}
    assert result["total_requirements"] == 0


def test_unrelated_step_titles_do_not_falsely_cover_a_requirement():
    plan = _plan("Redactar la introduccion del informe", "Formatear las referencias bibliograficas")
    result = coverage(plan, ["¿Cuál es la dosis recomendada de collarín cervical rígido?"])
    assert result["uncovered"] == ["¿Cuál es la dosis recomendada de collarín cervical rígido?"]


def test_coverage_used_with_a_deep_researchers_own_subquestions():
    """The intended research usage: requirements come straight from
    `DeepResearcher._extract_subquestions`."""
    from src.deep_research import DeepResearcher

    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    brief = "Tratamiento\n- collarin cervical blando\n- ejercicios de movilidad\n\nPronostico\n- factores de mal pronostico"
    subs = r._extract_subquestions(brief)
    assert subs  # sanity: the brief did decompose into requirements

    plan = _plan("Investigar el tratamiento con collarin cervical blando y ejercicios de movilidad")
    result = coverage(plan, subs)
    # At least the "Tratamiento" bullet-group requirement is covered by the
    # one step this plan happens to have; "Pronostico" is not.
    assert any("tratamiento" in req.lower() for req in result["covered"])
    assert any("pronostico" in req.lower() for req in result["uncovered"])
