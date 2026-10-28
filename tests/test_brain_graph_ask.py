"""graph_ask: planner parsing/validation, mutual index, fusion, level-parallel
execution, sub-question rewriting and the degraded paths. A scripted model
stands in for the real one; the graph is synthetic."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities, graph_ask, graph_plan  # noqa: E402
from src.brain.graph_index import MutualIndex, weighted_merge  # noqa: E402

OWNER = "ana"

TEXTS = {
    "mem:a1": "Project Alpha was created by Mara, who works at Nimbus and lives in Quito.",
    "mem:a2": "Project Alpha uses Rust and PostgreSQL for storage.",
    "mem:b1": "Project Beta uses PostgreSQL and Redis.",
    "mem:n1": "Nimbus is a company based in Quito. Its first office opened in 2012.",
    "mem:z1": "Totally unrelated note about gardening and tomatoes.",
}


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(entities, "_fact_from_source",
                        lambda owner, ref: ({"source_ref": ref, "text": TEXTS[ref]} if ref in TEXTS else None))
    e = {}
    for name, kind in (("Project Alpha", "project"), ("Project Beta", "project"), ("Mara", "person"),
                       ("Nimbus", "organization"), ("Quito", "place"), ("Rust", "tool"), ("PostgreSQL", "tool"),
                       ("Redis", "tool")):
        e[name] = entities.upsert_entity(OWNER, name, type=kind)["id"]
    rel = lambda a, r, b, ref: entities.add_relation(OWNER, e[a], r, dst_id=e[b], evidence=(ref,), confidence=0.8)
    rel("Mara", "created", "Project Alpha", "mem:a1")
    rel("Mara", "works_at", "Nimbus", "mem:a1")
    rel("Mara", "lives_in", "Quito", "mem:a1")
    rel("Nimbus", "located_in", "Quito", "mem:n1")
    rel("Project Alpha", "uses", "Rust", "mem:a2")
    rel("Project Alpha", "uses", "PostgreSQL", "mem:a2")
    rel("Project Beta", "uses", "PostgreSQL", "mem:b1")
    rel("Project Beta", "uses", "Redis", "mem:b1")
    for name, refs in (("Project Alpha", ("mem:a1", "mem:a2")), ("Mara", ("mem:a1",)), ("Nimbus", ("mem:a1", "mem:n1")),
                       ("Project Beta", ("mem:b1",))):
        for ref in refs:
            entities.add_mention(OWNER, e[name], ref)
    entities.upsert_entity("someone-else", "Secret Project", type="project")
    yield e
    brain_db.use_dir(None)


class Script:
    """A model that answers by recognising the prompt, and remembers it."""

    def __init__(self, plan_text="", **answers):
        self.plan_text, self.answers, self.prompts = plan_text, answers, []

    async def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        if prompt.startswith("You plan how to answer"):
            return self.plan_text
        if prompt.startswith("Rewrite the sub-question"):
            return self.answers.get("rewrite", "")
        if prompt.startswith("Answer the question in one or two sentences, in the language"):
            return self.answers.get("final", "FINAL")
        if prompt.startswith("Answer the question using only the numbered"):
            return self.answers.get("read", "NONE")
        if prompt.startswith("Answer the question in one or two sentences using only"):
            return self.answers.get("flat", "FLAT")
        return self.answers.get("other", "OTHER")


TWO_HOP = """Step1: Who created Project Alpha?
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:created_by, o=o1:person)
Step2: In which city does the person from Step1 live?
Action2: Retrieval(s=o1, p=p2:lives_in, o=o2:place)
Step3: Return the city.
Action3: Output(o2)"""


# -- the plan ---------------------------------------------------------------------------------------

def test_parse_binds_aliases_and_levels():
    steps = graph_plan.bind(graph_plan.parse(TWO_HOP))
    assert [s.op for s in steps] == ["retrieval", "retrieval", "output"]
    assert steps[1].s.ref == "o1" and steps[1].needs == {"o1"}
    assert [s.level for s in steps] == [0, 1, 2]
    assert steps[0].p == "created_by" and steps[0].s.name == "Project Alpha" and steps[0].s.type == "project"


def test_independent_steps_share_a_level_and_deduce_depends_on_both():
    text = """Step1: Tools of Alpha?
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:uses, o=o1:tool)
Step2: Tools of Beta?
Action2: Retrieval(s=s2:project[`Project Beta`], p=p2:uses, o=o2:tool)
Step3: Which is in both?
Action3: Deduce(op=extract, content=[`o1`, `o2`], target=`Which tool is in both lists?`)->d3
Step4: Answer.
Action4: Output(d3)"""
    steps = graph_plan.bind(graph_plan.parse(text))
    assert [[s.n for s in lvl] for lvl in graph_plan.levels(steps)] == [[1, 2], [3], [4]]
    assert steps[2].needs == {"o1", "o2"} and steps[2].deduce_op == "extract"


def test_alias_reuse_of_an_earlier_subject_and_bracketed_alias():
    text = """Step1: First.
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:uses, o=o1:tool)
Step2: Second.
Action2: Retrieval(s=s1, p=p2:created_by, o=o2:person)
Step3: Third.
Action3: Retrieval(s=s3:organization[`o2`], p=p3:located_in, o=o3:place)
Action4: Output(o3)"""
    # Step3/Action3 written with a bracketed alias; step lines of 4 missing is tolerated for Output
    text = text.replace("Action4: Output(o3)", "Step4: End.\nAction4: Output(o3)")
    steps = graph_plan.bind(graph_plan.parse(text))
    assert steps[1].s.ref == "s1"
    assert steps[2].s.ref == "o2" and steps[2].needs == {"o2"}


@pytest.mark.parametrize("text, message", [
    ("Step1: x", "no `Action`"),
    ("Step1: x\nAction1: Foo(a=b)", "not one of"),
    ("Step1: x\nAction1: Retrieval(p=p1:uses)", "needs s= and o="),
    ("Step1: x\nAction1: Retrieval(s=s1[`A`], p=p1, o=o1)\nStep2: y\nAction2: Output(zzz)", "which no step defines"),
    ("Step1: x\nAction1: Retrieval(s=s1[`A`], p=p1, o=o1)\nStep2: y\nAction2: Retrieval(s=s2[`B`], p=p2, o=o1:tool)", "defined twice"),
])
def test_bad_plans_are_refused_with_a_reason(text, message):
    with pytest.raises(graph_plan.PlanError) as err:
        graph_plan.bind(graph_plan.parse(text))
    assert message in str(err.value)


def test_a_step_cannot_use_an_alias_defined_later():
    text = """Step1: x
