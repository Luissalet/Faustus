"""U5 wiring gap: `routes/evolution_routes.py` (harness evolution — A26,
A27, A28, A30) is built and independently tested by
`tests/acceptance/test_a26_*.py` .. `test_a30_*.py`, but `app.py` is not
owned by U5 in this lot (`CONTRATO.md`'s ownership table) so it is not
registered there yet. Exact one-line diff: `U5_wiring.md`
(scratchpad/paridad_wave2/). This test proves the gap so it cannot silently
stay unwired once U5's files land.
"""
from __future__ import annotations

import pytest


def test_evolution_routes_are_registered_in_app():
    import app as app_module

    def _walk(routes):
        for r in routes:
            if hasattr(r, "path"):
                yield r.path
            inner = getattr(r, "original_router", None) or getattr(r, "router", None)
            if inner is not None and hasattr(inner, "routes"):
                yield from _walk(inner.routes)
    paths = set(_walk(app_module.app.routes))
    assert "/api/harness/revisions" in paths, (
        "routes/evolution_routes.py::setup_evolution_routes() is not yet "
        "app.include_router()'d in app.py — see U5_wiring.md")
