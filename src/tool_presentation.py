"""Bounded tool outcome fields shared by live chat events and saved history."""
from collections.abc import Mapping

from src.tool_result import normalize_tool_result


def tool_result_fields(raw) -> dict:
    try:
        result = normalize_tool_result(raw)
        status = result.status
        uncertainty = result.uncertainty.to_mapping() if result.uncertainty else None
    except Exception:
        status = "outcome_unknown"
        uncertainty = {"reason": "The result could not be confirmed.",
                       "reconcile_action": "Review the current state before retrying."}
    if not uncertainty and status in {"partial", "cancelled"} and isinstance(raw, Mapping):
        uncertainty = raw.get("uncertainty")
    fields = {"result_status": status}
    if isinstance(uncertainty, Mapping):
        detail = {key: uncertainty[key][:512] for key in ("reason", "reconcile_action")
                  if isinstance(uncertainty.get(key), str) and uncertainty[key]}
        if detail:
            fields["uncertainty"] = detail
    return fields
