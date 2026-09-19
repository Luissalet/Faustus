"""Perspective-guided question planning (opt-in: `research_perspectives`).

Covers `DeepResearcher._apply_perspective_plan` / `_generate_perspective_questions`
/ `_merge_perspective_plan` (src/deep_research.py): parsing a good and a
garbled model response, de-duplication, the round-1 query budget cap, that
perspective labels reach `self.perspective_plan` and the round-1 progress
event, that the setting off is byte-for-byte the old behaviour (same LLM
calls), and that a failed perspectives call falls back silently to the flat
plan.
"""
import asyncio
import json

from src.deep_research import (
    DeepResearcher,
    FIRST_ROUND_QUERY_BUDGET,
)


def _researcher(**kwargs):
    return DeepResearcher("http://unused.invalid", "m", **kwargs)


GOOD_PERSPECTIVES_JSON = json.dumps({
    "perspectives": [
        {"name": "Practitioner", "focus": "hands-on use",
         "questions": ["How is this used in daily practice?",
                       "What tools do practitioners rely on?"]},
        {"name": "Skeptic", "focus": "critical angle",
         "questions": ["What are the strongest objections to this?",
                       "Where has this approach failed before?"]},
        {"name": "Regulator", "focus": "compliance angle",
         "questions": ["What rules govern this activity?"]},
    ]
})


def test_generate_perspective_questions_parses_good_output():
    r = _researcher(research_perspectives=True)

    async def fake_llm(messages, **kwargs):
        return GOOD_PERSPECTIVES_JSON

    r._llm = fake_llm
    items = asyncio.run(r._generate_perspective_questions("solar panel adoption"))

    assert items, "expected parsed perspective questions"
    perspectives = {i["perspective"] for i in items}
    assert "Practitioner" in perspectives
    assert "Skeptic" in perspectives
    for item in items:
        assert item["question"]
        assert "perspective" in item and "focus" in item


def test_generate_perspective_questions_handles_garbled_output():
    r = _researcher(research_perspectives=True)

    async def fake_llm(messages, **kwargs):
        return "not json at all, sorry <<garbage>>"

    r._llm = fake_llm
    items = asyncio.run(r._generate_perspective_questions("solar panel adoption"))
    assert items == []


def test_generate_perspective_questions_handles_junk_shape():
    """Valid JSON, wrong shape (e.g. a flat array like the old query prompt
    would return) must degrade to empty, not raise."""
    r = _researcher(research_perspectives=True)

    async def fake_llm(messages, **kwargs):
        return json.dumps(["just", "a", "list"])

    r._llm = fake_llm
    items = asyncio.run(r._generate_perspective_questions("topic"))
    assert items == []


def test_merge_plan_dedupes_by_token_overlap_and_keeps_general():
    r = _researcher(research_perspectives=True)
    general = ["What is the cost of solar panel installation?"]
    perspective_items = [
        # Near-duplicate of the general question — should be dropped.
        {"perspective": "Practitioner", "focus": "",
         "question": "What is the installation cost of solar panels?"},
        {"perspective": "Skeptic", "focus": "",
         "question": "What are the strongest objections to solar subsidies?"},
    ]
    merged = r._merge_perspective_plan(general, perspective_items)

    questions = [m["question"] for m in merged]
    assert general[0] in questions
    # The near-duplicate practitioner question did not survive.
    assert not any(m["perspective"] == "Practitioner" for m in merged)
    assert any(m["perspective"] == "Skeptic" for m in merged)
    # General question always labeled "general".
    assert merged[0]["perspective"] == "general"


def test_merge_plan_caps_to_first_round_query_budget():
    r = _researcher(research_perspectives=True)
    general = ["General question one?"]
    perspective_items = [
        {"perspective": f"Persona{i}", "focus": "", "question": f"Distinct question number {i} about the topic?"}
        for i in range(10)
    ]
    merged = r._merge_perspective_plan(general, perspective_items)
    assert len(merged) <= max(len(general), FIRST_ROUND_QUERY_BUDGET)


def test_merge_plan_never_drops_general_even_over_budget():
    r = _researcher(research_perspectives=True)
    # Genuinely distinct wording per item (not just a swapped digit, which
    # the token-overlap de-dup would correctly treat as a rephrasing).
    general = [
        "How much does a residential installation cost?",
        "What is the typical project timeline?",
        "Which roof orientations perform best?",
        "How does battery storage affect payback?",
        "What are the local grid connection rules?",
        "How often does the system need maintenance?",
        "What do manufacturer warranties typically cover?",
    ]
    assert len(general) == FIRST_ROUND_QUERY_BUDGET + 3
    perspective_items = [
        {"perspective": "Persona", "focus": "", "question": "A totally distinct perspective question here?"},
    ]
    merged = r._merge_perspective_plan(general, perspective_items)
    kept_general = [m for m in merged if m["perspective"] == "general"]
    assert len(kept_general) == len(general)


def test_apply_perspective_plan_labels_propagate_into_the_plan_structure():
    r = _researcher(research_perspectives=True)
    r.subquestions = ["What is the cost of solar panels?"]

    async def fake_llm(messages, **kwargs):
        return GOOD_PERSPECTIVES_JSON

    r._llm = fake_llm
    asyncio.run(r._apply_perspective_plan("solar panel adoption"))

    assert r.perspective_plan, "expected a non-empty perspective plan"
    labels = {item["perspective"] for item in r.perspective_plan}
    assert "general" in labels
    assert labels - {"general"}, "expected at least one non-general perspective label"
    for item in r.perspective_plan:
        assert set(item.keys()) >= {"perspective", "focus", "question"}


