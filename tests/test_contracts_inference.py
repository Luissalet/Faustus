"""src/contracts/inference.py — INF-02 A1.

Round-trips for every shape, vocabulary rejections via `ContractError`, and
the one rule the whole spec insists on: `null` survives. A missing/unknown
boolean or number is never coerced into `False`/`0`; `unknown`/`unconfirmed`
are legitimate values, never errors.
"""
from __future__ import annotations

import pytest

from src.contracts import ContractError
from src.contracts import inference as inf


# ── EngineIdentity ───────────────────────────────────────────────────────────

def test_engine_identity_round_trips():
    raw = {
        "implementation": "llama-server",
        "version": "b4600",
        "build": "abc123",
        "platform": "linux",
        "host": "127.0.0.1",
        "port": 8080,
        "managed": "faustus",
        "generation": 2,
        "session_id": "serve-deadbeef",
    }
    engine = inf.EngineIdentity.parse(raw)
    assert engine.implementation == "llama-server"
    assert engine.generation == 2
    again = inf.EngineIdentity.parse(engine.to_dict())
    assert again == engine


def test_engine_identity_absent_fields_stay_none_not_defaults():
    engine = inf.EngineIdentity.parse({"implementation": "unknown"})
    assert engine.version is None
    assert engine.build is None
    assert engine.port is None
    assert engine.host is None
    # generation defaults to 1 (the first launch of a session), never 0.
    assert engine.generation == 1
    assert engine.managed == "faustus"


def test_engine_identity_rejects_bad_implementation():
    with pytest.raises(ContractError):
        inf.EngineIdentity.parse({"implementation": "llama.cpp"})


def test_engine_identity_rejects_unknown_key():
    with pytest.raises(ContractError):
        inf.EngineIdentity.parse({"implementation": "ollama", "bogus": 1})


# ── ModelDescriptor ──────────────────────────────────────────────────────────

def test_model_descriptor_round_trips_with_digest_confirmed():
    raw = {
        "artifact_id": "org/model",
        "revision": "main",
        "digest": "a" * 64,
        "architecture": "LlamaForCausalLM",
        "kind": "dense",
        "quantization": "q4_k_m",
        "total_params": 8_000_000_000,
        "active_params": 8_000_000_000,
        "mtp": False,
        "identity_state": "confirmed",
    }
    model = inf.ModelDescriptor.parse(raw)
    assert model.identity_state == "confirmed"
    assert inf.ModelDescriptor.parse(model.to_dict()) == model


def test_model_descriptor_without_digest_defaults_provisional():
    model = inf.ModelDescriptor.parse({"artifact_id": "org/model"})
    assert model.digest is None
    assert model.identity_state == "provisional"
    assert model.kind == "unknown"
    # mtp absent -> None, never False (absence is not "confirmed no MTP").
    assert model.mtp is None


def test_model_descriptor_mtp_null_survives():
    model = inf.ModelDescriptor.parse({"artifact_id": "x", "mtp": None})
    assert model.mtp is None
    assert model.to_dict()["mtp"] is None


def test_model_descriptor_rejects_bad_kind():
    with pytest.raises(ContractError):
        inf.ModelDescriptor.parse({"artifact_id": "x", "kind": "sparse"})


def test_model_descriptor_rejects_non_bool_mtp():
    with pytest.raises(ContractError):
        inf.ModelDescriptor.parse({"artifact_id": "x", "mtp": "yes"})


# ── HardwareSnapshot ─────────────────────────────────────────────────────────

def test_hardware_snapshot_round_trips_with_gpus():
    raw = {
        "host": "gpu-box",
        "gpus": [
            {"index": 0, "name": "RTX 4090", "uuid": "GPU-1", "bus_id": "0000:01:00.0",
             "vram_bytes": 25757220864, "provenance": "nvidia-smi"},
        ],
        "ram_bytes": 137438953472,
        "observed_at": "2026-09-12T00:00:00Z",
        "topology": "known",
        "provenance": "nvidia-smi",
    }
    snap = inf.HardwareSnapshot.parse(raw)
    assert snap.topology == "known"
    assert len(snap.gpus) == 1
    assert inf.HardwareSnapshot.parse(snap.to_dict()) == snap


def test_hardware_snapshot_defaults_to_unknown_topology_and_no_gpus():
    snap = inf.HardwareSnapshot.parse({"host": "box"})
    assert snap.topology == "unknown"
    assert snap.gpus == ()
    assert snap.ram_bytes is None


def test_hardware_snapshot_rejects_bad_topology():
    with pytest.raises(ContractError):
        inf.HardwareSnapshot.parse({"host": "box", "topology": "full"})


