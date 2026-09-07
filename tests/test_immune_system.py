import pytest

from src.durable_feature_store import DurableFeatureStore
from src.immune_system.contracts import ImmuneError
from src.immune_system.service import ImmuneService


@pytest.fixture
def immune(tmp_path):
    return ImmuneService(DurableFeatureStore(str(tmp_path / "immune.db")))


def _asset(immune, asset_id="tool://renderer", dependencies=None):
    return immune.register_asset(owner="alice", request={
        "asset_id": asset_id, "asset_version": "1.0.0", "kind": "tool",
        "dependencies": dependencies or [], "fallback_refs": [],
        "health_contract": {"checks": ["smoke test"], "ttl_seconds": 3600},
    })


def test_failures_deduplicate_and_third_failure_quarantines(immune):
    asset = _asset(immune)
    for occurrence in range(3):
        incident = immune.report_failure(owner="alice", asset_id=asset["id"], failure={
            "signature": "timeout:provider", "summary": "provider timed out", "severity": "medium",
        })
        assert incident["occurrences"] == occurrence + 1
    current = immune.asset(owner="alice", asset_id=asset["id"])
    assert current["status"] == "quarantined"
    assert immune.capability_allowed(owner="alice", asset_id=asset["id"])[0] is False
    assert len(immune.incidents(owner="alice", asset_id=asset["id"])) == 1


def test_critical_dependency_blocks_consumer(immune):
    dependency = _asset(immune, "connector://remote")
    consumer = _asset(immune, "workflow://publish", dependencies=[
        {"ref": dependency["id"], "critical": True}
    ])
    immune.report_failure(owner="alice", asset_id=dependency["id"], failure={
        "signature": "auth", "summary": "auth revoked", "severity": "critical",
    })
    current = immune.asset(owner="alice", asset_id=consumer["id"])
    assert current["effective_status"] == "blocked_by_dependency"
    assert current["blocked_by"] == dependency["id"]


def test_repair_cannot_promote_without_certification_and_canary_proof(immune):
    asset = _asset(immune)
    incident = immune.report_failure(owner="alice", asset_id=asset["id"], failure={
        "signature": "broken", "summary": "broken", "severity": "high",
    })
    repair = immune.create_repair(owner="alice", asset_id=asset["id"], request={
        "incident_id": incident["id"], "base_version": "1.0.0",
        "candidate_version": "1.0.1", "proof_refs": [], "delta_refs": [],
    })
    with pytest.raises(ImmuneError):
        immune.transition_repair(owner="alice", repair_id=repair["id"], action="certify",
                                 evidence={"passed": True, "proof_refs": []})
    repair = immune.transition_repair(owner="alice", repair_id=repair["id"], action="certify",
                                      evidence={"passed": True, "proof_refs": ["proof:test"]})
    repair = immune.transition_repair(owner="alice", repair_id=repair["id"], action="start_canary")
    repair = immune.transition_repair(owner="alice", repair_id=repair["id"], action="promote",
                                      evidence={"passed": True, "proof_refs": ["proof:canary"]})
    assert repair["status"] == "promoted"
    current = immune.asset(owner="alice", asset_id=asset["id"])
    assert current["asset_version"] == "1.0.1"
    assert current["active_incident_refs"] == []
    assert immune.incident(owner="alice", incident_id=incident["id"])["status"] == "resolved"


def test_new_asset_version_does_not_inherit_old_health(immune):
    asset = _asset(immune)
    immune.assess(owner="alice", asset_id=asset["id"], assessment={
        "status": "healthy", "evidence_refs": ["proof:v1"]
    })
    updated = immune.register_asset(owner="alice", request={
        "asset_id": asset["id"], "asset_version": "2.0.0", "kind": "tool",
        "dependencies": [], "fallback_refs": [],
        "health_contract": {"checks": ["smoke test"], "ttl_seconds": 3600},
    })
    assert updated["status"] == "unknown" and updated["valid_until"] == ""


def test_manual_unquarantine_requires_fresh_assessment(immune):
    asset = _asset(immune)
    quarantined = immune.quarantine(owner="alice", asset_id=asset["id"], reason="manual investigation")
    assert quarantined["status"] == "quarantined"
    released = immune.unquarantine(owner="alice", asset_id=asset["id"], reason="ready to retest")
    assert released["status"] == "unknown"
    assert immune.capability_allowed(owner="alice", asset_id=asset["id"], purpose="normal")[0] is False


def test_healthy_assessment_requires_proof_and_cannot_bypass_quarantine(immune):
    asset = _asset(immune)
    with pytest.raises(ImmuneError, match="observed proof"):
        immune.assess(owner="alice", asset_id=asset["id"], assessment={"status": "healthy"})
    immune.quarantine(owner="alice", asset_id=asset["id"], reason="investigate")
    with pytest.raises(ImmuneError, match="cannot be bypassed"):
        immune.assess(owner="alice", asset_id=asset["id"], assessment={
            "status": "healthy", "evidence_refs": ["proof:smoke"]
        })


def test_repair_is_bound_to_the_current_version_and_changes_it(immune):
    asset = _asset(immune)
    incident = immune.report_failure(owner="alice", asset_id=asset["id"], failure={
        "signature": "broken", "summary": "broken", "severity": "high",
    })
    with pytest.raises(ImmuneError, match="currently registered"):
        immune.create_repair(owner="alice", asset_id=asset["id"], request={
            "incident_id": incident["id"], "base_version": "0.9.0",
            "candidate_version": "1.0.1",
        })
    with pytest.raises(ImmuneError, match="must differ"):
        immune.create_repair(owner="alice", asset_id=asset["id"], request={
            "incident_id": incident["id"], "base_version": "1.0.0",
            "candidate_version": "1.0.0",
        })


def test_deduped_failure_escalates_and_never_downgrades_quarantine(immune):
    asset = _asset(immune)
    incident = immune.report_failure(owner="alice", asset_id=asset["id"], failure={
        "signature": "same-cause", "summary": "intermittent", "severity": "low",
        "auto_contain": False,
    })
    assert incident["severity"] == "low"
    incident = immune.report_failure(owner="alice", asset_id=asset["id"], failure={
        "signature": "same-cause", "summary": "now destructive", "severity": "critical",
    })
    assert incident["severity"] == "critical"
    assert immune.asset(owner="alice", asset_id=asset["id"])["status"] == "quarantined"
    immune.report_failure(owner="alice", asset_id=asset["id"], failure={
        "signature": "another-cause", "summary": "minor too", "severity": "low",
        "auto_contain": False,
    })
    assert immune.asset(owner="alice", asset_id=asset["id"])["status"] == "quarantined"


def test_two_owners_can_register_the_same_logical_asset(immune):
    alice = _asset(immune)
    bob = immune.register_asset(owner="bob", request={
        "asset_id": alice["id"], "asset_version": "2.0.0", "kind": "tool",
        "dependencies": [], "fallback_refs": [],
        "health_contract": {"checks": ["bob smoke"], "ttl_seconds": 60},
    })
    assert bob["id"] == alice["id"]
    assert immune.asset(owner="alice", asset_id=alice["id"])["asset_version"] == "1.0.0"
    assert immune.asset(owner="bob", asset_id=alice["id"])["asset_version"] == "2.0.0"
