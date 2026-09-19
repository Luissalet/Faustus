"""Tests for src/research_review.py — blind review of a finished research
report, independent of the process that produced it.

No network: `blind_review`'s model call is stubbed by monkeypatching
`src.llm_core.llm_call_async` (imported inside the function, so patching the
module attribute is enough).
"""
import asyncio

import pytest

import src.research_review as research_review
from src.research_review import (
    build_reviewer_prompt,
    blind_review,
    normalize_writer_self_score,
    strip_process,
)


# ---------------------------------------------------------------------------
# strip_process
# ---------------------------------------------------------------------------

STRIP_PROCESS_CASES = [
    pytest.param(
        "## Findings\nThe sky is blue.\n\n## Confidence\nI am 80% sure.\n\n## Sources\n[1] a.com\n",
        "## Findings\nThe sky is blue.\n\n## Sources\n[1] a.com",
        id="removes-confidence-section",
    ),
    pytest.param(
        "## Findings\nBody text.\n\n## Self-Assessment\nProcess notes here.\n",
        "## Findings\nBody text.",
        id="removes-self-assessment-section",
    ),
    pytest.param(
        "## Methodology\nWe searched five sites.\n\n## Findings\nBody.\n",
        "## Findings\nBody.",
        id="removes-methodology-even-when-first",
    ),
    pytest.param(
        "## Confidence Intervals in Clinical Trials\nReal subject-matter heading.\n",
        "## Confidence Intervals in Clinical Trials\nReal subject-matter heading.",
        id="keeps-heading-that-merely-contains-the-word",
    ),
    pytest.param(
        "## Confianza\nEsto es un proceso.\n\n## Hallazgos\nCuerpo.\n",
        "## Hallazgos\nCuerpo.",
        id="removes-spanish-process-heading",
    ),
    pytest.param(
        "## Findings\nAs an AI language model, I cannot be certain. The sky is blue.\n",
        "## Findings\nThe sky is blue.",
        id="removes-self-referential-sentence-inline",
    ),
    pytest.param("", "", id="empty-input"),
    pytest.param(
        "## Findings\nJust findings, nothing to strip.\n",
        "## Findings\nJust findings, nothing to strip.",
        id="nothing-to-strip",
    ),
]


@pytest.mark.parametrize("raw, expected", STRIP_PROCESS_CASES)
def test_strip_process_table(raw, expected):
    assert strip_process(raw).strip() == expected.strip()


def test_strip_process_removes_nested_subsections_of_a_process_heading():
    raw = (
        "## Findings\nBody.\n\n"
        "## Methodology\nIntro.\n### Search Strategy\nDetails.\n\n"
        "## Sources\n[1] a.com\n"
    )
    out = strip_process(raw)
    assert "Methodology" not in out
    assert "Search Strategy" not in out
    assert "Sources" in out


def test_strip_process_stops_at_next_heading_of_equal_level():
    raw = "## Confidence\nProcess text.\n\n## Findings\nReal content.\n"
    out = strip_process(raw)
    assert "Process text" not in out
    assert "Real content" in out


# ---------------------------------------------------------------------------
# normalize_writer_self_score
# ---------------------------------------------------------------------------

def test_normalize_writer_self_score_maps_grades_onto_0_1():
    assert normalize_writer_self_score({"grade": "none"}) == 0.0
    assert normalize_writer_self_score({"grade": "low"}) == pytest.approx(1 / 3)
    assert normalize_writer_self_score({"grade": "medium"}) == pytest.approx(2 / 3)
    assert normalize_writer_self_score({"grade": "high"}) == 1.0


def test_normalize_writer_self_score_none_for_missing_or_unrecognised():
    assert normalize_writer_self_score(None) is None
    assert normalize_writer_self_score({}) is None
    assert normalize_writer_self_score({"grade": "excellent"}) is None
    assert normalize_writer_self_score("high") is None  # not a dict


# ---------------------------------------------------------------------------
# reviewer prompt — no process/self-assessment text, no writer identity
# ---------------------------------------------------------------------------

