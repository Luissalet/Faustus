"""Round 2 of the review of the harness refinement layer.

1. Permission rules are ordered and the LAST match wins, so reordering them can
   turn a deny into an allow without adding or removing a single rule.
2. Approving / undoing checked the target and wrote it in two separate steps, so
   a change made by a person in between was overwritten.
"""
from __future__ import annotations

import itertools
import os

import pytest

from services.projects import ProjectStore
from src import agent_defs
from src.harness_refinement import store, targets
from src.harness_refinement.store import HarnessError
from src.memory import MemoryManager
from src.skills_runtime import sleep_optimize as so
from src.subagent_permissions import decide

OLD = """---
name: reviewer
mode: worker
tools: [write_file]
permission:
  - allow write src/**
  - deny write src/secrets/**
---
review
"""
REORDERED = OLD.replace("  - allow write src/**\n  - deny write src/secrets/**\n",
                        "  - deny write src/secrets/**\n  - allow write src/**\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    for mod in (store, targets, agent_defs):
        monkeypatch.setattr(mod, "DATA_DIR", str(data))
    monkeypatch.setattr(so, "DATA_DIR", str(data), raising=False)
    projects = ProjectStore(str(data))
    memories = MemoryManager(str(data))
    monkeypatch.setattr(targets, "_project_store", lambda: projects)
    monkeypatch.setattr(targets, "_memory_manager", lambda: memories)
    return type("Env", (), {"data": data, "projects": projects, "memories": memories})


def _agent(slug="reviewer", text=OLD):
    path = agent_defs.def_path(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def _make(axis, target, op, after):
    before = targets.read_current(axis, target, "")
    return store.create_proposal(axis=axis, target=target, op=op, before=before, after=after,
                                 rationale="r", evidence=[], trigger="test")


# -- 1. the order of permission rules ----------------------------------------

def test_reordering_permission_rules_is_a_widening(env):
    _agent()
    with pytest.raises(HarnessError) as exc:
        targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, REORDERED)
    assert exc.value.error_class == "harness.widens_authority"
    # and the verdict really would flip: this is not a theoretical objection
    old = agent_defs.parse(OLD, slug="reviewer", source=agent_defs.SOURCE_USER)
    new = agent_defs.parse(REORDERED, slug="reviewer", source=agent_defs.SOURCE_USER)
    assert decide(old.permission, "write", "src/secrets/key.txt") == "deny"
    assert decide(new.permission, "write", "src/secrets/key.txt") == "allow"


def test_the_reordered_edit_cannot_be_stored_through_the_proposer_path(env):
    _agent()
    with pytest.raises(HarnessError):
        targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, REORDERED)
    assert store.list_proposals() == []


@pytest.mark.parametrize("extra", ["  - deny write docs/**\n", "  - deny delegate *\n"])
def test_appending_a_deny_rule_at_the_end_still_narrows_and_is_allowed(env, extra):
    new = OLD.replace("---\nreview", extra + "---\nreview")
    after, _ = targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, new)
    assert extra.strip() in after


def test_dropping_an_allow_rule_is_allowed_and_dropping_a_deny_is_not(env):
    only_deny = OLD.replace("  - allow write src/**\n", "")
    targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, only_deny)
    only_allow = OLD.replace("  - deny write src/secrets/**\n", "")
    with pytest.raises(HarnessError) as exc:
        targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, only_allow)
    assert exc.value.error_class == "harness.widens_authority"


def test_inserting_a_deny_before_an_allow_is_refused_because_it_would_not_bind(env):
    new = OLD.replace("  - allow write src/**\n", "  - deny write docs/**\n  - allow write src/**\n")
    with pytest.raises(HarnessError):
        targets.validate_edit("subagent_spec", "agent:reviewer", "update", OLD, new)


RULES = ["allow write a/**", "deny write a/b/**", "allow write a/b/c", "deny write a/b/c/d", "allow write **"]
PROBES = ["a/x", "a/b/x", "a/b/c", "a/b/c/d", "z/y"]


def test_nothing_accepted_ever_makes_any_probe_more_permissive():
    """Exhaustive over every ordered selection of up to 3 of 5 rules, before and
    after: whenever the check says 'does not widen', the last-match-wins
    verdict of no probe path may go from deny to allow."""
    from src.agent_defs import Rule

    def rules_of(texts):
        out = []
        for t in texts:
            effect, action, pattern = t.split(" ", 2)
            out.append(Rule(action=action, pattern=pattern, effect=effect))
        return out

    sequences = [list(p) for n in range(0, 4) for p in itertools.permutations(RULES, n)]
    accepted = 0
    for old in sequences:
        for new in sequences:
            if targets._permission_widening(old, new):
                continue
            accepted += 1
            for probe in PROBES:
                before = decide(rules_of(old), "write", probe)
                after = decide(rules_of(new), "write", probe)
                assert not (before == "deny" and after == "allow"), (old, new, probe)
    assert accepted > len(sequences)          # it accepts real edits, not only identical lists


