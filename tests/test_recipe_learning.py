"""Recipes learned from finished work: parameter extraction, the second-case
try (leaks, unresolved steps), promotion and search (src/recipe_learning.py)."""
from __future__ import annotations

import json
import os

import pytest

from src import constants as constants_mod
from src import recipe_learning as rl
from src import recipes

OWNER = "alice"
REQUEST = ('Convert "Q3 report.docx" and C:\\Users\\luis\\docs\\q3.csv to PDF for customer ACME-42, '
           'keep the top 15 rows')
STEPS = [
    {"tool": "read_file", "args": {"path": "C:\\Users\\luis\\docs\\q3.csv"}},
    {"tool": "bash", "args": {"content": "head -n 15 C:\\Users\\luis\\docs\\q3.csv > /tmp/out.csv; echo ACME-42"}},
    {"tool": "write_file", "args": {"path": "/tmp/Q3 report.docx", "limit": 15, "mode": "w"}},
]
CASE2 = {"file": "Annual plan.docx", "path": "D:\\data\\plan.csv", "id": "BETA-7", "number": 40}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    yield


def propose(**kw):
    return rl.propose(OWNER, request=REQUEST, steps=STEPS, title="Convert a report", **kw)


def by_name(recipe):
    return {p["name"]: p for p in recipe.params}


def test_paths_names_ids_and_numbers_become_parameters_in_order():
    r = propose()
    p = by_name(r)
    assert set(p) == {"file", "path", "id", "number"}
    assert p["file"]["example"] == "Q3 report.docx" and p["id"]["example"] == "ACME-42"
    assert p["number"]["example"] == 15 and p["number"]["kind"] == "number"
    assert [t["tool"] for t in r.step_templates] == ["read_file", "bash", "write_file"]
    assert r.step_templates[0]["args"] == {"path": {"$param": "path"}}
    assert r.step_templates[1]["args"]["content"] == "head -n {{number}} {{path}} > /tmp/out.csv; echo {{id}}"
    assert r.step_templates[2]["args"] == {"path": "/tmp/{{file}}", "limit": {"$param": "number"}, "mode": "w"}
    assert r.status == "draft" and r.examples[0]["params"]["id"] == "ACME-42" and r.limits
    stored = recipes.get_recipe(r.id, OWNER)
    assert stored is not None and stored.step_templates == r.step_templates


def test_nothing_is_saved_by_a_failed_or_empty_proposal():
    with pytest.raises(rl.RecipeLearningError):
        rl.propose(OWNER, request="x", steps=[])
    with pytest.raises(rl.RecipeLearningError):
        rl.propose(OWNER)
    assert recipes.list_recipes(OWNER) == recipes.list_recipes(None)


def test_secrets_in_the_source_are_masked_before_they_reach_the_recipe():
    steps = [{"tool": "bash", "args": {"content": "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456' x"}}]
    r = rl.propose(OWNER, request="call it", steps=steps)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(r.to_dict())


def test_failed_calls_are_left_out():
    trace = rl.trace_from_events("do q3.csv", [
        {"tool": "bash", "command": "cat q3.csv", "exit_code": 1},
        {"tool": "bash", "command": "cat q3.csv", "exit_code": 0}])
    assert len(trace["steps"]) == 1 and trace["skipped_failed"] == 1


def test_second_case_without_leaks_is_promotable_and_promotion_reaches_the_skill_search(monkeypatch):
    r = propose()
    out = rl.try_recipe(r.id, OWNER, CASE2)
    assert out["executed"] is False
    assert out["resolved"] == out["total"] == 3 and out["leaks"] == [] and out["untested_params"] == []
    assert out["promotable"] is True
    assert out["outputs"][1]["args"]["content"] == "head -n 40 D:\\data\\plan.csv > /tmp/out.csv; echo BETA-7"
    added = {}

    class Mgr:
        def add_skill(self, **kw):
            added.update(kw)
            return {"name": kw["name"]}
    import src.skill_library as sl
    monkeypatch.setattr(sl, "_skills_manager", lambda data_dir=None: Mgr())
    done = rl.promote(r.id, OWNER)
    assert done["recipe"]["status"] == "published" and done["skill"]["added"] is True
    assert "recipe" in added["tags"] and added["owner"] == OWNER
    assert recipes.get_recipe(r.id, OWNER).status == "published"
    assert any(h["id"] == r.id for h in rl.search("convert report pdf customer", OWNER))


def test_a_leaked_literal_blocks_promotion():
    steps = [dict(s) for s in STEPS]
    # the customer id appears inside a value the request never spelled that way, so it stays literal
    steps[1] = {"tool": "bash", "args": {"content": "head -n 15 C:\\Users\\luis\\docs\\q3.csv > /tmp/out.csv; echo ACME-42"}}
    steps.append({"tool": "write_file", "args": {"path": "/tmp/notes.txt", "content": "for acme-42 / Q3 report.docx"}})
    r = rl.propose(OWNER, request=REQUEST, steps=steps)
    # force a leak: a hand-edited template that kept the first case's file name
    r.step_templates[3]["args"]["content"] = "report for Q3 report.docx"
    recipes.save_recipe(OWNER, r)
    out = rl.try_recipe(r.id, OWNER, CASE2)
    assert out["promotable"] is False
    assert out["leaks"] and out["leaks"][0]["param"] == "file" and out["leaks"][0]["step"] == 4
    assert any("literal of the first case" in w for w in out["why_not_promotable"])
    with pytest.raises(rl.RecipeLearningError) as e:
        rl.promote(r.id, OWNER)
    assert e.value.error_class == "recipes.try_not_clean"
    assert recipes.get_recipe(r.id, OWNER).status == "draft"


