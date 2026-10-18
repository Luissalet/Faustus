"""Preserve complete-input routing when skipping three impossible searches."""
import hashlib
import random

import pytest

from src import action_intents as intents


def _unfiltered(text, monkeypatch):
    # The exact same classifier/table/normalizer without the optimization.
    # No duplicate routing implementation or new expected precedence.
    with monkeypatch.context() as baseline:
        baseline.setattr(intents, "_ROUTING_PREFILTERS", {})
        return intents.classify_tool_intent(text)


@pytest.mark.parametrize("text", [
    "", "current-data-" * 5000,
    "weather in London", "FORECAST for Madrid", "London weather",
    "current price", "current prıce", "current prİce", "current STATUS",
    "hourly raın", "RAİN for tomorrow", "local precipitation", "showers now",
    "forecaſt in Rome", "forecast\u00a0for\u00a0Paris", "foreca\u0301st in Rome",
    "latest news", "live exchange", "current market", "online score",
    "current status", "current crate", "current-data-price", "rainbow current",
    "current\nprice", "current" + "." * 120 + "price",
    "current" + "." * 121 + "price",
    "hourly" + "." * 100 + "rain", "hourly" + "." * 101 + "rain",
    "forecast " + "x" * 80, "forecast " + "x" * 81,
    "How do I check the current price?", "can you explain the weather forecast",
    "open my calendar and check current weather", "take a note about rain",
    "research the current price", "control my screen and check the weather",
    "Añade una reunión mañana", "AÃ±ade una reuniÃ³n maÃ±ana",
    "current-data-" * 5000 + "\nweather in Madrid",
    "current-data-" * 5000 + "\ncurrent prıce",
    "current-data-" * 5000 + "\nhourly raın",
    "current-data-" * 5000 + "\nopen my calendar",
], ids=lambda text: f"{len(text)}chars-{hashlib.sha256(text.encode()).hexdigest()[:8]}")
def test_complete_intent_equals_unfiltered_including_unicode_and_tail(text, monkeypatch):
    assert intents.classify_tool_intent(text) == _unfiltered(text, monkeypatch)


def test_deterministic_mixed_corpus_keeps_category_reason_and_precedence(monkeypatch):
    generator = random.Random(880)
    pieces = ["current", "latest", "rain", "weather", "forecast", "status", "prıce",
        "hourly", "tomorrow", "now", "in", "for", "calendar", "add", "note",
        "How do I", "please", "research", "x", "資料", "cafe\u0301", "ß"]
    separators = [" ", "\n", "-", ".", "\u00a0", "\u200b", "_"]
    for _ in range(160):
        text = generator.choice(separators).join(generator.choices(pieces, k=8))
        assert intents.classify_tool_intent(text) == _unfiltered(text, monkeypatch)


def test_only_measured_three_patterns_have_necessary_literal_prefilters():
    assert set(intents._ROUTING_PREFILTERS) == {
        "bare weather lookup request", "rain lookup request", "current/latest lookup request",
    }
    patterns = {reason: pattern for _, reason, pattern in intents._ROUTING_PATTERNS}
    samples = ["weather in London", "London forecast", "current prıce", "current news",
        "local precipitation", "rain for tomorrow", "hourly showers", "hourly raın"]
    for reason, prefilter in intents._ROUTING_PREFILTERS.items():
        for text in samples:
            routing_text = intents._routing_text(text)
            if patterns[reason].search(routing_text):
                assert prefilter.search(routing_text)


def test_absent_literals_skip_only_the_impossible_expensive_searches(monkeypatch):
    class ImpossibleSearch:
        def search(self, text):
            raise AssertionError("An absent necessary literal must bypass this search")
    rows = tuple((category, reason,
        ImpossibleSearch() if reason in intents._ROUTING_PREFILTERS else pattern)
        for category, reason, pattern in intents._ROUTING_PATTERNS)
    monkeypatch.setattr(intents, "_ROUTING_PATTERNS", rows)
    result = intents.classify_tool_intent("current-data-" * 5000)
    assert result == intents.ToolIntent(False, reason="no tool-action pattern matched")
