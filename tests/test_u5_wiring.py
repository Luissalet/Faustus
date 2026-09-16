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


@pytest.mark.xfail(strict=True, reason="app.py wiring for setup_evolution_routes() "
                                       "is scratchpad/paridad_wave2/U5_wiring.md, not "
                                       "owned by U5 in this lot")
def test_evolution_routes_are_registered_in_app():
    import app as app_module

    paths = {route.path for route in app_module.app.routes}
    assert "/api/harness/revisions" in paths, (
        "routes/evolution_routes.py::setup_evolution_routes() is not yet "
        "app.include_router()'d in app.py — see U5_wiring.md")