def test_missing_parameter_and_unknown_tool_leave_steps_unresolved():
    steps = [{"tool": "no_such_tool_anywhere", "args": {"path": "C:\\Users\\luis\\docs\\q3.csv"}}]
    r = rl.propose(OWNER, request=REQUEST, steps=steps)
    out = rl.try_recipe(r.id, OWNER, {})
    assert out["promotable"] is False and out["problems"] == ["missing parameter `path`"]
    out = rl.try_recipe(r.id, OWNER, {"path": "D:\\x\\y.csv"})
    assert out["steps"][0]["status"] == "unresolved" and "tool not found" in out["steps"][0]["reason"]


def test_second_case_must_change_every_parameter():
    r = propose()
    same = {p["name"]: p["example"] for p in r.params}
    out = rl.try_recipe(r.id, OWNER, same)
    assert out["promotable"] is False and set(out["untested_params"]) == set(same)
    partial = dict(CASE2, id="ACME-42")
    out = rl.try_recipe(r.id, OWNER, partial)
    assert out["untested_params"] == ["id"] and out["promotable"] is False


def test_promotion_needs_a_try_and_a_recipe_unchanged_since():
    r = propose()
    with pytest.raises(rl.RecipeLearningError) as e:
        rl.promote(r.id, OWNER)
    assert e.value.error_class == "recipes.not_tried"
    rl.try_recipe(r.id, OWNER, CASE2)
    edited = recipes.get_recipe(r.id, OWNER)
    edited.step_templates[0]["tool"] = "write_file"
    recipes.save_recipe(OWNER, edited)
    with pytest.raises(rl.RecipeLearningError) as e:
        rl.promote(r.id, OWNER)
    assert e.value.error_class == "recipes.changed_since_try"


def test_executor_is_only_used_when_given_and_failures_are_reported():
    r = propose()
    seen = []
    out = rl.try_recipe(r.id, OWNER, CASE2, executor=lambda tool, args: seen.append(tool) or "ok", record=False)
    assert out["executed"] is True and seen == ["read_file", "bash", "write_file"]
    out = rl.try_recipe(r.id, OWNER, CASE2, executor=lambda tool, args: 1 / 0, record=False)
    assert out["promotable"] is False and out["steps"][0]["status"] == "failed"


def test_workflow_run_inputs_become_the_parameters(monkeypatch):
    steps = [{"tool": "workflow.agent", "args": {"prompt": "Summarise invoice INV-2041 for ACME"}}]
    r = rl.propose_from_trace({"request": "customer: ACME\ninvoice: INV-2041", "steps": steps,
                               "hints": {"customer": "ACME", "invoice": "INV-2041"},
                               "source": {"kind": "workflow_run", "run_id": "w1"}}, OWNER)
    assert r.step_templates[0]["args"]["prompt"] == "Summarise invoice {{invoice}} for {{customer}}"
    assert r.source["run_id"] == "w1" and r.source["learned"] is True


def test_run_log_trace_keeps_arguments_and_order(tmp_path, monkeypatch):
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    lines = [{"label": "convert", "session_id": ""},
             {"ev": "data: " + json.dumps({"type": "tool_start", "tool": "read_file", "call_id": "1",
                                          "full_command": json.dumps({"path": "/a/b.csv"})})},
             {"ev": "data: " + json.dumps({"type": "tool_output", "call_id": "1", "exit_code": 0})},
             {"ev": "data: " + json.dumps({"type": "tool_start", "tool": "bash", "call_id": "2", "full_command": "ls /a"})},
             {"ev": "data: " + json.dumps({"type": "tool_output", "call_id": "2", "exit_code": 2})},
             {"status": "done"}]
    (runs / "run1.jsonl").write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
    trace = rl.trace_from_run_log("run1", OWNER)
    assert [s["tool"] for s in trace["steps"]] == ["read_file"] and trace["skipped_failed"] == 1
    assert trace["steps"][0]["args"] == {"path": "/a/b.csv"}
    (runs / "run2.jsonl").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    with pytest.raises(rl.RecipeLearningError) as e:
        rl.trace_from_run_log("run2", OWNER)
    assert e.value.error_class == "recipes.run_not_finished"


def test_existing_recipe_shape_is_unchanged_for_other_recipes():
    d = recipes.Recipe(id="x", title="t").to_dict()
    assert "params" not in d and "step_templates" not in d and "version" not in d


def test_session_trace_reads_the_stored_tool_events_of_a_turn(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db_mod
    engine = create_engine("sqlite:///" + str(tmp_path / "t.db"), connect_args={"check_same_thread": False})
    db_mod.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    s = Session()
    s.add(db_mod.Session(id="s1", name="t", endpoint_url="http://x", model="m", owner=OWNER))
    t0 = datetime(2026, 1, 1)
    meta = json.dumps({"tool_events": [
        {"tool": "read_file", "command": json.dumps({"path": "/data/q3.csv"}), "exit_code": 0},
        {"tool": "bash", "command": "wc -l /data/q3.csv", "exit_code": 0}]})
    s.add(db_mod.ChatMessage(id="m1", session_id="s1", role="user", content="count the lines of /data/q3.csv", timestamp=t0))
    s.add(db_mod.ChatMessage(id="m2", session_id="s1", role="assistant", content="done", meta_data=meta,
                             timestamp=t0 + timedelta(seconds=1)))
    s.commit()
    s.close()
    r = rl.propose(OWNER, session_id="s1", title="Count lines")
    assert [p["example"] for p in r.params] == ["/data/q3.csv"]
    assert r.step_templates[1]["args"]["content"] == "wc -l {{path}}"
    assert r.source["session_id"] == "s1"
    with pytest.raises(rl.RecipeLearningError):
        rl.propose("mallory", session_id="s1")
