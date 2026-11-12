"""Frozen, locally masked trace batteries and text sidecars share cycle execution."""
import json

import pytest

from src.harness_evolution import cycles, personal, runtime
from src.harness_evolution.service import HarnessEvolutionService
from src.harness_evolution.store import HarnessEvolutionStore
from src.harness_refinement.signals import TMsg, Trajectory


@pytest.fixture
def store(tmp_path):
    return HarnessEvolutionStore(str(tmp_path / "harness.db"))


def loader(ident, owner, **kwargs):
    return Trajectory(session_id=ident, owner="fixture", project_name="HiddenProject", messages=[
        TMsg("1", "user", f"Calculate 2+2 for PrivateName at person@example.test C:\\Users\\Example\\a.txt {ident}", 1),
        TMsg("2", "assistant", "4", 2), TMsg("3", "user", "Perfecto, gracias", 3),
        TMsg("4", "user", f"Return only the capital of Spain {ident}", 4),
        TMsg("5", "assistant", "Barcelona", 5), TMsg("6", "user", "Incorrecto, responde Madrid", 6),
    ])


def build_spec():
    return personal.BuildSpec(session_ids=["source-a", "source-b"], redact_terms=["PrivateName"],
                              endpoint_url="http://127.0.0.1:9/v1", model="fixture")


async def synthesizer(url, model, messages, **kwargs):
    examples = json.loads(messages[0]["content"].split("\n", 1)[1])
    return {"cases": [{"example_id": e["id"], "group": "math" if "Calculate" in e["prompt"] else "geography",
                       "prompt": f"{e['id']}: " + ("Return only 2+2." if "Calculate" in e["prompt"] else "Return only Spain's capital."),
                       "reference": "4" if "Calculate" in e["prompt"] else "Madrid"} for e in examples]}


def test_collection_is_own_rated_deduplicated_and_masked_before_model():
    spec = build_spec()
    # Both conversations have identical masked tasks: retain one instance.
    examples = personal.collect_examples("fixture", spec, loader=loader)
    assert len(examples) == 2
    assert {e["outcome"] for e in examples} == {"positive", "negative"}
    payload = json.dumps(examples)
    for term in ("PrivateName", "person@example.test", "HiddenProject", "source-a", "C:\\Users"):
        assert term not in payload
    assert personal.collect_examples("stranger", spec, loader=loader) == []
    masked = personal.anonymize('api_key=secretvalue bearer abcdefghijkl https://example.test/x', [])
    assert "secretvalue" not in masked and "abcdefghijkl" not in masked and "example.test" not in masked


def distinct_loader(ident, owner, **kwargs):
    trajectory = loader(ident, owner, **kwargs)
    trajectory.messages[0].content += " task " + ("one" if ident == "source-a" else "two")
    trajectory.messages[3].content += " task " + ("one" if ident == "source-a" else "two")
    return trajectory


@pytest.mark.asyncio
async def test_fixed_cases_split_by_source_and_model_inputs_contain_only_masked_text(store):
    async def call(*args, **kwargs):
        assert "PrivateName" not in json.dumps(args)
        assert "source-a" not in json.dumps(args)
        return await synthesizer(*args, **kwargs)
    record = await personal.build(build_spec(), "fixture", store=store, loader=distinct_loader, model_call=call)
    assert len(record["cases"]) == 4
    assert record["coverage"]["splits"] == {"development": 2, "heldout": 2}
    assert {e["split"] for e in record["development_examples"]} == {"development"}
    assert personal.BenchmarkStore(store).get(record["id"], "stranger") is None
    assert personal.BenchmarkStore(store).get(record["id"], "fixture") == record
    assert "PrivateName" not in json.dumps(record)


@pytest.mark.asyncio
async def test_single_source_and_incomplete_synthesis_never_save_a_battery(store):
    with pytest.raises(ValueError, match="dos conversaciones"):
        await personal.build(build_spec(), "fixture", store=store, loader=loader, model_call=synthesizer)
    async def incomplete(*a, **k):
        data = await synthesizer(*a, **k)
        data["cases"] = data["cases"][:2]
        return data
    with pytest.raises(ValueError, match="exactamente"):
        await personal.build(build_spec(), "fixture", store=store, loader=distinct_loader, model_call=incomplete)
    assert personal.BenchmarkStore(store).list("fixture") == []


@pytest.mark.asyncio
async def test_measure_reuses_frozen_cases_and_does_not_promote_provider_errors(store):
    record = await personal.build(build_spec(), "fixture", store=store, loader=distinct_loader, model_call=synthesizer)
    async def model(url, name, messages, **kwargs):
        return "4" if "2+2" in messages[-1]["content"] else "Madrid"
    spec = personal.MeasureSpec(endpoint_url="http://127.0.0.1:9", model="fixture", repeats=2)
    result = await personal.measure(record["id"], "fixture", spec, store=store, model_call=model)
    assert len(result["rows"]) == 8
    assert all(g["score"] == 1 for g in result["groups"].values())
    async def broken(*a, **k): raise RuntimeError("unavailable")
    result = await personal.measure(record["id"], "fixture", spec, store=store, model_call=broken)
    assert result["status"] == "error" and all(g["score"] is None for g in result["groups"].values())
    stored = personal.BenchmarkStore(store).get(record["id"], "fixture")
    assert len(stored["measurements"]) == 2 and stored["digest"] == record["digest"]
    assert not runtime.snapshot("fixture", store=store)


