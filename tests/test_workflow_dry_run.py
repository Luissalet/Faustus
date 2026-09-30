"""The dry run: a workflow walked with inputs and nothing real behind it.

Pinned here: it never calls a model, a skill or a sender; its placeholders
satisfy the schemas the real nodes promise; a classify's label opens and closes
the branches below it; a condition is really evaluated; a loop really iterates
(and ends the way a real one does when `until` never holds); a template that
refers to nothing fails as it would in front of a model; a mock overrides one
node; and the same call twice gives the same answer.
"""
from __future__ import annotations

import json

import pytest

from src.contracts import WorkflowDefinition
from src.workflows import schema_check
from src.workflows.dry_run import dry_run, placeholder

START = {"id": "start", "type": "manual"}


def flow(*nodes, **top):
    return WorkflowDefinition.parse({"id": "dry.flow", "version": "1.0.0", "title": "Dry", "nodes": list(nodes), **top})


SCHEMA = {"type": "object", "required": ["title", "score", "tags", "kind"],
          "properties": {"title": {"type": "string", "minLength": 12, "maxLength": 40},
                         "score": {"type": "integer", "minimum": 3, "maximum": 9},
                         "tags": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                         "kind": {"type": "string", "enum": ["memo", "note"]},
                         "ratio": {"type": "number", "exclusiveMinimum": 0.5, "maximum": 2},
                         "ok": {"type": "boolean"}, "extra": {"type": ["string", "null"]}}}


def test_a_placeholder_satisfies_the_schema_it_is_built_from():
    value = placeholder(SCHEMA)
    assert schema_check.validate(value, SCHEMA) == []
    assert value["kind"] == "memo" and 3 <= value["score"] <= 9 and len(value["tags"]) == 2
    assert placeholder(SCHEMA) == value, "deterministic"


