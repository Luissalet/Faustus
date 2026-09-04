"""
The change set at the end of a turn: the wiring, and the card that shows it.

A rule nobody enforces is not a rule. Phase 5 gave Faustus a way to check "I
fixed it" against the diff; this pins that the check actually RUNS at the end
of every turn and that its worst answer — the answer named a file the
checkpoint did not see change — reaches the user's screen.

The renderer is run for real under node (the same approach as
test_functional_verification_ui.py), because the one line that matters is a
string built out of a dict, and asserting on the source instead would pass
happily while the markup said something else.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
HARNESS_JS = (_REPO / "static/js/agentHarnessUI.js").read_text(encoding="utf-8")
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

def _render(data):
    script = (
        "globalThis.window = globalThis;\n"
        "globalThis.document = { addEventListener() {}, getElementById() { return null; } };\n"
        "globalThis.CSS = { escape: s => s };\n"
        + HARNESS_JS.replace("export function", "function")
                    .replace("export async function", "async function")
                    .replace("export default agentHarnessUI;", "")
        + f"\nconsole.log(JSON.stringify(_changesetLine({json.dumps(data)})));\n"
    )
    proc = subprocess.run(["node", "--input-type=module"], input=script,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_a_claim_the_diff_does_not_support_is_shown_with_what_happened_instead():
    """The line that carries the whole phase. A reader can check the file list
    themselves; "it said it edited cart.py and cart.py did not change" is
    exactly what a confident summary leaves out."""
    html = _render({
        "verdict": "contradicted", "confidence": 0.05,
        "unsupported_claims": [{"path": "src/cache.py", "claimed": "modified",
                                "reason": "nothing changed at that path"}],
        "unclaimed_changes": ["src/secrets.py"],
    })
    # `is-fail` and not a new class name: style.css styles these three, and a
    # red line that renders as ordinary text is a line nobody reads. Found by
    # actually looking at the card in a browser.
    assert "is-fail" in html
    assert "said it modified <code>src/cache.py</code>" in html
    assert "nothing changed at that path" in html
    assert "changed without being mentioned" in html
    assert "<code>src/secrets.py</code>" in html


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_a_report_that_stands_up_says_so_quietly():
    html = _render({"verdict": "proved", "confidence": 1.0,
                    "unsupported_claims": [], "unclaimed_changes": []})
    assert "is-ok" in html and "proved" in html
    assert "said it" not in html


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_the_doubts_are_shown_when_there_is_no_contradiction_to_show():
    html = _render({
        "verdict": "partial", "confidence": 0.65,
        "unsupported_claims": [], "unclaimed_changes": [],
        "uncertainty": [{"kind": "no_verification_runner",
                         "detail": "nothing ran that could prove the work"}]})
    assert "is-inconclusive" in html
    assert "no_verification_runner" in html


@pytest.mark.skipif(not _HAS_NODE, reason="node is required")
def test_no_change_set_renders_nothing_at_all():
    assert _render(None) == ""
    assert _render({}) == ""


def test_the_headline_only_interrupts_for_a_contradiction():
    """A verdict on every turn would become wallpaper. The headline says
    something only when the answer and the diff disagree — or, briefly, when
    they agree."""
    head = HARNESS_JS.split("const _cs = d.changeset || null;", 1)[1][:400]
    assert "'contradicted'" in head and "claims do not match the diff" in head
    assert "'proved'" in head
    assert "'partial'" not in head, (
        "partial is the ordinary state of a turn with no test runner; putting "
        "it in the headline would train everyone to ignore the line")


def test_the_card_shows_the_change_set_next_to_the_tests_and_the_review():
    body = HARNESS_JS.split("details.push(_testsLine(d.tests));", 1)[1][:200]
    assert "_changesetLine(d.changeset)" in body


def test_every_state_class_it_uses_is_one_the_stylesheet_styles():
    """A class nobody styled renders as ordinary text, and a warning that
    looks like body copy is a warning nobody reads."""
    css = (_REPO / "static/style.css").read_text(encoding="utf-8")
    block = HARNESS_JS.split("function _changesetLine(cs) {", 1)[1].split("\n}", 1)[0]
    used = {c for c in ("is-fail", "is-ok", "is-inconclusive", "is-warn", "is-bad")
            if f"'{c}'" in block}
    assert used, "the renderer sets no state class at all"
    for name in used:
        assert f".harness-review.{name}" in css, (
            f"{name} is set on a .harness-review but the stylesheet does not "
            "style it")
