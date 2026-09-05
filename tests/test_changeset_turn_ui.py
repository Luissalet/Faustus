"""
The change set at the end of a turn: the wiring, and the card that shows it.

A rule nobody enforces is not a rule. Phase 5 gave Faustus a way to check "I
fixed it" against the diff; this pins that the check actually RUNS at the end
of every turn and that its worst answer — the answer named a file the
checkpoint did not see change — reaches the user's screen.

The wiring is checked against the loop, and the card against the component
that draws it (studio/src/screens/studio/Harness.tsx).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
LOOP_PY = (_REPO / "src/agent_loop.py").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None


# ── the wiring ────────────────────────────────────────────────────────────

def test_the_turn_builds_a_change_set_from_the_ledger_and_the_answer():
    """Source contract on `agent_loop`: the check has to run where the turn
    ends, from the ledger summary and the paths the ANSWER claimed."""
    block = LOOP_PY.split("# --- Harness summary:", 1)[1].split("# --- Final metrics", 1)[0]
    assert "from src import changesets as _changesets" in block
    assert "_harness.find_claimed_paths(full_response" in block, (
        "the claims have to come from what the answer said, not from the ledger; "
        "the ledger already knows what changed — the question is what was CLAIMED")
    assert "_changesets.from_turn(" in block and "_changesets.judge(" in block
    assert '_hsum["changeset"]' in block


def test_the_change_set_can_never_break_the_turn():
    """It is a report ABOUT the turn. A report that can take the turn down
    with it is worse than no report."""
    block = LOOP_PY.split("# --- Harness summary:", 1)[1].split("# --- Final metrics", 1)[0]
    inner = block.split("from src import changesets as _changesets", 1)[0]
    assert inner.rstrip().endswith("try:"), (
        "the change set needs its own try, inside the summary's")
    assert "except Exception as _cs_err" in block


def test_a_turn_with_a_change_set_is_always_streamed():
    """The card is only sent when something is worth saying. A change set is
    worth saying — otherwise the check runs and nobody ever sees it."""
    block = LOOP_PY.split("# --- Harness summary:", 1)[1].split("# --- Final metrics", 1)[0]
    condition = block.split("if (_ledger.events", 1)[1].split("):", 1)[0]
    assert '_hsum.get("changeset")' in condition


# ── the card ──────────────────────────────────────────────────────────────

def test_the_card_shows_the_verdict_and_both_kinds_of_mismatch():
    """A verdict alone is a grade; the two lists are what a person can act on.

    "Said it changed X and the checkpoint did not see it" is the claim that
    did not happen. "Changed Y without saying so" is the edit nobody
    mentioned. Both were the point of the feature, and both are easy to drop
    while keeping the headline.
    """
    src = (_REPO / "studio" / "src" / "screens" / "studio" / "Harness.tsx").read_text(encoding="utf-8")
    assert "summary.changeset?.verdict" in src, "the verdict word must be shown"
    assert "changeset.confidence" in src, "and the confidence beside it"
    assert 'data-testid="changeset-unsupported"' in src, "the claims the diff does not support"
    assert 'data-testid="changeset-unclaimed"' in src, "and the changes nobody claimed"
    # Both lists are warnings, not quiet prose: they are the reason to look.
    assert src.count('data-tone="warning"') >= 2


def test_the_card_says_nothing_when_there_is_nothing_to_say():
    """No change set, no lines. A card that always renders something trains
    people to ignore it."""
    src = (_REPO / "studio" / "src" / "screens" / "studio" / "Harness.tsx").read_text(encoding="utf-8")
    for guard in (
        "summary.changeset && summary.changeset.unsupported.length > 0",
        "summary.changeset && summary.changeset.unclaimed.length > 0",
    ):
        assert guard in src, f"missing guard: {guard}"
