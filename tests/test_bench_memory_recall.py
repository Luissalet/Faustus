"""src/bench/memory_recall.py — deterministic memory-recall regression suite.

Covers: isolation (never touches the real data dir), pure metrics math on a
hand-made case, forbidden-leak detection via an injected fake recall
function, determinism across two runs, compare() deltas, and a floor
regression gate on the current code's baseline numbers (documented here so
the gate's margin is legible, not a magic number).
"""
from __future__ import annotations

import copy
import os

from src import memory_engine
from src.bench import memory_recall


# ---------------------------------------------------------------------------
# Isolation: run() must never touch the real data dir / vector store.
# ---------------------------------------------------------------------------

def test_run_is_isolated_from_real_data_dir(tmp_path, monkeypatch):
    real_dir = str(tmp_path / "real_data")
    os.makedirs(real_dir, exist_ok=True)
    monkeypatch.setattr(memory_engine, "DATA_DIR", real_dir)
    memory_engine.reset_vector_store()

    report = memory_recall.run(k_values=(1, 3))

    # The real dir got no memory_engine.db from this run.
    assert not os.path.exists(os.path.join(real_dir, memory_engine.DB_FILENAME))
    # DATA_DIR and the vector store are restored to what they were before run().
    assert memory_engine.DATA_DIR == real_dir
    assert report["corpus_size"] == len(memory_recall.CORPUS)


def test_run_rejects_unknown_embedder():
    import pytest
    with pytest.raises(ValueError):
        memory_recall.run(embedder="openai")


# ---------------------------------------------------------------------------
# Metrics math on a tiny hand-made case.
# ---------------------------------------------------------------------------

def test_score_ranked_hit_recall_mrr():
    ranked = ["a", "b", "c", "d", "e"]
    expected = {"c", "z"}  # "z" is never ranked -> recall can't reach 1.0
    result = memory_recall.score_ranked(ranked, expected, forbidden_ids=set(),
                                         k_values=(1, 3, 5))
    assert result["scored"] is True
    assert result["per_k"][1] == {"hit": 0, "recall": 0.0}
    # "c" enters the top-3 -> hit, recall = 1/2 (only "c" of {"c","z"} found)
    assert result["per_k"][3] == {"hit": 1, "recall": 0.5}
    assert result["per_k"][5] == {"hit": 1, "recall": 0.5}
    # first expected hit ("c") is at rank 3 -> MRR contribution 1/3
    assert abs(result["reciprocal_rank"] - (1 / 3)) < 1e-5
    assert result["leak_count"] == 0


def test_score_ranked_empty_expected_is_unscored_negative_probe():
    result = memory_recall.score_ranked(["a", "b"], expected_ids=set(),
                                         forbidden_ids={"b"}, k_values=(1, 3))
    assert result["scored"] is False
    # hit/recall are 0 by construction for an empty expected set (no
    # denominator to divide by), and that is intentional — see run()'s
    # eval_n bookkeeping, which excludes unscored queries from the average.
    assert result["per_k"][1] == {"hit": 0, "recall": 0.0}
    assert result["leak_count"] == 1
    assert result["leaked_ids"] == ["b"]


def test_percentile_basic():
    assert memory_recall._percentile([], 50) == 0.0
    assert memory_recall._percentile([10.0], 95) == 10.0
    # nearest-rank on a small, sorted sample
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert memory_recall._percentile(values, 50) == 3.0
    assert memory_recall._percentile(values, 100) == 5.0


# ---------------------------------------------------------------------------
# Forbidden-leak detection: inject a fake recall function that deliberately
# leaks a forbidden item and confirm the suite catches it.
# ---------------------------------------------------------------------------

def test_forbidden_leak_detection_catches_injected_leak():
    def leaking_recall(query, owner, project, k, now, key_to_id):
        ranked = memory_recall._default_recall(query, owner, project, k, now, key_to_id)
        # Smuggle a forbidden item (owner/project-scoped away from this
        # query) to the very front of every result, the way a broken scope
        # filter would.
        forbidden_key = "other_editor"
        if forbidden_key in key_to_id:
            ranked = [key_to_id[forbidden_key]] + ranked
        return ranked

    leaked_report = memory_recall.run(k_values=(1, 3, 5), recall_fn=leaking_recall)
    assert leaked_report["aggregate"]["forbidden_leak_count"] > 0

    clean_report = memory_recall.run(k_values=(1, 3, 5))
    assert clean_report["aggregate"]["forbidden_leak_count"] == 0


