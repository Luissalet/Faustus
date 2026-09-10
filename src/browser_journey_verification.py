"""UI verification journeys against a test browser (DESK-02).

``docs/spec/v2/backlog.json`` (DESK-02, existing anchor ``studio/checks``):
a UI is not "verified" because TypeScript compiled or a route answered HTTP
200 -- it is verified by actually running a journey in a browser and
checking what the page shows, says on the console, and asked the network
for. The acceptance criterion is concrete: a page whose buttons render the
literal text ``[object Object]`` must fail a render/journey check even
though the backend behind it is healthy.

This module is the runner, kept independent of any one app or fixture
source (Studio's own dev server, a fixture HTML file, or a real deployed
instance) by taking an INJECTED async snapshot function -- the same
dependency-injection shape ``src/browser_actions.run_with_precondition``
already uses for its own snapshot/act split, so a journey is testable with
a fake page and no real browser (per this lote's own instruction: "fixtures
controladas antes de la instancia real"). Wiring this against Studio's real
dev server or ``studio/checks/*.check.mjs`` is outside this lote's file
ownership (Studio is explicitly off limits) -- see the final report for the
exact integration point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JourneySnapshot:
    """What the page actually showed after one step -- DOM text, console,
    network, a screenshot reference and any accessibility findings. Every
    field is plain data so a fixture can build one without a browser."""

    dom_text: str = ""
    console_messages: Tuple[str, ...] = ()
    network_failures: Tuple[str, ...] = ()
    screenshot: Optional[str] = None
    accessibility_issues: Tuple[str, ...] = ()

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "dom_text": self.dom_text,
            "console_messages": list(self.console_messages),
            "network_failures": list(self.network_failures),
            "screenshot": self.screenshot,
            "accessibility_issues": list(self.accessibility_issues),
        }


SnapshotFn = Callable[[], Awaitable[JourneySnapshot]]
StepActionFn = Callable[[], Awaitable[Any]]
AssertionFn = Callable[[JourneySnapshot], Optional[str]]


@dataclass(frozen=True)
class JourneyStep:
    """One step of a journey: perform `action` (a click, a navigation, a
    form fill -- whatever the caller's page abstraction exposes), then check
    `assertions` against a FRESH snapshot taken right after."""

    name: str
    action: StepActionFn
    assertions: Tuple[AssertionFn, ...] = ()


@dataclass(frozen=True)
class JourneyStepResult:
    name: str
    passed: bool
    reason: str = ""
    snapshot: Optional[JourneySnapshot] = None

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "snapshot": self.snapshot.to_mapping() if self.snapshot is not None else None,
        }


@dataclass(frozen=True)
class JourneyVerdict:
    """A reproducible result: pass/fail, every step's own outcome, and (on
    failure) which step failed and the screenshot taken at that moment --
    DESK-02's own requirement, "resultado reproducible con paso fallido y
    captura"."""

    passed: bool
    steps: Tuple[JourneyStepResult, ...]
    failed_step: Optional[str] = None
    screenshot: Optional[str] = None

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "steps": [s.to_mapping() for s in self.steps],
            "failed_step": self.failed_step,
            "screenshot": self.screenshot,
        }


async def run_journey(steps: Sequence[JourneyStep], snapshot_fn: SnapshotFn) -> JourneyVerdict:
    """Run `steps` in order against `snapshot_fn`, stopping at the FIRST
    failing assertion.

    A step's assertions run against the snapshot taken right after its own
    action -- never a snapshot from an earlier or later step -- so a failure
    always points at the step that actually caused it. Stopping early
    (rather than running every step regardless) is deliberate: a page that
    is already broken after step 2 produces one meaningful failure, not five
    confusing ones caused by a broken precondition for steps 3-5.
    """
    results: List[JourneyStepResult] = []
    for step in steps:
        await step.action()
        snapshot = await snapshot_fn()
        reason: Optional[str] = None
        for check in step.assertions:
            reason = check(snapshot)
            if reason:
                break
        passed = reason is None
        results.append(JourneyStepResult(name=step.name, passed=passed, reason=reason or "", snapshot=snapshot))
        if not passed:
            return JourneyVerdict(
                passed=False,
                steps=tuple(results),
                failed_step=step.name,
                screenshot=snapshot.screenshot,
            )
    return JourneyVerdict(passed=True, steps=tuple(results))


# ---------------------------------------------------------------------------
# Reusable assertions
# ---------------------------------------------------------------------------

# Matches the stringified-object leak this ID's acceptance scenario names
# directly ("[object Object]"), and its less common cousins ([object Array],
# a custom class's default toString, etc.) so a near-miss is still caught.
_OBJECT_LEAK_RE = re.compile(r"\[object [A-Za-z][A-Za-z0-9]*\]")


def detect_object_leak(dom_text: str) -> Optional[str]:
    """The first stringified-object artefact found in `dom_text`, or
    ``None``. A button/label rendering an unstringified object is a render
    bug no HTTP status code can catch -- this is what actually catches it."""
    match = _OBJECT_LEAK_RE.search(dom_text or "")
    return match.group(0) if match else None


def assert_no_object_leak(snapshot: JourneySnapshot) -> Optional[str]:
    leak = detect_object_leak(snapshot.dom_text)
    if leak:
        return f"rendered DOM contains a stringified object ({leak!r}) instead of real text"
    return None


def assert_no_console_errors(snapshot: JourneySnapshot) -> Optional[str]:
    errors = [m for m in snapshot.console_messages if "error" in (m or "").lower()]
    if errors:
        return f"console logged {len(errors)} error message(s): {errors[0][:200]}"
    return None


def assert_no_network_failures(snapshot: JourneySnapshot) -> Optional[str]:
    if snapshot.network_failures:
        return f"{len(snapshot.network_failures)} network request(s) failed: {snapshot.network_failures[0][:200]}"
    return None


def assert_no_accessibility_issues(snapshot: JourneySnapshot) -> Optional[str]:
    if snapshot.accessibility_issues:
        return f"{len(snapshot.accessibility_issues)} accessibility issue(s): {snapshot.accessibility_issues[0][:200]}"
    return None


def assert_dom_contains(text: str) -> AssertionFn:
    """Build an assertion that the DOM contains `text` -- for "the page
    actually shows the thing this step was supposed to produce"."""

    def _check(snapshot: JourneySnapshot) -> Optional[str]:
        if text not in (snapshot.dom_text or ""):
            return f"expected {text!r} in the rendered DOM; it was not found"
        return None

    return _check


DEFAULT_ASSERTIONS: Tuple[AssertionFn, ...] = (
    assert_no_object_leak,
    assert_no_console_errors,
    assert_no_network_failures,
    assert_no_accessibility_issues,
)