Action1: Retrieval(s=s1:project[`A`], p=p1:uses, o=o1)
Step2: y
Action2: Math(content=[`o3`], target=`t`)->m2
Step3: z
Action3: Retrieval(s=s3:tool[`B`], p=p3:uses, o=o3)"""
    steps = graph_plan.parse(text)
    with pytest.raises(graph_plan.PlanError):
        graph_plan.bind(steps)


def test_missing_output_is_added_and_step_limit_enforced():
    steps = graph_plan.bind(graph_plan.parse(
        "Step1: x\nAction1: Retrieval(s=s1:project[`Project Alpha`], p=p1:uses, o=o1:tool)"))
    assert steps[-1].op == "output" and steps[-1].outputs == ["o1"]
    many = "\n".join(f"Step{i}: x\nAction{i}: Retrieval(s=s{i}:project[`A`], p=p{i}, o=o{i})" for i in range(1, 12))
    with pytest.raises(graph_plan.PlanError):
        graph_plan.bind(graph_plan.parse(many), max_steps=8)


def test_prompt_is_grounded_in_the_graph(graph):
    schema = MutualIndex.build(OWNER).schema()
    prompt = graph_plan.build_prompt("Where does the creator of Project Alpha live?", schema, error="bad", previous="old")
    assert "lives_in" in prompt and "Project Alpha (project)" in prompt and "bad" in prompt and "old" in prompt
    assert "gardening" not in prompt


# -- fusion and the mutual index -------------------------------------------------------------------

def test_weighted_merge_is_min_max_and_weighted():
    merged = weighted_merge({"a": 10.0, "b": 0.0}, {"b": 5.0, "c": 1.0}, alpha=0.75)
    assert merged["a"] == pytest.approx(0.75)
    assert merged["b"] == pytest.approx(0 * 0.75 + 1.0 * 0.25)
    assert merged["c"] == pytest.approx(0.0)
    assert weighted_merge({"x": 3.0}, {}, 0.5) == {"x": 0.5}   # equal scores normalise to 1 then take their share
    assert weighted_merge({}, {}) == {}


def test_index_links_entities_and_fragments_both_ways(graph):
    idx = MutualIndex.build(OWNER)
    alpha = graph["Project Alpha"]
    assert set(idx.entity_fragments(alpha)) == {"mem:a1", "mem:a2"}
    assert set(idx.name_of(e) for e in idx.fragment_entities("mem:a1")) >= {"Project Alpha", "Mara", "Nimbus", "Quito"}
    assert "mem:z1" not in idx.ref_entities
    assert idx.find("project alpha") == [alpha] and idx.find("Alpha") == [alpha]
    assert idx.find("Project Zeta") == []          # one shared word is not the same name
    assert idx.find("Nowhere") == []
    assert "Secret Project" not in {e["name"] for e in idx.entities.values()}   # other owner
    scores = idx.text_scores("where does Mara live Quito")
    assert max(scores, key=scores.get) == "mem:a1"


# -- running a question -----------------------------------------------------------------------------

async def test_two_hop_question_binds_aliases_rewrites_and_answers(graph):
    llm = Script(TWO_HOP, rewrite="In which city does Mara live?", final="Mara vive en Quito.")
    out = await graph_ask.ask(OWNER, "¿En qué ciudad vive quien creó Project Alpha?", llm=llm)
    assert out["status"] == "ok" and out["found"] is True
    assert out["values"] == ["Quito"] and out["answer"] == "Mara vive en Quito."
    s1, s2 = out["steps"][0], out["steps"][1]
    assert s1["values"] == ["Mara"] and s1["via"] == "graph"
    assert s1["triples"][0]["p"] == "created" and s1["triples"][0]["evidence"] == ["mem:a1"]
    assert s2["rewritten"] == "In which city does Mara live?" and s2["values"] == ["Quito"]
    assert out["levels"] == [[1], [2], [3]] and "mem:a1" in out["sources"]
    assert any(p.startswith("Rewrite the sub-question") and "o1 = Mara" in p for p in llm.prompts)
    assert out["model_calls"] == len(llm.prompts) == 3     # plan, one rewrite, answer
    assert out["graph"]["entities"] == 8 and out["untrusted_content"] is True


async def test_three_hops_follow_the_chain(graph):
    plan = TWO_HOP.replace("Step3: Return the city.\nAction3: Output(o2)",
                           "Step3: Where is the company of that person?\nAction3: Retrieval(s=o1, p=p3:works_at, o=o3:organization)\n"
                           "Step4: Where is that organization located?\nAction4: Retrieval(s=o3, p=p4:located_in, o=o4:place)\n"
                           "Step5: Return it.\nAction5: Output(o4)")
    out = await graph_ask.ask(OWNER, "Where is the company of the creator of Project Alpha?", llm=Script(plan, rewrite=""))
    assert out["values"] == ["Quito"]
    assert [s["values"] for s in out["steps"][:4]] == [["Mara"], ["Quito"], ["Nimbus"], ["Quito"]]
    assert out["levels"] == [[1], [2, 3], [4], [5]]     # steps 2 and 3 only need o1 and run together


async def test_steps_of_one_level_run_together(graph, monkeypatch):
    plan = """Step1: Tools of Alpha?