def test_gpu_info_rejects_unknown_key():
    with pytest.raises(ContractError):
        inf.HardwareSnapshot.parse({"host": "box", "gpus": [{"index": 0, "name": "x", "vendor": "nvidia"}]})


# ── CapabilityAssessment ─────────────────────────────────────────────────────

def _assessment_raw(**overrides):
    raw = {
        "option": "kv_cache",
        "requested": "q8_0",
        "support": "supported",
        "scope": "server_start",
        "requirements": ["option:flash_attn==on"],
        "effective": {"value": None, "state": "unconfirmed"},
        "benefit": {"state": "not_evaluated", "benchmark_id": None},
        "evidence": {"kind": "versioned_manifest", "observed_at": None},
        "reasons": [],
    }
    raw.update(overrides)
    return raw


def test_capability_assessment_round_trips_the_spec_example():
    a = inf.CapabilityAssessment.parse(_assessment_raw())
    assert a.effective.state == "unconfirmed"
    assert a.benefit.state == "not_evaluated"
    assert a.evidence.kind == "versioned_manifest"
    assert inf.CapabilityAssessment.parse(a.to_dict()) == a


def test_capability_assessment_requested_value_is_never_coerced():
    # An explicit False/0/[] must survive as itself, not vanish or become
    # equivalent to "absent".
    a = inf.CapabilityAssessment.parse(_assessment_raw(requested=False))
    assert a.requested is False
    a2 = inf.CapabilityAssessment.parse(_assessment_raw(requested=0))
    assert a2.requested == 0 and a2.requested is not False


def test_capability_assessment_rejects_bad_support_vocabulary():
    with pytest.raises(ContractError):
        inf.CapabilityAssessment.parse(_assessment_raw(support="maybe"))


def test_capability_assessment_rejects_bad_scope_vocabulary():
    with pytest.raises(ContractError):
        inf.CapabilityAssessment.parse(_assessment_raw(scope="conversation"))


def test_capability_assessment_effective_state_never_defaults_to_boolean():
    a = inf.CapabilityAssessment.parse(_assessment_raw(effective={"value": None, "state": "unconfirmed"}))
    assert a.effective.value is None
    assert a.effective.state == "unconfirmed"
    with pytest.raises(ContractError):
        inf.CapabilityAssessment.parse(_assessment_raw(effective={"value": None, "state": "yes"}))


# ── LaunchReceipt ────────────────────────────────────────────────────────────

def _receipt_raw(**overrides):
    raw = {
        "session_id": "serve-12345678",
        "engine": {"implementation": "llama-server", "managed": "faustus"},
        "model": {"artifact_id": "org/model", "kind": "dense"},
        "requested_cmd": "llama-server --ctx-size 16384",
        "final_cmd": "llama-server --ctx-size 16384",
        "rewrites": [],
        "plan": {"implementation": "llama-server", "options": {"ctx": 16384}},
        "assessments": [_assessment_raw(option="ctx", requested=16384, requirements=[])],
        "observed": {"n_ctx": 8192},
        "differences": [{"option": "ctx", "requested": 16384, "observed": 8192, "state": "mismatch"}],
        "checks": [{"name": "http_reachable", "state": "passed", "detail": ""}],
        "created_at": "2026-09-12T00:00:00Z",
        "verified_at": "2026-09-12T00:01:00Z",
        "verify_state": "verified",
    }
    raw.update(overrides)
    return raw


def test_launch_receipt_round_trips():
    r = inf.LaunchReceipt.parse(_receipt_raw())
    assert r.verify_state == "verified"
    assert r.differences[0].state == "mismatch"
    again = inf.LaunchReceipt.parse(r.to_dict())
    assert again == r


def test_launch_receipt_model_none_survives():
    r = inf.LaunchReceipt.parse(_receipt_raw(model=None))
    assert r.model is None
    assert r.to_dict()["model"] is None


def test_launch_receipt_defaults_pending_with_empty_lists():
    raw = _receipt_raw()
    del raw["verify_state"]
    del raw["verified_at"]
    raw["rewrites"] = []
    raw["assessments"] = []
    raw["differences"] = []
    raw["checks"] = []
    r = inf.LaunchReceipt.parse(raw)
    assert r.verify_state == "pending"
    assert r.verified_at is None
    assert r.assessments == ()


def test_launch_receipt_rejects_bad_verify_state():
    with pytest.raises(ContractError):
        inf.LaunchReceipt.parse(_receipt_raw(verify_state="done"))


def test_launch_receipt_requires_created_at():
    raw = _receipt_raw()
    del raw["created_at"]
    with pytest.raises(ContractError):
        inf.LaunchReceipt.parse(raw)


