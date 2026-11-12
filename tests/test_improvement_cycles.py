"""Configuration experiments must change live inputs only after paired evidence."""
import copy
import math

import pytest
from pydantic import ValidationError

from src.harness_evolution import cycles, runtime
from src.harness_evolution.service import HarnessEvolutionService, HarnessEvolutionError
from src.harness_evolution.store import HarnessEvolutionStore, StaleParent
from src.harness_evolution.promotion import PromotionRefused


def rows(target=0, other=1):
    return [{"id": "t", "group": "target", "score": target, "error": ""},
            {"id": "r", "group": "other", "score": other, "error": ""}]


def spec():
    return cycles.CycleSpec(name="Fixture", target_group="target", endpoint_url="http://127.0.0.1:9/v1", model="fixture",
        cases=[cycles.Case(id="t", group="target", prompt="Target", checks={"answer_all": ["^fixed$"]}),
               cycles.Case(id="r", group="other", prompt="Other", checks={"answer_all": ["^stable$"]})])


@pytest.fixture
def store(tmp_path):
    return HarnessEvolutionStore(str(tmp_path / "harness.db"))


def test_each_group_decides_and_threshold_is_percentage_point():
    assert cycles.gate(rows(), rows(1, .99), "target")["eligible"]
    assert cycles.gate(rows(), rows(1, .989), "target")["reason"] == "regression"
    assert cycles.gate(rows(), rows(0, 1), "target")["reason"] == "target_not_improved"
    base = rows() + [{"id": "s", "group": "third", "score": 0}]
    after = rows(1, .95) + [{"id": "s", "group": "third", "score": 1}]
    assert not cycles.gate(base, after, "target")["eligible"]  # average gain cannot hide a group's loss


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, True, -1, 2])
def test_unknown_scores_cannot_be_promoted(bad):
    after = rows(1)
    after[1]["score"] = bad
    assert not cycles.gate(rows(), after, "target")["eligible"]


def test_missing_duplicate_changed_group_and_failed_calls_are_incomplete():
    for after in (rows(1)[:1], rows(1) + rows(1)[:1],
                  [{"id": "t", "group": "different", "score": 1}, rows(1)[1]],
                  [{**rows(1)[0], "error": "judge unavailable"}, rows(1)[1]]):
        assert cycles.gate(rows(), after, "target")["reason"] == "insufficient_data"


@pytest.mark.parametrize("edit", [
    {"kind": "model_parameter", "key": "temperature", "value": True},
    {"kind": "context", "key": "input_token_budget", "value": 3000},
    {"kind": "context", "key": "anything", "value": True},
    {"kind": "system_section", "key": "source.py", "value": "execute"},
    {"kind": "system_section", "key": "response", "value": "x" * 2001},
    {"kind": "model_parameter", "key": "max_tokens", "value": 32},
])
def test_edits_are_typed_and_bounded(edit):
    with pytest.raises(ValidationError): runtime.ConfigEdit.model_validate(edit)


def evaluated(svc, edit, *, owner="fixture", project=""):
    p = svc.propose_candidate(changes={"type": "runtime_config", "owner": owner, "project_id": project, "edit": edit})
    p.evaluation = {"before": rows(), "after": rows(1), "target_group": "target", "regression_threshold": .01}
    p.status = "evaluated"
    svc.store.update_patch(p)
    return p


def test_runtime_changes_and_individual_undo_preserve_unrelated_edits(store):
    svc = HarnessEvolutionService(store)
    edit = {"kind": "system_section", "key": "response", "value": "Reply exactly fixed."}
    first = evaluated(svc, edit)
    svc.promote_candidate(first.patch_id, actor="test", canary_runs=0)
    second = evaluated(svc, {"kind": "model_parameter", "key": "temperature", "value": .2})
    svc.promote_candidate(second.patch_id, actor="test", canary_runs=0)
    snapshot = runtime.snapshot("fixture", store=store)
    original = [{"role": "system", "content": "Base"}, {"role": "user", "content": "Hi"}]
    assert "Reply exactly fixed." in runtime.messages(snapshot, original)[0]["content"]
    assert original[0]["content"] == "Base"
    assert runtime.options(snapshot, {}, .8, 200)[1] == .2
    assert runtime.options(snapshot, {}, .8, 200, temperature_explicit=True)[1] == .8
    cycles.revert(first.patch_id, "fixture", store=store)
    assert runtime.snapshot("fixture", store=store) == [second.changes["edit"]]
    assert svc.get_patch(first.patch_id).status == "reverted"
    assert runtime.snapshot("other", store=store) == []
    with pytest.raises(ValueError): cycles.revert(second.patch_id, "other", store=store)