def test_forbidden_leak_detection_ignores_non_forbidden_injection():
    def honest_recall(query, owner, project, k, now, key_to_id):
        return memory_recall._default_recall(query, owner, project, k, now, key_to_id)

    report = memory_recall.run(k_values=(1,), recall_fn=honest_recall)
    assert report["aggregate"]["forbidden_leak_count"] == 0


# ---------------------------------------------------------------------------
# Determinism: two runs of the same code produce identical metrics (latency
# excluded — wall-clock timing legitimately varies run to run).
# ---------------------------------------------------------------------------

def _strip_latency(report):
    clean = copy.deepcopy(report)
    clean["aggregate"].pop("latency_p50_ms", None)
    clean["aggregate"].pop("latency_p95_ms", None)
    for row in clean["per_query"]:
        row.pop("latency_ms", None)
    return clean


def test_determinism_two_runs_same_numbers():
    report_a = memory_recall.run(k_values=(1, 3, 5))
    report_b = memory_recall.run(k_values=(1, 3, 5))
    assert _strip_latency(report_a) == _strip_latency(report_b)


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------

def test_compare_deltas():
    report_a = {
        "aggregate": {
            "mrr": 0.5, "forbidden_leak_count": 1,
            "latency_p50_ms": 10.0, "latency_p95_ms": 20.0,
            "per_k": {"1": {"hit_at_k": 0.5, "recall_at_k": 0.4},
                      "3": {"hit_at_k": 0.7, "recall_at_k": 0.6}},
        }
    }
    report_b = {
        "aggregate": {
            "mrr": 0.6, "forbidden_leak_count": 0,
            "latency_p50_ms": 8.0, "latency_p95_ms": 25.0,
            "per_k": {"1": {"hit_at_k": 0.6, "recall_at_k": 0.5},
                      "3": {"hit_at_k": 0.7, "recall_at_k": 0.6}},
        }
    }
    delta = memory_recall.compare(report_a, report_b)
    assert delta["mrr_delta"] == pytest_approx(0.1)
    assert delta["forbidden_leak_delta"] == -1
    assert delta["latency_p50_ms_delta"] == pytest_approx(-2.0)
    assert delta["latency_p95_ms_delta"] == pytest_approx(5.0)
    assert delta["per_k"]["1"]["hit_at_k_delta"] == pytest_approx(0.1)
    assert delta["per_k"]["1"]["recall_at_k_delta"] == pytest_approx(0.1)
    assert delta["per_k"]["3"]["hit_at_k_delta"] == pytest_approx(0.0)


def pytest_approx(value):
    import pytest
    return pytest.approx(value, abs=1e-6)


def test_compare_is_pure_never_mutates_inputs():
    report_a = memory_recall.run(k_values=(1,))
    report_b = memory_recall.run(k_values=(1,))
    snap_a = copy.deepcopy(report_a)
    snap_b = copy.deepcopy(report_b)
    memory_recall.compare(report_a, report_b)
    assert report_a == snap_a
    assert report_b == snap_b


# ---------------------------------------------------------------------------
# Floor regression gate — the current code's measured baseline, minus a
# margin. Numbers measured on this commit (see also the task report):
#   hit@1=0.812  hit@3=0.969  hit@5=0.969
#   recall@1=0.737 recall@3=0.943 recall@5=0.953
#   MRR=0.891  forbidden_leak_count=0
# A regression in memory_engine/two_tier_search/hash_embed ranking quality
# should fail this; unrelated churn should not.
# ---------------------------------------------------------------------------

def test_floor_hit_at_5_and_no_forbidden_leaks():
    report = memory_recall.run(k_values=(1, 3, 5))
    agg = report["aggregate"]
    assert agg["forbidden_leak_count"] == 0
    assert agg["per_k"]["5"]["hit_at_k"] >= 0.90  # measured 0.969, 0.07 margin
    assert agg["per_k"]["3"]["hit_at_k"] >= 0.88  # measured 0.969
    assert agg["per_k"]["1"]["hit_at_k"] >= 0.65  # measured 0.812
    assert agg["mrr"] >= 0.75                      # measured 0.891
