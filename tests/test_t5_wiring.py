"""Cabling this lot could not do: `src/agent_loop.py` is not T5's file
(`docs/spec/paridad/CONTRATO.md` ownership rules), so the two call sites
that must invoke `src.tool_result_offload.offload_if_oversized()` just
before `format_tool_result()`, and the `read_artifact` tool registration in
`src/tool_schemas.py`/`src/agent_tools/__init__.py` (T6's files), are left
as exact diffs in
`/tmp/claude-0/-home-claude/cd8d3c3e-16cc-5feb-bffa-6a4518d14f14/scratchpad/paridad_wave1/T5_wiring.md`
for the integrator to apply.

Both xfails here are mechanical, content-based checks on `src/agent_loop.py`
and `src/agent_tools/__init__.py` themselves — they flip to passing (and
must then be deleted per the contract) the moment those diffs land, with no
other change needed to this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_agent_loop_calls_offload_before_formatting_tool_results():
    text = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "tool_result_offload" in text
    assert "offload_if_oversized" in text


def test_read_artifact_tool_is_registered():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s.get("function", {}).get("name") for s in FUNCTION_TOOL_SCHEMAS}
    assert "read_artifact" in names
    assert "read_artifact" in TOOL_HANDLERS and "read_artifact" in TOOL_TAGS
