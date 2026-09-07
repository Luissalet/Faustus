import pytest

from src.branching_futures.contracts import BranchingError
from src.branching_futures.service import BranchingService
from src.durable_feature_store import DurableFeatureStore


@pytest.fixture
def futures(tmp_path):
    return BranchingService(DurableFeatureStore(str(tmp_path / "futures.db")))


def _future(futures):
    return futures.create(owner="alice", request={
        "title": "Choose implementation", "intent": "Find the strongest implementation",
        "project_id": "p1", "session_id": "s1", "mode": "simulate",
        "base_snapshot": {"state_revision": "abc"},
        "strategies": [{"id": "simple", "title": "Simple"},
                       {"id": "fast", "title": "Fast"}],
        "criteria": [{"name": "quality", "weight": 2, "direction": "max"},
                     {"name": "latency", "weight": 1, "direction": "min"}],
    })


def test_branches_share_base_and_forbid_real_external_effects(futures):
    future = _future(futures)
    assert len({branch["base_snapshot_id"] for branch in future["branches"]}) == 1
    assert all(branch["effect_policy"]["real_external_effects"] is False
               for branch in future["branches"])
    assert all(branch["namespace"].startswith("simulation:") for branch in future["branches"])


def test_evaluation_selection_and_commit_fails_closed_without_real_adapter(futures):
    future = _future(futures)
    scores = ({"quality": 9, "latency": 5}, {"quality": 7, "latency": 1})
    for branch, score in zip(future["branches"], scores):
        futures.start_branch(owner="alice", future_id=future["id"], branch_id=branch["id"])
        futures.submit_result(owner="alice", future_id=future["id"], branch_id=branch["id"],
                              result={"status": "completed", "scores": score,
                                      "proof_refs": [f"proof:{branch['id']}"]})
    evaluation = futures.evaluate(owner="alice", future_id=future["id"])
    winner = evaluation["ranking"][0]["branch_id"]
    futures.select(owner="alice", future_id=future["id"], branch_id=winner,
                   rationale="best weighted observed result")
    drift = futures.revalidate(owner="alice", future_id=future["id"],
                               real_state_fingerprint="changed")
    assert drift["status"] == "requires_regeneration"
    clean = futures.revalidate(owner="alice", future_id=future["id"],
                               real_state_fingerprint=future["base_fingerprint"])
    assert clean["status"] == "applicable"
    with pytest.raises(BranchingError, match="no state was changed"):
        futures.commit(owner="alice", future_id=future["id"],
                       real_state_fingerprint=future["base_fingerprint"],
                       proof_refs=["proof:real"], approved=True)
    assert futures.future(owner="alice", future_id=future["id"])["status"] == "selected"


def test_other_owner_cannot_see_future(futures):
    future = _future(futures)
    assert futures.future(owner="bob", future_id=future["id"]) is None


def test_missing_score_is_not_invented_and_blocking_gate_excludes(futures):
    future = futures.create(owner="alice", request={
        "title": "Safe choice", "intent": "compare", "mode": "simulate",
        "strategies": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "criteria": [{"name": "tests", "kind": "blocking"},
                     {"name": "quality", "kind": "scored", "weight": 1}],
    })
    a, b = future["branches"]
    futures.start_branch(owner="alice", future_id=future["id"], branch_id=a["id"])
    futures.start_branch(owner="alice", future_id=future["id"], branch_id=b["id"])
    futures.submit_result(owner="alice", future_id=future["id"], branch_id=a["id"], result={
        "status": "completed", "scores": {"quality": 99},
        "gate_results": {"tests": {"status": "failed", "proof_refs": ["proof:a"]}},
    })
    futures.submit_result(owner="alice", future_id=future["id"], branch_id=b["id"], result={
        "status": "completed",
        "gate_results": {"tests": {"status": "passed", "proof_refs": ["proof:b"]}},
    })
    evaluation = futures.evaluate(owner="alice", future_id=future["id"])
    rows = {row["branch_id"]: row for row in evaluation["ranking"]}
    assert rows[a["id"]]["eligible"] is False and rows[a["id"]]["failed_gates"] == ["tests"]
    assert rows[b["id"]]["eligible"] is False and rows[b["id"]]["score"] is None
    assert rows[b["id"]]["missing_scores"] == ["quality"]


def test_fusion_is_a_new_unevaluated_branch_and_respects_budget(futures):
    future = futures.create(owner="alice", request={
        "title": "Fuse", "intent": "combine", "mode": "simulate",
        "budget": {"max_branches": 3},
        "strategies": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
    })
    parents = [row["id"] for row in future["branches"]]
    for branch in future["branches"]:
        futures.start_branch(owner="alice", future_id=future["id"], branch_id=branch["id"])
        futures.submit_result(owner="alice", future_id=future["id"], branch_id=branch["id"],
                              result={"status": "completed", "scores": {"quality": 1}})
    futures.evaluate(owner="alice", future_id=future["id"])
    fused = futures.fuse(owner="alice", future_id=future["id"], parent_branch_ids=parents,
                         strategy={"id": "fusion", "title": "A plus B"})
    assert fused["parent_branch_ids"] == parents
    assert fused["status"] == "pending" and not fused["result_id"]
    futures.start_branch(owner="alice", future_id=future["id"], branch_id=fused["id"])
    futures.submit_result(owner="alice", future_id=future["id"], branch_id=fused["id"],
                          result={"status": "completed", "scores": {"quality": 1}})
    futures.evaluate(owner="alice", future_id=future["id"])
    with pytest.raises(BranchingError, match="cannot create a fusion branch"):
        futures.fuse(owner="alice", future_id=future["id"], parent_branch_ids=parents,
                     strategy={"id": "again", "title": "Again"})


def test_result_and_evaluation_require_the_declared_lifecycle(futures):
    future = _future(futures)
    first, second = future["branches"]
    with pytest.raises(BranchingError, match="start the branch"):
        futures.submit_result(owner="alice", future_id=future["id"], branch_id=first["id"],
                              result={"status": "completed", "scores": {"quality": 1, "latency": 1}})
    futures.start_branch(owner="alice", future_id=future["id"], branch_id=first["id"])
    futures.submit_result(owner="alice", future_id=future["id"], branch_id=first["id"],
                          result={"status": "completed", "scores": {"quality": 1, "latency": 1}})
    with pytest.raises(BranchingError, match="all branches must be terminal"):
        futures.evaluate(owner="alice", future_id=future["id"])
    with pytest.raises(BranchingError, match="evaluate the completed parent"):
        futures.fuse(owner="alice", future_id=future["id"],
                     parent_branch_ids=[first["id"], second["id"]],
                     strategy={"id": "fusion", "title": "Fusion"})


def test_no_eligible_candidate_is_inconclusive(futures):
    future = futures.create(owner="alice", request={
        "title": "Blocked", "intent": "compare", "mode": "simulate",
        "strategies": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "criteria": [{"name": "tests", "kind": "blocking"}],
    })
    for branch in future["branches"]:
        futures.start_branch(owner="alice", future_id=future["id"], branch_id=branch["id"])
        futures.submit_result(owner="alice", future_id=future["id"], branch_id=branch["id"], result={
            "status": "completed",
            "gate_results": {"tests": {"status": "failed", "proof_refs": ["proof:failed"]}},
        })
    evaluation = futures.evaluate(owner="alice", future_id=future["id"])
    assert not evaluation["tied_winner_branch_ids"]
    assert futures.future(owner="alice", future_id=future["id"])["status"] == "inconclusive"
