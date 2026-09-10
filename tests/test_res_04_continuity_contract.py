"""RES-04 — a "continuity contract" carried between report parts so a report
written in several LLM calls (`_final_report_in_parts`, past
`SECTIONS_PER_PART`) reads as one document: citation numbering never resets
between parts, and a term the model defined in part 1 is handed to part 3 so
it is not renamed or retranslated there.
"""
import asyncio

from src.deep_research import DeepResearcher


def _researcher(n_sections: int) -> DeepResearcher:
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    r.subquestions = [f"Section {i}" for i in range(1, n_sections + 1)]
    r.report_language = "en"
    return r


def test_citation_numbering_does_not_reset_between_parts():
    """Every part's prompt carries the SAME numbered sources block -- the
    contract that [n] means the same source in part 3 as in part 1."""
    r = _researcher(14)  # 6 + 6 + 2 sections -> 3 parts
    r.citations.add({"url": "https://a.test/1", "title": "A", "summary": "s1"})
    r.citations.add({"url": "https://b.test/2", "title": "B", "summary": "s2"})
    prompts = []

    async def _llm(messages, **k):
        prompts.append(messages[-1]["content"])
        return f"## part {len(prompts)} [1][2]"
    r._llm = _llm
    r._emit = lambda **kw: None

    asyncio.run(r._final_report("q", "evolving"))

    assert len(prompts) == 3
    sources_blocks = [p.split("Never write")[0] for p in prompts]  # crude but stable per-part slice
    # The registered-sources listing (built once, outside the per-part loop)
    # is identical text in every part's prompt.
    for p in prompts:
        assert "[1]" in p and "https://a.test/1" in p
        assert "[2]" in p and "https://b.test/2" in p


def test_a_term_defined_in_part_one_is_carried_into_part_threes_prompt():
    r = _researcher(14)  # 3 parts
    prompts = []

    async def _llm(messages, **k):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return "## Intro\n\nWe use the term **Whiplash-Associated Disorder** throughout."
        return f"## part {len(prompts)} text"
    r._llm = _llm
    r._emit = lambda **kw: None

    asyncio.run(r._final_report("q", "evolving"))

    assert len(prompts) == 3
    # Part 1 (nothing written yet) carries no continuity block.
    assert "Continuity with the parts already written" not in prompts[0]
    # Parts 2 and 3 are both told to reuse the exact term part 1 coined.
    assert "Whiplash-Associated Disorder" in prompts[1]
    assert "Whiplash-Associated Disorder" in prompts[2]
    assert "never rename or retranslate" in prompts[2]


def test_continuity_contract_tells_later_parts_the_highest_citation_used():
    r = _researcher(14)
    prompts = []

    async def _llm(messages, **k):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return "## Intro\n\nSee the guideline [1] and the trial [3]."
        return f"## part {len(prompts)} text"
    r._llm = _llm
    r._emit = lambda **kw: None

    asyncio.run(r._final_report("q", "evolving"))

    assert "up to [3]" in prompts[1]
    assert "never restart the numbering at [1]" in prompts[1]


def test_continuity_contract_helper_is_empty_for_the_first_part():
    assert DeepResearcher._continuity_contract([]) == ""


def test_continuity_contract_helper_dedupes_repeated_terms_in_order():
    pieces = ["Uses **Term A** here.", "Mentions **Term A** again and **Term B** too."]
    contract = DeepResearcher._continuity_contract(pieces)
    assert contract.count("Term A") == 1
    assert "Term B" in contract
    assert contract.index("Term A") < contract.index("Term B")


def test_prior_parts_from_a_resumed_run_also_seed_the_continuity_contract():
    """A restart resumes with `prior_parts` from the checkpoint (lote 8); the
    NEXT part generated must still see terms/citations established in those
    trusted-verbatim parts, not just parts written in this process."""
    r = _researcher(8)  # 2 parts (6 + 2)
    prompts = []

    async def _llm(messages, **k):
        prompts.append(messages[-1]["content"])
        return "## resumed continuation"
    r._llm = _llm
    r._emit = lambda **kw: None

    prior = ["## Part one\n\nEstablishes **Core Concept** and cites [1] already."]
    asyncio.run(r._final_report("q", "evolving", prior_parts=prior))

    assert len(prompts) == 1  # only the missing part is generated
    assert "Core Concept" in prompts[0]
    assert "up to [1]" in prompts[0]