@pytest.mark.parametrize("schema", [
    {"const": {"a": 1}}, {"enum": ["x", "y"]}, {"anyOf": [{"type": "integer", "minimum": 4}, {"type": "string"}]},
    {"allOf": [{"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
               {"properties": {"b": {"type": "integer", "minimum": 2}}, "required": ["b"]}]},
    {"type": "array", "minItems": 3, "items": {"type": "integer"}}, {"type": "array", "maxItems": 0},
    {"type": "string", "maxLength": 3}, {"type": "null"}, {"type": "boolean"}])
def test_placeholders_for_other_keywords_are_valid(schema):
    assert schema_check.validate(placeholder(schema), schema) == []


class Spy:
    """Anything that would reach outside is a failure of the test."""

    def __getattr__(self, name):
        raise AssertionError(f"a dry run reached for {name}")


def test_nothing_real_is_called(monkeypatch):
    from src.workflows import handlers, model_nodes, agent_turn
    for mod, names in ((handlers, ("skill_handler", "deliver_handler", "approval_handler", "artifact_handler")),
                       (model_nodes, ("agent_handler", "classify_handler", "extract_handler", "guard_handler")),
                       (agent_turn, ("run",))):
        for name in names:
            monkeypatch.setattr(mod, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    d = flow(START,
             {"id": "a", "type": "agent", "needs": ["start"], "config": {"prompt": "Do {{ inputs.topic }}", "output_schema": SCHEMA}},
             {"id": "ok", "type": "guard", "needs": ["a"], "config": {"text": "{{ results.a.text }}", "checks": ["secrets"]}},
             {"id": "s", "type": "skill", "needs": ["ok"], "branch": {"ok": "pass"}, "config": {"skill": "x.y"}},
             {"id": "h", "type": "human_approval", "needs": ["s"], "config": {"action": "publish"}},
             {"id": "w", "type": "wait", "needs": ["h"], "config": {"seconds": 99999}},
             {"id": "m", "type": "deliver", "needs": ["w"], "config": {"to": "x"}},
             {"id": "st", "type": "artifact_store", "needs": ["m"], "config": {"kind": "report"}})
    out = dry_run(d, {"topic": "tides"})
    assert out["status"] == "completed" and out["simulated"] is True
    assert out["human_waits"] == ["h"]
    assert set(out["outputs"]) == {"st"} and out["outputs"]["st"]["simulated"] is True
    assert out["nodes"]["a"]["result"]["data"]["kind"] == "memo"
    assert out["nodes"]["a"]["result"]["schema_ok"] is True
    assert json.loads(out["nodes"]["a"]["result"]["text"]) == out["nodes"]["a"]["result"]["data"]


def test_the_same_inputs_give_the_same_outputs():
    d = flow(START, {"id": "a", "type": "agent", "needs": ["start"], "config": {"prompt": "x", "output_schema": SCHEMA}})
    a, b = dry_run(d, {"k": 1}), dry_run(d, {"k": 1})
    a["nodes"]["start"]["result"].pop("at", None)
    b["nodes"]["start"]["result"].pop("at", None)
    assert a == b


def classify_flow(**config):
    cfg = {"text": "{{ inputs.ticket }}", "labels": ["billing", "support", "sales"], **config}
    return flow(START,
                {"id": "route", "type": "classify", "needs": ["start"], "config": cfg},
                {"id": "bill", "type": "skill", "needs": ["route"], "branch": {"route": "billing"}, "config": {"skill": "bill.reply"}},
                {"id": "help", "type": "skill", "needs": ["route"], "branch": {"route": ["support", "sales"]}, "config": {"skill": "help.reply"}})


def test_a_classify_takes_the_first_label_and_the_gates_follow():
    out = dry_run(classify_flow(), {"ticket": "charged twice"})
    assert out["nodes"]["route"]["result"]["label"] == "billing"
    assert out["nodes"]["route"]["result"]["receipt"]["simulated"] is True
    assert out["nodes"]["bill"]["status"] == "completed"
    assert out["nodes"]["help"]["status"] == "skipped" and out["nodes"]["help"]["reason"] == "branch not taken"
    assert set(out["outputs"]) == {"bill"}, "a skipped branch produces nothing"


def test_a_mock_picks_another_label_and_a_wrong_label_fails_the_node():
    out = dry_run(classify_flow(), {"ticket": "x"}, mocks={"route": {"label": "sales"}})
    assert out["nodes"]["help"]["status"] == "completed" and out["nodes"]["bill"]["status"] == "skipped"
    bad = dry_run(classify_flow(), {"ticket": "x"}, mocks={"route": {"label": "refunds"}})
    assert bad["status"] == "failed" and "not one of this node's labels" in bad["failed"][0]["reason"]


def test_a_guard_passes_unless_a_mock_fails_it():
    d = flow(START, {"id": "g", "type": "guard", "needs": ["start"], "config": {"text": "{{ inputs.t }}", "checks": ["secrets", "pii"]}},
             {"id": "ship", "type": "skill", "needs": ["g"], "branch": {"g": "pass"}, "config": {"skill": "s.s"}},
             {"id": "hold", "type": "skill", "needs": ["g"], "branch": {"g": "fail"}, "config": {"skill": "h.h"}})
    ok = dry_run(d, {"t": "hello"})
    assert ok["nodes"]["ship"]["status"] == "completed" and ok["nodes"]["hold"]["status"] == "skipped"
    assert [c["type"] for c in ok["nodes"]["g"]["result"]["checks"]] == ["secrets", "pii"]
    bad = dry_run(d, {"t": "hello"}, mocks={"g": {"passed": False}})
    assert bad["nodes"]["hold"]["status"] == "completed" and bad["nodes"]["g"]["result"]["failed"] == ["secrets-1", "pii-2"]


def test_an_extract_mock_merges_into_the_placeholder():
    d = flow(START, {"id": "e", "type": "extract", "needs": ["start"],
                     "config": {"text": "{{ inputs.t }}", "schema": {"type": "object", "required": ["n", "who"],
                                                                     "properties": {"n": {"type": "integer"}, "who": {"type": "string"}}}}})
    assert dry_run(d, {"t": "a"})["nodes"]["e"]["result"]["data"] == {"n": 0, "who": "[simulated output]"}
    assert dry_run(d, {"t": "a"}, mocks={"e": {"data": {"n": 7}}})["nodes"]["e"]["result"]["data"] == {"n": 7, "who": "[simulated output]"}


def test_a_template_that_refers_to_nothing_fails_like_the_real_node():
    d = flow(START, {"id": "a", "type": "agent", "needs": ["start"], "config": {"prompt": "About {{ inputs.missing }}"}})
    out = dry_run(d, {"topic": "x"})
    assert out["status"] == "failed" and "inputs.missing" in out["failed"][0]["reason"]
    assert out["outputs"] == {}


def test_a_condition_is_really_evaluated():
    d = flow(START, {"id": "big", "type": "condition", "needs": ["start"],
                     "config": {"when": {"left": {"path": "inputs.n"}, "op": "gte", "right": 10}}},
             {"id": "act", "type": "skill", "needs": ["big"], "config": {"skill": "do.it"}})
    assert dry_run(d, {"n": 12})["nodes"]["act"]["status"] == "completed"
    small = dry_run(d, {"n": 2})
    assert small["nodes"]["big"]["status"] == "skipped" and small["nodes"]["act"]["status"] == "skipped"
    assert small["status"] == "completed" and small["outputs"] == {}


def loop_flow(until=None, **budget):
    cfg = {"body": ["draft", "check"], "budget": {"max_iterations": 4, **budget}}
    if until:
        cfg["until"] = until
    return flow(START,
                {"id": "lp", "type": "loop", "needs": ["start"], "config": cfg},
                {"id": "draft", "type": "agent", "needs": ["start"], "config": {"prompt": "Draft {{ loop.iteration }}", "output_schema": {"type": "object", "required": ["done"], "properties": {"done": {"type": "boolean"}}}}},
                {"id": "check", "type": "skill", "needs": ["draft"], "config": {"skill": "lint.run"}},
                {"id": "after", "type": "skill", "needs": ["lp"], "config": {"skill": "after.run"}})


def test_a_loop_without_until_runs_its_ceiling_and_the_body_is_not_listed_separately():
    out = dry_run(loop_flow(), {})
    assert out["status"] == "completed"
    assert out["nodes"]["lp"]["result"]["iterations"] == 4 and out["nodes"]["lp"]["result"]["exited_by"] == "max_iterations"
    assert "draft" not in out["nodes"] and "check" not in out["nodes"]
    assert out["nodes"]["after"]["status"] == "completed"


def test_a_loop_stops_at_until_when_the_placeholders_satisfy_it():
    until = {"left": {"path": "loop.iteration"}, "op": "gte", "right": 2}
    out = dry_run(loop_flow(until), {})
    assert out["nodes"]["lp"]["result"]["iterations"] == 2 and out["nodes"]["lp"]["result"]["exited_by"] == "until"


def test_a_loop_that_never_reaches_until_ends_like_a_real_one():
    until = {"left": {"path": "loop.results.draft.data.done"}, "op": "truthy"}
    paused = dry_run(loop_flow(until), {})
    assert paused["status"] == "paused" and paused["paused_on"] == ["lp"]
    assert paused["nodes"]["after"]["status"] == "skipped", "what is downstream of a parked loop does not run"
    assert "never held" in paused["nodes"]["lp"]["reason"]
    mocked = dry_run(loop_flow(until), {}, mocks={"draft": {"data": {"done": True}}})
    assert mocked["status"] == "completed" and mocked["nodes"]["lp"]["result"]["iterations"] == 1
    failing = loop_flow(until, on_exhausted="fail")
    assert dry_run(failing, {})["status"] == "failed"


def test_a_failing_body_node_fails_the_loop_with_the_iteration():
    out = dry_run(loop_flow(), {}, mocks={"check": {"status": "failed", "reason": "lint broke"}})
    assert out["status"] == "failed" and "iteration 1" in out["failed"][0]["reason"] and "lint broke" in out["failed"][0]["reason"]


def test_a_huge_ceiling_is_capped_with_a_warning():
    out = dry_run(loop_flow(max_iterations=5000), {})
    assert out["nodes"]["lp"]["result"]["iterations"] == 50
    assert any("stops after 50" in w for w in out["warnings"])


def test_a_mock_for_a_node_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="nope"):
        dry_run(classify_flow(), {"ticket": "x"}, mocks={"nope": {}})


def test_a_mocked_failure_stops_what_depends_on_it_unless_it_continues():
    out = dry_run(classify_flow(), {"ticket": "x"}, mocks={"route": {"status": "failed", "reason": "model down"}})
    assert out["status"] == "failed" and out["nodes"]["bill"]["status"] == "skipped"
    assert out["failed"] == [{"node": "route", "reason": "model down"}]


def test_a_schema_the_placeholder_cannot_satisfy_is_reported():
    d = flow(START, {"id": "a", "type": "agent", "needs": ["start"],
                     "config": {"prompt": "x", "output_schema": {"type": "object", "required": ["id"],
                                                                 "properties": {"id": {"type": "string", "pattern": "^[0-9]{4}$"}}}}})
    out = dry_run(d, {})
    assert out["nodes"]["a"]["result"]["schema_ok"] is False
    assert any("give this node a mock" in w for w in out["warnings"])
