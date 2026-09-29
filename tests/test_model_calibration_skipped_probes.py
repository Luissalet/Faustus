"""Keep observations distinct from attempts in a disposable manifest store."""
from copy import deepcopy

import pytest

from src import model_calibration as calibration, model_capabilities as capabilities

KEY = "synthetic-model"
OBSERVED_AT = "2026-09-29T12:00:00Z"
ATTEMPTED_AT = "2026-09-30T12:00:00Z"
ANNOUNCED = {"capabilities": {"tools": True}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    # No side write into creator/model_identity.db, including personal stores.
    monkeypatch.setattr(calibration, "_record_deployment_evidence", lambda *a, **kw: None)
    return str(tmp_path)


def save(store, result, announced=ANNOUNCED):
    return calibration.save_tested(KEY, {calibration.TEST_TOOL_CALLING: result},
        announced=announced, data_dir=store)


def observation(ok):
    return {"ok": ok, "tested_at": OBSERVED_AT,
        "evidence": {"response": "synthetic observed response", "method": "native-stream"},
        "source": "synthetic probe"}


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("attempt", [
    {"ok": None, "tested_at": ATTEMPTED_AT, "evidence": {"skipped": "time budget exceeded"}},
    {"ok": None, "tested_at": ATTEMPTED_AT, "evidence": {"skipped": "vision not announced"}},
    {"ok": None, "tested_at": ATTEMPTED_AT, "evidence": {"error": "synthetic transport error"}},
])
def test_unobserved_attempt_preserves_pass_or_failure_and_original_evidence(store, ok, attempt):
    original = observation(ok)
    save(store, original)
    incoming = deepcopy(attempt)
    save(store, incoming)
    restored = calibration.get_manifest(KEY, data_dir=store)
    result = restored["tested"][calibration.TEST_TOOL_CALLING]
    assert {k: result[k] for k in original} == original
    assert incoming == attempt
    assert result["last_attempt"] == {"ok": None, "attempted_at": ATTEMPTED_AT,
        "status": "skipped" if "skipped" in attempt["evidence"] else "unknown"}
    assertion = capabilities.assertions_from_calibration_manifest(restored)[capabilities.CAP_TOOL_CALL]
    assert assertion.status == (capabilities.ASSERTION_VERIFIED if ok else capabilities.ASSERTION_UNSUPPORTED)
    assert assertion.tested_at == OBSERVED_AT
    assert assertion.source == capabilities.SOURCE_CAPABILITY_PROBE
    assert bool(restored["degraded"]) is (not ok)


def test_initial_skipped_probe_is_unknown_without_prior_observation(store):
    save(store, {"ok": None, "tested_at": ATTEMPTED_AT,
        "evidence": {"skipped": "time budget exceeded"}}, announced={"capabilities": {}})
    restored = calibration.get_manifest(KEY, data_dir=store)
    assert restored["tested"][calibration.TEST_TOOL_CALLING]["ok"] is None
    assert "last_attempt" not in restored["tested"][calibration.TEST_TOOL_CALLING]
    assert capabilities.assertions_from_calibration_manifest(restored)[capabilities.CAP_TOOL_CALL].status == capabilities.ASSERTION_UNKNOWN


@pytest.mark.parametrize("old_ok,new_ok", [(True, False), (False, True), (True, True), (False, False)])
def test_new_boolean_observation_replaces_previous_and_attempt_note(store, old_ok, new_ok):
    save(store, observation(old_ok))
    save(store, {"ok": None, "evidence": {"skipped": "budget"}})
    newest = {"ok": new_ok, "tested_at": ATTEMPTED_AT, "evidence": {"response": "new observed evidence"}}
    save(store, newest)
    restored = calibration.get_manifest(KEY, data_dir=store)
    assert restored["tested"][calibration.TEST_TOOL_CALLING] == newest
    assert capabilities.assertions_from_calibration_manifest(restored)[capabilities.CAP_TOOL_CALL].tested_at == ATTEMPTED_AT


def test_attempt_note_is_bounded_latest_only_and_has_no_error_or_prompt_body(store):
    save(store, observation(True))
    for i in range(20):
        save(store, {"ok": None, "tested_at": "x" * 100, "evidence": {
            "error": "synthetic private error body", "request": "synthetic private prompt", "attempt": i}})
    result = calibration.get_manifest(KEY, data_dir=store)["tested"][calibration.TEST_TOOL_CALLING]
    assert result["last_attempt"] == {"ok": None, "attempted_at": "x" * 64, "status": "unknown"}
    assert result["evidence"] == observation(True)["evidence"]


def test_other_capabilities_and_announced_refresh_still_merge(store):
    save(store, observation(False))
    calibration.save_tested(KEY, {calibration.TEST_TOOL_CALLING: {"ok": None},
        calibration.TEST_JSON_MODE: {"ok": True, "tested_at": ATTEMPTED_AT}},
        announced={"capabilities": {"tools": True, "vision": True}}, data_dir=store)
    restored = calibration.get_manifest(KEY, data_dir=store)
    assert restored["tested"][calibration.TEST_TOOL_CALLING]["ok"] is False
    assert restored["tested"][calibration.TEST_JSON_MODE]["ok"] is True
    assert restored["announced"]["capabilities"]["vision"] is True
