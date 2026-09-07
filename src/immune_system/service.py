"""Health verdicts, incident dedupe, containment and repair gates."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.base import now_iso
from src.immune_system import contracts, persistence

logger = logging.getLogger(__name__)
_service: Optional["ImmuneService"] = None
_SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat()


def _expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except Exception:
        return True


class ImmuneService:
    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _db(self):
        return self._store or persistence.store()

    def _emit(self, owner: str, name: str, kind: str, ident: str, payload: Mapping[str, Any]) -> None:
        self._db().emit(owner=owner, name=name, entity_kind=kind, entity_id=ident, payload=payload)

    def register_asset(self, *, owner: str, request: Any) -> Dict[str, Any]:
        data = contracts.asset_request(request)
        ident = data["asset_id"]
        existing = self._db().get("asset", ident, owner=owner)
        version_changed = bool(existing and existing.get("asset_version") != data["asset_version"])
        row = {**data, "id": ident,
               # A health result belongs to the version that was checked.  A
               # registry refresh must not let v2 inherit v1's green light.
               "status": "unknown" if version_changed else str((existing or {}).get("status") or "unknown"),
               "assessed_at": "" if version_changed else str((existing or {}).get("assessed_at") or ""),
               "valid_until": "" if version_changed else str((existing or {}).get("valid_until") or ""),
               "evidence_refs": list((existing or {}).get("evidence_refs") or []),
               "active_incident_refs": list((existing or {}).get("active_incident_refs") or []),
               "allowed_uses": list((existing or {}).get("allowed_uses") or []),
               "failure_count": int((existing or {}).get("failure_count") or 0)}
        saved = self._db().put("asset", row, owner=owner, project_id=data["project_id"],
                              status=row["status"],
                              expected_revision=int(existing["revision"]) if existing else None)
        self._emit(owner, "immune_asset_registered", "asset", ident,
                   {"asset_version": data["asset_version"], "kind": data["kind"]})
        return saved

    def asset(self, *, owner: str, asset_id: str, resolve_staleness: bool = True) -> Optional[Dict[str, Any]]:
        row = self._db().get("asset", asset_id, owner=owner)
        if row is None:
            return None
        if resolve_staleness and row.get("status") not in ("quarantined", "blocked_by_dependency") \
                and _expired(str(row.get("valid_until") or "")):
            row = dict(row)
            row["effective_status"] = "unknown"
            row["stale"] = True
        else:
            row["effective_status"] = row.get("status", "unknown")
            row["stale"] = False
        blocked = self._blocking_dependency(owner=owner, asset=row)
        if blocked:
            row["effective_status"] = "blocked_by_dependency"
            row["blocked_by"] = blocked
        return row

    def assets(self, *, owner: str, project_id: str = "", status: str = "",
               limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._db().list("asset", owner=owner, project_id=project_id, status=status, limit=limit)
        return [self.asset(owner=owner, asset_id=row["id"]) or row for row in rows]

    def _blocking_dependency(self, *, owner: str, asset: Mapping[str, Any],
                             seen: Optional[set] = None) -> str:
        seen = set(seen or ())
        current_id = str(asset.get("id") or asset.get("asset_id") or "")
        if current_id:
            if current_id in seen:
                return ""
            seen.add(current_id)
        for dependency in asset.get("dependencies", []) or []:
            if not isinstance(dependency, Mapping) or not dependency.get("critical", True):
                continue
            ref = str(dependency.get("ref") or "")
            child = self._db().get("asset", ref, owner=owner) if ref else None
            if not child:
                continue
            if child.get("status") in ("failing", "quarantined", "blocked_by_dependency"):
                return ref
            if self._blocking_dependency(owner=owner, asset=child, seen=seen):
                return ref
        return ""

    def assess(self, *, owner: str, asset_id: str, assessment: Any) -> Dict[str, Any]:
        asset = self._db().get("asset", asset_id, owner=owner)
        if asset is None:
            raise contracts.ImmuneError("asset_id", "not found")
        data = contracts.assessment_request(assessment)
        if data["status"] == "blocked_by_dependency":
            raise contracts.ImmuneError(
                "assessment.status", "blocked_by_dependency is derived from the dependency graph"
            )
        if data["status"] == "healthy":
            if not data["evidence_refs"]:
                raise contracts.ImmuneError(
                    "assessment.evidence_refs", "healthy requires observed proof"
                )
            if asset.get("status") == "quarantined" or asset.get("active_incident_refs"):
                raise contracts.ImmuneError(
                    "assessment.status",
                    "an active incident or quarantine cannot be bypassed by a direct healthy assessment; "
                    "unquarantine or promote a certified repair first",
                )
        ttl = int((asset.get("health_contract") or {}).get("ttl_seconds") or 3600)
        asset.update(data)
        asset.update({"status": data["status"], "assessed_at": now_iso(),
                      "valid_until": _future(ttl)})
        saved = self._db().put("asset", asset, owner=owner,
                              project_id=str(asset.get("project_id") or ""), status=data["status"],
                              expected_revision=int(asset["revision"]))
        self._emit(owner, "immune_health_assessed", "asset", asset_id,
                   {"status": data["status"], "valid_until": saved["valid_until"],
                    "evidence_refs": data["evidence_refs"]})
        return saved

    def report_failure(self, *, owner: str, asset_id: str, failure: Any) -> Dict[str, Any]:
        asset = self._db().get("asset", asset_id, owner=owner)
        if asset is None:
            raise contracts.ImmuneError("asset_id", "not found")
        data = contracts.failure_request(failure)
        existing = next((row for row in self._db().list("incident", owner=owner, limit=1000)
                         if row.get("asset_id") == asset_id and row.get("signature") == data["signature"]
                         and row.get("asset_version") == asset.get("asset_version")
                         and row.get("status") not in ("resolved", "closed")), None)
        if existing:
            incident = existing
            incident["occurrences"] = int(incident.get("occurrences") or 1) + 1
            incident["last_seen_at"] = now_iso()
            # Dedupe must not erase escalation.  A signature first observed as
            # medium and later reproduced as critical is a critical incident.
            previous_severity = str(incident.get("severity") or "medium")
            if _SEVERITY[data["severity"]] > _SEVERITY.get(previous_severity, 1):
                incident["severity"] = data["severity"]
                incident["summary"] = data["summary"]
            incident["evidence_refs"] = list(dict.fromkeys(
                list(incident.get("evidence_refs") or []) + data["evidence_refs"]))
            expected = int(existing["revision"])
        else:
            incident = {**data, "id": _id("incident"), "asset_id": asset_id,
                        "asset_version": asset.get("asset_version", ""), "status": "open",
                        "occurrences": 1, "first_seen_at": now_iso(), "last_seen_at": now_iso(),
                        "repair_candidate_refs": []}
            expected = None
        contain = asset.get("status") == "quarantined" or (data["auto_contain"] and (
            str(incident.get("severity") or data["severity"]) in ("high", "critical")
            or int(incident["occurrences"]) >= 3
        ))
        if contain:
            incident["status"] = "contained"
            asset["status"] = "quarantined"
            asset["allowed_uses"] = ["diagnostic_only"]
        else:
            asset["status"] = "failing"
            asset["allowed_uses"] = ["manual_with_warning", "diagnostic_only"]
        saved_incident = self._db().put("incident", incident, owner=owner,
                                      project_id=str(asset.get("project_id") or ""),
                                      status=incident["status"], expected_revision=expected)
        refs = list(asset.get("active_incident_refs") or [])
        if saved_incident["id"] not in refs:
            refs.append(saved_incident["id"])
        asset.update({"active_incident_refs": refs,
                      "failure_count": int(asset.get("failure_count") or 0) + 1,
                      "assessed_at": now_iso(), "valid_until": _future(300)})
        self._db().put("asset", asset, owner=owner, project_id=str(asset.get("project_id") or ""),
                       status=asset["status"], expected_revision=int(asset["revision"]))
        self._emit(owner, "immune_capability_quarantined" if contain else "immune_failure_detected",
                   "incident", saved_incident["id"],
                   {"asset_id": asset_id, "severity": data["severity"],
                    "occurrences": saved_incident["occurrences"]})
        return saved_incident

    def quarantine(self, *, owner: str, asset_id: str, reason: str) -> Dict[str, Any]:
        asset = self._db().get("asset", asset_id, owner=owner)
        if asset is None:
            raise contracts.ImmuneError("asset_id", "not found")
        reason = str(reason or "").strip()
        if not reason:
            raise contracts.ImmuneError("reason", "is required")
        asset.update({"status": "quarantined", "allowed_uses": ["diagnostic_only"],
                      "manual_quarantine_reason": reason[:4000], "assessed_at": now_iso(),
                      "valid_until": ""})
        saved = self._db().put("asset", asset, owner=owner,
                              project_id=str(asset.get("project_id") or ""), status="quarantined",
                              expected_revision=int(asset["revision"]))
        self._emit(owner, "immune_capability_quarantined", "asset", asset_id,
                   {"reason": reason[:4000], "manual": True})
        return saved

    def unquarantine(self, *, owner: str, asset_id: str, reason: str) -> Dict[str, Any]:
        asset = self._db().get("asset", asset_id, owner=owner)
        if asset is None:
            raise contracts.ImmuneError("asset_id", "not found")
        if asset.get("status") != "quarantined":
            raise contracts.ImmuneError("asset.status", "is not quarantined", got=asset.get("status"))
        reason = str(reason or "").strip()
        if not reason:
            raise contracts.ImmuneError("reason", "is required")
        # Removing containment never declares the asset healthy.  It returns to
        # unknown and must pass a fresh check before automatic routing trusts it.
        asset.update({"status": "unknown", "allowed_uses": ["diagnostic_only"],
                      "manual_unquarantine_reason": reason[:4000], "assessed_at": "",
                      "valid_until": ""})
        saved = self._db().put("asset", asset, owner=owner,
                              project_id=str(asset.get("project_id") or ""), status="unknown",
                              expected_revision=int(asset["revision"]))
        self._emit(owner, "immune_capability_unquarantined", "asset", asset_id,
                   {"reason": reason[:4000], "manual": True})
        return saved

    def incidents(self, *, owner: str, asset_id: str = "", status: str = "",
                  limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._db().list("incident", owner=owner, status=status, limit=limit)
        return [row for row in rows if not asset_id or row.get("asset_id") == asset_id]

    def incident(self, *, owner: str, incident_id: str) -> Optional[Dict[str, Any]]:
        return self._db().get("incident", incident_id, owner=owner)

    def create_repair(self, *, owner: str, asset_id: str, request: Any) -> Dict[str, Any]:
        asset = self._db().get("asset", asset_id, owner=owner)
        if asset is None:
            raise contracts.ImmuneError("asset_id", "not found")
        data = contracts.repair_request(request)
        incident = self._db().get("incident", data["incident_id"], owner=owner)
        if incident is None or incident.get("asset_id") != asset_id:
            raise contracts.ImmuneError("repair.incident_id", "not found for this asset")
        if incident.get("status") in ("resolved", "closed"):
            raise contracts.ImmuneError("repair.incident_id", "incident is already resolved")
        current_version = str(asset.get("asset_version") or "")
        if data["base_version"] != current_version:
            raise contracts.ImmuneError(
                "repair.base_version", "must match the currently registered asset version",
                got=data["base_version"],
            )
        if data["candidate_version"] == data["base_version"]:
            raise contracts.ImmuneError(
                "repair.candidate_version", "must differ from the base version"
            )
        row = {**data, "id": _id("repair"), "asset_id": asset_id, "status": "candidate",
               "certification": {}, "canary": {}, "rollback_version": data["base_version"]}
        saved = self._db().put("repair", row, owner=owner,
                              project_id=str(asset.get("project_id") or ""), status="candidate")
        refs = list(incident.get("repair_candidate_refs") or []) + [saved["id"]]
        incident["repair_candidate_refs"] = list(dict.fromkeys(refs))
        incident["status"] = "repairing"
        self._db().put("incident", incident, owner=owner,
                       project_id=str(asset.get("project_id") or ""), status="repairing",
                       expected_revision=int(incident["revision"]))
        self._emit(owner, "immune_repair_candidate_created", "repair", saved["id"],
                   {"asset_id": asset_id, "incident_id": data["incident_id"]})
        return saved

    def repair(self, *, owner: str, repair_id: str) -> Optional[Dict[str, Any]]:
        return self._db().get("repair", repair_id, owner=owner)

    def transition_repair(self, *, owner: str, repair_id: str, action: str,
                          evidence: Any = None) -> Dict[str, Any]:
        repair = self._db().get("repair", repair_id, owner=owner)
        if repair is None:
            raise contracts.ImmuneError("repair_id", "not found")
        allowed = {
            "certify": ({"candidate", "testing"}, "certified"),
            "start_canary": ({"certified"}, "canary"),
            "promote": ({"canary"}, "promoted"),
            "reject": ({"candidate", "testing", "certified", "canary"}, "rejected"),
            "rollback": ({"canary", "promoted"}, "rolled_back"),
        }
        if action not in allowed:
            raise contracts.ImmuneError("action", "unknown repair transition", got=action)
        sources, target = allowed[action]
        if repair.get("status") not in sources:
            raise contracts.ImmuneError("repair.status", f"cannot {action} from this status",
                                        got=repair.get("status"))
        proof = evidence if isinstance(evidence, Mapping) else {}
        if action in ("certify", "promote"):
            passed = proof.get("passed") is True
            proof_refs = proof.get("proof_refs") or []
            if not passed or not isinstance(proof_refs, list) or not proof_refs:
                raise contracts.ImmuneError("evidence", f"{action} requires passed=true and proof_refs")
        repair["status"] = target
        repair[action] = dict(proof)
        saved = self._db().put("repair", repair, owner=owner, status=target,
                              expected_revision=int(repair["revision"]))
        asset = self._db().get("asset", str(repair.get("asset_id") or ""), owner=owner)
        if asset and target == "promoted":
            asset.update({"asset_version": repair["candidate_version"], "status": "healthy",
                          "allowed_uses": [], "assessed_at": now_iso(),
                          "valid_until": _future(int((asset.get("health_contract") or {}).get("ttl_seconds") or 3600))})
            self._db().put("asset", asset, owner=owner,
                           project_id=str(asset.get("project_id") or ""), status="healthy",
                           expected_revision=int(asset["revision"]))
            # Promotion resolves the incident that produced this candidate;
            # old active refs must not survive on a healthy asset.
            incident = self._db().get("incident", str(repair.get("incident_id") or ""), owner=owner)
            if incident:
                incident.update({"status": "resolved", "resolved_at": now_iso(),
                                 "resolution_repair_id": repair_id})
                self._db().put("incident", incident, owner=owner,
                               project_id=str(asset.get("project_id") or ""), status="resolved",
                               expected_revision=int(incident["revision"]))
            fresh_asset = self._db().get("asset", str(repair.get("asset_id") or ""), owner=owner)
            if fresh_asset:
                fresh_asset["active_incident_refs"] = [
                    ref for ref in fresh_asset.get("active_incident_refs", [])
                    if ref != repair.get("incident_id")
                ]
                self._db().put("asset", fresh_asset, owner=owner,
                               project_id=str(fresh_asset.get("project_id") or ""), status="healthy",
                               expected_revision=int(fresh_asset["revision"]))
        elif asset and target == "rolled_back":
            asset.update({"asset_version": repair["rollback_version"], "status": "degraded",
                          "allowed_uses": ["manual_with_warning"]})
            self._db().put("asset", asset, owner=owner,
                           project_id=str(asset.get("project_id") or ""), status="degraded",
                           expected_revision=int(asset["revision"]))
        self._emit(owner, f"immune_repair_{target}", "repair", repair_id,
                   {"asset_id": repair.get("asset_id"), "proof_refs": proof.get("proof_refs", [])})
        return saved

    def capability_allowed(self, *, owner: str, asset_id: str,
                           purpose: str = "normal") -> Tuple[bool, str]:
        asset = self.asset(owner=owner, asset_id=asset_id)
        if asset is None:
            return True, "unregistered"
        status = str(asset.get("effective_status") or "unknown")
        if status == "healthy":
            return True, "healthy"
        if status == "degraded" and purpose in ("manual", "diagnostic"):
            return True, "degraded_manual"
        if status in ("failing", "quarantined", "blocked_by_dependency"):
            return False, status
        return (purpose == "diagnostic"), "health_unknown_or_stale"

    def events(self, *, owner: str, since: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        return self._db().events(owner=owner, since=since, limit=limit)


def service() -> ImmuneService:
    global _service
    if _service is None:
        _service = ImmuneService()
    return _service


def reset_service() -> None:
    global _service
    _service = None


def capability_allowed(*, owner: str, asset_id: str, purpose: str = "normal") -> Tuple[bool, str]:
    return service().capability_allowed(owner=owner, asset_id=asset_id, purpose=purpose)
