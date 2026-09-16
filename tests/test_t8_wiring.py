"""T8 wiring gap (A15): `read_overflow` exists and works
(`src/agent_tools/context_overflow_tool.py`, proven directly by
`tests/acceptance/test_a15_reacquisition.py`) but is not yet registered in
`src/tool_schemas.py` / `src/agent_tools/__init__.py` — those files are not
owned by T8 in this lot (docs/spec/paridad/CONTRATO.md rule 6). Exact diff:
`T8_wiring.md`. This proves the gap so it cannot silently stay unwired.
"""
from __future__ import annotations

import pytest


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


def test_inlined_read_tool_schemas_match_their_handler_copies():
    """tool_schemas.py holds literal copies (the parity tests literal_eval the
    list); the handler modules hold the originals. Drift here would hand the
    model one schema and validate calls against another."""
    import src.agent_tools  # noqa: F401 - import order (see tool_schemas' cycle)
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.agent_tools.context_overflow_tool import TOOL_SCHEMA as RO
    from src.agent_tools.artifact_read_tool import TOOL_SCHEMA as RA
    names = {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}
    assert names["read_overflow"] == RO
    assert names["read_artifact"] == RA