def test_launch_receipt_observed_over_32kb_is_rejected():
    raw = _receipt_raw(observed={"blob": "x" * 40_000})
    with pytest.raises(ContractError):
        inf.LaunchReceipt.parse(raw)


def test_rewrite_step_round_trips_inf01_shape():
    raw = _receipt_raw(rewrites=[{"step": "normalize_line_continuations", "before": "a\\\nb", "after": "a b"}])
    r = inf.LaunchReceipt.parse(raw)
    assert r.rewrites[0].step == "normalize_line_continuations"
    assert r.rewrites[0].before == "a\\\nb"


# ── ExecutionMetrics (INF-03's shape, fixed now) ─────────────────────────────

def test_execution_metrics_round_trips_with_mixed_sources():
    raw = {
        "phases": {
            "queue_wait_ms": {"value": 12.5, "source": "observed_client"},
            "load_ms": {"value": None, "source": "absent"},
            "prefill_ms": {"value": 340, "source": "reported_engine"},
            "generation_ms": {"value": 900, "source": "reported_engine"},
            "tools_ms": {"value": None, "source": "absent"},
            "total_ms": {"value": 1252.5, "source": "computed"},
        },
        "tokens": {
            "prompt": {"value": 128, "source": "reported_engine"},
            "generated": {"value": 64, "source": "reported_engine"},
        },
        "scope": "request",
        "engine": {"implementation": "llama-server"},
        "observed_at": "2026-09-12T00:00:00Z",
    }
    m = inf.ExecutionMetrics.parse(raw)
    assert m.phases.tools_ms.value is None
    assert m.phases.tools_ms.source == "absent"
    assert m.tokens.prompt.value == 128
    again = inf.ExecutionMetrics.parse(m.to_dict())
    assert again == m


def test_execution_metrics_defaults_every_phase_to_absent():
    m = inf.ExecutionMetrics.parse({"phases": {}, "tokens": {}, "scope": "instance"})
    for key in ("queue_wait_ms", "load_ms", "prefill_ms", "generation_ms", "tools_ms", "total_ms"):
        phase = getattr(m.phases, key)
        assert phase.value is None
        assert phase.source == "absent"


def test_execution_metrics_rejects_bad_scope():
    with pytest.raises(ContractError):
        inf.ExecutionMetrics.parse({"phases": {}, "tokens": {}, "scope": "session"})


def test_execution_metrics_rejects_bad_metric_source():
    with pytest.raises(ContractError):
        inf.ExecutionMetrics.parse({
            "phases": {"queue_wait_ms": {"value": 1, "source": "guessed"}},
            "tokens": {}, "scope": "request",
        })


def test_execution_metrics_notes_default_empty_and_round_trip():
    # No `notes` key at all: defaults to an empty tuple, not None/absent-field.
    m = inf.ExecutionMetrics.parse({"phases": {}, "tokens": {}, "scope": "request"})
    assert m.notes == ()
    assert m.to_dict()["notes"] == []

    # INF-03's overlap caveat: a phase caveat text survives a round trip
    # unchanged, same as every other field on this contract.
    raw = {
        "phases": {}, "tokens": {}, "scope": "request",
        "notes": ["phases overlap: engine and client clocks are not additive"],
    }
    with_notes = inf.ExecutionMetrics.parse(raw)
    assert with_notes.notes == ("phases overlap: engine and client clocks are not additive",)
    assert inf.ExecutionMetrics.parse(with_notes.to_dict()) == with_notes


# ── InferenceProfile / BenchmarkCase / BenchmarkRun / Comparison (INF-04) ───

_MODEL = {"artifact_id": "org/model"}
_ENGINE = {"implementation": "ollama"}


def _profile_raw(**overrides):
    raw = {
        "id": "profile-1", "label": "My profile", "model": _MODEL, "engine": _ENGINE,
        "hardware_id": None, "options": {"num_ctx": 8192}, "objective": "interactive",
        "created_at": "2026-09-12T00:00:00Z",
    }
    raw.update(overrides)
    return raw


def test_inference_profile_round_trips_and_derives_fingerprint():
    profile = inf.InferenceProfile.parse(_profile_raw())
    assert profile.evaluation == "not_evaluated"  # default
    assert profile.source == "manual"  # default
    assert profile.fingerprint  # computed, never blank
    again = inf.InferenceProfile.parse(profile.to_dict())
    assert again == profile


def test_inference_profile_fingerprint_changes_with_options_not_with_label():
    a = inf.InferenceProfile.parse(_profile_raw(id="a"))
    b = inf.InferenceProfile.parse(_profile_raw(id="b", label="Different label"))
    c = inf.InferenceProfile.parse(_profile_raw(id="c", options={"num_ctx": 4096}))
    assert a.fingerprint == b.fingerprint  # label/id are not part of the identity
    assert a.fingerprint != c.fingerprint  # options are