# -- 2. check and write are one step ------------------------------------------

def _stale_first_read(monkeypatch, after_first_read):
    """The first read (the up-front check in approve/undo) sees the old value and
    then a person edits the target; every later read is real."""
    real = targets.read_current
    calls = []

    def wrapper(axis, target, owner=""):
        value = real(axis, target, owner)
        if not calls:
            calls.append(1)
            after_first_read()
        return value

    monkeypatch.setattr(targets, "read_current", wrapper)


def test_approve_does_not_overwrite_an_instruction_edit_made_after_the_check(env, monkeypatch):
    project = env.projects.create("P", instructions="before", scaffold_memory=False)
    rec = _make("prompt_layer", f"project:{project['id']}", "update", "proposed")
    _stale_first_read(monkeypatch, lambda: env.projects.update(project["id"], {"instructions": "human edit"}))
    with pytest.raises(HarnessError) as exc:
        store.approve(rec["id"])
    assert exc.value.error_class == "harness.target_changed"
    assert env.projects.get(project["id"])["instructions"] == "human edit"
    assert store.get(rec["id"])["status"] == "pending"


def test_undo_does_not_overwrite_an_instruction_edit_made_after_the_check(env, monkeypatch):
    project = env.projects.create("P", instructions="before", scaffold_memory=False)
    rec = _make("prompt_layer", f"project:{project['id']}", "update", "proposed")
    store.approve(rec["id"])
    _stale_first_read(monkeypatch, lambda: env.projects.update(project["id"], {"instructions": "human edit"}))
    with pytest.raises(HarnessError) as exc:
        store.undo(rec["id"])
    assert exc.value.error_class == "harness.target_changed_since_apply"
    assert env.projects.get(project["id"])["instructions"] == "human edit"
    assert store.get(rec["id"])["status"] == "applied"


def test_the_instruction_compare_and_write_hold_the_project_stores_lock(env, monkeypatch):
    """A real second thread editing through the same store cannot slip between
    the compare and the write: it waits for the lock the apply holds."""
    import threading
    project = env.projects.create("P", instructions="before", scaffold_memory=False)
    rec = _make("prompt_layer", f"project:{project['id']}", "update", "proposed")
    started, done = threading.Event(), threading.Event()
    order = []
    real_update = env.projects.update

    def slow_guard_then_edit():
        started.set()
        env.projects.update(project["id"], {"instructions": "human edit"})
        order.append("human")
        done.set()

    real_guard = targets._guard

    def guard(*args, **kwargs):
        real_guard(*args, **kwargs)
        if not order and args[0] == "prompt_layer":
            threading.Thread(target=slow_guard_then_edit, daemon=True).start()
            started.wait(2)
            threading.Event().wait(0.2)         # give the human thread every chance to run
            order.append("apply-still-holding-the-lock" if not done.is_set() else "human-got-in")

    monkeypatch.setattr(targets, "_guard", guard)
    store.approve(rec["id"])
    done.wait(3)
    assert order[0] == "apply-still-holding-the-lock"
    # the person's edit came AFTER the apply, so it is the final word, not lost
    assert env.projects.get(project["id"])["instructions"] == "human edit"


def test_approve_does_not_overwrite_a_memory_edit_made_after_the_check(env, monkeypatch):
    entry = env.memories.add_entry("Old fact", owner=None)
    env.memories.save([entry])
    rec = _make("memory", f"memory:{entry['id']}", "update", "New fact")

    def edit():
        entries = env.memories.load_all()
        entries[0]["text"] = "Edited by hand"
        env.memories.save(entries)

    _stale_first_read(monkeypatch, edit)
    with pytest.raises(HarnessError) as exc:
        store.approve(rec["id"])
    assert exc.value.error_class == "harness.target_changed"
    assert env.memories.load_all()[0]["text"] == "Edited by hand"


def test_approve_does_not_overwrite_an_agent_file_edited_after_the_check(env, monkeypatch):
    path = _agent()
    new = OLD.replace("review\n", "review carefully\n")
    rec = _make("subagent_spec", "agent:reviewer", "update", new)

    def edit():
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(OLD.replace("review\n", "edited by hand\n"))

    _stale_first_read(monkeypatch, edit)
    with pytest.raises(HarnessError) as exc:
        store.approve(rec["id"])
    assert exc.value.error_class == "harness.target_changed"
    assert "edited by hand" in open(path, encoding="utf-8").read()


def test_undo_does_not_overwrite_an_agent_file_edited_after_the_check(env, monkeypatch):
    path = _agent()
    new = OLD.replace("review\n", "review carefully\n")
    rec = _make("subagent_spec", "agent:reviewer", "update", new)
    store.approve(rec["id"])

    def edit():
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new.replace("review carefully", "edited later"))

    _stale_first_read(monkeypatch, edit)
    with pytest.raises(HarnessError) as exc:
        store.undo(rec["id"])
    assert exc.value.error_class == "harness.target_changed_since_apply"
    assert "edited later" in open(path, encoding="utf-8").read()