def test_undo_cannot_overwrite_later_edit_of_same_slot(store):
    svc = HarnessEvolutionService(store)
    ids = []
    for value in ("First", "Second"):
        p = evaluated(svc, {"kind": "system_section", "key": "response", "value": value})
        svc.promote_candidate(p.patch_id, actor="test", canary_runs=0)
        ids.append(p.patch_id)
    with pytest.raises(ValueError): cycles.revert(ids[0], "fixture", store=store)
    cycles.revert(ids[1], "fixture", store=store)
    assert runtime.snapshot("fixture", store=store)[0]["value"] == "First"
    cycles.revert(ids[0], "fixture", store=store)
    assert not runtime.snapshot("fixture", store=store)


def test_high_risk_waits_and_stale_evidence_never_auto_rebases(store):
    svc = HarnessEvolutionService(store)
    p = evaluated(svc, {"kind": "context", "key": "no_memory", "value": True})
    with pytest.raises(HarnessEvolutionError): svc.promote_candidate(p.patch_id, actor="model", canary_runs=0)
    cycles.accept(p.patch_id, "fixture", store=store)
    stale = evaluated(svc, {"kind": "context", "key": "no_skills", "value": True})
    other = evaluated(svc, {"kind": "model_parameter", "key": "temperature", "value": .2})
    svc.promote_candidate(other.patch_id, actor="test", canary_runs=0)
    with pytest.raises(StaleParent): cycles.accept(stale.patch_id, "fixture", store=store)


def test_promotion_recomputes_gate_instead_of_trusting_cached_verdict(store):
    svc = HarnessEvolutionService(store)
    p = evaluated(svc, {"kind": "system_section", "key": "response", "value": "Bad"})
    p.evaluation["after"] = rows(1, 0)
    p.evaluation["comparison"] = {"eligible": True}
    svc.store.update_patch(p)
    with pytest.raises(PromotionRefused): svc.promote_candidate(p.patch_id, actor="test", canary_runs=0)


@pytest.mark.asyncio
async def test_full_cycle_accepts_one_rejects_regression_and_uses_real_runtime_inputs(store, monkeypatch):
    monkeypatch.setattr(cycles, "collect_signals", lambda *a: [])
    inputs = []
    async def model(url, model, messages, **kwargs):
        inputs.append(copy.deepcopy(messages))
        system, prompt = messages[0]["content"], messages[-1]["content"]
        if prompt == "Other": return "broken" if "Break other" in system else "stable"
        return "fixed" if "Fix target" in system else "wrong"
    async def proposals(*args):
        return [{"edit": {"kind": "system_section", "key": "response", "value": value}, "reason": value}
                for value in ("Fix target", "Fix target. Break other")]
    record = cycles.create(spec(), "fixture", store=store)
    result = await cycles.run(record["id"], "fixture", store=store, model_call=model, proposer_fn=proposals)
    assert result["status"] == "done", result
    assert [c["status"] for c in result["candidates"]] == ["accepted", "rejected"]
    assert runtime.snapshot("fixture", store=store)[0]["value"] == "Fix target"
    # Rejected arm was evaluated after the first accepted edit, never made active.
    assert "Fix target" in inputs[-2][0]["content"]
    assert cycles.CycleStore(store).get(record["id"], "stranger") is None
    again = await cycles.run(record["id"], "fixture", store=store, model_call=model, proposer_fn=proposals)
    assert again == result


@pytest.mark.asyncio
async def test_error_in_baseline_does_not_turn_into_an_improvement(store, monkeypatch):
    monkeypatch.setattr(cycles, "collect_signals", lambda *a: [])
    async def broken(*a, **k): raise RuntimeError("endpoint down")
    record = cycles.create(spec(), "fixture", store=store)
    result = await cycles.run(record["id"], "fixture", store=store, model_call=broken)
    assert result["status"] == "error"
    assert not result["candidates"]
    assert not runtime.snapshot("fixture", store=store)