def test_inference_profile_rejects_bad_objective():
    with pytest.raises(ContractError):
        inf.InferenceProfile.parse(_profile_raw(objective="fast"))


def test_inference_profile_rejects_unknown_key():
    with pytest.raises(ContractError):
        inf.InferenceProfile.parse(_profile_raw(bogus=1))


def test_benchmark_case_round_trips_with_prompt():
    raw = {
        "id": "case-1", "suite": "es_conversation",
        "prompt": "¿Cuál es la capital de Francia?",
        "checks": [{"kind": "contains", "arg": "París"}, {"kind": "max_words", "arg": 10}],
        "max_tokens": 60, "tags": ["geografia"],
    }
    case = inf.BenchmarkCase.parse(raw)
    assert case.messages is None
    assert case.checks[0].kind == "contains"
    assert inf.BenchmarkCase.parse(case.to_dict()) == case


def test_benchmark_case_requires_prompt_or_messages():
    with pytest.raises(ContractError):
        inf.BenchmarkCase.parse({"id": "c", "suite": "s"})


def test_benchmark_case_rejects_both_prompt_and_messages():
    with pytest.raises(ContractError):
        inf.BenchmarkCase.parse({
            "id": "c", "suite": "s", "prompt": "hi",
            "messages": [{"role": "user", "content": "hi"}],
        })


def test_benchmark_case_rejects_bad_check_kind():
    with pytest.raises(ContractError):
        inf.BenchmarkCase.parse({
            "id": "c", "suite": "s", "prompt": "hi",
            "checks": [{"kind": "vibes"}],
        })


def _run_raw(**overrides):
    raw = {
        "id": "run-1", "suite_id": "es_conversation", "suite_version": "1.0.0",
        "profile": _profile_raw(), "baseline_run_id": None, "state": "planned",
        "budget": {"max_cases": 8, "max_seconds": 120, "max_generated_tokens": None, "repeats": 1},
        "conditions": {}, "samples": [], "summary": {}, "interruptions": [],
        "started_at": None, "finished_at": None,
    }
    raw.update(overrides)
    return raw


def test_benchmark_run_round_trips_with_samples():
    raw = _run_raw(
        state="completed",
        samples=[{
            "case_id": "case-1", "repeat": 1,
            "metrics": None,
            "quality": {"passed": True, "failed_checks": []},
            "output_chars": 12, "error": None,
        }],
        summary={"cases_run": 1, "cases_planned": 1, "quality_pass_rate": 1.0,
                 "gen_tps": {"median": 30.0, "p95": 32.0, "n": 3},
                 "ttft_ms": {"median": 200.0, "p95": 250.0, "n": 3}, "total_ms": 1000.0},
    )
    run = inf.BenchmarkRun.parse(raw)
    assert run.state == "completed"
    assert run.samples[0].quality.passed is True
    assert run.summary.gen_tps.n == 3
    assert inf.BenchmarkRun.parse(run.to_dict()) == run


def test_benchmark_run_rejects_bad_state():
    with pytest.raises(ContractError):
        inf.BenchmarkRun.parse(_run_raw(state="optimized"))


def test_benchmark_run_interruption_round_trips():
    raw = _run_raw(state="partial", interruptions=[
        {"at": "2026-09-12T00:05:00Z", "reason": "budget_seconds"},
    ])
    run = inf.BenchmarkRun.parse(raw)
    assert run.interruptions[0].reason == "budget_seconds"
    assert inf.BenchmarkRun.parse(run.to_dict()) == run


def _comparison_raw(**overrides):
    raw = {
        "baseline_run_id": "run-a", "candidate_run_id": "run-b",
        "verdict": "inconclusive", "reasons": ["n < 3 in candidate"],
        "deltas": {}, "sample_sizes": {"baseline": 3, "candidate": 2},
        "comparable": True,
    }
    raw.update(overrides)
    return raw


def test_comparison_round_trips():
    comparison = inf.Comparison.parse(_comparison_raw())
    assert comparison.verdict == "inconclusive"
    assert comparison.comparable is True
    assert inf.Comparison.parse(comparison.to_dict()) == comparison


def test_comparison_rejects_bad_verdict():
    with pytest.raises(ContractError):
        inf.Comparison.parse(_comparison_raw(verdict="better"))


def test_comparison_not_comparable_still_requires_reasons_field_present():
    comparison = inf.Comparison.parse(_comparison_raw(
        comparable=False, verdict="inconclusive", reasons=["different suite_id"],
    ))
    assert comparison.comparable is False
    assert comparison.reasons == ("different suite_id",)
