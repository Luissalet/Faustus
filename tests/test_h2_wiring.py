"""H2 (dependency_drift) is a pure-service lote — src/agent_loop.py is the
INTEGRATOR's file and is not touched here (harness_wave/CONTRATO.md rule 2).
This test proves the wiring anchor described in
`harness_wave/H2_wiring.md` is not yet applied; it is expected to XFAIL
until the integrator wires `src.dependency_drift.check_drift`/`system_note`
into the turn-start block of `src/agent_loop.py`, at which point it should
start passing and this xfail marker should be removed.
"""
from __future__ import annotations

import inspect

import pytest


@pytest.mark.xfail(strict=True, reason="H2 wiring not yet applied to src/agent_loop.py (see H2_wiring.md)")
def test_dependency_drift_is_wired_into_agent_loop():
    from src import agent_loop

    source = inspect.getsource(agent_loop)
    assert "dependency_drift" in source, (
        "src/agent_loop.py does not reference src.dependency_drift yet — "
        "apply the insertion described in harness_wave/H2_wiring.md "
        "(right after the 'project continue-turn working set injected' block, "
        "before 'round_num = 0')"
    )
    assert "check_drift" in source
    assert "system_note" in source
