"""Tests for the report-quality half of src/deep_research.py.

Covers what the engine promises the reader: sections in the order the user
asked their questions, a report written in the language they asked in, evidence
handed to the model as numbered sources so a marker can be checked afterwards,
and two degradation paths that used to fail silently (a failed planner, an
extraction with evidence but no summary).

No network, no LLM, no DB — the end-to-end test drives a full research() run on
a fake LLM that returns canned strings.
"""
import asyncio
import json
import sys
import types

import pytest

from src.deep_research import CATEGORY_PROMPTS, DeepResearcher
from src.research_citations import find_markers, sources_heading

PHYSIO_QUESTION = """\
Necesito una revisión sobre fisioterapia para la tendinopatía rotuliana en \
deportistas amateur.
1. ¿Qué protocolos de carga excéntrica tienen mejor evidencia y con qué dosis?
2. ¿Cuánto tarda la recuperación y qué criterios marcan la vuelta al deporte?
3. ¿Las ondas de choque aportan algo frente al ejercicio isométrico?
4. ¿Qué dicen los ensayos clínicos recientes sobre la carga semanal óptima?
"""


def _researcher(**kwargs):
    return DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# A5 — sub-question extraction
# ---------------------------------------------------------------------------


def test_a_numbered_list_of_questions_survives_intact_and_in_order():
    subs = _researcher()._extract_subquestions(PHYSIO_QUESTION)
    assert len(subs) == 4
    assert subs[0].startswith("¿Qué protocolos de carga excéntrica")
    assert subs[1].startswith("¿Cuánto tarda la recuperación")
    assert subs[3].endswith("?")
    # The framing sentence is context, not a question to answer in its own
    # section — it must not become one.
    assert not any("Necesito una revisión" in s for s in subs)


def test_bulleted_questions_without_question_marks_are_still_questions():
    subs = _researcher()._extract_subquestions(
        "Compare the options:\n- dosage of eccentric loading\n"
        "- expected recovery timeline\n* return-to-sport criteria\n")
    assert subs == ["dosage of eccentric loading", "expected recovery timeline",
                    "return-to-sport criteria"]


def test_a_single_paragraph_packing_several_questions_is_split():
    subs = _researcher()._extract_subquestions(
        "What loading protocols work best for patellar tendinopathy? "
        "And how long does recovery usually take for amateur athletes?")
    assert len(subs) == 2
    assert subs[0].endswith("?") and subs[1].startswith("And how long")


def test_a_dense_paragraph_with_no_questions_yields_nothing_deterministically():
    """This is the case the planner's own sub_questions field has to cover —
    the deterministic pass must say "I found none" rather than invent one."""
    assert _researcher()._extract_subquestions(
        "Compare eccentric loading against shockwave therapy for patellar "
        "tendinopathy, covering dosage, timelines and return-to-sport criteria."
    ) == []


def test_subquestions_are_capped_and_deduplicated():
    question = "\n".join(f"{i}. Is option {i % 3} any good?" for i in range(1, 30))
    subs = _researcher()._extract_subquestions(question)
    assert len(subs) == 3          # only three distinct questions in there
    long_list = "\n".join(f"{i}. Is option {i} any good?" for i in range(1, 30))
    assert len(_researcher()._extract_subquestions(long_list)) == 12


def test_junk_in_gives_an_empty_list_not_an_exception():
    for junk in (None, "", 42, "   "):
        assert _researcher()._extract_subquestions(junk) == []


def test_the_planner_adopts_its_own_subquestions_when_the_user_gave_none():
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return json.dumps({"sub_questions": ["What is X?", "What is Y?"],
                           "key_topics": ["x", "y"], "success_criteria": "done"})

    researcher._llm = fake_llm
    plan = asyncio.run(researcher._create_plan("Explain X and Y in detail."))
    assert researcher.subquestions == ["What is X?", "What is Y?"]
    assert "Sub-questions: What is X?; What is Y?" in plan


def test_the_planner_does_not_overwrite_the_user_s_own_questions():
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return json.dumps({"sub_questions": ["something the model made up"]})

    researcher._llm = fake_llm
    asyncio.run(researcher._create_plan(PHYSIO_QUESTION))
    assert len(researcher.subquestions) == 4
    assert not any("made up" in s for s in researcher.subquestions)


# ---------------------------------------------------------------------------
# A6 — language
# ---------------------------------------------------------------------------


def test_the_report_language_follows_the_question():
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return "{}"

    researcher._llm = fake_llm
    asyncio.run(researcher._create_plan(PHYSIO_QUESTION))
    assert researcher.report_language == "es"