def test_apply_perspective_plan_failure_falls_back_silently():
    r = _researcher(research_perspectives=True)
    r.subquestions = ["What is the cost of solar panels?"]

    async def failing_llm(messages, **kwargs):
        raise RuntimeError("model endpoint unreachable")

    r._llm = failing_llm
    # Must not raise.
    asyncio.run(r._apply_perspective_plan("solar panel adoption"))
    assert r.perspective_plan == []


def test_apply_perspective_plan_off_by_default_makes_no_call():
    r = _researcher()  # research_perspectives defaults to False
    r.subquestions = ["What is the cost of solar panels?"]
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(messages)
        return GOOD_PERSPECTIVES_JSON

    r._llm = fake_llm
    asyncio.run(r._apply_perspective_plan("solar panel adoption"))

    assert calls == []
    assert r.perspective_plan == []


def test_create_plan_with_perspectives_off_makes_same_calls_as_before():
    """Setting off: identical to old behaviour — exactly one `_llm` call
    (the base RESEARCH_PLAN_PROMPT), no perspectives call, plan text
    unaffected."""
    r = _researcher()  # off by default
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(messages[0]["content"])
        return "{}"

    r._llm = fake_llm
    plan_text = asyncio.run(r._create_plan("What is the cost of solar panels?"))

    assert len(calls) == 1
    assert r.perspective_plan == []
    assert isinstance(plan_text, str)


def test_create_plan_with_perspectives_on_emits_labeled_plan_event():
    r = _researcher(research_perspectives=True)
    events = []
    r._progress = events.append

    responses = iter(["{}", GOOD_PERSPECTIVES_JSON])

    async def fake_llm(messages, **kwargs):
        return next(responses)

    r._llm = fake_llm
    asyncio.run(r._create_plan("What is the cost of solar panels?"))

    planning_events = [e for e in events if e.get("phase") == "planning"]
    assert planning_events, "expected a 'planning' progress event"
    last = planning_events[-1]
    assert last.get("perspectives") is True
    plan = last.get("plan")
    assert plan and any(item["perspective"] != "general" for item in plan)


def test_generate_queries_round_1_uses_perspective_plan_as_queries():
    r = DeepResearcher.__new__(DeepResearcher)
    r.research_plan = ""
    r.queries_used = set()
    r.perspective_plan = [
        {"perspective": "general", "focus": "", "question": "General question about the topic?"},
        {"perspective": "Skeptic", "focus": "", "question": "What are the strongest objections?"},
    ]
    r.query_perspectives = {}

    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append(messages)
        return '["should not be used"]'

    r._llm = fake_llm

    queries = asyncio.run(r._generate_queries("solar panel adoption", "", 1))

    assert queries == ["General question about the topic?", "What are the strongest objections?"]
    # Round 1 skipped the normal query-generation LLM call entirely.
    assert calls == []
    assert r.query_perspectives["What are the strongest objections?"] == "Skeptic"
    assert r.query_perspectives["General question about the topic?"] == "general"


def test_generate_queries_round_1_falls_back_to_llm_when_no_perspective_plan():
    r = DeepResearcher.__new__(DeepResearcher)
    r.research_plan = ""
    r.queries_used = set()
    r.perspective_plan = []
    r.query_perspectives = {}

    async def fake_llm(messages, **kwargs):
        return '["fallback query one", "fallback query two"]'

    r._llm = fake_llm
    queries = asyncio.run(r._generate_queries("solar panel adoption", "", 1))
    assert queries == ["fallback query one", "fallback query two"]


def test_merge_plan_keeps_one_slot_per_perspective_when_general_fills_budget():
    r = _researcher(research_perspectives=True, research_perspectives_max=3)
    general = [
        "How much does a residential installation cost?",
        "What is the typical project timeline?",
        "Which roof orientations perform best?",
        "How does battery storage affect payback?",
        "What are the local grid connection rules?",
    ]
    items = [
        {"perspective": "Installer", "focus": "", "question": "Which mounting hardware fails first in coastal climates?"},
        {"perspective": "Installer", "focus": "", "question": "How long do crews spend on permits versus wiring?"},
        {"perspective": "Skeptic", "focus": "", "question": "Do subsidies distort the claimed savings figures?"},
    ]
    merged = r._merge_perspective_plan(general, items)
    labels = [m["perspective"] for m in merged if m["perspective"] != "general"]
    assert "Installer" in labels and "Skeptic" in labels
    assert len(labels) <= 3


def test_merge_plan_ignores_topic_words_when_deduping():
    r = _researcher(research_perspectives=True)
    general = ["What is the public opinion on electric buses in Valencia?"]
    items = [
        {"perspective": "Operator", "focus": "",
         "question": "How would electric buses change operational costs for Valencia's transit system?"},
        {"perspective": "Echo", "focus": "",
         "question": "What is the public opinion about electric buses in Valencia?"},
    ]
    merged = r._merge_perspective_plan(general, items, topic="Should Valencia electrify its city bus fleet?")
    labels = [m["perspective"] for m in merged]
    assert "Operator" in labels
    assert "Echo" not in labels
