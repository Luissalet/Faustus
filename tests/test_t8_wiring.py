"""T8 wiring gap (A15): `read_overflow` exists and works
(`src/agent_tools/context_overflow_tool.py`, proven directly by
`tests/acceptance/test_a15_reacquisition.py`) but is not yet registered in
`src/tool_schemas.py` / `src/agent_tools/__init__.py` — those files are not
owned by T8 in this lot (docs/spec/paridad/CONTRATO.md rule 6). Exact diff:
`T8_wiring.md`. This proves the gap so it cannot silently stay unwired.
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=True, reason=(
    "read_overflow (src/agent_tools/context_overflow_tool.py) is not yet "
    "registered in src.tool_schemas.FUNCTION_TOOL_SCHEMAS or "
    "src.agent_tools.TOOL_HANDLERS -- see T8_wiring.md for the exact diff. "
    "Once the integrator applies it this test starts passing (and should "
    "be deleted or have its xfail marker removed)."
))
def test_read_overflow_tool_is_registered_in_the_live_tool_loop():
    from src import agent_tools
    from src import tool_schemas

    assert "read_overflow" in agent_tools.TOOL_HANDLERS, (
        "read_overflow missing from agent_tools.TOOL_HANDLERS"
    )
    names = {
        entry.get("function", {}).get("name")
        for entry in tool_schemas.FUNCTION_TOOL_SCHEMAS
        if isinstance(entry, dict)
    }
    assert "read_overflow" in names, (
        "read_overflow missing from tool_schemas.FUNCTION_TOOL_SCHEMAS"
    )
