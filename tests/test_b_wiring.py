"""tests/test_b_wiring.py — xfail(strict=True) placeholder for lot B's one
integrator hook (see `B_wiring.md` at the worktree root for the exact diff).

`app.py` is not owned by this lot (COMMON.md's hard rules), so the router
registration is not made from here. This test pins the gap: it fails today,
and the moment the integrator applies the diff in `B_wiring.md` it flips to
an unexpected pass — at that point delete the marker and keep the assertion
as a plain regression check.
"""
from __future__ import annotations

import pytest


def test_app_registers_the_bug_hunt_router():
    import app as app_module

    paths = set()
    for r in app_module.app.routes:
        if hasattr(r, "path"):
            paths.add(r.path)
            continue
        inner = getattr(r, "original_router", None)
        prefix = getattr(getattr(r, "include_context", None), "prefix", "") or ""
        for sub in getattr(inner, "routes", []) or []:
            if hasattr(sub, "path"):
                paths.add(prefix + sub.path)

    assert "/api/bug-hunt" in paths
    assert "/api/bug-hunt/reports" in paths