Action1: Retrieval(s=s1:project[`Project Alpha`], p=p1:uses, o=o1:tool)
Step2: Tools of Beta?
Action2: Retrieval(s=s2:project[`Project Beta`], p=p2:uses, o=o2:tool)
Step3: Which is in both?
Action3: Deduce(op=extract, content=[`o1`, `o2`], target=`Which tool is in both lists?`)->d3
Step4: Answer.
Action4: Output(d3)"""
    events = []
    original = graph_ask._run_step

    async def traced(run, step):
        events.append(("start", step.n))
        await asyncio.sleep(0.05)
        await original(run, step)
        events.append(("end", step.n))

    monkeypatch.setattr(graph_ask, "_run_step", traced)
    out = await graph_ask.ask(OWNER, "Which tool do Alpha and Beta share?", llm=Script(plan, other="PostgreSQL", final="PostgreSQL."))
    assert events[:2] == [("start", 1), ("start", 2)]          # both before either ends
    assert out["values"] == ["PostgreSQL"]
    assert set(out["steps"][0]["values"]) == {"Rust", "PostgreSQL"}


async def test_a_hop_the_graph_lacks_is_read_from_the_fused_fragments(graph):
    plan = """Step1: When did the first office of Nimbus open?
Action1: Retrieval(s=s1:organization[`Nimbus`], p=p1:first_office_opened, o=o1:date)
Step2: Return it.
Action2: Output(o1)"""
    llm = Script(plan, read="2012", final="En 2012.")
    out = await graph_ask.ask(OWNER, "When did the first office of Nimbus open?", llm=llm)
    first = out["steps"][0]
    assert first["via"] == "text" and first["values"] == ["2012"]
    assert first["fragments"][0]["ref"] == "mem:n1" and first["fragments"][0]["text_score"] > 0
    assert out["answer"] == "En 2012." and "mem:z1" not in out["sources"]


async def test_math_does_plain_arithmetic_without_the_model(graph):
    plan = """Step1: Add.
