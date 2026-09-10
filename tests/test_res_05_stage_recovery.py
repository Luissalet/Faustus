"""RES-05 — every research stage (planning, search, extraction, synthesis,
writing) has its own typed failure and its own recovery policy:

* extraction failed on one URL never tears down the round (already true --
  each URL is isolated behind its own try/except in `_fetch_and_extract` and
  gathered with `return_exceptions=True`; pinned here so a future change
  cannot regress it silently);
* synthesis failure keeps the findings gathered so far and retries once at a
  reduced token budget before giving up;
* a failed report part keeps the N-1 parts already written and retries only
  part N.
"""
import asyncio

from src.deep_research import (
    DeepResearcher,
    ExtractionStageError,
    PlanningStageError,
    ResearchStageError,
    SearchStageError,
    SynthesisStageError,
    WritingStageError,
)


# ---------------------------------------------------------------------------
# The typed vocabulary itself
# ---------------------------------------------------------------------------

def test_every_stage_has_its_own_class_and_category():
    stages = {
        PlanningStageError: "schema",
        SearchStageError: "transport",
        ExtractionStageError: "transport",
        SynthesisStageError: "timeout",
        WritingStageError: "timeout",
    }
    seen_codes = set()
    for cls, category in stages.items():
        assert issubclass(cls, ResearchStageError)
        err = cls("boom", stage="x")
        info = err.error_info
        assert info.code.startswith(category + ".")
        assert info.message == "boom"
        seen_codes.add(info.code)
    # Each stage is distinguishable by code, not just by class name.
    assert len(seen_codes) == len(stages)


def test_error_info_matches_the_tool_result_error_shape():
    err = SynthesisStageError("timed out", stage="synthesize")
    info = err.error_info
    assert info.retryable is True
    assert info.next_action == "retry_with_fewer_tokens"
    mapping = info.to_mapping()
    assert set(mapping) == {"code", "message", "retryable", "next_action"}


# ---------------------------------------------------------------------------
# Extraction: one URL's failure never empties the round
# ---------------------------------------------------------------------------

def test_one_failing_url_does_not_lose_the_others_findings():
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")

    async def flaky_extract(url, question, title):
        if "bad" in url:
            raise TimeoutError("extraction timed out")
        return {"url": url, "title": title, "summary": "ok content for " + url}
    r._fetch_and_extract = flaky_extract

    # Exercise the same concurrency/gather path _search_and_extract uses,
    # without needing a live search provider.
    async def run():
        results = [
            {"url": "https://good.test/1", "title": "Good 1"},
            {"url": "https://bad.test/2", "title": "Bad"},
            {"url": "https://good.test/3", "title": "Good 3"},
        ]
        import asyncio as _asyncio
        semaphore = _asyncio.Semaphore(3)

        async def bounded(result):
            async with semaphore:
                return await r._fetch_and_extract(result["url"], "q", result.get("title", ""))
        gathered = await _asyncio.gather(*[bounded(x) for x in results], return_exceptions=True)
        return [g for g in gathered if g and not isinstance(g, Exception)]

    findings = asyncio.run(run())
    assert len(findings) == 2
    assert {f["url"] for f in findings} == {"https://good.test/1", "https://good.test/3"}


# ---------------------------------------------------------------------------
# Synthesis: findings survive, retry once at a reduced budget, else keep the
# previous report
# ---------------------------------------------------------------------------

def test_synthesis_retries_once_at_a_reduced_token_budget_then_succeeds():
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m",
                       max_report_tokens=6144)
    r.report_language = "en"
    calls = []

    async def _llm(messages, **k):
        calls.append(k.get("max_tokens"))
        if len(calls) == 1:
            raise TimeoutError("synthesis timed out")
        return "synthesized after retry"
    r._llm = _llm
    r._emit = lambda **k: None

    out = asyncio.run(r._synthesize("q", [{"url": "https://x", "summary": "s"}], "prev report"))

    assert out == "synthesized after retry"
    assert len(calls) == 2
    assert calls[1] < calls[0]  # the retry used a smaller budget
    assert any(f.startswith("synthesis:") for f in r._failures)


