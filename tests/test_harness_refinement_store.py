"""Harness proposals: the store, the status machine and undo, on the four real axes.

The rules under test:

* creating a proposal never touches its target;
* approving is the only writer, is idempotent, and refuses when the target
  changed after the proposal was made;
* undo restores the exact previous content, refuses (and touches nothing) when
  the target was edited after the apply, and an undo cannot be undone;
* every decision leaves a ``trigger -> result`` line in the log.
"""
from __future__ import annotations

import json

import pytest

from services.memory.skill_format import Skill
from services.projects import ProjectStore
from src import agent_defs
from src.harness_refinement import store, targets
from src.harness_refinement.store import HarnessError
from src.memory import MemoryManager
from src.skills_runtime import sleep_optimize as so


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(store, "DATA_DIR", str(data))
    monkeypatch.setattr(targets, "DATA_DIR", str(data))
    monkeypatch.setattr(agent_defs, "DATA_DIR", str(data))
    monkeypatch.setattr(so, "DATA_DIR", str(data), raising=False)
    root = data / "skill_proposals"
    monkeypatch.setattr(so, "PROPOSALS_ROOT", str(root), raising=False)
    monkeypatch.setattr(so, "PROPOSALS_FILE", str(root / "proposals.json"), raising=False)
    monkeypatch.setattr(so, "SNAPSHOT_DIR", str(root / "snapshots"), raising=False)
    monkeypatch.setattr(so, "VERSIONS_FILE", str(root / "versions.json"), raising=False)
    projects = ProjectStore(str(data))
    memories = MemoryManager(str(data))
    monkeypatch.setattr(targets, "_project_store", lambda: projects)
    monkeypatch.setattr(targets, "_memory_manager", lambda: memories)
    return type("Env", (), {"data": data, "projects": projects, "memories": memories})


def _project(env, instructions="Answer briefly."):
    return env.projects.create("Demo", instructions=instructions, scaffold_memory=False)


def _make(axis, target, op, after, *, owner="", delegate=None, trigger="turn_end:correction", session_id="s1"):
    before = targets.read_current(axis, target, owner)
    return store.create_proposal(
        axis=axis, target=target, op=op, before=before, after=after, rationale="because",
        evidence=[{"id": "e1", "kind": "correction", "quote": "no, shorter"}], trigger=trigger,
        owner=owner, session_id=session_id, model="m", meta={"delegate": delegate} if delegate else {})


# -- creation is inert -------------------------------------------------------

