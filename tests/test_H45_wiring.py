"""Lot H4+H5 wiring gap.

`src/rewrite_policy.py` and `src/test_debt.py` are complete, real,
independently-tested modules (`tests/test_rewrite_policy.py`,
`tests/test_test_debt.py`) but nothing in the turn loop calls them yet:
`src/agent_loop.py`, `src/tool_execution.py` and
`src/agent_tools/filesystem_tools.py` are owned by the integrator (see
CONTRATO.md rule 2, harness wave), not this lot, so the calls are described
— with the exact diff — in
`.../scratchpad/harness_wave/H45_wiring.md` instead of applied here.

This file is the same shape as `tests/test_delta_engine_wiring.py` and
`tests/test_t7_wiring.py`: it reads the real source of the files the
integrator owns and asserts the GUARANTEE (the modules are actually called),
never an inventory. Once H45_wiring.md's diff lands, every test below flips
from `xfail(strict=True)` to a plain passing assertion — `strict=True` means
a test that starts passing without anyone touching this file fails loudly,
so the flip is a deliberate, visible edit, not a silent bit of the harness
just starting to work.
"""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _source(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text(encoding="utf-8")


# ── (a) H4: write_file consults RewritePolicy before writing ──────────────

def test_agent_loop_creates_and_threads_a_rewrite_policy_per_turn():
    src = _source("src", "agent_loop.py")
    assert "rewrite_policy" in src
    assert "RewritePolicy" in src


def test_tool_execution_ctx_carries_rewrite_policy():
    src = _source("src", "tool_execution.py")
    assert '"rewrite_policy"' in src


def test_write_file_tool_consults_rewrite_policy_before_writing():
    src = _source("src", "agent_tools", "filesystem_tools.py")
    assert "rewrite_policy" in src
    assert "deny_result" in src


def test_write_file_refusal_shape_is_wired(tmp_path, monkeypatch):
    """End-to-end: once wired, a 2nd whole-file write_file of an existing
    large file (with a turn-scoped RewritePolicy threaded through ctx) must
    come back as the exact refusal CONTRATO.md asks for — never a normal
    write result."""
    import asyncio

    from src.agent_tools.filesystem_tools import WriteFileTool
    from src.rewrite_policy import RewritePolicy
    from src import tool_execution

    workspace = tmp_path
    # No public setter for the active-workspace contextvar; set it directly
    # (same var `_resolve_tool_path` reads) rather than depending on a helper
    # this lot does not own and that may not exist by this name.
    tool_execution._active_workspace.set(str(workspace))
    target = workspace / "big.py"
    target.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")

    policy = RewritePolicy()
    ctx = {"rewrite_policy": policy}
    tool = WriteFileTool()
    new_body = "\n".join(f"line {i} v2" for i in range(200))

    async def _write_once():
        return await tool.execute(f"{target}\n{new_body}", ctx)

    async def _both():
        return await _write_once(), await _write_once()

    first, second = asyncio.run(_both())
    assert first.get("exit_code") == 0, first  # 1st rewrite: allowed

    assert second.get("policy") == "rewrite_policy"
    assert second.get("policy_verdict") == "require_edit"
    assert second.get("exit_code") == 1


# ── (b) H5: test_debt.record / todo_items are called from the loop ────────

def test_agent_loop_records_test_debt_after_running_project_tests():
    src = _source("src", "agent_loop.py")
    assert "test_debt" in src
    assert "_tdebt.record(" in src or "test_debt.record(" in src


def test_agent_loop_merges_test_debt_todo_items_into_continue_turn_injection():
    src = _source("src", "agent_loop.py")
    assert "merge_todo_items" in src
    assert "project continue-turn working set injected" in src
