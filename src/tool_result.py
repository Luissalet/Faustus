"""
tool_result.py — CALL-05: a typed result for one tool call.

`src/contracts/tool.py::ToolResult` gives spec v2's typed shape
(`status`/`output`/`evidence_refs`/`effects`/`error`/`uncertainty`) a home,
but every one of the ~40 files under `src/agent_tools/` still returns its
own ad hoc dict (`{"output"/"stdout", "error", "exit_code", ...}`, no two
quite the same) — rewriting all of them to construct a `ToolResult` directly
would be the "arquitectura L" MAPA_REUTILIZACION calls this ID, and would
touch dozens of files this lote does not own. `normalize_tool_result` is the
adapter instead: ONE function, applied at ONE point — `execute_tool_block`
in `src/tool_execution.py`, the single place every caller (chat, workflow,
subagent, MCP) already funnels through — that reads whatever ad hoc dict a
tool returned and classifies it into the typed `status` spec §34.5 asks for,
without changing a single line of what any tool itself returns.

The rule this exists to enforce, literally CALL-05's acceptance: a tool call
that transported cleanly (no exception reached `execute_tool_block`, the
dict came back) but reports a FUNCTIONAL error — `result["error"]` is set,
or `exit_code` is non-zero — normalizes to `status="failed"`, never
`"succeeded"`. A dict a tool marked `blocked`/`locked` (refused by a policy
gate before doing anything — `src/tool_capabilities.py::blocked_tool_result`,
the subagent file lock in `tool_execution.py`) is `"denied"`, distinct from
an ordinary failure: nothing was attempted. A dict carrying `status:
"conflict"` or `error_code: "BASE_REVISION_MISMATCH"` (EDIT-01/CALL-02's
stale-precondition refusal — `filesystem_tools.py::_base_revision_conflict`)
is `"conflict"`: the caller is told to reconcile, not that the tool broke.
Anything else with no error signal at all is `"succeeded"`.

This module never executes a tool and never mutates the dict it is given —
pure classification of an already-finished result.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from src.contracts.errors import ErrorInfo
from src.contracts.tool import ToolResult

#: Placeholder id used when a caller does not have (or does not care about)
#: a real `call_id`/`attempt_id` for this normalization — `execute_tool_block`
#: today has no OBS-01 `call_id` in scope (that lives one layer up, in
#: `src/agent_runs.py`), so this adapter must work without one rather than
#: refuse to classify a result for want of an id nobody has yet.
_UNKNOWN_ID = "unknown"


def _first_nonempty(*values: Optional[str]) -> str:
    for v in values:
        if v:
            return v
    return _UNKNOWN_ID


def normalize_tool_result(raw: Any, *, call_id: str = "", attempt_id: str = "") -> ToolResult:
    """Classify one tool's raw result dict into a typed `ToolResult`.

    `call_id`/`attempt_id` are optional and default to a documented
    placeholder (`"unknown"`, itself a valid `ref_id` — see
    `contracts.tool.ref_id`'s pattern) rather than raising: this adapter is
    meant to run at a point in the pipeline that may not have a real id for
    either yet, and refusing to classify a finished result for want of
    bookkeeping metadata would defeat its own purpose.
    """
    cid = _first_nonempty(call_id, _UNKNOWN_ID)
    aid = _first_nonempty(attempt_id, _UNKNOWN_ID)

    if not isinstance(raw, Mapping):
        # Not the dict shape every tool actually returns. The honest
        # classification is "cannot judge this", not a guessed success —
        # the same reasoning `outcome_unknown` exists for elsewhere in this
        # contract, just triggered by a malformed result instead of a
        # transport cut.
        return ToolResult(
            call_id=cid, attempt_id=aid, status="outcome_unknown",
            output=raw if raw is not None else None,
            uncertainty=_uncertainty(
                "tool result was not a mapping",
                "inspect the tool's raw output directly"),
        )

    if raw.get("blocked") or raw.get("locked"):
        return ToolResult(
            call_id=cid, attempt_id=aid, status="denied", output=dict(raw),
            error=ErrorInfo(
                code="permission.denied",
                message=str(raw.get("error") or "blocked by policy"),
                retryable=False, next_action="request_approval"),
        )

    if raw.get("status") == "conflict" or raw.get("error_code") == "BASE_REVISION_MISMATCH":
        subcode = str(raw.get("error_code") or "conflict").lower()
        return ToolResult(
            call_id=cid, attempt_id=aid, status="conflict", output=dict(raw),
            error=ErrorInfo(
                code=f"conflict.{subcode}",
                message=str(raw.get("error") or "the underlying resource changed"),
                retryable=False, next_action="read_current_and_reconcile"),
        )

    exit_code = raw.get("exit_code")
    nonzero_exit = isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and int(exit_code) != 0
    has_error = bool(raw.get("error"))
    if has_error or nonzero_exit:
        # This is the acceptance's own example: transport succeeded (the
        # call reached here, no exception), but the tool's own dict says the
        # ACTION failed — that must never read as "done".
        return ToolResult(
            call_id=cid, attempt_id=aid, status="failed", output=dict(raw),
            error=ErrorInfo(
                code="unknown.tool_error",
                message=str(raw.get("error") or f"exit_code {exit_code}"),
                retryable=False, next_action="fix_payload_and_retry"),
        )

    return ToolResult(call_id=cid, attempt_id=aid, status="succeeded", output=dict(raw))


def _uncertainty(reason: str, reconcile_action: str):
    from src.contracts.tool import ToolUncertainty
    return ToolUncertainty(reason=reason, reconcile_action=reconcile_action)


__all__ = ["normalize_tool_result"]