def test_creating_a_proposal_never_touches_the_target(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Answer briefly. Use metric units.")
    assert rec["status"] == "pending"
    assert env.projects.get(proj["id"])["instructions"] == "Answer briefly."
    assert rec["before"] == "Answer briefly."
    assert "+Use metric units" in rec["diff"] or "metric" in rec["diff"]
    assert rec["applied_at"] is None and rec["undo_of"] is None


def test_record_shape(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "x")
    for key in ("id", "axis", "target", "op", "before", "after", "diff", "rationale", "evidence", "trigger",
                "created_at", "status", "applied_at", "undo_of"):
        assert key in rec
    assert rec["evidence"][0]["quote"] == "no, shorter"


def test_unknown_axis_and_op_are_refused(env):
    with pytest.raises(HarnessError) as e:
        store.create_proposal(axis="base_prompt", target="x", op="update", before="", after="y",
                              rationale="", evidence=[], trigger="t")
    assert e.value.error_class == "harness.bad_axis"
    with pytest.raises(HarnessError) as e:
        store.create_proposal(axis="memory", target="memory:abcdefgh", op="rewrite", before="", after="y",
                              rationale="", evidence=[], trigger="t")
    assert e.value.error_class == "harness.bad_op"


# -- approve ---------------------------------------------------------------

def test_approve_applies_and_is_idempotent(env, monkeypatch):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Answer briefly. Use metric units.")
    writes = []
    real_update = env.projects.update
    monkeypatch.setattr(env.projects, "update", lambda *a, **k: writes.append(a) or real_update(*a, **k))
    first = store.approve(rec["id"], by="tester")
    again = store.approve(rec["id"], by="tester")
    assert first["status"] == "applied" and first["applied_by"] == "tester"
    assert again.get("already_applied") is True
    assert len(writes) == 1
    assert env.projects.get(proj["id"])["instructions"] == "Answer briefly. Use metric units."
    assert first["applied_content"] == "Answer briefly. Use metric units."


def test_approve_refuses_when_target_changed_after_the_proposal(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Answer briefly. Use metric units.")
    env.projects.update(proj["id"], {"instructions": "Somebody edited this."})
    with pytest.raises(HarnessError) as e:
        store.approve(rec["id"])
    assert e.value.error_class == "harness.target_changed"
    assert e.value.status_code == 409
    assert env.projects.get(proj["id"])["instructions"] == "Somebody edited this."
    assert store.get(rec["id"])["status"] == "pending"


def test_approve_unknown_and_wrong_state(env):
    with pytest.raises(HarnessError) as e:
        store.approve("nope")
    assert e.value.error_class == "harness.not_found" and e.value.status_code == 404
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Other text.")
    store.reject(rec["id"], reason="no")
    with pytest.raises(HarnessError) as e:
        store.approve(rec["id"])
    assert e.value.error_class == "harness.not_pending"


# -- reject ----------------------------------------------------------------

def test_reject_leaves_the_target_alone_and_is_idempotent(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Other text.")
    out = store.reject(rec["id"], by="tester", reason="not now")
    assert out["status"] == "rejected" and out["reject_reason"] == "not now"
    assert env.projects.get(proj["id"])["instructions"] == "Answer briefly."
    assert store.reject(rec["id"]).get("already_rejected") is True


# -- undo ------------------------------------------------------------------

def test_undo_restores_the_exact_previous_content(env):
    proj = _project(env, instructions="Line one.\n\nLine two with  odd   spacing.")
    original = env.projects.get(proj["id"])["instructions"]
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Line one.\n\nLine two, fixed.")
    store.approve(rec["id"])
    assert env.projects.get(proj["id"])["instructions"] == "Line one.\n\nLine two, fixed."
    undo = store.undo(rec["id"], by="tester")
    assert env.projects.get(proj["id"])["instructions"] == original
    assert undo["undo_of"] == rec["id"] and undo["status"] == "applied" and undo["restore_exact"] is True
    assert undo["op"] == "update" and undo["before"] == "Line one.\n\nLine two, fixed."
    done = store.get(rec["id"])
    assert done["status"] == "undone" and done["undo_id"] == undo["id"]


def test_undo_refuses_when_the_target_changed_after_the_apply(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Answer briefly. Use metric units.")
    store.approve(rec["id"])
    env.projects.update(proj["id"], {"instructions": "A later hand edit."})
    with pytest.raises(HarnessError) as e:
        store.undo(rec["id"])
    assert e.value.error_class == "harness.target_changed_since_apply"
    assert "edited after" in e.value.message
    assert env.projects.get(proj["id"])["instructions"] == "A later hand edit."
    assert store.get(rec["id"])["status"] == "applied"


def test_undo_state_machine(env):
    proj = _project(env)
    pending = _make("prompt_layer", f"project:{proj['id']}", "update", "Other text.")
    with pytest.raises(HarnessError) as e:
        store.undo(pending["id"])
    assert e.value.error_class == "harness.not_applied"
    store.approve(pending["id"])
    undo = store.undo(pending["id"])
    with pytest.raises(HarnessError) as e:
        store.undo(pending["id"])                      # already undone
    assert e.value.error_class == "harness.not_applied"
    with pytest.raises(HarnessError) as e:
        store.undo(undo["id"])                         # an undo is not undone
    assert e.value.error_class == "harness.undo_of_undo"
    with pytest.raises(HarnessError) as e:
        store.approve(pending["id"])                   # an undone proposal is not re-applied
    assert e.value.error_class == "harness.not_pending"


def test_create_op_on_empty_instructions_undoes_to_empty(env):
    proj = env.projects.create("Empty", instructions="", scaffold_memory=False)
    rec = _make("prompt_layer", f"project:{proj['id']}", "create", "Always cite sources.")
    assert rec["before"] is None
    store.approve(rec["id"])
    assert env.projects.get(proj["id"])["instructions"] == "Always cite sources."
    store.undo(rec["id"])
    assert env.projects.get(proj["id"])["instructions"] == ""


# -- memory ----------------------------------------------------------------

def test_memory_create_update_delete_and_exact_undo(env):
    first = env.memories.add_entry("User prefers metric units", owner=None)
    second = env.memories.add_entry("Project uses Python 3.12", owner=None)
    third = env.memories.add_entry("Never use tabs", owner=None)
    second["tags"] = ["stack"]
    env.memories.save([first, second, third])
    snapshot = json.dumps(env.memories.load_all(), sort_keys=True)

    # delete from the middle, then put it back where it was, with every field
    rec = _make("memory", f"memory:{second['id']}", "delete", None)
    store.approve(rec["id"])
    assert [e["id"] for e in env.memories.load_all()] == [first["id"], third["id"]]
    store.undo(rec["id"])
    assert json.dumps(env.memories.load_all(), sort_keys=True) == snapshot

    # update keeps the other fields, undo restores the entry exactly
    rec = _make("memory", f"memory:{second['id']}", "update", "Project uses Python 3.13")
    store.approve(rec["id"])
    assert next(e for e in env.memories.load_all() if e["id"] == second["id"])["text"] == "Project uses Python 3.13"
    store.undo(rec["id"])
    assert json.dumps(env.memories.load_all(), sort_keys=True) == snapshot

    # create, then undo removes it
    new_id = "newmemory0001"
    rec = _make("memory", f"memory:{new_id}", "create", "Prefers short answers")
    assert rec["before"] is None
    store.approve(rec["id"])
    created = next(e for e in env.memories.load_all() if e["id"] == new_id)
    assert created["text"] == "Prefers short answers" and created["source"] == "harness_refinement"
    store.undo(rec["id"])
    assert json.dumps(env.memories.load_all(), sort_keys=True) == snapshot


def test_memory_undo_refuses_after_a_later_edit(env):
    entry = env.memories.add_entry("Old fact", owner=None)
    env.memories.save([entry])
    rec = _make("memory", f"memory:{entry['id']}", "update", "New fact")
    store.approve(rec["id"])
    entries = env.memories.load_all()
    entries[0]["text"] = "Edited by hand"
    env.memories.save(entries)
    with pytest.raises(HarnessError) as e:
        store.undo(rec["id"])
    assert e.value.error_class == "harness.target_changed_since_apply"
    assert env.memories.load_all()[0]["text"] == "Edited by hand"


# -- sub-agent specs -------------------------------------------------------

AGENT_V1 = "---\nname: Reviewer\ndescription: Reads code\nmode: reviewer\ntools: [read_file]\n---\nReview carefully.\n"
AGENT_V2 = "---\nname: Reviewer\ndescription: Reads code\nmode: reviewer\ntools: [read_file]\n---\nReview carefully and cite lines.\n"


def test_agent_spec_update_delete_create_with_exact_undo(env):
    path = agent_defs.def_path("reviewer")
    import os
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(AGENT_V1)
    rec = _make("subagent_spec", "agent:reviewer", "update", AGENT_V2)
    store.approve(rec["id"])
    assert open(path, encoding="utf-8", newline="").read() == AGENT_V2
    store.undo(rec["id"])
    assert open(path, encoding="utf-8", newline="").read() == AGENT_V1

    rec = _make("subagent_spec", "agent:reviewer", "delete", None)
    store.approve(rec["id"])
    assert not os.path.exists(path)
    store.undo(rec["id"])
    assert open(path, encoding="utf-8", newline="").read() == AGENT_V1

    fresh = agent_defs.def_path("helper")
    rec = _make("subagent_spec", "agent:helper", "create", AGENT_V1.replace("Reviewer", "Helper"))
    store.approve(rec["id"])
    assert os.path.exists(fresh)
    store.undo(rec["id"])
    assert not os.path.exists(fresh)


# -- skills: through the existing skill proposal store ----------------------

def _skill(env, procedure):
    sk = Skill(name="deploy-helper", description="deploys", version="1.0.0", category="general",
               status="published", owner="", when_to_use="when deploying", procedure=procedure,
               pitfalls=["watch out"], verification=["check it worked"])
    folder = env.data / "skills" / "general" / "deploy-helper"
    folder.mkdir(parents=True, exist_ok=True)
    return sk, folder / "SKILL.md"


def _skill_proposal(env, original_text, revised_text, owner=""):
    pid = store.new_id()
    delegate = targets.create_skill_delegate(
        proposal_id=pid, skill_name="deploy-helper", owner=owner, current_md=original_text,
        revised_md=revised_text, rationale="because", evidence_ids=["e1"], model="m")
    return store.create_proposal(
        axis="skill", target="skill:deploy-helper", op="update", before=original_text, after=revised_text,
        rationale="because", evidence=[], trigger="turn_end:correction", owner=owner, session_id="s1",
        model="m", meta={"delegate": delegate}, proposal_id=pid)


def test_skill_proposal_shares_the_skill_proposal_store_and_undoes_exactly(env):
    sk, path = _skill(env, ["step one", "step two"])
    # a hand-written file the skill store would normalise if it re-serialised it
    original = sk.to_markdown().replace("step one", "step one   ") + "\n\n\n"
    path.write_text(original, encoding="utf-8", newline="")
    revised = sk.to_markdown().replace("step two", "step two\n2. step three: verify")
    rec = _skill_proposal(env, original, revised)

    mirror = so.get_proposal(rec["id"])
    assert mirror and mirror["status"] == "pending" and mirror["source"] == "harness_refinement"
    assert path.read_text(encoding="utf-8") == original            # nothing applied

    applied = store.approve(rec["id"], by="tester")
    assert applied["status"] == "applied"
    assert so.get_proposal(rec["id"])["status"] == "approved"
    assert "step three" in path.read_text(encoding="utf-8")

    store.undo(rec["id"])
    assert path.read_bytes() == original.encode("utf-8")


def test_skill_undo_refuses_after_a_later_edit(env):
    sk, path = _skill(env, ["step one"])
    original = sk.to_markdown()
    path.write_text(original, encoding="utf-8", newline="")
    revised = sk.to_markdown().replace("step one", "step one\n2. step two")
    rec = _skill_proposal(env, original, revised)
    store.approve(rec["id"])
    current = path.read_text(encoding="utf-8")
    path.write_text(current + "\nhand note\n", encoding="utf-8", newline="")
    with pytest.raises(HarnessError) as e:
        store.undo(rec["id"])
    assert e.value.error_class == "harness.target_changed_since_apply"
    assert path.read_text(encoding="utf-8").endswith("hand note\n")


def test_skill_decided_in_the_skills_tab_is_mirrored(env):
    sk, path = _skill(env, ["step one"])
    original = sk.to_markdown()
    path.write_text(original, encoding="utf-8", newline="")
    rec = _skill_proposal(env, original, sk.to_markdown().replace("step one", "step one\n2. step two"))
    so.approve_proposal(rec["id"], by="tester")            # the existing Approve button
    assert store.sync_delegates() == 1
    assert store.get(rec["id"])["status"] == "applied"
    assert "step two" in store.get(rec["id"])["applied_content"]

    other = _skill_proposal(env, store.get(rec["id"])["applied_content"],
                            store.get(rec["id"])["applied_content"].replace("step two", "step 2"))
    so.reject_proposal(other["id"], by="tester")
    store.sync_delegates()
    assert store.get(other["id"])["status"] == "rejected"


def test_rejecting_a_skill_proposal_rejects_the_mirror_too(env):
    sk, path = _skill(env, ["step one"])
    original = sk.to_markdown()
    path.write_text(original, encoding="utf-8", newline="")
    rec = _skill_proposal(env, original, sk.to_markdown().replace("step one", "step one\n2. step two"))
    store.reject(rec["id"], reason="no")
    assert so.get_proposal(rec["id"])["status"] == "rejected"
    assert path.read_text(encoding="utf-8") == original


# -- listing, scoping, log -------------------------------------------------

def test_list_filters_and_owner_scope(env):
    proj = _project(env)
    a = _make("prompt_layer", f"project:{proj['id']}", "update", "A text.", owner="alice", session_id="s1")
    b = _make("prompt_layer", f"project:{proj['id']}", "update", "B text.", owner="bob", session_id="s2")
    assert {r["id"] for r in store.list_proposals()} == {a["id"], b["id"]}
    assert [r["id"] for r in store.list_proposals(owner="alice")] == [a["id"]]
    assert [r["id"] for r in store.list_proposals(session_id="s2")] == [b["id"]]
    store.reject(b["id"])
    assert [r["id"] for r in store.list_proposals(status="rejected")] == [b["id"]]
    assert store.counts()["pending"] == 1 and store.counts()["rejected"] == 1
    assert store.pending_for_session("s1")["id"] == a["id"]
    assert store.pending_for_session("s2") is None


def test_log_records_trigger_to_result(env):
    proj = _project(env)
    rec = _make("prompt_layer", f"project:{proj['id']}", "update", "Other text.", trigger="turn_end:correction")
    store.approve(rec["id"], by="tester")
    store.undo(rec["id"], by="tester")
    events = [e["event"] for e in reversed(store.read_log(proposal_id=rec["id"]))]
    assert events == ["proposed", "applied", "undone"]
    first = store.read_log(proposal_id=rec["id"])[-1]
    assert first["trigger"] == "turn_end:correction" and "prompt_layer:update" in first["result"]
    bad = _make("prompt_layer", f"project:{proj['id']}", "update", "Yet other text.")
    env.projects.update(proj["id"], {"instructions": "changed"})
    with pytest.raises(HarnessError):
        store.approve(bad["id"])
    assert store.read_log(proposal_id=bad["id"])[0]["event"] == "apply_refused"


def test_processed_marker_only_moves_forward(env):
    assert store.processed_until("s1") == 0.0
    store.mark_processed("s1", 100.0)
    store.mark_processed("s1", 50.0)
    assert store.processed_until("s1") == 100.0


def test_diff_keeps_one_line_per_entry_without_a_final_newline():
    diff = store.make_diff("Answer briefly.", "Answer briefly.\nUse bullets.", "project:1")
    assert diff.splitlines() == [
        "--- project:1@current", "+++ project:1@proposed", "@@ -1 +1,2 @@",
        " Answer briefly.", "+Use bullets."]
    assert store.make_diff("same", "same", "t") == ""
    assert "-old" in store.make_diff("old", "new", "t").splitlines()