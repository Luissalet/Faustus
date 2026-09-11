"""Lote 67 — ola A wiring: `browser_actions.check_precondition` now checks
`require_element` via `src.browser_view.element_present` (exact `ref=`
identity) instead of a bare substring test.

The gap this closes: a snapshot whose old `ref=e1` is gone but a new,
unrelated `ref=e10` exists satisfies a substring check (`"e1" in text`,
since "e1" is a literal substring of "e10") — exactly the false "still
present" WEB-04 exists to prevent. `tests/test_web_browser_actions_readback.py`
and `tests/test_l36_browser_action_precondition.py` already cover the
ordinary paths (plain-text element labels, snapshots that don't parse as
ref-shaped elements at all) and stay green; this file isolates the one case
those did not: a snapshot that DOES parse as ref elements, where the stale
ref is a substring of a live one.
"""
from __future__ import annotations

from src.browser_actions import ActionPrecondition, check_precondition

# A real aria-snapshot shape (src.browser_view.parse_snapshot_elements
# format A) where the stale ref "e1" no longer exists, but "e10" — which
# CONTAINS "e1" as a substring — does.
SNAPSHOT_AFTER_LAYOUT_SHIFT = (
    "- Page URL: https://example.com/cart\n"
    "- Page Title: Cart\n"
    "- button \"Continue shopping\" [ref=e10]\n"
    "- button \"Checkout\" [ref=e12]\n"
)


def test_stale_ref_that_is_a_substring_of_a_live_ref_is_still_refused():
    reason = check_precondition(
        ActionPrecondition(require_element="e1"),
        current_url="https://example.com/cart",
        snapshot_text=SNAPSHOT_AFTER_LAYOUT_SHIFT,
    )
    assert reason is not None
    assert "e1" in reason


def test_a_ref_that_really_is_still_present_passes():
    reason = check_precondition(
        ActionPrecondition(require_element="e10"),
        current_url="https://example.com/cart",
        snapshot_text=SNAPSHOT_AFTER_LAYOUT_SHIFT,
    )
    assert reason is None


def test_uses_the_shared_element_present_classifier_not_a_reimplementation(monkeypatch):
    """Proves the integration point itself: patching
    src.browser_view.element_present changes what check_precondition
    decides."""
    import src.browser_view as browser_view

    calls = []

    def _fake_element_present(ref, snapshot_text):
        calls.append((ref, snapshot_text))
        return False

    monkeypatch.setattr(browser_view, "element_present", _fake_element_present)
    reason = check_precondition(
        ActionPrecondition(require_element="e10"),
        current_url="https://example.com/cart",
        snapshot_text=SNAPSHOT_AFTER_LAYOUT_SHIFT,
    )
    assert reason is not None
    assert calls == [("e10", SNAPSHOT_AFTER_LAYOUT_SHIFT)]