def test_project_scopes_and_tool_schema_copies(store):
    svc = HarnessEvolutionService(store)
    p = evaluated(svc, {"kind": "system_section", "key": "response", "value": "Project only"}, project="project_a")
    svc.promote_candidate(p.patch_id, actor="test", canary_runs=0)
    assert not runtime.snapshot("fixture", "project_b", store=store)
    assert runtime.snapshot("fixture", "project_a", store=store)
    tools = [{"type": "function", "function": {"name": "read_file", "description": "Original"}}]
    assert not runtime.tools([{"kind": "tool_selection", "key": "read_file", "value": False}], tools)
    modified = runtime.tools([{"kind": "tool_description", "key": "read_file", "value": "Improved"}], tools)
    assert modified[0]["function"]["description"] == "Improved"
    assert tools[0]["function"]["description"] == "Original"


@pytest.mark.asyncio
async def test_proposer_accepts_fenced_array_from_real_provider():
    async def model(*args, **kwargs):
        return '```json\n[{"edit":{"kind":"system_section","key":"response","value":"Calculate"},"reason":"Fix"}]\n```'
    proposals = await cycles.propose(spec(), rows(), [], ("http://127.0.0.1:9", "fixture", None), model)
    assert proposals[0]["edit"]["value"] == "Calculate"


@pytest.mark.asyncio
async def test_native_galton_case_import_and_unavailable_judge(monkeypatch):
    calls = []
    async def native(url, name, args):
        calls.append(name)
        if name == "suite_get": return {"total_cases": 1, "cases": [{"id": "case_a"}]}
        if name == "case_get": return {"case": {"id": "case_a", "prompt": {"text": "Question", "system": "Be brief"}, "reference": "answer", "checker": {"type": "exact", "expected": "answer"}}}
        return {"score": 0, "unavailable": "judge disconnected"}
    monkeypatch.setattr(cycles, "galton_call", native)
    imported = await cycles.galton_cases("http://fixture/evaluation", ["suite_a"])
    assert imported[0]["source"] == "galton:suite_a"
    assert imported[0]["messages"] == [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Question"}]
    cfg = spec().model_copy(update={"galton_url": "http://fixture/evaluation"})
    case = cycles.Case.model_validate(imported[0])
    async def model(*a, **k): return "answer"
    row = await cycles._evaluate_case(case, [], cfg, ("url", "model", None), "fixture", model)
    assert row["score"] is None and row["error"]
    assert calls == ["suite_get", "case_get", "answer_check"]


def test_http_cycle_history_and_patches_are_owner_scoped(store, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes import evolution_routes
    monkeypatch.setenv("AUTH_ENABLED", "false")
    original = cycles.CycleStore
    monkeypatch.setattr(cycles, "CycleStore", lambda: original(store))
    monkeypatch.setattr(evolution_routes, "_service", lambda: HarnessEvolutionService(store))
    app = FastAPI()
    @app.middleware("http")
    async def owner(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-Owner", "fixture")
        return await call_next(request)
    app.include_router(evolution_routes.setup_evolution_routes())
    record = {"id": "cycle_http", "owner": "fixture", "candidates": []}
    original(store).save(record)
    with TestClient(app) as client:
        assert client.get("/api/harness/cycles").json()["cycles"] == [record]
        assert client.get("/api/harness/cycles/cycle_http").status_code == 200
        assert client.get("/api/harness/cycles/cycle_http", headers={"X-Test-Owner": "stranger"}).status_code == 404
        assert client.get("/api/harness/cycles", headers={"X-Test-Owner": "stranger"}).json()["cycles"] == []
        assert client.post("/api/harness/cycles", json={"target_group": "only", "cases": []}).status_code == 422
        assert client.post("/api/harness/cycles/patches/missing/revert").status_code == 409


@pytest.mark.asyncio
async def test_scheduler_delegates_to_same_cycle_without_a_second_timer(store, monkeypatch):
    seen = []
    def create(cfg, owner):
        seen.append((cfg.target_group, owner))
        return {"id": "scheduled"}
    async def run(ident, owner): return {"id": ident, "status": "done"}
    monkeypatch.setattr(cycles, "create", create)
    monkeypatch.setattr(cycles, "run", run)
    result, ok = await cycles.scheduled_entry("fixture", spec().model_dump_json())
    assert ok and 'scheduled' in result and seen == [("target", "fixture")]
