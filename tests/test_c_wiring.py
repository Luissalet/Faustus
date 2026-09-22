"""tests/test_c_wiring.py — xfail(strict=True) placeholders for lot C's two
integrator hooks (see `C_wiring.md` at the worktree root for the exact
diffs).

Neither `src/agent_loop.py` nor `app.py` is owned by this lot (COMMON.md's
hard rules), so the actual calls are not made from here. These tests pin the
gap: they fail today, and the moment the integrator applies the diff in
`C_wiring.md`, both start passing — which flips the `xfail(strict=True)`
into an unexpected pass, a loud signal to delete the marker and keep the
assertions as plain regression checks.
"""
from __future__ import annotations

import inspect

import pytest


@pytest.mark.xfail(strict=True, reason="wired by the integrator (C_wiring.md #1)")
def test_agent_loop_injects_the_project_rules_block():
    from src import agent_loop
    source = inspect.getsource(agent_loop)
    assert "project_rules" in source, (
        "src/agent_loop.py does not yet call src.project_rules.block(); "
        "see C_wiring.md #1"
    )


@pytest.mark.xfail(strict=True, reason="wired by the integrator (C_wiring.md #2)")
def test_app_registers_the_library_routers():
    import app as app_module

    paths = {getattr(r, "path", None) for r in app_module.app.routes}
    assert "/api/rules/library" in paths, (
        "app.py does not yet register routes.project_rules_routes; see C_wiring.md #2"
    )
    assert "/api/skills/library" in paths, (
        "app.py does not yet register routes.skill_library_routes; see C_wiring.md #2"
    )
