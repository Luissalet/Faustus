"""Measured stopping for Deep Research: the saturation rule, the confidence
signal, the stop reasons they record, `max_time` staying the ceiling, and the
per-page pruning path wired into the extraction prompt."""
import asyncio
from pathlib import Path

import pytest

from src import deep_research
from src.deep_research import (
    STOP_REASON_CONFIDENCE,
    STOP_REASON_MAX_ROUNDS,
    STOP_REASON_MEASURED_SATURATION,
    STOP_REASON_TIME_BUDGET,
    DeepResearcher,
    classify_stop_reason,
    stop_rationale_text,
)
from src.research_saturation import (
    STOP_CONFIDENCE,
    STOP_SATURATED,
    SaturationTracker,
    canonical_url,
    finding_facts,
    jaccard,
    source_key,
    split_facts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "research_pages"


def _finding(url, *sentences):
    return {"url": url, "title": "T", "summary": " ".join(sentences), "evidence": ""}


# ---------------------------------------------------------------------------
# Facts, sources, dedupe
# ---------------------------------------------------------------------------

def test_split_facts_drops_fragments_and_keeps_sentences():
    facts = split_facts("Short one. The token bucket refills at a constant rate every second. Yes.")
    assert facts == ["The token bucket refills at a constant rate every second."]


def test_finding_facts_falls_back_to_evidence_when_summary_empty():
    f = {"summary": "", "evidence": "Exponential backoff with jitter spreads retries across time."}
    assert finding_facts(f) == ["Exponential backoff with jitter spreads retries across time."]
    assert finding_facts({"summary": "", "evidence": ""}) == []
    assert finding_facts("not a dict") == []


def test_jaccard_bounds():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard(set(), {"a"}) == 0.0


def test_canonical_url_and_source_key_ignore_noise():
    assert canonical_url("https://www.Example.com/a/?utm_source=x&b=2&a=1#frag") == "https://example.com/a?a=1&b=2"
    assert source_key("https://www.example.com/x") == source_key("http://example.com/y") == "example.com"
    assert source_key("") == ""


def test_exact_and_near_duplicate_facts_are_not_new():
    t = SaturationTracker(["token bucket"], min_new_facts=1)
    row1 = t.add_round(1, [_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    assert row1["new_facts"] == 1
    # same text with different case/punctuation, and a near-duplicate (one word differs out of ten)
    row2 = t.add_round(2, [
        _finding("https://b.org/1", "the TOKEN bucket refills at a constant rate every second!!"),
        _finding("https://c.org/1", "The token bucket refills at a constant rate every minute."),
    ])
    assert row2["new_facts"] == 0
    assert t.total_facts == 1


def test_clearly_different_fact_is_new():
    t = SaturationTracker(["x"], min_new_facts=1)
    t.add_round(1, [_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    row = t.add_round(2, [_finding("https://a.org/2", "Exponential backoff with jitter spreads retries across time.")])
    assert row["new_facts"] == 1
    assert row["new_sources"] == 0          # same domain: not a new source


# ---------------------------------------------------------------------------
# Saturation rule
# ---------------------------------------------------------------------------

def _round_of(tracker, n, url, *sentences):
    return tracker.add_round(n, [_finding(url, *sentences)])


def test_round_with_few_new_facts_and_no_new_source_is_saturated():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=1)
    _round_of(t, 1, "https://a.org/1", "The token bucket refills at a constant rate every second.",
              "A sliding window avoids the boundary burst at the price of storing more state.")
    assert t.should_stop() is None                      # one round is never enough
    row = _round_of(t, 2, "https://a.org/2", "The token bucket refills at a constant rate every second.")
    assert row["saturated_round"] is True
    verdict = t.should_stop()
    assert verdict["code"] == STOP_SATURATED
    assert "saturated" in verdict["reason"]


def test_a_new_source_keeps_the_run_going_even_with_no_new_facts():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=1)
    _round_of(t, 1, "https://a.org/1", "The token bucket refills at a constant rate every second.")
    row = _round_of(t, 2, "https://other.net/1", "The token bucket refills at a constant rate every second.")
    assert row["new_sources"] == 1 and row["saturated_round"] is False
    assert t.should_stop() is None


def test_enough_new_facts_keep_the_run_going():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=1)
    _round_of(t, 1, "https://a.org/1", "The token bucket refills at a constant rate every second.")
    _round_of(t, 2, "https://a.org/2", "Exponential backoff with jitter spreads retries across time.",
              "Responses should carry the remaining quota and the reset time in headers.")
    assert t.should_stop() is None


def test_patience_needs_consecutive_saturated_rounds():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=2)
    base = "The token bucket refills at a constant rate every second."
    _round_of(t, 1, "https://a.org/1", base)
    _round_of(t, 2, "https://a.org/2", base)
    assert t.should_stop() is None                      # one saturated round, patience is two
    _round_of(t, 3, "https://a.org/3", base)
    assert t.should_stop()["code"] == STOP_SATURATED


def test_a_productive_round_resets_the_streak():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=2)
    base = "The token bucket refills at a constant rate every second."
    _round_of(t, 1, "https://a.org/1", base)
    _round_of(t, 2, "https://a.org/2", base)                        # saturated
    _round_of(t, 3, "https://a.org/3", "Exponential backoff with jitter spreads retries across time.",
              "Responses should carry the remaining quota and the reset time in headers.")
    assert t.rounds[-1]["saturated_streak"] == 0
    _round_of(t, 4, "https://a.org/4", base)                        # saturated again, streak 1
    assert t.should_stop() is None


