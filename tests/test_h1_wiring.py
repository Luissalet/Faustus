"""tests/test_h1_wiring.py — xfail(strict=True) placeholder for H1's wiring.

`src/ui_smoke.py` is ready and covered by `tests/test_h1.py`. Calling it
from the turn-close loop and folding its result into `TurnLedger` is the
INTEGRATOR's change (`src/agent_harness.py` / `src/agent_loop.py`), spelled
out exactly in
`/tmp/claude-0/-home-claude/cd8d3c3e-16cc-5feb-bffa-6a4518d14f14/scratchpad/harness_wave/H1_wiring.md`.

This file asserts, as plain tests, exactly what "wired" means — the wiring
landed in the integrator pass after the harness wave.
"""
from __future__ import annotations


from src.agent_harness import TurnLedger


def test_turn_ledger_has_ui_smoke_slot():
    ledger = TurnLedger(workspace=".")
    # §1: TurnLedger.__init__ carries a ui_smoke slot next to `tests`.
    assert hasattr(ledger, "ui_smoke")
    assert ledger.ui_smoke is None
    assert hasattr(ledger, "ui_smoke_runs")
    assert ledger.ui_smoke_runs == 0


def test_ui_verify_status_counts_ui_smoke_as_evidence_without_mcp_browser():
    """The chat #24 sub-turn-1 shape: UI files mutated, no MCP browser tool
    used, but ui_smoke itself ran and found nothing wrong. §2 of the wiring
    doc says this must read as "ok_smoke", not "missing" — the harness's
    own evidence, weaker than a human/MCP browser pass but real."""
    ledger = TurnLedger(workspace=".")
    ledger.events = [
        {"tool": "edit_file", "ok": True, "kind": "mutation", "paths": ["static/editor/app.mjs"]},
    ]
    ledger.ui_smoke = {"ran": True, "ok": True, "summary": "ui_smoke ok: 1 page(s), 2 asset(s) checked"}

    assert ledger.needs_ui_verify() is True
    assert ledger.has_browser_evidence() is False
    assert ledger.has_ui_smoke_evidence() is True
    assert ledger.ui_verify_status() == "ok_smoke"


def test_ui_verify_status_missing_when_ui_smoke_failed():
    """The actual chat #24 sub-turn-1 bug, reproduced: ui_smoke ran and
    caught the .mjs/text/plain mismatch. That must NOT read as verified."""
    ledger = TurnLedger(workspace=".")
    ledger.events = [
        {"tool": "edit_file", "ok": True, "kind": "mutation", "paths": ["static/editor/app.mjs"]},
    ]
    ledger.ui_smoke = {
        "ran": True, "ok": False,
        "summary": "ui_smoke FAILED: 1 asset(s) failed (e.g. /static/editor/app.mjs: content_type)",
    }

    assert ledger.has_ui_smoke_evidence() is False
    assert ledger.ui_verify_status() == "missing"


def test_mcp_browser_evidence_still_outranks_ui_smoke():
    """A real MCP browser pass is richer than the harness's own HTTP crawl
    and must still win — this is a floor, not a replacement. Already true
    today (has_browser_evidence short-circuits before ui_smoke is even
    consulted) and must stay true once §2 lands — a plain regression test,
    not an xfail placeholder."""
    from src.agent_harness import BROWSER_EVIDENCE_MARKERS
    ledger = TurnLedger(workspace=".")
    ledger.events = [
        {"tool": "edit_file", "ok": True, "kind": "mutation", "paths": ["static/editor/app.mjs"]},
        {"tool": BROWSER_EVIDENCE_MARKERS[0], "ok": True, "kind": "effect", "paths": []},
    ]
    ledger.ui_smoke = {"ran": True, "ok": False, "summary": "ui_smoke FAILED: ..."}

    assert ledger.has_browser_evidence() is True
    assert ledger.ui_verify_status() == "ok"


def test_summary_includes_ui_smoke():
    ledger = TurnLedger(workspace=".")
    ledger.ui_smoke = {"ran": True, "ok": True, "summary": "ui_smoke ok"}
    summary = ledger.summary()
    assert summary.get("ui_smoke") == ledger.ui_smoke