Action1: Math(content=[`30`, `+`, `6`], target=`30+6`)->m1
Step2: Out.
Action2: Output(m1)"""
    llm = Script(plan, final="36")
    out = await graph_ask.ask(OWNER, "30 + 6?", llm=llm)
    assert out["steps"][0]["via"] == "arithmetic" and out["values"] == ["36"]
    assert graph_ask.safe_eval("2 * (3 + 4) ** 2") == 98
    assert graph_ask.safe_eval("__import__('os')") is None and graph_ask.safe_eval("9**99") is None
    assert graph_ask.safe_eval("1/0") is None


async def test_unknown_entity_is_reported_not_invented(graph):
    plan = """Step1: Who created Project Zeta?
Action1: Retrieval(s=s1:project[`Project Zeta`], p=p1:created_by, o=o1:person)
Step2: Out.
Action2: Output(o1)"""
    out = await graph_ask.ask(OWNER, "Who created Project Zeta?", llm=Script(plan))
    assert out["found"] is False and out["values"] == []
    assert "no entity called `Project Zeta`" in out["steps"][0]["error"]
    assert "No he encontrado" in out["answer"]


async def test_a_bad_plan_is_repaired_once_then_falls_back_to_a_flat_search(graph):
    llm = Script("nonsense", flat="Mara created it.")
    out = await graph_ask.ask(OWNER, "Who created Project Alpha?", llm=llm)
    assert out["status"] == "degraded" and out["answer"] == "Mara created it."
    assert out["planner"]["attempts"] == 2 and out["planner"]["errors"]
    assert sum(p.startswith("You plan how to answer") for p in llm.prompts) == 2
    assert any("Your previous plan could not be used" in p for p in llm.prompts)
    assert out["fragments"][0]["ref"] == "mem:a1"


async def test_plan_only_returns_the_plan_and_runs_nothing(graph):
    out = await graph_ask.ask(OWNER, "q?", llm=Script(TWO_HOP), plan_only=True)
    assert out["plan"][1]["s"] == "o1" and out["steps"] == [] and out["model_calls"] == 1


async def test_no_model_empty_graph_and_empty_question(graph, tmp_path, monkeypatch):
    monkeypatch.setattr(graph_ask, "default_llm", lambda owner, **kw: None)
    assert (await graph_ask.ask(OWNER, "q?"))["status"] == "no_model"
    assert (await graph_ask.ask(OWNER, "  ", llm=Script()))["status"] == "error"
    assert (await graph_ask.ask("nobody", "q?", llm=Script()))["status"] == "empty"


async def test_a_model_that_fails_does_not_crash_the_run(graph):
    async def broken(prompt, **kw):
        raise RuntimeError("down")
    out = await graph_ask.ask(OWNER, "Who created Project Alpha?", llm=broken)
    assert out["status"] == "error" and "model" in out["error"] and out["answer"] == ""


async def test_timeout_is_reported(graph):
    async def slow(prompt, **kw):
        await asyncio.sleep(5)
        return ""
    out = await graph_ask.ask(OWNER, "q?", llm=slow, timeout_s=0.1)
    assert out["status"] == "error" and "timed out" in out["error"]


async def test_other_owners_cannot_see_the_graph(graph):
    plan = """Step1: x
Action1: Retrieval(s=s1:project[`Secret Project`], p=p1:uses, o=o1)
Step2: y
Action2: Output(o1)"""
    out = await graph_ask.ask(OWNER, "Secret Project?", llm=Script(plan))
    assert out["found"] is False and "no entity called" in out["steps"][0]["error"]


def test_predicates_worded_differently_by_planner_and_extractor_still_match():
    assert graph_ask._predicate_score("member_of", "part_of") >= 0.6
    assert graph_ask._predicate_score("resides_in", "lives_in") >= 0.6
    assert graph_ask._predicate_score("founded", "created") >= 0.6
    assert graph_ask._predicate_score("studied_at", "works_at") < 0.6
    assert graph_ask._predicate_score("uses", "lives_in") < 0.6


async def test_a_literal_that_only_restates_a_found_entity_is_not_a_second_value(graph):
    entities.add_relation(OWNER, graph["Project Alpha"], "created_by", dst_value="by Mara", evidence=("mem:a1",), confidence=0.6)
    out = await graph_ask.ask(OWNER, "Who created Project Alpha?", llm=Script(TWO_HOP, rewrite="In which city does Mara live?"))
    assert out["steps"][0]["values"] == ["Mara"]
