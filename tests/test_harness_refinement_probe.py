"""Probed harness refinement: the validator, the read-only repeat, the aggregator's rules, the blind
selection, the automatic adoption with its exact undo, and the EXPERIENCE.md lessons.

No model and no GPU: the proposer, the aggregator and the judge are one fake coroutine that answers by
the schema it is asked for, and the agent loop is a fake generator that behaves differently when the
edit under test is in its configuration."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.database import ChatMessage as DbChatMessage
from core.database import Session as DbSession
from services.projects import ProjectStore
from src import agent_defs
from src.harness_refinement import compose, experience, probe, probe_store, proposer, rollout, runner, store, targets
from src.harness_refinement import validator
from src.harness_refinement.store import HarnessError
from src.memory import MemoryManager
from src.skills_runtime import sleep_optimize as so

NOW = datetime(2026, 10, 1, 12, 0, 0)
FAILED_TOOL = {"tool": "read_file", "command": "notes.md", "output": "Error: boom", "exit_code": 1}
CORRECTION = [("user", "write the release notes for v2", None),
              ("assistant", "Here are the release notes: long long long...", [FAILED_TOOL]),
              ("user", "that's wrong, I asked for three bullet points", None)]
NEW_TEXT = "Answer briefly. Use bullet points when asked."


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for mod in (store, targets, agent_defs):
        monkeypatch.setattr(mod, "DATA_DIR", str(data))
    monkeypatch.setattr(so, "DATA_DIR", str(data), raising=False)
    root = data / "skill_proposals"
    monkeypatch.setattr(so, "PROPOSALS_ROOT", str(root), raising=False)
    monkeypatch.setattr(so, "PROPOSALS_FILE", str(root / "proposals.json"), raising=False)
    monkeypatch.setattr(so, "SNAPSHOT_DIR", str(root / "snapshots"), raising=False)
    monkeypatch.setattr(so, "VERSIONS_FILE", str(root / "versions.json"), raising=False)
    projects = ProjectStore(str(data))
    monkeypatch.setattr(targets, "_project_store", lambda: projects)
    monkeypatch.setattr(targets, "_memory_manager", lambda: MemoryManager(str(data)))
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.database.SessionLocal", factory, raising=False)
    project = projects.create("Demo", instructions="Answer briefly.", scaffold_memory=False)
    monkeypatch.setattr("services.projects.project_for_session", lambda sid, owner=None: project)
    monkeypatch.setattr(probe, "_setting", lambda key, default: default)
    proposer._INFLIGHT.clear()
    probe_store._ACTIVE.clear()
    return type("Env", (), {"data": data, "projects": projects, "db": factory, "project": project})


def _seed(env, sid, turns):
    db = env.db()
    db.add(DbSession(id=sid, name=sid, endpoint_url="http://x", model="m", owner="", message_count=0))
    for i, (role, text, events) in enumerate(turns):
        db.add(DbChatMessage(id=f"{sid}-m{i}", session_id=sid, role=role, content=text,
                             timestamp=NOW + timedelta(seconds=i * 10),
                             meta_data=json.dumps({"tool_events": events}) if events else None))
    db.commit()
    db.close()


def _instructions(env):
    return env.projects.get(env.project["id"])["instructions"]


def sse(**event):
    return "data: " + json.dumps(event) + "\n\n"


def make_loop(edited_marker=NEW_TEXT):
    """A fake agent loop: with the edit in its configuration it answers in bullets, without it in prose."""
    async def loop(url, model, messages, **kwargs):
        loop.calls.append({"messages": messages, **kwargs})
        text = messages[-1]["content"]
        if edited_marker in text:
            yield sse(type="tool_output", tool="read_file", command="notes.md", output="ok", exit_code=0)
            yield sse(delta="- fixed\n- three\n- bullets")
        else:
            yield sse(type="tool_output", tool="read_file", command="notes.md", output="Error: boom", exit_code=1)
            yield sse(delta="Long prose release notes that go on and on.")
    loop.calls = []
    return loop


class Fake:
    """The model of every role of the round, answering by the schema it is asked for."""

    def __init__(self, project_id, *, inv=None, dom=None, accept=True, evidence=None, keep_base=False,
                 pick="bullets", lesson=None):
        self.project_id = project_id
        self.inv = inv if inv is not None else {
            "propose": True, "reason": "Asked for bullets, got prose.", "axis": "prompt_layer",
            "target": f"project:{project_id}", "op": "update", "after": NEW_TEXT, "evidence_ids": []}
        self.dom = dom if dom is not None else {"propose": False, "reason": "nothing about the domain"}
        self.accept, self.keep_base, self.pick = accept, keep_base, pick
        self.evidence = evidence if evidence is not None else [
            {"run": "o", "step": 2, "quote": "read_file failed"}, {"run": "b1", "step": 1, "quote": "Error: boom"}]
        self.lesson = lesson if lesson is not None else {
            "no_entry": False, "edits": [{"candidate": "c1", "observed": "bullets appeared", "verdict": "helped"}],
            "cue": "the person asks for a fixed shape of answer", "lesson": "put the shape in the project instructions",
            "evidence": "c1 vs b1/b2", "confidence": "medium"}
        self.prompts = []

    async def __call__(self, url, model, prompt, schema):
        self.prompts.append((schema, prompt))
        if schema is proposer.RESPONSE_SCHEMA:
            return json.dumps(self.inv if "how the agent WORKS" in prompt else self.dom)
        if schema is compose.COMPOSE_SCHEMA:
            return json.dumps({"keep_base": self.keep_base, "notes": "",
                               "decisions": [{"candidate": "c1", "accept": self.accept, "reason": "bullets",
                                              "evidence": self.evidence},
                                             {"candidate": "c2", "accept": False, "reason": "no"}]})
        if schema is compose.SELECT_SCHEMA:
            labels = [(line[len("### Candidate "):].strip(), body) for line, body in _candidates_of(prompt)]
            want = [lab for lab, body in labels if (self.pick in body)]
            return json.dumps({"why": "closest to the request", "choice": (want or ["A"])[0]})
        if schema is compose.EXPERIENCE_SCHEMA:
            return json.dumps(self.lesson)
        raise AssertionError("unexpected schema")


def _candidates_of(prompt):
    out = []
    body = prompt.split("# CANDIDATES\n", 1)[1].split("\n\n# ANSWER", 1)[0]
    for block in body.split("### ")[1:]:
        head, _, text = block.partition("\n")
        out.append(("### " + head, text))
    return out


async def run_probe(env, fake, loop=None, sid="s1", **kw):
    kw.setdefault("route", ("http://stub/v1", "stub", None))
    kw.setdefault("k", 2)
    kw.setdefault("auto_apply", True)
    return await probe.probe_for_session(sid, llm=fake, loop=loop or make_loop(), **kw)


# -- the harness validator ----------------------------------------------------------------------

def test_validator_flags_what_an_edit_introduces_and_only_that(env, monkeypatch):
    class Skills:
        def read_skill_md(self, name, owner=None):
            return "# skill" if name == "deploy-notes" else None
    monkeypatch.setattr(targets, "_skills_manager", lambda: Skills())
    target = f"project:{env.project['id']}"
    ok = validator.check_edit("prompt_layer", target, "update", "Answer briefly.",
                              "Answer briefly. When writing release notes, load the `deploy-notes` skill.")
    assert ok == []
    bad = validator.check_edit("prompt_layer", target, "update", "Answer briefly.",
                               "Answer briefly. Load the `ghost-skill` skill. See {{ name }} and run python scripts/check.py")
    assert any("ghost-skill" in p for p in bad)
    assert any("template" in p for p in bad)
    assert any("scripts/check.py" in p for p in bad)
    # a reference that was already there is not this edit's fault
    already = "Load the `ghost-skill` skill."
    assert validator.check_edit("prompt_layer", target, "update", already, already + " Be brief.") == []
    assert validator.check_edit("memory", "memory:abcdefgh", "delete", "x", None) == []


def test_validator_accepts_a_script_that_exists_in_the_project_folder(env, tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "scripts" / "check.py").write_text("print(1)")
    project = env.projects.create("WS", instructions="x", workspace=str(workspace), scaffold_memory=False)
    target = f"project:{project['id']}"
    assert validator.check_edit("prompt_layer", target, "update", "x", "x. Run python scripts/check.py first.") == []
    problems = validator.check_edit("prompt_layer", target, "update", "x", "x. Run python scripts/other.py first.")
    assert problems and "scripts/other.py" in problems[0]


@pytest.mark.asyncio
async def test_an_incoherent_edit_is_refused_before_it_is_stored(env):
    _seed(env, "s1", CORRECTION)
    bad = {"propose": True, "reason": "r", "axis": "prompt_layer", "target": f"project:{env.project['id']}",
           "op": "update", "after": "Answer briefly. Fill {{ the_name }}."}

    async def llm(url, model, prompt, user_initiated):
        return json.dumps(bad)

    result = await proposer.propose_for_session("s1", llm=llm)
    assert result["status"] == "error" and result["reason"] == "harness.incoherent"
    assert store.list_proposals() == []


# -- one repeat -------------------------------------------------------------------------------------------

def test_the_overlay_differs_between_arms_only_by_the_edited_component(env):
    comps = [rollout.Component("prompt_layer", "project:p1", "instructions", "Answer briefly."),
             rollout.Component("memory", "memory:abcdefgh", "memory", "The repo uses tabs.")]
    edit = {"axis": "prompt_layer", "target": "project:p1", "op": "update", "after": NEW_TEXT}
    base = rollout.render_overlay(comps)
    variant = rollout.render_overlay(rollout.with_edit(comps, edit))
    assert "The repo uses tabs." in base and "The repo uses tabs." in variant
    assert NEW_TEXT in variant and NEW_TEXT not in base
    assert base.replace("Answer briefly.", NEW_TEXT) == variant
    added = rollout.with_edit(comps, {"axis": "memory", "target": "memory:newnewnew", "op": "create", "after": "New fact."})
    assert [c.content for c in added][-1] == "New fact." and len(added) == 3
    removed = rollout.with_edit(comps, {"axis": "memory", "target": "memory:abcdefgh", "op": "delete", "after": None})
    assert len(removed) == 1


@pytest.mark.asyncio
async def test_a_repeat_runs_read_only_and_records_steps_and_the_answer():
    loop = make_loop()
    msgs = rollout.build_messages("write the notes", [rollout.Component("prompt_layer", "project:p", "x", "Be brief.")])
    run = await rollout.run_rollout("b1", "base", msgs, url="u", model="m", loop=loop)
    assert run["delivered"] and run["text"].startswith("Long prose")
    assert run["steps"][0]["tool"] == "read_file" and run["steps"][0]["ok"] is False
    call = loop.calls[0]
    assert call["autonomy_preset"] == "read_only" and call["unattended"] is True and call["workload"] == "background"
    assert call["harness_options"]["checkpoints"] is False


@pytest.mark.asyncio
async def test_a_repeat_that_fails_or_times_out_is_a_record_not_an_exception():
    async def boom(url, model, messages, **kw):
        raise RuntimeError("model went away")
        yield ""

    async def slow(url, model, messages, **kw):
        import asyncio
        await asyncio.sleep(5)
        yield sse(delta="late")

    failed = await rollout.run_rollout("b1", "base", [{"role": "user", "content": "x"}], url="u", model="m", loop=boom)
    assert failed["delivered"] is False and "model went away" in failed["error"]
    timed = await rollout.run_rollout("b2", "base", [{"role": "user", "content": "x"}], url="u", model="m",
                                      loop=slow, timeout_s=0.05)
    assert timed["delivered"] is False and timed["error"] == "timeout"


# -- the aggregator's rules ----------------------------------------------------------------------------------

def _runs():
    steps = [{"n": 1, "kind": "tool", "tool": "t", "ok": False}, {"n": 2, "kind": "tool", "tool": "t", "ok": True}]
    return {"o": {"id": "o", "steps": steps, "delivered": True}, "b1": {"id": "b1", "steps": steps, "delivered": True},
            "b2": {"id": "b2", "steps": steps, "delivered": True},
            "c1": {"id": "c1", "steps": steps, "delivered": True}}


def _enforce(parsed, runs=None, cand=None):
    runs = runs or _runs()
    return compose.enforce(parsed, ["o", "b1", "b2"], runs, {"c1": cand or runs["c1"]}, ["c1"])[0]


def _decision(evidence, accept=True):
    return {"decisions": [{"candidate": "c1", "accept": accept, "reason": "r", "evidence": evidence}]}


def test_an_edit_needs_a_cited_moment_repeated_in_two_observed_runs():
    two = [{"run": "o", "step": 1, "quote": "failed"}, {"run": "b1", "step": 1, "quote": "failed"}]
    assert _enforce(_decision(two))["accepted"] is True
    one_run = [{"run": "b1", "step": 1, "quote": "failed"}, {"run": "b1", "step": 2, "quote": "again"}]
    assert _enforce(_decision(one_run))["rejected_because"] == "failure_not_repeated"      # 1 of k is noise
    invented = [{"run": "b1", "step": 9, "quote": "?"}, {"run": "b2", "step": 0, "quote": "?"}]
    assert _enforce(_decision(invented))["rejected_because"] == "no_valid_citation"
    assert _enforce(_decision([]))["rejected_because"] == "no_valid_citation"
    only_candidate = [{"run": "c1", "step": 1, "quote": "better"}]
    assert _enforce(_decision(only_candidate))["accepted"] is False


def test_keep_base_an_empty_repeat_and_garbage_are_all_valid_outcomes():
    two = [{"run": "o", "step": 1}, {"run": "b2", "step": 2}]
    assert _enforce({"keep_base": True, **_decision(two)})["rejected_because"] == "keep_base"
    runs = _runs()
    empty = dict(runs["c1"], delivered=False)
    assert _enforce(_decision(two), runs, empty)["rejected_because"] == "candidate_run_delivered_nothing"
    assert _enforce(None)["rejected_because"] == "compose_unparseable"
    assert _enforce({"decisions": []})["rejected_because"] == "no_decision"
    assert _enforce(_decision(two, accept=False))["rejected_because"] == "not_accepted"


def test_the_shuffle_hides_the_arm_and_is_reproducible_from_its_seed():
    ids = ["b1", "b2", "c1", "c2"]
    a = compose.shuffle_pool(ids, 7)
    assert a == compose.shuffle_pool(ids, 7)
    assert sorted(label for label, _ in a) == ["A", "B", "C", "D"] and sorted(rid for _, rid in a) == sorted(ids)
    orders = {tuple(rid for _, rid in compose.shuffle_pool(ids, s)) for s in range(30)}
    assert len(orders) > 5
    assert compose.parse_choice({"choice": "Candidate b"}, a) == dict(a)["B"]
    assert compose.parse_choice({"choice": "Z"}, a) is None and compose.parse_choice(None, a) is None


def test_a_verdict_of_helped_needs_an_accepted_edit_that_won():
    rows = [{"id": "c1", "accepted": True, "won": True}, {"id": "c2", "accepted": False, "won": False}]
    verdicts = compose.read_verdicts({"edits": [{"candidate": "c1", "verdict": "Helped"},
                                                {"candidate": "c2", "verdict": "helped"},
                                                {"candidate": "c9", "verdict": "helped"}]}, rows)
    assert verdicts == {"c1": "helped", "c2": "confounded"}


# -- the whole round ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_winning_edit_is_applied_by_itself_and_undone_by_id(env):
    _seed(env, "s1", CORRECTION)
    fake = Fake(env.project["id"])
    result = await run_probe(env, fake)
    assert result["status"] == "adopted" and result["outcome"]["mode"] == "auto"
    assert _instructions(env) == NEW_TEXT                                  # no approval was asked
    proposal = store.get(result["outcome"]["proposal_id"])
    assert proposal["status"] == "applied" and proposal["applied_by"] == "harness-probe"
    assert proposal["probe_result"] == "adopted" and proposal["probe_line"] == "inv"
    assert {c["run"] for c in proposal["probe_evidence"]} == {"o", "b1"}

    probe_record = probe_store.get(result["probe_id"])
    assert probe_record["status"] == "done"
    assert [r["id"] for r in probe_record["rollouts"]] == ["b1", "b2", "c1"]   # k=2 base repeats, one per candidate
    assert probe_record["selection"]["winner"] == "c1" and probe_record["selection"]["seed"]
    undone = store.undo(proposal["id"], by="test")
    assert undone["restore_exact"] is True and _instructions(env) == "Answer briefly."


@pytest.mark.asyncio
async def test_the_probe_repeats_the_task_without_leaking_the_correction_or_the_edit_into_the_base(env):
    _seed(env, "s1", CORRECTION)
    loop = make_loop()
    await run_probe(env, Fake(env.project["id"]), loop)
    sent = [c["messages"][-1]["content"] for c in loop.calls]
    assert len(sent) == 3
    assert all("write the release notes for v2" in text for text in sent)
    assert all("three bullet points" not in text for text in sent)            # the correction is not in the repeat
    assert [NEW_TEXT in text for text in sent] == [False, False, True]


@pytest.mark.asyncio
async def test_a_losing_candidate_is_discarded_with_its_evidence_and_the_two_lines_do_not_collide(env):
    _seed(env, "s1", CORRECTION)
    dom = {"propose": True, "reason": "A practitioner knows release notes are short.", "axis": "memory",
           "target": "memory:new", "op": "create", "after": "Release notes are at most three bullets."}
    fake = Fake(env.project["id"], dom=dom)
    result = await run_probe(env, fake)
    assert result["status"] == "adopted"
    by_line = {p["probe_line"]: p for p in store.list_proposals(limit=10)}
    assert by_line["inv"]["status"] == "applied"
    assert by_line["dom"]["status"] == "rejected" and by_line["dom"]["rejected_by"] == "harness-probe"
    assert by_line["dom"]["probe_result"] == "discarded" and by_line["dom"]["probe_reason"]
    dom_prompt = next(p for s, p in fake.prompts if s is proposer.RESPONSE_SCHEMA and "PRACTITIONER" in p)
    assert f"target: project:{env.project['id']}" not in dom_prompt             # INV already took that target


@pytest.mark.asyncio
async def test_keep_base_discards_everything_and_touches_nothing(env):
    _seed(env, "s1", CORRECTION)
    result = await run_probe(env, Fake(env.project["id"], keep_base=True))
    assert result["status"] == "kept_base" and _instructions(env) == "Answer briefly."
    only = store.list_proposals(limit=10)
    assert [p["status"] for p in only] == ["rejected"] and only[0]["probe_result"] == "discarded"


@pytest.mark.asyncio
async def test_the_base_can_win_the_blind_selection(env):
    _seed(env, "s1", CORRECTION)
    result = await run_probe(env, Fake(env.project["id"], pick="prose"))       # the judge prefers a base answer
    assert result["status"] == "kept_base" and _instructions(env) == "Answer briefly."
    assert probe_store.get(result["probe_id"])["selection"]["winner"] == "base"


@pytest.mark.asyncio
async def test_a_noisy_failure_is_not_enough(env):
    _seed(env, "s1", CORRECTION)
    one_run = [{"run": "b1", "step": 1, "quote": "x"}, {"run": "b1", "step": 1, "quote": "y"}]
    result = await run_probe(env, Fake(env.project["id"], evidence=one_run))
    assert result["status"] == "kept_base" and _instructions(env) == "Answer briefly."
    assert store.list_proposals()[0]["probe_reason"].startswith("the failure it cites is not repeated")


@pytest.mark.asyncio
async def test_with_auto_apply_off_the_winner_waits_for_a_person(env):
    _seed(env, "s1", CORRECTION)
    result = await run_probe(env, Fake(env.project["id"]), auto_apply=False)
    assert result["status"] == "adopted" and result["outcome"]["mode"] == "pending" and result["outcome"]["applied"] is False
    assert _instructions(env) == "Answer briefly."
    pending = store.list_proposals(status="pending")
    assert len(pending) == 1 and pending[0]["probe_result"] == "adopted"
    store.approve(pending[0]["id"], by="person")
    assert _instructions(env) == NEW_TEXT


@pytest.mark.asyncio
async def test_a_candidate_that_breaks_the_harness_never_reaches_the_repeat(env):
    _seed(env, "s1", CORRECTION)
    inv = {"propose": True, "reason": "r", "axis": "prompt_layer", "target": f"project:{env.project['id']}",
           "op": "update", "after": "Answer briefly. Load the `ghost` skill."}
    loop = make_loop()
    result = await run_probe(env, Fake(env.project["id"], inv=inv), loop)
    assert result["status"] == "none" and result["reason"] == "no_valid_candidate"
    assert loop.calls == [] and store.list_proposals() == []
    record = probe_store.get(result["probe_id"])
    assert record["candidates"][0]["result"] == "invalid" and record["candidates"][0]["reason"] == "harness.incoherent"


@pytest.mark.asyncio
async def test_a_round_that_loses_admission_is_dropped_whole(env):
    _seed(env, "s1", CORRECTION)
    state = {"calls": 0}

    def admission(url, model):
        state["calls"] += 1
        return "foreground_activity" if state["calls"] > 3 else None

    async def no_sleep(_):
        return None

    result = await run_probe(env, Fake(env.project["id"]), admission=admission, sleep=no_sleep, wait_s=10, poll_s=5)
    assert result["status"] == "skipped" and result["reason"] == "foreground_activity" and result["blocked"]
    assert store.list_proposals() == [] and _instructions(env) == "Answer briefly."
    assert probe_store.get(result["probe_id"])["status"] == "aborted"


@pytest.mark.asyncio
async def test_a_model_that_is_down_aborts_instead_of_adopting_on_noise(env):
    _seed(env, "s1", CORRECTION)

    async def dead(url, model, messages, **kw):
        raise ConnectionError("no route to the Sparks")
        yield ""

    result = await run_probe(env, Fake(env.project["id"]), dead)
    assert result["status"] == "error" and result["reason"] == "model_unavailable"
    assert store.list_proposals() == [] and _instructions(env) == "Answer briefly."


@pytest.mark.asyncio
async def test_a_task_that_went_fine_costs_nothing(env):
    _seed(env, "s1", [("user", "what is the capital of France?", None), ("assistant", "Paris.", None),
                      ("user", "perfecto, gracias", None)])
    fake = Fake(env.project["id"])
    result = await run_probe(env, fake)
    assert result["status"] == "skipped" and result["reason"] == "no_signal" and fake.prompts == []


# -- EXPERIENCE.md --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lesson_is_written_and_read_by_the_next_proposer(env):
    _seed(env, "s1", CORRECTION)
    result = await run_probe(env, Fake(env.project["id"]))
    assert result["status"] == "adopted"
    domain = experience.domain_for(env.project["id"], "Demo")
    entries = experience.entries(domain)
    assert len(entries) == 1 and entries[0]["confidence"] == "medium" and entries[0]["verdict"].startswith("helped")
    assert probe_store.get(result["probe_id"])["experience"]["status"] == "written"

    _seed(env, "s2", CORRECTION)
    fake2 = Fake(env.project["id"], keep_base=True)
    await run_probe(env, fake2, sid="s2")
    first_prompt = next(p for s, p in fake2.prompts if s is proposer.RESPONSE_SCHEMA)
    assert "LESSONS FROM EARLIER TASKS" in first_prompt and "put the shape in the project instructions" in first_prompt


@pytest.mark.asyncio
async def test_no_entry_and_confounded_lessons_write_nothing(env):
    _seed(env, "s1", CORRECTION)
    no_entry = Fake(env.project["id"], lesson={"no_entry": True})
    result = await run_probe(env, no_entry, auto_apply=False)
    assert probe_store.get(result["probe_id"])["experience"]["status"] == "no_entry"
    assert experience.entries(experience.domain_for(env.project["id"], "Demo")) == []

    env2_lesson = {"no_entry": False, "edits": [{"candidate": "c1", "verdict": "confounded"}], "cue": "c",
                   "lesson": "l", "evidence": "e", "confidence": "high"}
    _seed(env, "s2", CORRECTION)
    result = await run_probe(env, Fake(env.project["id"], lesson=env2_lesson), sid="s2", auto_apply=False)
    assert probe_store.get(result["probe_id"])["experience"]["reason"] == "only_confounded"
    assert experience.entries(experience.domain_for(env.project["id"], "Demo")) == []


def test_a_project_with_a_folder_keeps_its_lessons_in_its_own_memory(env, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = env.projects.create("WS", instructions="x", workspace=str(workspace), scaffold_memory=False)
    domain = experience.domain_for(project["id"], "WS")
    entry = {"cue": "a shape of situation", "lesson": "edit the skill, not the prompt", "evidence": "c1",
             "confidence": "high", "verdict": "helped"}
    written = experience.append(domain, entry, label="skill:x")
    assert written["written"] and written["path"].replace("\\", "/").endswith(".faustus/EXPERIENCE.md")
    assert experience.append(domain, entry)["reason"] == "duplicate"
    assert experience.entries(domain)[0]["verdict"] == "helped (skill:x)"
    assert "[Experience](EXPERIENCE.md)" in env.projects.read_index(project)       # the project's index lists it
    assert {d["key"] for d in experience.list_domains()} == {project["id"]}


def test_a_lesson_with_an_injection_or_without_substance_is_refused(env):
    assert experience.check_lesson({"cue": "c", "lesson": "", "evidence": "e"}, ["helped"])["skipped"] == "incomplete_entry"
    assert experience.check_lesson(None, ["helped"])["skipped"] == "no_entry"
    assert experience.check_lesson({"cue": "c", "lesson": "l", "evidence": "e"}, [])["skipped"] == "only_confounded"
    ok = experience.check_lesson({"cue": "c", "lesson": "l", "evidence": "e", "confidence": "HIGH, because"}, ["hurt"])
    assert ok["entry"]["confidence"] == "high" and ok["entry"]["verdict"] == "hurt"


# -- the background runner takes the probe path ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_runner_probes_by_default_and_proposes_only_when_the_probe_is_off(env, monkeypatch):
    _seed(env, "s1", CORRECTION)
    runner.reset_for_tests()
    monkeypatch.setattr(probe, "resolve_route", lambda owner="": ("http://stub/v1", "stub", None))
    fake = Fake(env.project["id"])
    result = await runner.run_job({"session_id": "s1", "owner": ""}, settle_s=0, admission=lambda u, m: None,
                                  probe_llm=fake, rollout_loop=make_loop())
    assert result["status"] == "adopted" and _instructions(env) == NEW_TEXT

    monkeypatch.setattr(probe, "probe_enabled", lambda: False)
    _seed(env, "s2", CORRECTION)

    async def old_llm(url, model, prompt, user_initiated):
        return json.dumps({"propose": True, "reason": "r", "axis": "prompt_layer",
                           "target": f"project:{env.project['id']}", "op": "update", "after": NEW_TEXT + " Be kind."})

    result = await runner.run_job({"session_id": "s2", "owner": ""}, settle_s=0, admission=lambda u, m: None, llm=old_llm)
    assert result["status"] == "proposed" and store.get(result["proposal"]["id"])["status"] == "pending"


# -- the REST surface ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.harness_proposals_routes import setup_harness_proposals_routes

    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    monkeypatch.setattr(probe, "resolve_route", lambda owner="": ("http://stub/v1", "stub", None))
    monkeypatch.setattr(probe, "_make_llm", lambda headers: Fake(env.project["id"]))
    monkeypatch.setattr(rollout, "_default_loop", lambda: make_loop())
    app = FastAPI()

    @app.middleware("http")
    async def _user(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(setup_harness_proposals_routes())
    with TestClient(app) as client:
        yield client


def _wait_done(client, probe_id, seconds=10.0):
    import time
    end = time.time() + seconds
    while time.time() < end:
        record = client.get(f"/api/harness/proposals/probes/{probe_id}").json()["probe"]
        if record["status"] != "running":
            return record
        time.sleep(0.1)
    raise AssertionError("the probe did not finish")


def test_a_round_started_from_the_api_runs_in_the_background_and_can_be_read_back(env, client):
    _seed(env, "s1", CORRECTION)
    started = client.post("/api/harness/proposals/probes", json={"session_id": "s1"})
    assert started.status_code == 200
    probe_id = started.json()["probe_id"]
    record = _wait_done(client, probe_id)
    assert record["status"] == "done" and record["outcome"]["winner"] == "c1"
    assert _instructions(env) == NEW_TEXT

    listed = client.get("/api/harness/proposals/probes").json()
    assert listed["probes"][0]["id"] == probe_id and listed["probes"][0]["winner"] == "c1"
    assert listed["probe"] is True and listed["auto_apply"] is True and listed["k"] == 2
    one = client.get(f"/api/harness/proposals/probes/{probe_id}").json()
    assert one["proposals"][0]["status"] == "applied" and one["proposals"][0]["probe_line"] == "inv"
    assert "before" not in one["proposals"][0]
    status = client.get("/api/harness/proposals/status").json()
    assert status["probe"] is True and status["auto_apply"] is True
    assert client.get("/api/harness/proposals/probes/nope").status_code == 404


def test_the_api_answers_a_round_that_cannot_start_at_once_and_lists_the_lessons(env, client):
    _seed(env, "s1", [("user", "what is the capital of France?", None), ("assistant", "Paris.", None),
                      ("user", "perfecto, gracias", None)])
    refused = client.post("/api/harness/proposals/probes", json={"session_id": "s1"})
    assert refused.status_code == 409 and refused.json()["detail"]["error_class"] == "harness.no_signal"
    assert client.post("/api/harness/proposals/probes", json={"session_id": "ghost"}).status_code == 404

    _seed(env, "s2", CORRECTION)
    probe_id = client.post("/api/harness/proposals/probes", json={"session_id": "s2"}).json()["probe_id"]
    _wait_done(client, probe_id)
    domains = client.get("/api/harness/proposals/lessons").json()["domains"]
    assert domains[0]["key"] == env.project["id"] and domains[0]["entries"] == 1
    entries = client.get("/api/harness/proposals/lessons", params={"project_id": env.project["id"]}).json()
    assert entries["entries"][0]["lesson"] == "put the shape in the project instructions"
    assert client.get("/api/harness/proposals/lessons", params={"project_id": "ghost"}).status_code == 404


# -- the repeats see the project's files ---------------------------------------------------------------------------------

async def test_the_repeat_gets_the_project_workspace_and_without_one_none(env, tmp_path):
    loop = make_loop()
    messages = [{"role": "user", "content": "x"}]
    await rollout.run_rollout("b1", "base", messages, url="u", model="m", loop=loop, workspace=str(tmp_path))
    await rollout.run_rollout("b2", "base", messages, url="u", model="m", loop=loop)
    assert loop.calls[0]["workspace"] == str(tmp_path)
    assert "workspace" not in loop.calls[1]
    assert loop.calls[0]["autonomy_preset"] == "read_only"      # it can read the folder, never write to it


def test_workspace_of_returns_an_existing_project_folder_only(env, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with_ws = env.projects.create("WS2", instructions="x", workspace=str(folder), scaffold_memory=False)
    assert probe.workspace_of(with_ws["id"], "") == str(folder)
    assert probe.workspace_of("nope", "") == "" and probe.workspace_of("", "") == ""
    assert probe.workspace_of(env.project["id"], "") in ("", env.project.get("workspace") or "")


@pytest.mark.asyncio
async def test_a_lesson_after_a_win_rests_on_the_winners_verdict_never_a_losers():
    def row(cid, line, won):
        return {"id": cid, "line": line, "axis": "memory", "target": f"memory:{cid}", "accepted": won, "delivered": won,
                "won": won, "rationale": "the command goes in memory", "rejected_because": None if won else "not_accepted"}

    rows = [row("c1", "inv", False), row("c2", "dom", True)]
    edits = {r["id"]: {"axis": "memory", "target": r["target"]} for r in rows}
    runs = {"b1": {"delivered": True}, "c1": {"delivered": False}, "c2": {"delivered": True}}

    async def gate():
        return None

    def llm_with(winner_verdict):
        async def llm(url, model, prompt, schema):
            return json.dumps({"no_entry": False, "cue": "the person states a fixed command", "lesson": "put it in memory",
                               "evidence": "c2 vs b1", "confidence": "low",
                               "edits": [{"candidate": "c1", "observed": "no answer", "verdict": "no observed effect"},
                                         {"candidate": "c2", "observed": "right command", "verdict": winner_verdict}]})
        return llm

    async def lesson(winner_verdict):
        return await probe._lesson("p1", {"text": "task"}, rows, edits, ["b1"], runs, [], "c2", {"why": "w"}, "u", "m",
                                   llm_with(winner_verdict), gate)

    # the loser's "no observed effect" must not be written as the lesson about the winner
    info = await lesson("confounded")
    assert info["status"] == "no_entry" and info["reason"] == "only_confounded"
    info = await lesson("helped")
    assert info["status"] == "lesson" and info["entry"]["verdict"] == "helped" and info["about"] == "c2"
