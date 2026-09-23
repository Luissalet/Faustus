"""Lot S wiring — pins the exact diffs `S_wiring.md` hands the integrator.

`src/agent_loop.py` and `app.py` are integrator-owned files this lot may
not edit directly (see `COMMON.md`). These assertions describe the wired
state and are expected to fail until the integrator applies `S_wiring.md`
— hence `xfail(strict=True)`: this test must start passing the moment the
diff lands, and must fail again (loudly) if the wiring is ever reverted.

Only the two grep-testable pieces of `S_wiring.md` are pinned here (the
`sm.get_relevant_skills(...)` -> `selector.select(...)` swap at both call
sites, and the `app.py` router registration). The outcome-prior loop
(§3 of `S_wiring.md`) is explicitly left as a proposal, not a diff, since
the right insertion point among several `metrics = {...}` sites in
`agent_loop.py` is an integrator call this lot can't pin with a plain
source-text assertion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_agent_loop_uses_the_hybrid_selector_for_level1_injection():
    text = (REPO_ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "from src.skills_runtime import selector" in text
    assert "selector.select(" in text
    # The old direct call must be gone from both sites described in
    # S_wiring.md — a partial wiring (one site swapped, one left behind)
    # should not read as done.
    assert "sm.get_relevant_skills(" not in text
    assert "_sm.get_relevant_skills(" not in text


def test_app_registers_the_skill_selector_router():
    text = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "from routes.skill_selector_routes import setup_skill_selector_routes" in text
    assert "app.include_router(setup_skill_selector_routes())" in text