def test_reviewer_prompt_contains_no_process_text_and_no_writer_model_name():
    report = (
        "## Findings\nThe study found X.\n\n"
        "## Confidence\nI am very confident because I am gpt-mega-9000.\n\n"
        "## Sources\n[1] a.com\n"
    )
    prompt = build_reviewer_prompt(
        "What did the study find?", report, [{"title": "A", "url": "https://a.com"}],
    )
    assert "gpt-mega-9000" not in prompt
    assert "Confidence" not in prompt.split("<report>")[1].split("</report>")[0]
    assert "I am very confident" not in prompt
    assert "plan" not in prompt.lower().split("<report>")[0] or "you were not involved" in prompt.lower()
    # The instructions explicitly say the reviewer has no process visibility.
    assert "not know" in prompt.lower() or "not involved" in prompt.lower()


def test_reviewer_prompt_includes_question_report_and_sources():
    prompt = build_reviewer_prompt(
        "Is coffee healthy?", "## Findings\nMixed evidence.\n",
        [{"title": "Study A", "url": "https://a.example", "verdict": "supported"}],
    )
    assert "Is coffee healthy?" in prompt
    assert "Mixed evidence." in prompt
    assert "Study A" in prompt
    assert "supported" in prompt


# ---------------------------------------------------------------------------
# blind_review — parsing (good / garbled / partial), calibration, gating
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _patch_llm(monkeypatch, raw_response=None, raise_exc=None):
    calls = []

    async def fake_llm_call_async(**kwargs):
        calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return raw_response

    monkeypatch.setattr(research_review, "asyncio", asyncio)
    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    return calls


GOOD_JSON = (
    '{"scores": {"answers_question": 4, "evidence_support": 3, '
    '"source_quality": 5, "internal_consistency": 4, "completeness": 3}, '
    '"overall": 4, "weaknesses": ["thin on recent data"], '
    '"unsupported_claims": ["claim about X"]}'
)


def test_blind_review_parses_good_json(monkeypatch):
    _patch_llm(monkeypatch, raw_response=GOOD_JSON)
    result = _run(blind_review(
        "question", "## Findings\nBody.\n", [{"title": "A", "url": "https://a.com"}],
        url="http://local/v1/chat/completions", model="reviewer-model",
    ))
    assert result.get("error") is None
    assert result["overall"] == 4
    assert result["scores"]["source_quality"] == 5
    assert result["weaknesses"] == ["thin on recent data"]
    assert result["unsupported_claims"] == ["claim about X"]
    assert result["model"] == "reviewer-model"


def test_blind_review_parses_garbled_response_with_think_tags_and_fences(monkeypatch):
    garbled = (
        "<think>let me consider this carefully</think>\n"
        "```json\n" + GOOD_JSON.replace('"overall": 4,', '"overall": 4,\n  // trailing comment removed,') + "\n```"
    )
    # The inline comment above is not valid JSON; use a cleaner garbled-but-recoverable case instead.
    garbled = "<think>reasoning...</think>\n```json\n" + GOOD_JSON + "\n```"
    _patch_llm(monkeypatch, raw_response=garbled)
    result = _run(blind_review(
        "q", "report", [], url="http://local", model="m",
    ))
    assert result.get("error") is None
    assert result["overall"] == 4


def test_blind_review_parses_json_with_trailing_commas():
    from src.research_review import _parse_review
    trailing = (
        '{"scores": {"answers_question": 2, "evidence_support": 2, '
        '"source_quality": 2, "internal_consistency": 2, "completeness": 2,}, '
        '"overall": 2, "weaknesses": [], "unsupported_claims": [],}'
    )
    parsed = _parse_review(trailing)
    assert parsed is not None
    assert parsed["overall"] == 2