def test_empty_rounds_are_not_counted_as_saturation():
    t = SaturationTracker(["x"], min_new_facts=2, patience=1)
    t.add_round(1, [_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    row = t.add_round(2, [])
    assert row["pages"] == 0 and row["saturated_round"] is False
    assert t.should_stop() is None


def test_saturation_stop_can_be_switched_off():
    t = SaturationTracker(["rate limits"], min_new_facts=2, patience=1, saturation_stop=False)
    base = "The token bucket refills at a constant rate every second."
    _round_of(t, 1, "https://a.org/1", base)
    _round_of(t, 2, "https://a.org/2", base)
    assert t.should_stop() is None


def test_seeded_findings_do_not_count_as_progress():
    t = SaturationTracker(["rate limits"], min_new_facts=1)
    t.seed([_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    row = _round_of(t, 1, "https://a.org/2", "The token bucket refills at a constant rate every second.")
    assert row["new_facts"] == 0 and row["new_sources"] == 0


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def test_confidence_formula_weights():
    t = SaturationTracker(["token bucket burst", "retry backoff jitter"], min_new_facts=1)
    t.add_round(1, [
        _finding("https://a.org/1", "The token bucket allows a burst up to its size before rejecting requests."),
        _finding("https://b.org/1", "The token bucket allows a burst up to its size before rejecting requests."),
    ])
    row = t.add_round(2, [_finding("https://c.org/1", "Clients should retry with backoff and jitter after a rejection.")])
    # 2 of 2 sub-questions covered; 1 of 2 facts has two sources; last round added 1 of 2 facts
    assert row["coverage"] == 1.0
    assert row["consistency"] == 0.5
    assert row["saturation"] == 0.5
    assert row["confidence"] == pytest.approx(0.4 * 1.0 + 0.3 * 0.5 + 0.3 * 0.5, abs=1e-3)


def test_coverage_counts_only_sub_questions_with_a_supporting_fact():
    t = SaturationTracker(["token bucket", "database indexing"], min_new_facts=1)
    row = t.add_round(1, [_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    assert row["coverage"] == 0.5


def test_no_subquestions_means_covered_once_any_fact_exists():
    t = SaturationTracker([], min_new_facts=1)
    assert t.coverage() == 0.0
    t.add_round(1, [_finding("https://a.org/1", "The token bucket refills at a constant rate every second.")])
    assert t.coverage() == 1.0


def test_confidence_stop_fires_at_the_threshold_and_not_below():
    base = "The token bucket allows a burst up to its size before rejecting requests."
    other = "Exponential backoff with jitter spreads retries across time for many clients."

    def run(threshold, second_fact):
        t = SaturationTracker(["token bucket burst"], min_new_facts=0, confidence_threshold=threshold)
        t.add_round(1, [_finding("https://a.org/1", base)])
        t.add_round(2, [_finding("https://b.org/1", second_fact)])
        return t

    # a different fact in round 2: coverage 1, nothing corroborated, saturation 0.5 -> 0.55
    t = run(0.7, other)
    assert t.rounds[-1]["confidence"] == pytest.approx(0.55, abs=1e-3)
    assert t.should_stop() is None
    assert run(0.5, other).should_stop()["code"] == STOP_CONFIDENCE
    # the same fact from a second source: corroborated and saturated -> 1.0
    verdict = run(0.7, base).should_stop()
    assert verdict["code"] == STOP_CONFIDENCE and verdict["confidence"] >= 0.7


def test_confidence_off_when_threshold_is_zero():
    t = SaturationTracker(["token bucket burst"], min_new_facts=0, confidence_threshold=0.0)
    base = "The token bucket allows a burst up to its size before rejecting requests."
    t.add_round(1, [_finding("https://a.org/1", base)])
    t.add_round(2, [_finding("https://b.org/1", base)])
    assert t.should_stop() is None


# ---------------------------------------------------------------------------
# DeepResearcher: the run stops by measurement and says so
# ---------------------------------------------------------------------------

def _researcher(**kw):
    kw.setdefault("max_rounds", 6)
    kw.setdefault("min_rounds", 6)        # keep the model's own "enough" out of the way
    return DeepResearcher(llm_endpoint="http://127.0.0.1:11434/v1/chat/completions",
                          llm_model="m", max_time=300, **kw)


def _ready(value):
    async def _coro(*a, **k):
        return value
    return _coro()


class _Script:
    """Drives a run without a model: round N searches one page of the given
    domain and 'reads' it into the given facts."""

    def __init__(self, r, monkeypatch, plan):
        self.r = r
        self.plan = plan            # list of (url, [facts]) per round
        self.llm_calls = 0
        self.round = 0
        monkeypatch.setattr(r, "_create_plan", lambda q: _ready("plan"))
        monkeypatch.setattr(r, "_classify_category", lambda q: _ready(None))
        monkeypatch.setattr(r, "_synthesize", lambda *a, **k: _ready("report so far"))
        monkeypatch.setattr(r, "_final_report", lambda q, rep: _ready("final: " + rep))
        monkeypatch.setattr(r, "_generate_queries", self._queries)
        monkeypatch.setattr(r, "_search", self._search)
        monkeypatch.setattr(r, "_fetch_and_extract", self._extract)
        monkeypatch.setattr(r, "_should_stop", self._should_stop)
        self.should_stop_calls = 0

    async def _queries(self, question, report, round_num):
        self.round = round_num
        return [f"query {round_num}"]

    async def _search(self, query):
        idx = min(self.round, len(self.plan)) - 1
        url, _facts = self.plan[idx]
        return [{"url": url, "title": "T"}]

    async def _extract(self, url, question, title=""):
        idx = min(self.round, len(self.plan)) - 1
        _url, facts = self.plan[idx]
        return {"url": url, "title": title, "summary": " ".join(facts), "evidence": ""}

    async def _should_stop(self, question, report, round_num):
        self.should_stop_calls += 1
        return False


F1 = "The token bucket refills at a constant rate every second."
F2 = "A sliding window avoids the boundary burst at the price of storing more state."
F3 = "Exponential backoff with jitter spreads retries across time for many clients."
F4 = "Responses should carry the remaining quota and the reset time in headers."
F5 = "A fixed window counter lets a client send double the limit across a boundary."


def test_run_stops_as_saturated_without_asking_the_model(monkeypatch):
    r = _researcher(saturation_stop=True, saturation_min_new_facts=2, confidence_stop=0.0)
    script = _Script(r, monkeypatch, [
        ("https://a.org/1", [F1, F2, F3]),
        ("https://a.org/2", [F1, F2]),        # round 2: nothing new, same domain
    ] + [("https://a.org/9", [F1])] * 10)
    asyncio.run(r.research("rate limit design?"))
    assert r.stop_reason["code"] == STOP_REASON_MEASURED_SATURATION == "saturated"
    assert r.stop_reason["rounds_completed"] == "2 of 6"
    assert "saturated" in stop_rationale_text(r.stop_reason)
    assert script.should_stop_calls == 0                   # the model was never asked
    assert [row["round"] for row in r.research_trace["rounds"]] == [1, 2]
    assert r.research_trace["rounds"][1]["saturated_round"] is True
    assert r.get_stats()["Stopped"].startswith("saturated:")


def test_run_continues_while_rounds_keep_adding_facts_or_sources(monkeypatch):
    r = _researcher(max_rounds=3, saturation_stop=True, confidence_stop=0.0)
    script = _Script(r, monkeypatch, [
        ("https://a.org/1", [F1, F2]),
        ("https://b.org/1", [F3, F4]),
        ("https://c.org/1", [F5, F1]),
    ])
    asyncio.run(r.research("rate limit design?"))
    assert r.stop_reason["code"] == STOP_REASON_MAX_ROUNDS
    assert len(r.research_trace["rounds"]) == 3


def test_setting_off_keeps_the_original_behaviour_but_still_records_rounds(monkeypatch):
    r = _researcher(max_rounds=3, saturation_stop=False, confidence_stop=0.0)
    _Script(r, monkeypatch, [("https://a.org/1", [F1, F2, F3]), ("https://a.org/2", [F1]), ("https://a.org/3", [F1])])
    asyncio.run(r.research("rate limit design?"))
    assert r.stop_reason["code"] == STOP_REASON_MAX_ROUNDS
    assert [row["saturated_round"] for row in r.research_trace["rounds"]] == [False, True, True]


def test_confidence_stop_ends_the_run_and_records_the_reason(monkeypatch):
    r = _researcher(saturation_stop=False, confidence_stop=0.6)
    _Script(r, monkeypatch, [("https://a.org/1", [F1, F2]), ("https://b.org/1", [F1, F2])])
    asyncio.run(r.research("rate limit design: token bucket window"))
    assert r.stop_reason["code"] == STOP_REASON_CONFIDENCE == "confidence_reached"
    assert r.stop_reason["detail"]["confidence"] >= 0.6
    assert "confidence" in r.stop_reason["reason"]


def test_max_time_is_still_the_ceiling(monkeypatch):
    r = _researcher(saturation_stop=True, confidence_stop=0.0)
    # never saturated (each round brings new facts AND a new domain), so only the clock can stop it
    _Script(r, monkeypatch, [
        ("https://a.org/1", [F1, F2]), ("https://b.org/1", [F3, F4]), ("https://c.org/1", [F5]),
        ("https://d.org/1", [F1]), ("https://e.org/1", [F2]), ("https://f.org/1", [F3]),
    ])
    ticks = {"n": 0}

    def _exceeded():
        ticks["n"] += 1
        return ticks["n"] > 3            # the clock runs out before round 4 starts

    monkeypatch.setattr(r, "_time_exceeded", _exceeded)
    asyncio.run(r.research("rate limit design?"))
    assert r.stop_reason["code"] == STOP_REASON_TIME_BUDGET
    assert r.round_count < 6


def test_clock_wins_over_saturation_when_both_apply(monkeypatch):
    r = _researcher(saturation_stop=True, confidence_stop=0.0)
    _Script(r, monkeypatch, [("https://a.org/1", [F1, F2, F3])] + [("https://a.org/2", [F1])] * 5)
    monkeypatch.setattr(r, "_time_exceeded", lambda: True)
    with pytest.raises(deep_research.ResearchFailed):
        asyncio.run(r.research("rate limit design?"))      # the ceiling cut the run before anything ran
    assert r.stop_reason["code"] == STOP_REASON_TIME_BUDGET


def test_classify_stop_reason_accepts_an_explicit_reason_and_detail():
    info = classify_stop_reason(STOP_REASON_MEASURED_SATURATION, round_num=2, max_rounds=6, urls_fetched=4,
                                findings=3, reason="custom text", detail={"new_facts": 1})
    assert info["reason"] == "custom text" and info["detail"] == {"new_facts": 1}
    plain = classify_stop_reason(STOP_REASON_MAX_ROUNDS, round_num=2, max_rounds=2, urls_fetched=1, findings=1)
    assert "detail" not in plain


# ---------------------------------------------------------------------------
# Pruning inside the extraction path
# ---------------------------------------------------------------------------

class _Capture:
    """Runs `_fetch_and_extract` against a fixture page and records the
    prompt the model would have been given."""

    def __init__(self, monkeypatch, r, *, name="whiplash_es.html", with_html=True):
        html = (FIXTURES / name).read_text(encoding="utf-8")
        # the "extracted text" stands in for what the fetcher already returns
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, "html.parser").get_text("\n")
        self.full_text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        self.prompts = []
        self.calls = []
        page = {"success": True, "content": self.full_text, "title": "Fixture", "og_image": "", "headers": {}}
        if with_html:
            page["raw_html"] = html

        def fake_fetch(url, timeout=5, **kw):
            self.calls.append(kw)
            return dict(page)

        async def fake_llm(messages, **k):
            self.prompts.append(messages[-1]["content"])
            return '{"rational": "r", "evidence": "some evidence from the page about grades", "summary": "A useful summary sentence about grades."}'

        monkeypatch.setattr("src.search.fetch_webpage_content", fake_fetch)
        monkeypatch.setattr(r, "_llm", fake_llm)
        monkeypatch.setattr(r, "_active_search_provider", lambda: "searxng")
        r.max_content_chars = 15000


def test_extraction_reads_pruned_text_when_enabled(monkeypatch):
    r = _researcher(prune_pages=True, prune_max_chars=900)
    r.subquestions = ["Clasificación de los WAD y características de cada grado"]
    r._url_query["https://x.org/p"] = "clasificación grados WAD"
    cap = _Capture(monkeypatch, r)
    finding = asyncio.run(r._fetch_and_extract("https://x.org/p", "Guía sobre whiplash", "Guía"))
    assert finding and finding["summary"]
    prompt = cap.prompts[0]
    assert "Utilizamos cookies" not in prompt and "Suscríbete" not in prompt and "Política de privacidad" not in prompt
    assert "La clasificación más utilizada" in prompt
    assert len(prompt) < len(cap.full_text)
    assert cap.calls == [{"keep_html": True}]                       # the DOM was requested
    row = r.research_trace["pages"][0]
    assert row["url"] == "https://x.org/p"
    assert row["original_chars"] == len(cap.full_text)
    assert 0 < row["pruned_chars"] < row["original_chars"]
    assert row["blocks_kept"] >= 1 and row["top_bm25"] > 0
    assert "WAD" in row["focus"]
    assert "Pruned" in r.get_stats()


def test_extraction_reads_the_whole_page_when_disabled(monkeypatch):
    r = _researcher(prune_pages=False)
    cap = _Capture(monkeypatch, r)
    asyncio.run(r._fetch_and_extract("https://x.org/p", "Guía sobre whiplash", "Guía"))
    prompt = cap.prompts[0]
    assert "Utilizamos cookies" in prompt and "Suscríbete" in prompt      # the original path, untouched
    assert cap.calls == [{}]                                              # no markup requested
    assert r.research_trace["pages"] == []


def test_pruning_falls_back_to_text_when_the_fetcher_returns_no_markup(monkeypatch):
    r = _researcher(prune_pages=True, prune_max_chars=900)
    cap = _Capture(monkeypatch, r, with_html=False)
    r._url_query["https://x.org/p"] = "tratamiento ejercicio terapéutico"
    finding = asyncio.run(r._fetch_and_extract("https://x.org/p", "Guía sobre whiplash", "Guía"))
    assert finding
    assert r.research_trace["pages"][0]["mode"] == "text"
    assert len(cap.prompts[0]) < len(cap.full_text)


def test_a_fetcher_without_the_markup_option_still_works(monkeypatch):
    r = _researcher(prune_pages=True)
    cap = _Capture(monkeypatch, r, with_html=False)

    def strict_fetch(url, timeout=5):                 # the older signature
        return {"success": True, "content": cap.full_text, "title": "t", "og_image": "", "headers": {}}

    monkeypatch.setattr("src.search.fetch_webpage_content", strict_fetch)
    assert asyncio.run(r._fetch_and_extract("https://x.org/p", "q", "t"))


def test_a_pruning_failure_reads_the_full_page(monkeypatch):
    r = _researcher(prune_pages=True)
    cap = _Capture(monkeypatch, r)

    def boom(*a, **k):
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr("src.research_prune.prune_page", boom)
    assert asyncio.run(r._fetch_and_extract("https://x.org/p", "q", "t"))
    assert "Utilizamos cookies" in cap.prompts[0]
    assert r.research_trace["pages"] == []


def test_search_remembers_which_query_surfaced_each_url(monkeypatch):
    r = _researcher(prune_pages=True)

    async def search(q):
        return [{"url": f"https://x.org/{q}", "title": q}]

    async def extract(url, question, title=""):
        return None

    monkeypatch.setattr(r, "_search", search)
    monkeypatch.setattr(r, "_fetch_and_extract", extract)
    asyncio.run(r._search_and_extract(["alpha", "beta"], "question"))
    assert r._url_query == {"https://x.org/alpha": "alpha", "https://x.org/beta": "beta"}


def test_prune_focus_uses_query_plus_best_matching_subquestion():
    r = _researcher(prune_pages=True)
    r.subquestions = ["Factores pronósticos de mala evolución", "Criterios de derivación médica"]
    r._url_query["u"] = "criterios derivación"
    focus = r._prune_focus("u", "whole question")
    assert focus.startswith("criterios derivación") and "Criterios de derivación médica" in focus
    assert r._prune_focus("unknown", "whole question") == "whole question"


# ---------------------------------------------------------------------------
# Handler wiring: settings reach the researcher, and the run's record keeps
# the stop reason and the trace.
# ---------------------------------------------------------------------------

def _run_handler(monkeypatch, settings):
    from src.research_handler import ResearchHandler

    made = {}

    class FakeResearcher:
        def __init__(self, **kw):
            made.update(kw)
            self.stop_reason = None

        async def research(self, *a, **k):
            return "report"

        def get_stats(self):
            return {"Rounds": 1}

    async def _noop(*a, **k):
        return None

    handler = ResearchHandler.__new__(ResearchHandler)
    monkeypatch.setattr(handler, "_probe_endpoint", _noop)
    monkeypatch.setattr(handler, "_ensure_search_backend", _noop)
    monkeypatch.setattr(handler, "_format_completed_report", lambda *a, **k: "formatted")
    monkeypatch.setattr("src.deep_research.DeepResearcher", FakeResearcher)
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: settings.get(k, d))
    asyncio.run(handler.call_research_service("q", "http://unused.invalid", "m", max_time=120))
    return made


def test_handler_defaults_turn_pruning_and_measured_stops_on(monkeypatch):
    made = _run_handler(monkeypatch, {})
    assert made["prune_pages"] is True and made["prune_max_chars"] == 6000 and made["prune_threshold"] == 0.48
    assert made["saturation_stop"] is True and made["saturation_min_new_facts"] == 2
    assert made["saturation_patience"] == 1 and made["confidence_stop"] == 0.7
    assert made["max_time"] == 120                                    # the ceiling is still handed through


def test_handler_passes_saved_overrides_and_survives_bad_values(monkeypatch):
    made = _run_handler(monkeypatch, {"research_prune_pages": False, "research_prune_max_chars": "4000",
                                      "research_saturation_stop": False, "research_saturation_min_new_facts": 5,
                                      "research_saturation_patience": 3, "research_confidence_stop": 0})
    assert made["prune_pages"] is False and made["prune_max_chars"] == 4000
    assert made["saturation_stop"] is False and made["saturation_min_new_facts"] == 5
    assert made["saturation_patience"] == 3 and made["confidence_stop"] == 0.0
    bad = _run_handler(monkeypatch, {"research_prune_threshold": "x", "research_confidence_stop": "y",
                                     "research_prune_max_chars": 1})
    assert bad["prune_threshold"] == 0.48 and bad["confidence_stop"] == 0.7 and bad["prune_max_chars"] == 500


def test_saved_result_carries_stop_reason_and_trace(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from src import research_handler

    handler = research_handler.ResearchHandler.__new__(research_handler.ResearchHandler)
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", tmp_path)
    stop = classify_stop_reason(STOP_REASON_MEASURED_SATURATION, round_num=2, max_rounds=6, urls_fetched=3, findings=2)
    trace = {"pages": [{"url": "u", "original_chars": 100, "pruned_chars": 40}], "rounds": [{"round": 1}]}
    researcher = SimpleNamespace(citations=None, findings=[], stop_reason=stop, research_trace=trace)
    handler._save_result("rp-stop", {"query": "q", "status": "done", "result": "r", "started_at": 1,
                                     "researcher": researcher})
    saved = json.loads((tmp_path / "rp-stop.json").read_text(encoding="utf-8"))
    assert saved["stop_reason"]["code"] == "saturated"
    assert saved["trace"]["pages"][0]["pruned_chars"] == 40


def test_summary_shows_stop_reason_confidence_and_pruning(monkeypatch):
    from src.research_handler import ResearchHandler

    handler = ResearchHandler.__new__(ResearchHandler)
    stats = {"Rounds": 2, "Queries": 2, "URLs": 3, "Pruned": "3 page(s), 9000 -> 2400 chars",
             "Confidence": "0.81", "Stopped": "saturated: the evidence stopped growing (saturated)"}
    text = handler._format_research_report("q", "body", stats, 12.0)
    assert "**Stopped:** saturated" in text and "**Confidence:** 0.81" in text and "**Pruned:** 3 page(s)" in text
