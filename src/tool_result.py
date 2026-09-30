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
Declared partial/cancelled/failed/denied statuses survive normalization.
Explicit timeouts preserve uncertainty about effects. Other legacy mappings
with no error signal default to `"succeeded"`.

This module never executes a tool and never mutates the dict it is given —
pure classification of an already-finished result.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from src.contracts.errors import ErrorInfo
from src.contracts.tool import ToolResult

#: Placeholder id used when a caller does not have (or does not care about)
#: a real `call_id`/`attempt_id` for this normalization. The execution wrapper
#: forwards its call_id; legacy/headless callers and durable attempt identity
#: may still be missing, so classification must work without them.
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

    uncertain_status = (raw.get("status") in ("outcome_unknown", "timeout", "timed_out")
                        or raw.get("outcome_unknown") is True or raw.get("timed_out") is True)
    if not uncertain_status and (raw.get("blocked") or raw.get("locked") or raw.get("status") == "denied"):
        return ToolResult(
            call_id=cid, attempt_id=aid, status="denied", output=dict(raw),
            error=ErrorInfo(
                code="permission.denied",
                message=str(raw.get("error") or "blocked by policy"),
                retryable=False, next_action="request_approval"),
        )

    if uncertain_status:
        uncertainty = raw.get("uncertainty")
        uncertainty = uncertainty if isinstance(uncertainty, Mapping) else {}
        return ToolResult(
            call_id=cid, attempt_id=aid, status="outcome_unknown", output=dict(raw),
            uncertainty=_uncertainty(
                str(uncertainty.get("reason") or raw.get("error") or "the tool response was lost after dispatch"),
                str(uncertainty.get("reconcile_action") or raw.get("reconcile_action") or "read_current_state_before_retry")),
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

    if raw.get("status") in ("partial", "cancelled"):
        return ToolResult(
            call_id=cid, attempt_id=aid, status=raw["status"], output=dict(raw),
            error=(ErrorInfo(code="unknown.tool_error", message=str(raw["error"]),
                             retryable=False, next_action="read_current_state_before_retry")
                   if raw.get("error") else None),
        )

    exit_code = raw.get("exit_code")
    nonzero_exit = isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and int(exit_code) != 0
    has_error = bool(raw.get("error"))
    if raw.get("status") == "failed" or has_error or nonzero_exit:
        # This is the acceptance's own example: transport succeeded (the
        # call reached here, no exception), but the tool's own dict says the
        # ACTION failed — that must never read as "done".
        return ToolResult(
            call_id=cid, attempt_id=aid, status="failed", output=dict(raw),
            error=ErrorInfo(
                code="unknown.tool_error",
                message=str(raw.get("error") or (f"exit_code {exit_code}" if nonzero_exit else "tool reported failure")),
                retryable=False, next_action="fix_payload_and_retry"),
        )

    return ToolResult(call_id=cid, attempt_id=aid, status="succeeded", output=dict(raw))


def _uncertainty(reason: str, reconcile_action: str):
    from src.contracts.tool import ToolUncertainty
    return ToolUncertainty(reason=reason, reconcile_action=reconcile_action)


def effect_state(result: Optional[ToolResult]) -> str:
    """Project execution status without turning uncertainty into no effect."""
    if result is None or result.status in {"outcome_unknown", "cancelled"}:
        return "unknown"
    if result.status == "partial":
        return "partial"
    return "confirmed" if result.status == "succeeded" else "failed"


#: Effect classes whose failures cannot be read as "nothing happened" without
#: an explicit marker: they leave the process (or change something that stays).
_UNCERTAIN_ON_GENERIC_FAILURE = frozenset({"external", "sensitive"})


def effect_certainty(result: Optional[ToolResult], *, effect_class: str = "",
                     raw: Optional[Mapping[str, Any]] = None) -> str:
    """How sure we are about what this call did to the world.

    ``none``      certainly nothing happened (a read, a refusal, an explicit
                  "not dispatched")
    ``confirmed`` the effect happened
    ``partial``   part of it happened
    ``unknown``   it may have happened; do not repeat it without checking

    A generic failure of an external-effect call without an explicit
    not-dispatched marker is ``unknown``: an error string after dispatch does
    not prove the destination did nothing.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    if result is None or result.status in ("outcome_unknown", "cancelled"):
        return "unknown"
    if result.status == "partial":
        return "partial"
    if result.status == "succeeded":
        return "none" if effect_class in ("", "read") else "confirmed"
    if result.status in ("denied", "conflict") or raw.get("effect_not_dispatched") or raw.get("blocked"):
        return "none"
    return "unknown" if effect_class in _UNCERTAIN_ON_GENERIC_FAILURE else "none"


__all__ = ["normalize_tool_result", "effect_state", "effect_certainty"]