def test_the_final_report_prompt_names_the_language_explicitly():
    researcher = _researcher()
    researcher.report_language = "es"
    researcher.subquestions = ["¿Qué protocolos?"]
    seen = {}

    async def fake_llm(messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        return "x " * 500

    researcher._llm = fake_llm
    asyncio.run(researcher._final_report(PHYSIO_QUESTION, "evolving report"))
    assert "Write the report in Spanish" in seen["prompt"]
    assert "¿Qué protocolos?" in seen["prompt"]


def test_the_synthesis_prompt_names_the_language_too():
    researcher = _researcher()
    researcher.report_language = "es"
    seen = {}

    async def fake_llm(messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        return "report"

    researcher._llm = fake_llm
    asyncio.run(researcher._synthesize(
        PHYSIO_QUESTION,
        [{"url": "https://a.test/1", "title": "A", "summary": "s"}], ""))
    assert "Write the report in Spanish" in seen["prompt"]


# ---------------------------------------------------------------------------
# A4 — what the prompts now demand
# ---------------------------------------------------------------------------


def _rendered_final_prompt(researcher, question="q", report="evolving"):
    seen = {}

    async def fake_llm(messages, **kwargs):
        seen.setdefault("prompt", messages[0]["content"])
        return "x " * 500

    researcher._llm = fake_llm
    asyncio.run(researcher._final_report(question, report))
    return seen["prompt"]


def test_the_final_report_prompt_demands_numbered_citations_not_links():
    researcher = _researcher()
    researcher.report_language = "es"
    researcher.subquestions = ["¿Qué protocolos?", "¿Cuánto tarda?"]
    researcher.citations.add({"url": "https://a.test/1", "title": "Alfredson",
                              "summary": "180 repeticiones diarias"})
    prompt = _rendered_final_prompt(researcher, PHYSIO_QUESTION)

    assert "[like this](url)" not in prompt          # the old, uncheckable style
    assert "[3]" in prompt                            # a concrete numbered marker
    assert "comparison table" in prompt
    assert "in practice" in prompt                    # the table's "so what" column
    assert "> **Implicación práctica:**" in prompt    # localised callout
    # The sections are the user's own questions, numbered, in their order.
    assert "1. ¿Qué protocolos?\n2. ¿Cuánto tarda?" in prompt
    # The evidence arrives as a numbered list keyed to the registry.
    assert "[1] Alfredson — https://a.test/1" in prompt
    assert "180 repeticiones diarias" in prompt


def test_the_final_report_prompt_forbids_the_model_writing_the_legend():
    """The legend is a claim about the report's own reliability. It is counted
    in python; a model-written one would be one more generated assertion."""
    prompt = _rendered_final_prompt(_researcher()).lower()
    assert "do not write a \"how to read this report\" section" in prompt
    assert "do not write a" in prompt and "sources list at the end" in prompt


def test_the_final_report_prompt_says_so_when_there_is_nothing_to_cite():
    """A run whose fetches all failed must not be told to write [n] markers —
    it would invent them, and repair would strip every one."""
    prompt = _rendered_final_prompt(_researcher())
    assert "No numbered sources were registered" in prompt


def test_the_synthesis_prompt_hands_over_numbered_evidence():
    researcher = _researcher()
    seen = {}

    async def fake_llm(messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        return "report"

    researcher._llm = fake_llm
    asyncio.run(researcher._synthesize("q", [
        {"url": "https://a.test/1", "title": "A", "summary": "first"},
        {"url": "https://b.test/2", "title": "B", "summary": "second"},
    ], ""))
    assert "[1] A — https://a.test/1\nfirst" in seen["prompt"]
    assert "[2] B — https://b.test/2\nsecond" in seen["prompt"]
    assert "[3]" in seen["prompt"]      # the marker style the model must use


def test_every_category_prompt_still_requires_numbered_citations():
    assert set(CATEGORY_PROMPTS) == {"product", "comparison", "howto", "factcheck"}
    for name, prompt in CATEGORY_PROMPTS.items():
        assert "FORMAT OVERRIDE" in prompt, name
        assert "numbered marker like [3]" in prompt, name


def test_a_category_override_reaches_the_final_report_prompt():
    researcher = _researcher(category="comparison")
    prompt = _rendered_final_prompt(researcher)
    assert "COMPARISON report" in prompt
    assert "numbered marker like [3]" in prompt


def test_evidence_is_handed_over_as_a_numbered_list_keyed_to_the_registry():
    researcher = _researcher()
    findings = [
        {"url": "https://a.test/one", "title": "One", "summary": "first summary"},
        {"url": "https://b.test/two", "title": "Two", "evidence": "second evidence"},
        # The same page again under a dirtier URL — it must not take a new number.
        {"url": "https://a.test/one/?utm_source=x", "title": "One", "summary": "first summary"},
    ]
    block = researcher._numbered_findings(findings)
    assert "[1] One — https://a.test/one" in block
    assert "[2] Two — https://b.test/two" in block
    assert "[3]" not in block
    assert "second evidence" in block
    assert len(researcher.citations) == 2


def test_a_finding_with_no_url_cannot_be_numbered_and_is_left_out():
    researcher = _researcher()
    block = researcher._numbered_findings([
        {"url": "", "title": "nowhere", "summary": "s"},
        {"url": "https://a.test/1", "title": "A", "summary": "s"},
    ])
    assert "nowhere" not in block
    assert "[1] A" in block


# ---------------------------------------------------------------------------
# A7 — the two degraded paths, made honest
# ---------------------------------------------------------------------------


def test_a_failed_planner_falls_back_to_the_user_s_own_questions():
    events = []
    researcher = _researcher(progress_callback=events.append)

    async def boom(messages, **kwargs):
        raise TimeoutError("model timed out after 90s")

    researcher._llm = boom
    plan = asyncio.run(researcher._create_plan(PHYSIO_QUESTION))

    # The plan is a real plan, not the empty string the engine used to return.
    assert plan.strip()
    assert "¿Qué protocolos de carga excéntrica" in plan
    warnings = [e for e in events if e.get("phase") == "warning"]
    assert len(warnings) == 1
    message = warnings[0]["message"]
    assert "Planning step failed" in message
    assert "model timed out after 90s" in message          # names what broke
    assert "researching the 4 questions directly" in message


def test_a_failed_planner_with_no_extractable_questions_still_plans():
    events = []
    researcher = _researcher(progress_callback=events.append)

    async def boom(messages, **kwargs):
        raise RuntimeError("connection refused")

    researcher._llm = boom
    plan = asyncio.run(researcher._create_plan("Explain photosynthesis."))
    assert "Explain photosynthesis." in plan
    assert "researching the 1 question directly" in events[0]["message"]


def _install_fetch(monkeypatch, content="useful page content"):
    search_mod = types.ModuleType("src.search")
    search_mod.fetch_webpage_content = lambda url, timeout: {
        "success": True, "content": content, "title": "Page", "og_image": "",
    }
    monkeypatch.setitem(sys.modules, "src.search", search_mod)

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


@pytest.mark.asyncio
async def test_an_extraction_with_evidence_but_no_summary_is_kept(monkeypatch):
    """We already paid to fetch and read the page. An empty `summary` field is
    an extractor formatting slip, not a verdict that the page was useless."""
    _install_fetch(monkeypatch)
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return json.dumps({"rational": "relevant", "summary": "",
                           "evidence": "The trial ran for 12 weeks."})

    researcher._llm = fake_llm
    result = await researcher._fetch_and_extract("https://a.test/1", "q", "Title")
    assert result is not None
    assert result["evidence"] == "The trial ran for 12 weeks."
    assert result["url"] == "https://a.test/1"


@pytest.mark.asyncio
async def test_an_extraction_with_neither_summary_nor_evidence_is_still_dropped(monkeypatch):
    _install_fetch(monkeypatch)
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return json.dumps({"rational": "r", "summary": "", "evidence": ""})

    researcher._llm = fake_llm
    assert await researcher._fetch_and_extract("https://a.test/1", "q", "T") is None


@pytest.mark.asyncio
async def test_an_extraction_that_says_the_page_was_useless_is_still_dropped(monkeypatch):
    _install_fetch(monkeypatch)
    researcher = _researcher()

    async def fake_llm(messages, **kwargs):
        return json.dumps({"rational": "r", "evidence": "cookie consent banner",
                           "summary": "No relevant information on this page."})

    researcher._llm = fake_llm
    assert await researcher._fetch_and_extract("https://a.test/1", "q", "T") is None


# ---------------------------------------------------------------------------
# Wiring: stats
# ---------------------------------------------------------------------------


def test_stats_report_citation_coverage():
    from src.research_citations import SourceRegistry, finalize_report

    researcher = _researcher()
    for i in range(1, 4):
        researcher.citations.add({"url": f"https://a.test/{i}", "title": f"S{i}",
                                  "summary": "El dolor bajó un 40% en 12 semanas.",
                                  "evidence": "El dolor bajó un 40% en 12 semanas."})
    md = "El dolor bajó un 40% en 12 semanas [1]. Otra frase sin cita.\n"
    _out, audit, _graded = finalize_report(md, researcher.citations, "es")
    researcher.citation_audit = audit

    stats = researcher.get_stats()
    assert stats["Citations"] == "1 of 3 sources"
    assert stats["Claims cited"] == "50%"


def test_stats_stay_quiet_when_no_research_ran():
    stats = _researcher().get_stats()
    assert "Citations" not in stats
    assert "Claims cited" not in stats


# ---------------------------------------------------------------------------
# End to end, on a fake LLM
# ---------------------------------------------------------------------------

_MODEL_REPORT = """\
# Tendinopatía rotuliana en deportistas amateur

## ¿Qué protocolos de carga excéntrica tienen mejor evidencia y con qué dosis?

El protocolo de Alfredson usa 180 repeticiones diarias durante 12 semanas [1].
La carga lenta pesada obtuvo resultados comparables con menos sesiones [2].
Esta frase no lleva ninguna cita en absoluto.

| Protocolo | Dosis | Qué significa en la práctica |
|---|---|---|
| Alfredson | 180/día [1] | Barato pero exige constancia |
| Carga lenta | 3 sesiones/semana [2] | Mejor adherencia |

> **Implicación práctica:** empezar por la carga lenta pesada si la adherencia
> preocupa.

## ¿Cuánto tarda la recuperación?

La vuelta al deporte se sitúa entre 12 y 24 semanas [2]. Una cifra inventada
sin ninguna fuente detrás [9].

Consulta también [el ensayo original](https://a.test/1) para el detalle.
"""


class _FakeLLMResearcher(DeepResearcher):
    """A researcher whose every LLM call returns a canned string, and whose
    searches return two fixed pages."""

    async def _search(self, query):
        return [
            {"url": "https://a.test/1", "title": "Alfredson protocol"},
            {"url": "https://b.test/2", "title": "Heavy slow resistance"},
        ]

    async def _llm(self, messages, temperature=0.3, max_tokens=4096, timeout=60):
        prompt = messages[0]["content"]
        if len(messages) == 3:                       # the "too brief" expansion
            return _MODEL_REPORT
        if "Classify this research question" in prompt:
            return "general"
        if "research strategist" in prompt:
            return json.dumps({"sub_questions": ["ignored"], "key_topics": ["carga"],
                               "success_criteria": "completo"})
        if "planning web searches" in prompt:
            return json.dumps(["carga excéntrica tendinopatía rotuliana"])
        if "Extract relevant information" in prompt:
            return json.dumps({
                "rational": "relevante",
                "evidence": ("El protocolo de Alfredson usa 180 repeticiones "
                             "diarias durante 12 semanas."),
                "summary": ("El protocolo de Alfredson usa 180 repeticiones "
                            "diarias durante 12 semanas."),
            })
        if "updating an evolving research report" in prompt:
            return "Informe provisional con hallazgos [1] y [2]."
        if "deciding whether a research report" in prompt:
            return "YES — el informe cubre todos los aspectos."
        return _MODEL_REPORT


@pytest.mark.asyncio
async def test_a_full_run_produces_a_report_whose_every_marker_resolves(monkeypatch):
    _install_fetch(monkeypatch)
    researcher = _FakeLLMResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model", max_rounds=2, min_rounds=1, max_time=60,
    )

    report = await researcher.research(PHYSIO_QUESTION)

    # Every marker in the finished report points at a real source.
    body = report.split(sources_heading("es"))[0]
    numbers = {n for marker in find_markers(body) for n in marker.numbers}
    assert numbers, "the report ended up with no citations at all"
    for n in numbers:
        assert researcher.citations.source(n) is not None, n

    # The invented [9] was removed rather than left standing.
    assert 9 not in numbers
    assert researcher.citation_audit.removed == [9]
    assert researcher.citation_audit.dangling == []

    # Spanish question, Spanish deterministic sections.
    assert researcher.report_language == "es"
    assert "## Cómo leer este informe" in report
    assert "## Fuentes" in report
    assert "## Sources" not in report
    assert "no es una objeción a la afirmación" in report
    assert "dice si la afirmación es cierta en el mundo" in report

    # The sources list carries exactly the sources cited, as links.
    tail = report.split("## Fuentes")[1]
    assert "1. [Alfredson protocol](https://a.test/1)" in tail
    assert "a.test" in tail

    # The markdown-link citation converged on the numbered dialect.
    assert "[el ensayo original](https://a.test/1)" not in report
    assert "el ensayo original [1]" in report

    # And the run reports what it did.
    stats = researcher.get_stats()
    assert stats["Citations"].endswith("sources")
    assert stats["Claims cited"].endswith("%")
    assert researcher.subquestions[0].startswith("¿Qué protocolos")


@pytest.mark.asyncio
async def test_finalisation_failure_does_not_lose_the_report(monkeypatch):
    """Citation repair runs after the expensive part of the job. If it breaks,
    the user still gets the report the model wrote, plus a warning saying the
    citations were not checked."""
    _install_fetch(monkeypatch)
    events = []
    researcher = _FakeLLMResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model", max_rounds=1, min_rounds=1, max_time=60,
        progress_callback=events.append,
    )
    monkeypatch.setattr("src.deep_research.finalize_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    report = await researcher.research(PHYSIO_QUESTION)
    assert "Alfredson" in report
    assert any("boom" in str(e.get("message", "")) for e in events if e.get("phase") == "warning")
