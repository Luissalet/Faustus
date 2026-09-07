"""Contracts for health, incidents and repair candidates."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from src.contracts.base import ContractError, as_mapping, flag, reject_unknown, text, text_list, whole

HEALTH_STATUSES: Tuple[str, ...] = (
    "healthy", "degraded", "failing", "quarantined", "unknown", "blocked_by_dependency",
)
INCIDENT_STATUSES: Tuple[str, ...] = (
    "open", "contained", "diagnosing", "repairing", "canary", "resolved", "rolled_back", "closed",
)
REPAIR_STATUSES: Tuple[str, ...] = (
    "candidate", "testing", "certified", "canary", "promoted", "rejected", "rolled_back",
)


class ImmuneError(ContractError):
    pass


def asset_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "asset")
    allowed = {"asset_id", "asset_version", "kind", "project_id", "dependencies",
               "fallback_refs", "health_contract", "metadata"}
    reject_unknown(data, allowed, "asset")
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(x, Mapping) for x in dependencies):
        raise ImmuneError("asset.dependencies", "must be a list of objects", got=dependencies)
    health = data.get("health_contract", {})
    if not isinstance(health, Mapping):
        raise ImmuneError("asset.health_contract", "must be an object", got=health)
    checks = health.get("checks", [])
    if not isinstance(checks, list) or any(not isinstance(x, str) or not x.strip() for x in checks):
        raise ImmuneError("asset.health_contract.checks", "must be a list of non-blank strings", got=checks)
    ttl = health.get("ttl_seconds", 3600)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 31_536_000:
        raise ImmuneError("asset.health_contract.ttl_seconds", "must be 1..31536000", got=ttl)
    metadata = data.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ImmuneError("asset.metadata", "must be an object", got=metadata)
    return {
        "asset_id": text(data, "asset_id", "asset", max_len=1000),
        "asset_version": text(data, "asset_version", "asset", max_len=300),
        "kind": text(data, "kind", "asset", max_len=100),
        "project_id": text(data, "project_id", "asset", required=False, default="", max_len=200),
        "dependencies": [dict(x) for x in dependencies],
        "fallback_refs": list(text_list(data, "fallback_refs", "asset", max_items=100, max_len=1000)),
        "health_contract": {"checks": [x.strip() for x in checks], "ttl_seconds": ttl},
        "metadata": dict(metadata),
    }


def assessment_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "assessment")
    allowed = {"status", "evidence_refs", "reason", "allowed_uses", "check_results"}
    reject_unknown(data, allowed, "assessment")
    status = text(data, "status", "assessment", max_len=50)
    if status not in HEALTH_STATUSES:
        raise ImmuneError("assessment.status", f"must be one of {list(HEALTH_STATUSES)}", got=status)
    checks = data.get("check_results", {})
    if not isinstance(checks, Mapping):
        raise ImmuneError("assessment.check_results", "must be an object", got=checks)
    return {"status": status,
            "evidence_refs": list(text_list(data, "evidence_refs", "assessment", max_items=100, max_len=1000)),
            "reason": text(data, "reason", "assessment", required=False, default="", max_len=4000),
            "allowed_uses": list(text_list(data, "allowed_uses", "assessment", max_items=20, max_len=100)),
            "check_results": dict(checks)}


def failure_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "failure")
    allowed = {"signature", "summary", "severity", "evidence_refs", "run_id", "auto_contain"}
    reject_unknown(data, allowed, "failure")
    severity = text(data, "severity", "failure", required=False, default="medium", max_len=20)
    if severity not in ("low", "medium", "high", "critical"):
        raise ImmuneError("failure.severity", "must be low, medium, high or critical", got=severity)
    return {"signature": text(data, "signature", "failure", max_len=500),
            "summary": text(data, "summary", "failure", max_len=4000),
            "severity": severity,
            "evidence_refs": list(text_list(data, "evidence_refs", "failure", max_items=100, max_len=1000)),
            "run_id": text(data, "run_id", "failure", required=False, default="", max_len=200),
            "auto_contain": flag(data, "auto_contain", "failure", default=True)}


def repair_request(value: Any) -> Dict[str, Any]:
    data = as_mapping(value, "repair")
    allowed = {"incident_id", "base_version", "candidate_version", "branch_result_ref",
               "delta_refs", "proof_refs", "description"}
    reject_unknown(data, allowed, "repair")
    return {"incident_id": text(data, "incident_id", "repair", max_len=200),
            "base_version": text(data, "base_version", "repair", max_len=300),
            "candidate_version": text(data, "candidate_version", "repair", max_len=300),
            "branch_result_ref": text(data, "branch_result_ref", "repair", required=False, default="", max_len=1000),
            "delta_refs": list(text_list(data, "delta_refs", "repair", max_items=100, max_len=1000)),
            "proof_refs": list(text_list(data, "proof_refs", "repair", max_items=100, max_len=1000)),
            "description": text(data, "description", "repair", required=False, default="", max_len=8000)}
