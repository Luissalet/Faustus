"""Evaluating a saved workflow against a set of example inputs.

Pinned here: a set is validated when saved (every problem at once); scorers are
deterministic and never raise; the model judge is off unless its setting is on
and a judge that cannot answer fails the case instead of passing it; simulate
is the default and calls nothing real, real needs two explicit opt-ins and a
smaller ceiling, a case that stops for a person ends as an error; the report is
aggregated (pass rate, per scorer, duration) and persisted per owner; and the
routes answer with the same codes.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, default_handlers, evaluation
from src.workflows.evaluation import EvaluationError, EvaluationStore, score_case, validate_set
from src.workflows.library import WorkflowLibrary
from tests.test_workflow_published import Brief, Mail, START, store  # noqa: F401  (fixtures)

INPUTS = {"type": "object", "required": ["ticket"],
          "properties": {"ticket": {"type": "string", "minLength": 1}, "lang": {"type": "string", "default": "en"}}}
REPLY = {"type": "object", "required": ["subject", "total"],
         "properties": {"subject": {"type": "string"}, "total": {"type": "number"}}}


def triage(inputs=INPUTS):
    return WorkflowDefinition.parse({
        "id": "ticket.triage", "version": "1.0.0", "title": "Triage", "description": "Route a ticket and draft a reply.",
        "inputs": inputs,
        "nodes": [START,
                  {"id": "route", "type": "classify", "needs": ["start"],
                   "config": {"text": "{{ inputs.ticket }}", "labels": ["billing", "support"]}},
                  {"id": "bill", "type": "agent", "needs": ["route"], "branch": {"route": "billing"},
                   "config": {"prompt": "Reply to {{ inputs.ticket }}", "output_schema": REPLY, "tools": []}},
                  {"id": "help", "type": "agent", "needs": ["route"], "branch": {"route": "support"},
                   "config": {"prompt": "Help with {{ inputs.ticket }}", "tools": []}}]})


@pytest.fixture()
def saved(store):  # noqa: F811
    WorkflowLibrary().save("ana", triage())
    return "ticket_triage"


SET = {"scorers": [{"type": "json_schema", "path": "bill.data", "schema": REPLY}],
       "cases": [{"id": "billing-1", "inputs": {"ticket": "charged twice"}},
                 {"id": "support-1", "inputs": {"ticket": "app crashes"}, "mocks": {"route": {"label": "support"}},
                  "scorers": [{"type": "contains", "path": "help.text", "expected": "SIMULATED"}]}]}


# ── validating a set ──────────────────────────────────────────────────────

def test_a_good_set_is_normalised_and_keeps_its_scorers():
    out = validate_set(SET, triage())
    assert [c["id"] for c in out["cases"]] == ["billing-1", "support-1"]
    assert out["cases"][0]["scorers"] == [] and out["scorers"][0]["type"] == "json_schema"


@pytest.mark.parametrize("payload,needle", [
    ([], "an object"),
    ({"cases": []}, "at least one case"),
    ({"cases": [{"id": "a"}], "extra": 1}, "unknown field"),
    ({"cases": [{"id": "a"}, {"id": "a"}]}, "used twice"),
    ({"cases": [{"id": "bad id"}]}, "not usable"),
    ({"cases": [{"id": "a", "inputs": []}]}, "`inputs` must be an object"),
    ({"cases": [{"id": "a", "mocks": {"nobody": {}}}]}, "no node named"),
    ({"cases": [{"id": "a", "mocks": {"route": "billing"}}]}, "`mocks` must be"),
    ({"cases": [{"id": "a", "scorers": [{"type": "vibes"}]}]}, "`type` must be one of"),
    ({"cases": [{"id": "a", "scorers": [{"type": "regex", "pattern": "("}]}]}, "not a valid regular expression"),
    ({"cases": [{"id": "a", "scorers": [{"type": "regex", "pattern": "a", "flags": "x"}]}]}, "unknown regex flag"),
    ({"cases": [{"id": "a", "scorers": [{"type": "numeric", "expected": "3"}]}]}, "must be a number"),
    ({"cases": [{"id": "a", "scorers": [{"type": "numeric", "expected": 3, "tolerance": -1}]}]}, "at least 0"),
    ({"cases": [{"id": "a", "scorers": [{"type": "exact"}]}]}, "needs `expected`"),
    ({"cases": [{"id": "a", "scorers": [{"type": "json_schema", "schema": {"$ref": "#/x"}}]}]}, "unsupported keyword"),
    ({"cases": [{"id": "a", "scorers": [{"type": "judge"}]}]}, "needs `criteria`"),
    ({"cases": [{"id": "a", "scorers": [{"type": "exact", "expected": 1, "colour": "red"}]}]}, "unknown field"),
])
def test_a_bad_set_is_refused_with_the_reason(payload, needle):
    with pytest.raises(EvaluationError) as err:
        validate_set(payload, triage())
    assert needle in str(err.value) or needle in " ".join(err.value.problems)


def test_every_problem_is_reported_at_once():
    with pytest.raises(EvaluationError) as err:
        validate_set({"cases": [{"id": "a", "inputs": 3}, {"id": "a", "mocks": {"x": {}}}]}, triage())
    assert len(err.value.problems) >= 3


def test_too_many_cases_are_refused():
    with pytest.raises(EvaluationError, match="at most"):
        validate_set({"cases": [{"id": f"c{i}"} for i in range(evaluation.MAX_CASES + 1)]})


# ── scoring ───────────────────────────────────────────────────────────────

OUT = {"bill": {"data": {"subject": "Refund", "total": 12.5}, "text": "Dear customer, your REFUND is on its way"}}


def scores(*specs, **kw):
    return score_case(OUT, specs, **kw)


def test_exact_and_contains_and_regex():
    r = scores({"type": "exact", "path": "bill.data.subject", "expected": "Refund"},
               {"type": "exact", "path": "bill.data.subject", "expected": "refund"},
               {"type": "contains", "path": "bill.text", "expected": "refund"},
               {"type": "contains", "path": "bill.text", "expected": "refund", "case_sensitive": True},
               {"type": "contains", "path": "bill.data", "expected": "total"},
               {"type": "regex", "path": "bill.text", "pattern": r"^dear .* on its way$", "flags": "i"},
               {"type": "regex", "path": "bill.text", "pattern": r"^nope"})
    assert [x["passed"] for x in r] == [True, False, True, False, True, True, False]
    assert r[1]["detail"] == 'expected "refund", got "Refund"'


def test_numeric_absolute_and_relative_tolerance_and_strings():
    r = scores({"type": "numeric", "path": "bill.data.total", "expected": 12, "tolerance": 0.5},
               {"type": "numeric", "path": "bill.data.total", "expected": 12, "tolerance": 0.4},
               {"type": "numeric", "path": "bill.data.total", "expected": 12, "tolerance": 0.05, "relative": True},
               {"type": "numeric", "path": "bill.data.subject", "expected": 1},
               {"type": "exact", "path": "bill.data.total", "expected": 12.5})
    assert [x["passed"] for x in r] == [True, False, True, False, True]
    assert score_case({"n": " 3.0 "}, [{"type": "numeric", "path": "n", "expected": 3}])[0]["passed"]
    assert not score_case({"n": True}, [{"type": "numeric", "path": "n", "expected": 1}])[0]["passed"]


def test_json_schema_reads_objects_and_json_text():
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}
    assert score_case({"x": {"a": 1}}, [{"type": "json_schema", "path": "x", "schema": schema}])[0]["passed"]
    assert score_case({"x": '```json\n{"a": 2}\n```'}, [{"type": "json_schema", "path": "x", "schema": schema}])[0]["passed"]
    bad = score_case({"x": {"a": "s"}}, [{"type": "json_schema", "path": "x", "schema": schema}])[0]
    assert not bad["passed"] and "expected integer" in bad["detail"]
    assert "not JSON" in score_case({"x": "hello"}, [{"type": "json_schema", "path": "x", "schema": schema}])[0]["detail"]


def test_a_missing_path_fails_and_no_path_scores_the_whole_output():
    r = scores({"type": "exact", "path": "bill.nothing", "expected": 1}, {"type": "contains", "expected": "Refund"})
    assert not r[0]["passed"] and "nothing at `bill.nothing`" in r[0]["detail"]
    assert r[1]["passed"] is True
    assert evaluation.dig({"a": [{"b": 5}]}, "a.0.b") == 5 and evaluation.dig({"a": []}, "a.0") is evaluation._MISSING


class Judge:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_the_judge_is_off_by_default_and_fails_rather_than_passes():
    spec = {"type": "judge", "path": "bill.text", "criteria": "is polite"}
    judge = Judge('{"score": 1}')
    r = score_case(OUT, [spec], models=judge)[0]
    assert r["passed"] is False and r["unavailable"] is True and "workflow_eval_model_judge" in r["detail"]
    assert judge.calls == [], "no model call when the setting is off"


@pytest.mark.parametrize("reply,passed,needle", [
    ('{"score": 0.9, "reason": "polite"}', True, "polite"),
    ('{"score": 0.5, "reason": "curt"}', False, "curt"),
    ('{"score": 7}', True, ""),
    ("no idea", False, "no usable score"),
    (RuntimeError("model down"), False, "could not answer"),
])
def test_the_judge_when_enabled(reply, passed, needle):
    judge = Judge(reply)
    r = score_case(OUT, [{"type": "judge", "path": "bill.text", "criteria": "is polite"}], models=judge,
                   judge_enabled=True)[0]
    assert r["passed"] is passed and needle in r["detail"]
    assert "is polite" in judge.calls[0][0][1]["content"] and "REFUND" in judge.calls[0][0][1]["content"]
    if isinstance(reply, str) and reply.startswith('{"score": 7'):
        assert r["score"] == 1.0, "a score is clamped to 0..1"


def test_the_judge_setting_is_declared_and_off():
    from src.settings import DEFAULT_SETTINGS
    from src.agent_settings_schema import schema_keys
    assert DEFAULT_SETTINGS["workflow_eval_model_judge"] is False and "workflow_eval_model_judge" in schema_keys()
    assert evaluation.judge_is_enabled() is False


def test_aggregate_counts_rates_and_scorers():
    res = [{"status": "passed", "duration_ms": 10, "scores": [{"type": "exact", "passed": True}]},
           {"status": "failed", "duration_ms": 30, "scores": [{"type": "exact", "passed": False}, {"type": "regex", "passed": True}]},
           {"status": "error", "duration_ms": 20, "scores": []}]
    a = evaluation.aggregate(res)
    assert (a["total"], a["passed"], a["failed"], a["error"]) == (3, 1, 1, 1)
    assert a["pass_rate"] == 0.3333 and a["duration_ms_total"] == 60 and a["duration_ms_avg"] == 20
    assert a["duration_ms_p95"] == 30 and a["unscored"] == 1
    assert a["by_scorer"] == {"exact": {"passed": 1, "total": 2}, "regex": {"passed": 1, "total": 1}}
    assert evaluation.aggregate([])["pass_rate"] == 0.0


# ── running ───────────────────────────────────────────────────────────────

def put(owner, name, payload, set_name="smoke"):
    return EvaluationStore().save_set(owner, name, set_name, validate_set(payload, WorkflowLibrary().definition(owner, name)))


def test_simulate_is_the_default_and_the_report_is_persisted(saved):
    put("ana", saved, SET)
    out = evaluation.run_evaluation("ana", saved, set_name="smoke")
    assert out["status"] == "completed" and out["summary"]["mode"] == "simulate" and "simulated" in out["summary"]["note"]
    cases = {c["id"]: c for c in out["cases"]}
    assert cases["billing-1"]["status"] == "passed" and cases["support-1"]["status"] == "passed"
    assert cases["support-1"]["output"]["help"]["simulated"] is True and cases["billing-1"]["run_status"] == "completed"
    assert out["summary"]["pass_rate"] == 1.0 and out["summary"]["total"] == 2
    stored = EvaluationStore().get_report("ana", out["id"])
    assert stored["status"] == "completed" and stored["summary"]["pass_rate"] == 1.0 and len(stored["cases"]) == 2
    assert EvaluationStore().get_report("bob", out["id"]) is None, "a report is its owner's"
    assert [r["id"] for r in EvaluationStore().list_reports("ana", saved)] == [out["id"]]
    assert EvaluationStore().list_reports("bob", saved) == []


def test_a_failing_score_fails_the_case_and_a_bad_input_is_an_error_with_no_run(saved):
    put("ana", saved, {"cases": [
        {"id": "wrong", "inputs": {"ticket": "x"}, "scorers": [{"type": "exact", "path": "bill.data.subject", "expected": "Refund"}]},
        {"id": "no-input", "inputs": {}},
        {"id": "smoke", "inputs": {"ticket": "y"}}]})
    out = evaluation.run_evaluation("ana", saved, set_name="smoke")
    by = {c["id"]: c for c in out["cases"]}
    assert by["wrong"]["status"] == "failed" and 'expected "Refund"' in by["wrong"]["scores"][0]["detail"]
    assert by["no-input"]["status"] == "error" and "missing required field 'ticket'" in by["no-input"]["error"]
    assert by["smoke"]["status"] == "passed" and "no scorers" in by["smoke"]["note"]
    assert (out["summary"]["passed"], out["summary"]["failed"], out["summary"]["error"]) == (1, 1, 1)
    assert out["summary"]["unscored"] == 2


def test_a_mock_that_fails_a_node_fails_the_case_with_the_reason(saved):
    put("ana", saved, {"cases": [{"id": "down", "inputs": {"ticket": "x"},
                                  "mocks": {"route": {"status": "failed", "reason": "classifier down"}}}]})
    case = evaluation.run_evaluation("ana", saved, set_name="smoke")["cases"][0]
    assert case["status"] == "failed" and case["run_status"] == "failed" and "classifier down" in case["error"]


def test_picking_cases_and_unknown_names(saved):
    put("ana", saved, SET)
    out = evaluation.run_evaluation("ana", saved, set_name="smoke", case_ids=["support-1"])
    assert [c["id"] for c in out["cases"]] == ["support-1"]
    for kwargs, code in (({"set_name": "nope"}, "not_found"), ({"set_name": "smoke", "case_ids": ["x"]}, "not_found")):
        with pytest.raises(EvaluationError) as err:
            evaluation.run_evaluation("ana", saved, **kwargs)
        assert err.value.code == code
    with pytest.raises(EvaluationError) as err:
        evaluation.run_evaluation("ana", "ghost", set_name="smoke")
    assert err.value.code == "not_found"
    with pytest.raises(EvaluationError) as err:
        evaluation.run_evaluation("bob", saved, set_name="smoke")
    assert err.value.code == "not_found", "another owner's workflow does not exist"


def test_real_mode_needs_two_opt_ins_and_the_engine(saved, store):  # noqa: F811
    put("ana", saved, SET)
    with pytest.raises(EvaluationError) as err:
        evaluation.run_evaluation("ana", saved, set_name="smoke", mode="real")
    assert err.value.code == "real_not_confirmed"
    with pytest.raises(EvaluationError) as err:
        evaluation.run_evaluation("ana", saved, set_name="smoke", mode="real", allow_real=True)
    assert err.value.code == "unavailable"
    with pytest.raises(EvaluationError, match="mode"):
        evaluation.run_evaluation("ana", saved, set_name="smoke", mode="turbo")


class ModelStub:
    def __init__(self, label, repair=None):
        self.label = label
        self.repair = json.dumps({"subject": "Refund", "total": 12}) if repair is None else repair

    def complete(self, messages, **kw):
        return self.repair

    def decide(self, context, fields, **kw):
        from src.typed_decision import Decision
        return {fields[0].name: Decision(field=fields[0].name, value=self.label, confidence=0.99, mass=1.0,
                                         distribution={}, method="logprobs", reason="", best=self.label, model="m")}


def test_real_mode_runs_the_real_workflow_and_scores_what_it_returned(saved, store):  # noqa: F811
    put("ana", saved, {"cases": [
        {"id": "real-1", "inputs": {"ticket": "charged twice"},
         "scorers": [{"type": "exact", "path": "bill.data.subject", "expected": "Refund"},
                     {"type": "numeric", "path": "bill.data.total", "expected": 12, "tolerance": 0}]}]})
    brief = Brief()
    brief_calls: List[Any] = []

    def agent(spec):
        brief_calls.append(spec["prompt"])
        return {"text": json.dumps({"subject": "Refund", "total": 12}), "rounds": 1, "tool_calls": 0, "model": "m",
                "profile": "", "session_id": "s", "tool_events": []}

    engine = WorkflowEngine(default_handlers(agent=agent, models=ModelStub("billing")), store)
    out = evaluation.run_evaluation("ana", saved, set_name="smoke", mode="real", allow_real=True, store=store,
                                    engine=engine, timeout_s=30)
    case = out["cases"][0]
    assert case["status"] == "passed", case
    assert case["run_id"].startswith("wfr_") and case["output"]["bill"]["data"]["total"] == 12
    assert brief_calls == ["Reply to charged twice"] and out["summary"]["mode"] == "real"
    assert "note" not in out["summary"]
    assert store.get_run(case["run_id"])["run"].owner == "ana"


def test_real_mode_reports_a_failed_run_and_a_run_waiting_on_a_person(saved, store):  # noqa: F811
    put("ana", saved, {"cases": [{"id": "r", "inputs": {"ticket": "x"}}]})

    def broken(spec):
        return {"text": "not json at all", "rounds": 1, "tool_calls": 0, "model": "m", "profile": "", "session_id": "s"}

    engine = WorkflowEngine(default_handlers(agent=broken, models=ModelStub("billing", repair="still not json")), store)
    case = evaluation.run_evaluation("ana", saved, set_name="smoke", mode="real", allow_real=True, store=store,
                                     engine=engine, timeout_s=30)["cases"][0]
    assert case["status"] == "failed" and "bill" in case["error"] and case["run_status"] == "failed"

    wf = WorkflowDefinition.parse({"id": "gate.flow", "version": "1.0.0", "title": "Gate", "inputs": INPUTS,
                                   "nodes": [START, {"id": "ok", "type": "skill", "needs": ["start"], "config": {"skill": "x.y"}}]})
    WorkflowLibrary().save("ana", wf, name="gate")
    put("ana", "gate", {"cases": [{"id": "g", "inputs": {"ticket": "x"}}]})
    paused = WorkflowEngine(default_handlers(skill=lambda n, c: {"status": "paused", "approval_id": "apr_1", "reason": "needs a person"}), store)
    case = evaluation.run_evaluation("ana", "gate", set_name="smoke", mode="real", allow_real=True, store=store,
                                     engine=paused, timeout_s=30)["cases"][0]
    assert case["status"] == "error" and "does not answer approvals" in case["error"]


def test_real_mode_caps_the_cases(saved, store):  # noqa: F811
    put("ana", saved, {"cases": [{"id": f"c{i}", "inputs": {"ticket": "x"}} for i in range(evaluation.MAX_REAL_CASES + 1)]})
    with pytest.raises(EvaluationError, match="at most"):
        evaluation.run_evaluation("ana", saved, set_name="smoke", mode="real", allow_real=True, store=store,
                                  engine=WorkflowEngine(default_handlers(), store))


def test_a_set_that_no_longer_fits_the_workflow_is_refused_at_run_time(saved):
    put("ana", saved, {"cases": [{"id": "a", "inputs": {"ticket": "x"}, "mocks": {"route": {"label": "support"}}}]})
    smaller = WorkflowDefinition.parse({"id": "ticket.triage", "version": "1.0.1", "title": "T", "inputs": INPUTS,
                                        "nodes": [START]})
    WorkflowLibrary().save("ana", smaller, name=saved)
    with pytest.raises(EvaluationError, match="no node named"):
        evaluation.run_evaluation("ana", saved, set_name="smoke")


def test_a_background_evaluation_returns_its_id_and_a_dead_one_is_not_left_running(saved):
    put("ana", saved, SET)
    rid, future = evaluation.start_evaluation("ana", saved, set_name="smoke")
    assert future.result(timeout=30)["status"] == "completed"
    assert EvaluationStore().get_report("ana", rid)["status"] == "completed"
    ghost = EvaluationStore().open_report("ana", saved, {"id": None, "name": "smoke"}, "simulate")
    assert EvaluationStore().fail_stale("ana") == 1
    assert EvaluationStore().get_report("ana", ghost)["status"] == "interrupted"


def test_set_storage_is_per_owner_and_capped(saved):
    put("ana", saved, SET)
    assert EvaluationStore().get_set("bob", saved, "smoke") is None
    assert [s["name"] for s in EvaluationStore().list_sets("ana", saved)] == ["smoke"]
    assert EvaluationStore().list_sets("ana", saved)[0]["cases"] == 2
    with pytest.raises(EvaluationError, match="usable set name"):
        EvaluationStore().save_set("ana", saved, "Bad Name", {"cases": []})
    for i in range(evaluation.MAX_SETS_PER_WORKFLOW - 1):
        put("ana", saved, SET, set_name=f"s{i}")
    with pytest.raises(EvaluationError, match="at most"):
        put("ana", saved, SET, set_name="one-too-many")
    assert EvaluationStore().delete_set("ana", saved, "smoke") is True and EvaluationStore().delete_set("ana", saved, "smoke") is False


# ── over HTTP ─────────────────────────────────────────────────────────────

from tests.test_workflow_published import client  # noqa: E402,F401  (fixture)


def save_wf(client, d=None, **extra):
    return client.post("/api/workflows/library", json={"definition": (d or triage()).to_dict(), **extra})


def test_the_routes_put_run_and_read_back_a_set(client):
    assert save_wf(client).status_code == 200
    base = "/api/workflows/library/ticket_triage"
    assert client.put(f"{base}/eval-sets/smoke", json=SET).status_code == 200
    listed = client.get(f"{base}/eval-sets").json()
    assert [s["name"] for s in listed["sets"]] == ["smoke"] and listed["model_judge"] is False
    assert len(client.get(f"{base}/eval-sets/smoke").json()["set"]["set"]["cases"]) == 2
    run = client.post(f"{base}/evaluate", json={"set": "smoke", "wait_seconds": 30})
    assert run.status_code == 200
    body = run.json()
    assert body["finished"] is True and body["report"]["summary"]["pass_rate"] == 1.0
    assert body["report"]["summary"]["mode"] == "simulate" and len(body["report"]["cases"]) == 2
    assert [r["id"] for r in client.get(f"{base}/evaluations").json()["reports"]] == [body["report_id"]]
    assert client.get(f"/api/workflows/evaluations/{body['report_id']}").json()["report"]["status"] == "completed"
    assert client.get("/api/workflows/evaluations/wer_nope").status_code == 404
    assert client.delete(f"{base}/eval-sets/smoke").status_code == 200
    assert client.delete(f"{base}/eval-sets/smoke").status_code == 404


def test_the_routes_refuse_and_name_what_is_wrong(client):
    save_wf(client)
    base = "/api/workflows/library/ticket_triage"
    bad = client.put(f"{base}/eval-sets/smoke", json={"cases": [{"id": "a", "mocks": {"x": {}}}]})
    assert bad.status_code == 400 and bad.json()["detail"]["problems"]
    assert client.put("/api/workflows/library/ghost/eval-sets/smoke", json=SET).status_code == 404
    assert client.post(f"{base}/evaluate", json={"set": "nope"}).status_code == 404
    client.put(f"{base}/eval-sets/smoke", json=SET)
    real = client.post(f"{base}/evaluate", json={"set": "smoke", "mode": "real"})
    assert real.status_code == 400 and real.json()["detail"]["code"] == "real_not_confirmed"
    assert client.post(f"{base}/evaluate", json={"set": "smoke", "wait_seconds": 999}).status_code == 400
    assert client.post(f"{base}/evaluate", json={"set": "smoke", "cases": "all"}).status_code == 400
    assert client.post(f"{base}/evaluate", json={"set": "smoke", "allow_real": "yes", "mode": "real"}).status_code == 400
    assert client.post(f"{base}/evaluate", json={"set": "smoke", "timeout_s": 0}).status_code == 400


def test_deleting_a_saved_workflow_deletes_its_sets(client):
    save_wf(client)
    base = "/api/workflows/library/ticket_triage"
    client.put(f"{base}/eval-sets/smoke", json=SET)
    client.delete(base)
    save_wf(client)
    assert client.get(f"{base}/eval-sets").json()["sets"] == []


def test_the_dry_run_route_returns_placeholders_and_honours_mocks(client):
    d = triage().to_dict()
    ok = client.post("/api/workflows/dry-run", json={"definition": d, "inputs": {"ticket": "x"},
                                                      "mocks": {"route": {"label": "support"}}})
    assert ok.status_code == 200
    result = ok.json()["result"]
    assert result["simulated"] is True and result["nodes"]["help"]["status"] == "completed"
    assert result["nodes"]["bill"]["status"] == "skipped"
    assert client.post("/api/workflows/dry-run", json={"definition": d, "mocks": {"nobody": {}}}).status_code == 400
    assert client.post("/api/workflows/dry-run", json={"definition": d, "inputs": []}).status_code == 400
    assert client.post("/api/workflows/dry-run", json={"definition": {"id": "x"}}).status_code == 400