def test_blind_review_clamps_out_of_range_scores_and_fills_missing_overall():
    from src.research_review import _parse_review
    partial = '{"scores": {"answers_question": 9, "evidence_support": -3}, "weaknesses": [], "unsupported_claims": []}'
    parsed = _parse_review(partial)
    assert parsed is not None
    assert parsed["scores"]["answers_question"] == 5  # clamped to max
    assert parsed["scores"]["evidence_support"] == 1  # clamped to min
    # Missing keys default to 3 (middle of the scale), never crash.
    assert parsed["scores"]["source_quality"] == 3
    assert 1 <= parsed["overall"] <= 5


def test_blind_review_unparsable_text_records_error_never_raises(monkeypatch):
    _patch_llm(monkeypatch, raw_response="I refuse to answer in JSON, sorry.")
    result = _run(blind_review(
        "q", "report", [], url="http://local", model="m",
    ))
    assert result["error"]
    assert result["overall"] is None


def test_blind_review_never_raises_on_model_failure(monkeypatch):
    _patch_llm(monkeypatch, raise_exc=RuntimeError("connection refused"))
    result = _run(blind_review(
        "q", "report", [], url="http://local", model="m",
    ))
    assert "connection refused" in result["error"]
    assert result["overall"] is None


def test_blind_review_no_endpoint_configured_records_error_not_raise(monkeypatch):
    # No url/model given, and resolve_endpoint finds nothing either.
    def fake_resolve_endpoint(*a, **kw):
        return None, None, None

    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint", fake_resolve_endpoint)
    result = _run(blind_review("q", "report", []))
    assert result["error"] == "no reviewer endpoint/model configured"


def test_blind_review_calibration_gap_math(monkeypatch):
    _patch_llm(monkeypatch, raw_response=GOOD_JSON)  # overall = 4 -> normalized 0.75
    result = _run(blind_review(
        "q", "report", [], url="http://local", model="m",
        writer_self_score=1.0,
    ))
    assert result["calibration_gap"] == pytest.approx(1.0 - 0.75)


def test_blind_review_calibration_gap_none_without_writer_self_score(monkeypatch):
    _patch_llm(monkeypatch, raw_response=GOOD_JSON)
    result = _run(blind_review("q", "report", [], url="http://local", model="m"))
    assert result["calibration_gap"] is None


def test_blind_review_timeout_records_error(monkeypatch):
    async def fake_llm_call_async(**kwargs):
        return GOOD_JSON

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(research_review.asyncio, "wait_for", fake_wait_for)
    result = _run(blind_review(
        "q", "report", [], url="http://local", model="m", timeout_s=0.01,
    ))
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# setting-off gating (wired in src/research_handler.py)
# ---------------------------------------------------------------------------

def test_research_handler_skips_blind_review_when_setting_is_off(monkeypatch):
    from src.research_handler import ResearchHandler

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: False
                         if key == "research_blind_review" else default)

    called = {"n": 0}

    async def fake_blind_review(*a, **kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(research_review, "blind_review", fake_blind_review)

    handler = ResearchHandler()
    entry = {"researcher": None, "result": "report text", "owner": ""}
    _run(handler._maybe_blind_review(
        entry, query="q", llm_endpoint="http://local", llm_model="m",
    ))
    assert called["n"] == 0
    assert "blind_review" not in entry


def test_research_handler_records_error_without_raising_when_review_blows_up(monkeypatch):
    from src.research_handler import ResearchHandler
    import src.research_handler as research_handler_mod

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: True
                         if key == "research_blind_review" else default)

    def boom(*a, **kw):
        raise RuntimeError("blew up importing research_review")

    # Force the deferred `from src.research_review import blind_review, ...`
    # inside _maybe_blind_review to fail, proving the handler still doesn't raise.
    monkeypatch.setitem(__import__("sys").modules, "src.research_review", None)

    handler = ResearchHandler()
    entry = {"researcher": None, "result": "report text", "owner": ""}
    _run(handler._maybe_blind_review(
        entry, query="q", llm_endpoint="http://local", llm_model="m",
    ))
    assert "blind_review" in entry
    assert entry["blind_review"].get("error")
