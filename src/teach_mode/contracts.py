"""Strict domain vocabulary for demonstrations and learned procedures."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence, Tuple

from src.contracts.base import ContractError, as_mapping, flag, reject_unknown, text, text_list

DEMONSTRATION_STATUSES: Tuple[str, ...] = (
    "recording", "paused", "recorded", "analyzing", "needs_clarification", "candidate",
    "interrupted", "cancelled", "failed",
)
PROCEDURE_STATUSES: Tuple[str, ...] = (
    "candidate", "simulating", "ready_for_replay", "replaying", "evaluating",
    "needs_correction", "validated", "approved", "installed", "established",
    "deprecated", "revoked", "quarantined",
)
DEMONSTRATION_TYPES: Tuple[str, ...] = (
    "tool_native", "workflow", "workspace", "computer_use", "hybrid",
)
STEP_CLASSES: Tuple[str, ...] = (
    "required_for_core", "professional_safeguard", "optional_bonus",
    "exploratory", "incidental", "unknown",
)


class TeachError(ContractError):
    pass


def demonstration_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "demonstration")
    allowed = {"title", "intent", "type", "project_id", "session_id", "source_refs",
               "target_refs", "completion_mode", "environment_fingerprint"}
    reject_unknown(data, allowed, "demonstration")
    kind = text(data, "type", "demonstration", required=False, default="tool_native")
    if kind not in DEMONSTRATION_TYPES:
        raise TeachError("demonstration.type", f"must be one of {list(DEMONSTRATION_TYPES)}", got=kind)
    return {
        "title": text(data, "title", "demonstration", max_len=240),
        "intent": text(data, "intent", "demonstration", max_len=8000),
        "type": kind,
        "project_id": text(data, "project_id", "demonstration", required=False, default="", max_len=200),
        "session_id": text(data, "session_id", "demonstration", required=False, default="", max_len=200),
        "source_refs": list(text_list(data, "source_refs", "demonstration", max_items=100, max_len=1000)),
        "target_refs": list(text_list(data, "target_refs", "demonstration", max_items=100, max_len=1000)),
        "completion_mode": text(data, "completion_mode", "demonstration", required=False, default="professional", max_len=40),
        "environment_fingerprint": text(data, "environment_fingerprint", "demonstration", required=False, default="", max_len=500),
    }


def observation_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "observation")
    allowed = {"kind", "tool", "arguments", "result", "success", "classification",
               "note", "before_ref", "after_ref", "artifact_refs", "duration_ms"}
    reject_unknown(data, allowed, "observation")
    classification = text(data, "classification", "observation", required=False,
                          default="unknown", max_len=40)
    if classification not in STEP_CLASSES:
        raise TeachError("observation.classification", f"must be one of {list(STEP_CLASSES)}",
                         got=classification)
    arguments = data.get("arguments", {})
    result = data.get("result", {})
    if not isinstance(arguments, (dict, list, str, int, float, bool, type(None))):
        raise TeachError("observation.arguments", "must be JSON-compatible", got=arguments)
    if not isinstance(result, (dict, list, str, int, float, bool, type(None))):
        raise TeachError("observation.result", "must be JSON-compatible", got=result)
    duration = data.get("duration_ms", 0)
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise TeachError("observation.duration_ms", "must be a non-negative whole number", got=duration)
    return {
        "kind": text(data, "kind", "observation", required=False, default="tool_call", max_len=80),
        "tool": text(data, "tool", "observation", required=False, default="", max_len=200),
        "arguments": arguments, "result": result,
        "success": flag(data, "success", "observation", default=True),
        "classification": classification,
        "note": text(data, "note", "observation", required=False, default="", max_len=4000),
        "before_ref": text(data, "before_ref", "observation", required=False, default="", max_len=1000),
        "after_ref": text(data, "after_ref", "observation", required=False, default="", max_len=1000),
        "artifact_refs": list(text_list(data, "artifact_refs", "observation", max_items=100, max_len=1000)),
        "duration_ms": duration,
    }


def replay_evidence(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "evidence")
    allowed = {"passed", "proof_refs", "limitations", "notes"}
    reject_unknown(data, allowed, "evidence")
    return {
        "passed": flag(data, "passed", "evidence", default=False),
        "proof_refs": list(text_list(data, "proof_refs", "evidence", max_items=100, max_len=1000)),
        "limitations": list(text_list(data, "limitations", "evidence", max_items=100, max_len=2000)),
        "notes": text(data, "notes", "evidence", required=False, default="", max_len=8000),
    }