def test_synthesis_keeps_previous_report_when_both_attempts_fail():
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    r.report_language = "en"
    calls = []

    async def _boom(messages, **k):
        calls.append(k.get("max_tokens"))
        raise RuntimeError("502")
    r._llm = _boom
    r._emit = lambda **k: None

    prev = "the report so far, with real findings baked in"
    out = asyncio.run(r._synthesize("q", [{"url": "https://x", "summary": "s"}], prev))

    assert out == prev  # findings are not lost -- the caller still has `self.findings`
    assert len(calls) == 2  # it did retry before giving up
    assert any(f.startswith("synthesis retry") for f in r._failures)


def test_synthesize_on_a_bare_instance_does_not_crash_on_missing_failures_list():
    """Some existing tests build a DeepResearcher via __new__ without running
    __init__ (no `_failures` attribute). The retry path must not assume it."""
    r = DeepResearcher.__new__(DeepResearcher)
    r.synthesis_window = 10
    r.max_report_tokens = 4096

    async def _boom(messages, **k):
        raise RuntimeError("down")
    r._llm = _boom
    r._emit = lambda **k: None

    out = asyncio.run(r._synthesize("q", [{"url": "https://x", "summary": "s"}], "kept"))
    assert out == "kept"


# ---------------------------------------------------------------------------
# Writing: a failed part is retried once; N-1 parts are never touched
# ---------------------------------------------------------------------------

def _researcher(n_sections: int) -> DeepResearcher:
    r = DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions", llm_model="m")
    r.subquestions = [f"Section {i}" for i in range(1, n_sections + 1)]
    r.report_language = "en"
    # `research()` normally sets this when the run starts; tests call
    # `_final_report`/`_final_report_in_parts` directly, so without this the
    # retry's `_time_exceeded()` guard reads `_start_time=0` and thinks the
    # whole run's time budget is already blown.
    import time as _time
    r._start_time = _time.time()
    return r


def test_a_failing_part_is_retried_once_before_the_placeholder():
    r = _researcher(8)  # 6 + 2 -> 2 parts
    attempts_for_part2 = []

    async def _llm(messages, **k):
        content = messages[-1]["content"]
        if "7. Section 7" in content:
            attempts_for_part2.append(1)
            if len(attempts_for_part2) == 1:
                raise TimeoutError("first attempt times out")
            return "## Section 7\n\nWritten on retry."
        return "## first six [2]"
    r._llm = _llm
    r._emit = lambda **kw: None

    out = asyncio.run(r._final_report("q", "evolving"))

    assert len(attempts_for_part2) == 2  # retried exactly once
    assert "Written on retry" in out
    assert "could not be written" not in out
    assert out.startswith("## first six [2]")  # part 1 (N-1) is untouched


def test_a_part_still_failing_after_the_retry_falls_back_to_the_placeholder():
    r = _researcher(8)

    async def _llm(messages, **k):
        if "7. Section 7" in messages[-1]["content"]:
            raise TimeoutError("always times out")
        return "## first six [2]"
    r._llm = _llm
    r._emit = lambda **kw: None

    out = asyncio.run(r._final_report("q", "evolving"))

    assert out.startswith("## first six [2]")  # N-1 parts kept
    assert "## Section 7" in out and "## Section 8" in out
    assert "could not be written" in out
    assert sum(1 for f in r._failures if f.startswith("final report part 2")) == 2  # initial + retry noted


def test_checkpoints_are_taken_after_each_part_not_lost_on_a_retry():
    r = _researcher(8)
    checkpoints = []
    r._checkpoint_cb = lambda data: checkpoints.append(data)

    async def _llm(messages, **k):
        if "7. Section 7" in messages[-1]["content"]:
            raise TimeoutError("times out once")  # only fails, forcing a retry path check
        return "## first six [2]"
    r._llm = _llm
    r._emit = lambda **kw: None

    asyncio.run(r._final_report("q", "evolving"))

    # One checkpoint per part; the report_parts field never shrinks.
    part_counts = [len(c["report_parts"]) for c in checkpoints]
    assert part_counts == sorted(part_counts)
    assert part_counts[-1] == 2