@pytest.mark.asyncio
async def test_text_cycle_has_no_heldout_leak_and_rejects_edit_outside_target(store, monkeypatch):
    record = await personal.build(build_spec(), "fixture", store=store, loader=distinct_loader, model_call=synthesizer)
    cfg = cycles.CycleSpec(target_group="math", cases=record["cases"], benchmark_id=record["id"],
                           text_target={"kind": "system_section", "key": "response"},
                           endpoint_url="http://127.0.0.1:9", model="fixture")
    async def model(url, name, messages, **kwargs):
        if "2+2" in messages[-1]["content"]: return "4" if "Calculate" in messages[0]["content"] else "wrong"
        return "Madrid"
    async def propose(spec, before, signals, *args):
        assert len(before) == 2
        assert len(signals[0]["evidence"]) == 2
        assert all(e["split"] == "development" for e in signals[0]["evidence"])
        return [{"edit": {"kind": "system_section", "key": "response", "value": "Calculate correctly"}, "reason": "Compute"},
                {"edit": {"kind": "model_parameter", "key": "temperature", "value": 1}, "reason": "Outside target"}]
    cycle = cycles.create(cfg, "fixture", store=store)
    result = await cycles.run(cycle["id"], "fixture", store=store, model_call=model, proposer_fn=propose)
    assert result["status"] == "done", result
    assert [c["status"] for c in result["candidates"]] == ["accepted", "error"]
    cfg.cases[0].prompt = "Changed after freezing"
    with pytest.raises(ValueError, match="coincidir"):
        cycles.create(cfg, "fixture", store=store)
    with pytest.raises(ValueError): cycles.create(cfg, "stranger", store=store)


def test_skill_text_uses_existing_patch_store_and_reverts_without_writing_original(store, monkeypatch):
    from src.harness_refinement import targets
    monkeypatch.setattr(targets, "read_current", lambda *args: "Original skill")
    svc = HarnessEvolutionService(store)
    patch = svc.propose_candidate(changes={"type": "runtime_config", "owner": "fixture",
        "edit": {"kind": "skill_text", "key": "fixture-skill", "value": "Improved skill text"}})
    assert patch.status != "rejected", patch.trace
    patch.evaluation = {"before": [{"id": "a", "group": "target", "score": 0}, {"id": "b", "group": "other", "score": 1}],
                        "after": [{"id": "a", "group": "target", "score": 1}, {"id": "b", "group": "other", "score": 1}], "target_group": "target"}
    patch.status = "evaluated"
    svc.store.update_patch(patch)
    cycles.accept(patch.patch_id, "fixture", store=store)
    original = [{"role": "system", "content": "Base"}]
    rendered = json.dumps(runtime.messages(runtime.snapshot("fixture", store=store), original, "fixture"))
    assert "Improved skill text" in rendered and "Original skill" not in rendered
    assert original == [{"role": "system", "content": "Base"}]
    cycles.revert(patch.patch_id, "fixture", store=store)
    assert not runtime.snapshot("fixture", store=store)
    assert targets.read_current("skill", "skill:fixture-skill", "fixture") == "Original skill"


@pytest.mark.asyncio
async def test_export_uses_native_suite_create_preserves_checker_and_digest(store, monkeypatch):
    record = await personal.build(build_spec(), "fixture", store=store, loader=distinct_loader, model_call=synthesizer)
    async def call(url, tool, args):
        assert tool == "suite_create" and args["category"] == "custom"
        assert record["digest"] in args["description"]
        assert len(args["cases"]) == 4
        assert args["cases"][0]["checker"]["type"] == "regex"
        assert "PrivateName" not in json.dumps(args)
        return {"suite": {"id": "suite-fixture", "name": args["name"]}}
    monkeypatch.setattr(cycles, "galton_call", call)
    await personal.export_galton(record["id"], "fixture", "http://fixture/evaluation", store=store)
    assert len(personal.BenchmarkStore(store).get(record["id"], "fixture")["galton_exports"]) == 1


@pytest.mark.asyncio
async def test_real_proposer_never_sees_reserved_prompts():
    cfg = cycles.CycleSpec(target_group="target", cases=[
        cycles.Case(id="a", group="target", prompt="Development", checks={"answer_all": ["ok"]}),
        cycles.Case(id="b", group="other", prompt="Secret heldout", checks={"answer_all": ["ok"]}, split="heldout")])
    async def call(url, model, messages, **kwargs):
        assert "Secret heldout" not in json.dumps(messages)
        return {"candidates": []}
    assert await cycles.propose(cfg, [], [], ("url", "model", None), call) == []
