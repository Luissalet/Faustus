"""DESK-02 — UI verification journeys.

``src/browser_journey_verification.py`` did not exist before this lote: a
render bug like a button showing the literal text ``[object Object]``
passed every check a healthy HTTP 200 backend implied.
"""
import asyncio

import pytest

from src.browser_journey_verification import (
    DEFAULT_ASSERTIONS,
    JourneySnapshot,
    JourneyStep,
    assert_dom_contains,
    assert_no_accessibility_issues,
    assert_no_console_errors,
    assert_no_network_failures,
    assert_no_object_leak,
    detect_object_leak,
    run_journey,
)


def _snapshots(*snaps):
    it = iter(snaps)

    async def _next():
        return next(it)
    return _next


def test_detect_object_leak_finds_the_literal_string():
    assert detect_object_leak("<button>[object Object]</button>") == "[object Object]"
    assert detect_object_leak("<button>Save</button>") is None


def test_journey_passes_when_every_step_is_clean():
    steps = [
        JourneyStep("open", action=_noop, assertions=DEFAULT_ASSERTIONS),
        JourneyStep("click save", action=_noop, assertions=DEFAULT_ASSERTIONS),
    ]
    clean = JourneySnapshot(dom_text="<button>Save</button>")
    verdict = asyncio.run(run_journey(steps, _snapshots(clean, clean)))
    assert verdict.passed is True
    assert verdict.failed_step is None
    assert len(verdict.steps) == 2


# ---------------------------------------------------------------------------
# The acceptance criterion itself: the [object Object] case fails a
# render/journey check regardless of what the backend answered.
# ---------------------------------------------------------------------------

def test_object_leak_fails_the_journey_even_though_backend_is_fine():
    steps = [
        JourneyStep("open", action=_noop, assertions=DEFAULT_ASSERTIONS),
        JourneyStep("render list", action=_noop, assertions=DEFAULT_ASSERTIONS),
    ]
    ok = JourneySnapshot(dom_text="<div>fine</div>")
    broken = JourneySnapshot(
        dom_text="<button>[object Object]</button>",
        screenshot="data:image/png;base64,AAA",
    )
    verdict = asyncio.run(run_journey(steps, _snapshots(ok, broken)))
    assert verdict.passed is False
    assert verdict.failed_step == "render list"
    assert "object" in verdict.steps[-1].reason.lower()
    assert verdict.screenshot == "data:image/png;base64,AAA"


def test_journey_stops_at_the_first_failure_not_all_steps():
    calls = []

    def _tracker(name):
        async def _run():
            calls.append(name)
        return _run

    steps = [
        JourneyStep("a", action=_tracker("a"), assertions=DEFAULT_ASSERTIONS),
        JourneyStep("b", action=_tracker("b"), assertions=DEFAULT_ASSERTIONS),
        JourneyStep("c", action=_tracker("c"), assertions=DEFAULT_ASSERTIONS),
    ]
    broken = JourneySnapshot(dom_text="[object Object]")
    verdict = asyncio.run(run_journey(steps, _snapshots(broken, broken, broken)))
    assert verdict.failed_step == "a"
    assert calls == ["a"]  # b and c never ran


def test_console_network_and_accessibility_assertions():
    assert assert_no_console_errors(JourneySnapshot(console_messages=("TypeError: x is undefined",))) is not None
    assert assert_no_console_errors(JourneySnapshot(console_messages=("log: ok",))) is None
    assert assert_no_network_failures(JourneySnapshot(network_failures=("GET /api 500",))) is not None
    assert assert_no_network_failures(JourneySnapshot()) is None
    assert assert_no_accessibility_issues(JourneySnapshot(accessibility_issues=("button has no label",))) is not None
    assert assert_no_accessibility_issues(JourneySnapshot()) is None


def test_assert_dom_contains_checks_the_expected_content_rendered():
    check = assert_dom_contains("Welcome back")
    assert check(JourneySnapshot(dom_text="<h1>Welcome back</h1>")) is None
    assert check(JourneySnapshot(dom_text="<h1>Error</h1>")) is not None


async def _noop():
    return None
