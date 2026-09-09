"""A report with many sections is written in parts, not cut off.

09-09-2026: the WAD physiotherapy guide asked ~30 sub-questions; one
generation of 8192 tokens covered 14 of them and the report stopped at
"Movilidad cervical" with a conclusion bolted on. Past SECTIONS_PER_PART the
final report is written a few sections per call, all parts seeing the same
numbered evidence, and joined.
"""
import asyncio

from src.deep_research import DeepResearcher


def _researcher(n_sections: int) -> DeepResearcher:
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    r.subquestions = [f"Section {i}" for i in range(1, n_sections + 1)]
    r.report_language = "en"
    return r


def test_many_sections_are_written_in_parts_and_joined():
    r = _researcher(14)                       # 6 + 6 + 2
    prompts = []

    async def _llm(messages, **k):
        prompts.append(messages[-1]["content"])
        return f"## part {len(prompts)} text [1]"
    r._llm = _llm
    r._emit = lambda **kw: None

    out = asyncio.run(r._final_report("q", "evolving"))

    assert len(prompts) == 3
    assert out == "## part 1 text [1]\n\n## part 2 text [1]\n\n## part 3 text [1]"
    # each part names only its own sections, with the original numbering
    assert "1. Section 1" in prompts[0] and "6. Section 6" in prompts[0] and "7. Section 7" not in prompts[0]
    assert "7. Section 7" in prompts[1] and "12. Section 12" in prompts[1]
    assert "13. Section 13" in prompts[2] and "14. Section 14" in prompts[2]
    # the first opens the report, the last closes it, the middle does neither
    assert "executive summary for the WHOLE report" in prompts[0]
    assert "Do NOT write a conclusion" in prompts[0]
    assert "Do NOT write an introduction" in prompts[1] and "Do NOT write a conclusion" in prompts[1]
    assert "End with a conclusion" in prompts[2]


def test_few_sections_keep_the_single_call():
    r = _researcher(4)
    calls = []

    async def _llm(messages, **k):
        calls.append(1)
        return " ".join(["word"] * 500)
    r._llm = _llm
    asyncio.run(r._final_report("q", "evolving"))
    assert len(calls) == 1


def test_a_failed_part_leaves_its_headings_and_a_note():
    r = _researcher(8)                        # 6 + 2

    async def _llm(messages, **k):
        if "7. Section 7" in messages[-1]["content"]:
            raise TimeoutError("POST timed out")
        return "## first six [2]"
    r._llm = _llm
    r._emit = lambda **kw: None

    out = asyncio.run(r._final_report("q", "evolving"))
    assert out.startswith("## first six [2]")
    assert "## Section 7" in out and "## Section 8" in out
    assert "could not be written" in out
    assert any(f.startswith("final report part 2") for f in r._failures)


def test_progress_names_the_sections_being_written():
    r = _researcher(7)
    events = []
    r._progress = events.append

    async def _llm(messages, **k):
        return "text"
    r._llm = _llm
    asyncio.run(r._final_report("q", "evolving"))
    msgs = [e["message"] for e in events if e.get("phase") == "writing"]
    assert msgs == ["Writing sections 1-6 of 7 (part 1 of 2)", "Writing sections 7-7 of 7 (part 2 of 2)"]
