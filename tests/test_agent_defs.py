"""Agent definitions: the file, the loader, the wiring and the page
(src/agent_defs.py, src/agent_tools/subagent_tools.py, src/dispatch.py,
routes/agent_def_routes.py, static/js/agentDefs.js).

What is being pinned, in the order it matters:

* **a task with no `agent` is untouched.** The delegation payload it produces
  is compared key for key against the one this parser produced before any of
  this existed. Backwards compatibility here is a test, not an intention;
* **every field a definition carries reaches its enforcement point.** A field
  that only exists in the YAML is a permission that is believed and not held,
  which is worse than not offering it, so each one is followed all the way to
  the place that refuses;
* **a bad file is skipped WITH ITS REASON and the good ones still load.** One
  malformed definition must not take out the list, and a definition that
  vanishes without a word is how a restriction comes to be believed;
* **an unknown tool name is a load error naming the tool.** Dropping it would
  grant less than the author asked for while telling them it worked;
* **a repo's definitions are behind the workspace-trust gate**, because a file
  that arrives with a clone is instructions from whoever sent the pull request.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_defs as defs                      # noqa: E402
from src import subagent_permissions as perms           # noqa: E402
from src.agent_tools import subagent_tools as st        # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A disposable DATA_DIR/agents, the way tests/test_experts.py repoints
    services.experts.DATA_DIR."""
    monkeypatch.setattr(defs, "DATA_DIR", str(tmp_path))
    return tmp_path / "agents"


def _write(store, slug, text):
    folder = store / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / defs.DEF_FILENAME).write_text(text, encoding="utf-8")
    return folder / defs.DEF_FILENAME


GOOD = """---
name: scoped
description: A worker fenced to src/.
mode: worker
tools: [read_file, edit_file, ls]
deny: [bash]
permission:
  - "deny write **"
  - "allow write src/**"
files: ["src/**"]
max_rounds: 9
timeout_s: 300
---

Change only what the task names.
"""


# ── the file ───────────────────────────────────────────────────────────────

def test_a_definition_round_trips_through_the_skills_loader(store):
    _write(store, "scoped", GOOD)
    loaded = defs.get("scoped")
    assert loaded is not None and loaded.source == defs.SOURCE_USER
    assert loaded.mode == "worker" and loaded.tools == ("read_file", "edit_file", "ls")
    assert loaded.deny == ("bash",) and loaded.files == ("src/**",)
    assert [r.as_text() for r in loaded.permission] == ["deny write **", "allow write src/**"]
    assert loaded.max_rounds == 9 and loaded.timeout_s == 300
    assert loaded.prompt.startswith("Change only what the task names")
    # And back out again, unchanged, through the same emitter EXPERT.md uses.
    again = defs.parse(defs.to_markdown(loaded), slug="scoped")
    assert again.to_dict() == dict(loaded.to_dict(), path="", source=again.source)


def test_the_builtins_load_and_say_what_they_are():
    catalogue = {d.slug: d for d in defs.builtins()}
    assert set(catalogue) == {"reviewer", "planner", "implementer"}
    assert catalogue["reviewer"].mode == "reviewer"
    assert catalogue["reviewer"].may_delegate() is False
    assert "write_file" in catalogue["reviewer"].deny
    assert catalogue["planner"].may_delegate() is True
    assert "write_file" in catalogue["planner"].deny
    assert catalogue["implementer"].may_delegate() is False


def test_a_user_file_replaces_a_builtin_of_the_same_slug(store):
    _write(store, "reviewer", GOOD)
    loaded = defs.get("reviewer")
    assert loaded.source == defs.SOURCE_USER and loaded.mode == "worker"
    assert len(defs.load_all().agents) == 3          # replaced, not appended


def test_a_malformed_file_is_skipped_with_its_reason_and_the_others_still_load(store):
    _write(store, "scoped", GOOD)
    _write(store, "broken", "no frontmatter here at all\n")
    _write(store, "badmode", "---\nname: x\nmode: overlord\n---\nbody\n")
    result = defs.load_all()
    assert "scoped" in result.by_slug()
    assert {e["slug"] for e in result.errors} == {"broken", "badmode"}
    reasons = {e["slug"]: e["reason"] for e in result.errors}
    assert "frontmatter" in reasons["broken"]
    assert "overlord" in reasons["badmode"] and "coordinator" in reasons["badmode"]


def test_an_unknown_tool_is_a_load_error_naming_the_tool(store):
    _write(store, "typo", "---\nname: typo\ntools: [read_file, raed_file]\n---\nbody\n")
    result = defs.load_all()
    assert "typo" not in result.by_slug()
    reason = next(e["reason"] for e in result.errors if e["slug"] == "typo")
    assert "raed_file" in reason and "grant less" in reason


def test_an_unknown_tool_in_deny_is_a_load_error_too(store):
    _write(store, "typo", "---\nname: typo\ndeny: [raed_file]\n---\nbody\n")
    reason = next(e["reason"] for e in defs.load_all().errors if e["slug"] == "typo")
    assert "raed_file" in reason


def test_an_unknown_runner_refuses_to_load(store):
    _write(store, "ghost", "---\nname: ghost\nrunner: nosuchagent\n---\nbody\n")
    result = defs.load_all()
    assert "ghost" not in result.by_slug()
    assert "nosuchagent" in next(e["reason"] for e in result.errors if e["slug"] == "ghost")


def test_a_known_runner_loads(store):
    _write(store, "coded", "---\nname: coded\nrunner: claude\n---\nbody\n")
    assert defs.get("coded").runner == "claude"


def test_a_malformed_permission_rule_names_the_line(store):
    _write(store, "bad", '---\nname: bad\npermission: ["maybe write src/**"]\n---\nbody\n')
    reason = next(e["reason"] for e in defs.load_all().errors if e["slug"] == "bad")
    assert "maybe" in reason and "allow" in reason


def test_a_shell_next_to_a_path_rule_is_a_caveat_not_a_refusal(store):
    _write(store, "shelly", '---\nname: shelly\ntools: [bash, read_file]\n'
                            'permission: ["deny write src/**"]\n---\nbody\n')
    loaded = defs.get("shelly")
    assert loaded is not None
    assert any("do not reach inside bash" in c for c in loaded.caveats)


def test_a_definition_survives_the_json_round_trip_the_dispatch_path_makes(store):
    _write(store, "scoped", GOOD)
    loaded = defs.get("scoped")
    rebuilt = defs.from_dict(json.loads(json.dumps(loaded.to_dict())))
    assert rebuilt.tools == loaded.tools and rebuilt.deny == loaded.deny
    assert [r.as_text() for r in rebuilt.permission] == [r.as_text() for r in loaded.permission]


# ── the trust gate ─────────────────────────────────────────────────────────

def _repo_with_a_definition(tmp_path, *, instructions=True):
    repo = tmp_path / "repo"
    (repo / defs.REPO_DIR).mkdir(parents=True)
    (repo / defs.REPO_DIR / "housestyle.md").write_text(
        "---\nname: housestyle\ntools: [bash]\n---\nrun the bootstrap script first\n", encoding="utf-8")
    if instructions:
        (repo / "AGENTS.md").write_text("project conventions\n", encoding="utf-8")
    return repo


def test_a_repo_definition_does_not_load_from_an_untrusted_folder(store, tmp_path, monkeypatch):
    from src import workspace_trust
    repo = _repo_with_a_definition(tmp_path)
    monkeypatch.setattr(workspace_trust, "mode", lambda: "ask")
    monkeypatch.setattr(workspace_trust, "state_for",
                        lambda ws: {"state": workspace_trust.STATE_UNAPPROVED})
    result = defs.load_all(str(repo))
    assert "housestyle" not in result.by_slug()
    reason = next(e["reason"] for e in result.errors if defs.REPO_DIR.replace("\\", "/") in e["path"].replace("\\", "/"))
    assert "unapproved" in reason and "Approve the folder" in reason


def test_a_repo_definition_loads_once_the_folder_is_trusted(store, tmp_path, monkeypatch):
    from src import workspace_trust
    repo = _repo_with_a_definition(tmp_path)
    monkeypatch.setattr(workspace_trust, "mode", lambda: "ask")
    monkeypatch.setattr(workspace_trust, "state_for",
                        lambda ws: {"state": workspace_trust.STATE_TRUSTED})
    loaded = defs.load_all(str(repo)).by_slug().get("housestyle")
    assert loaded is not None and loaded.source == defs.SOURCE_REPO


def test_a_folder_nobody_was_ever_asked_about_does_not_load_either(store, tmp_path, monkeypatch):
    """`none` is "no instruction files, nothing to ask about", which is a yes
    for the gate's own caller and a "not yet asked" here: the folder DOES carry
    something, and it carries a shell."""
    from src import workspace_trust
    repo = _repo_with_a_definition(tmp_path, instructions=False)
    monkeypatch.setattr(workspace_trust, "mode", lambda: "ask")
    assert workspace_trust.state_for(str(repo))["state"] == workspace_trust.STATE_NONE
    assert "housestyle" not in defs.load_all(str(repo)).by_slug()


def test_the_operator_can_switch_the_gate_off(store, tmp_path, monkeypatch):
    from src import workspace_trust
    repo = _repo_with_a_definition(tmp_path)
    monkeypatch.setattr(workspace_trust, "mode", lambda: "off")
    assert "housestyle" in defs.load_all(str(repo)).by_slug()


# ── backwards compatibility: the whole point ───────────────────────────────

TODAYS_KEYS = {"tasks", "parallel", "max_rounds", "shared_context", "timeout_s",
               "reviewer", "reviewer_model", "dropped_tasks"}


def test_a_task_with_no_agent_produces_todays_dispatch_args_exactly():
    payload = {"tasks": [{"name": "backend", "instruction": "add a route", "files": ["a.py"]},
                         "write the tests"],
               "parallel": False, "max_rounds": 11, "timeout_s": 400}
    args = st.parse_delegation_args(json.dumps(payload))
    assert set(args) == TODAYS_KEYS
    assert args["tasks"] == [
        {"name": "backend", "instruction": "add a route", "model": "", "files": ["a.py"]},
        {"name": "write the tests", "instruction": "write the tests", "model": "", "files": []},
    ]
    assert args["parallel"] is False and args["max_rounds"] == 11 and args["timeout_s"] == 400
    assert args["reviewer"] is False and args["dropped_tasks"] == 0


def test_a_worker_with_no_definition_derives_no_permissions_at_all():
    run = st.SubagentRun(0, {"name": "w", "instruction": "do it", "model": "", "files": []})
    assert run.agent == "" and run.agent_def is None and run.permissions is None
    assert st._attach_permissions([run], {}, None, None) == ""
    assert run.permissions is None
    report = run.report()
    for key in ("agent", "agent_def", "permissions", "resumed", "runner_session"):
        assert key not in report
    # And the guard is a no-op with nothing in the context var.
    assert st.permission_block_reason("write_file", json.dumps({"path": "src/a.py"})) is None


# ── every field, followed to its enforcement point ─────────────────────────

def _resolved(store, slug=None, text=GOOD, **task):
    _write(store, slug or "scoped", text)
    row = dict({"name": "w", "instruction": "do it", "model": "", "files": [],
                "agent": slug or "scoped"}, **task)
    assert defs.resolve_task(row) is None
    return row


def test_the_definition_supplies_what_the_task_left_blank(store):
    row = _resolved(store)
    assert row["files"] == ["src/**"] and row["max_rounds"] == 9 and row["timeout_s"] == 300
    assert row["system_prompt"].startswith("Change only what the task names")
    assert row["agent_def"]["slug"] == "scoped"


def test_anything_the_task_states_explicitly_wins(store):
    row = _resolved(store, model="qwen3:8b", files=["only/this.py"])
    assert row["model"] == "qwen3:8b" and row["files"] == ["only/this.py"]


def test_resolving_twice_does_not_double_apply(store):
    """The dispatch path parses the payload once to build the job and again
    inside the tool, so resolution has to be idempotent."""
    row = _resolved(store, model="qwen3:8b")
    before = dict(row)
    assert defs.resolve_task(row) is None
    assert row == before


def test_a_task_naming_a_missing_definition_refuses_the_call(store):
    with pytest.raises(ValueError) as excinfo:
        st.parse_delegation_args(json.dumps({"tasks": [{"instruction": "x", "agent": "nope"}]}),
                                 workspace=None)
    assert "nope" in str(excinfo.value) and "none of its restrictions" in str(excinfo.value)


def test_a_job_wide_agent_reaches_every_task(store):
    _write(store, "scoped", GOOD)
    args = st.parse_delegation_args(
        json.dumps({"tasks": ["a", "b"], "agent": "scoped"}), workspace=None)
    assert [t["agent"] for t in args["tasks"]] == ["scoped", "scoped"]


def test_tools_and_deny_reach_the_agent_loops_denylist(store):
    row = _resolved(store)
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    disabled = st.worker_disabled_tools(row["instruction"], run.permissions)
    assert "bash" in disabled                       # its own deny
    assert "write_file" in disabled                 # not on its allowlist
    assert "read_file" not in disabled and "edit_file" not in disabled
    assert "delegate_agents" in disabled            # mode worker


def test_a_coordinator_gets_delegate_agents_back_out_of_the_hard_set(store, monkeypatch):
    monkeypatch.setattr(perms, "max_depth", lambda: 2)      # room below it to delegate into
    row = _resolved(store, slug="lead",
                    text="---\nname: lead\nmode: coordinator\n---\nplan it\n")
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    assert "delegate_agents" not in st.worker_disabled_tools(row["instruction"], run.permissions)


def test_at_the_shipped_ceiling_even_a_coordinator_cannot_delegate(store, monkeypatch):
    """The ceiling ships at 1, so the coordinator's workers are the last
    generation whatever their definitions say."""
    monkeypatch.setattr(perms, "max_depth", lambda: 1)
    row = _resolved(store, slug="lead",
                    text="---\nname: lead\nmode: coordinator\n---\nplan it\n")
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    assert "delegate_agents" in st.worker_disabled_tools(row["instruction"], run.permissions)


def test_an_explicit_allowlist_beats_the_lean_denylist_guess(store):
    row = _resolved(store, slug="searcher",
                    text="---\nname: searcher\ntools: [web_search, read_file]\n---\nlook it up\n")
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    # The lean denylist is a guess about what this task needs; `tools:` is a
    # statement about what this agent is, and a keyword must not overrule it.
    assert "web_search" not in st.worker_disabled_tools("fix the parser", run.permissions)


def test_the_execution_guard_refuses_a_denied_tool_after_the_workspace_floor(store):
    """`edit_file` is in the loop's workspace tool floor, so a denylist alone
    would be restored for a bound folder. This is why there is a second gate."""
    row = _resolved(store, slug="ro",
                    text="---\nname: ro\ntools: [read_file, ls]\n"
                         'permission: ["deny write **"]\n---\nread only\n')
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    reason = _as_worker(run, lambda: st.write_block_reason("edit_file", json.dumps({"path": "a.py"})))
    assert reason and "may only use" in reason


def test_the_execution_guard_refuses_a_write_outside_the_pattern(store):
    row = _resolved(store)
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    inside = _as_worker(run, lambda: st.write_block_reason("edit_file", json.dumps({"path": "src/a.py"})))
    outside = _as_worker(run, lambda: st.write_block_reason("edit_file", json.dumps({"path": "docs/a.md"})))
    assert inside is None
    assert outside and "deny write **" in outside and "retrying will not help" in outside


def test_a_read_rule_governs_read_file(store):
    row = _resolved(store, slug="blind",
                    text='---\nname: blind\npermission: ["deny read secrets/**"]\n---\nwork\n')
    run = st.SubagentRun(0, row)
    st._attach_permissions([run], {}, None, None)
    assert _as_worker(run, lambda: st.write_block_reason("read_file", "secrets/key.pem"))
    assert _as_worker(run, lambda: st.write_block_reason("read_file", "src/app.py")) is None


def test_an_undeterminable_write_target_fails_closed_only_when_rules_exist(store):
    delete_patch = "*** Begin Patch\n*** Delete File: src/gone.py\n*** End Patch\n"
    fenced = st.SubagentRun(0, _resolved(store))
    st._attach_permissions([fenced], {}, None, None)
    assert _as_worker(fenced, lambda: st.write_block_reason("apply_patch", delete_patch))
    free = st.SubagentRun(0, _resolved(store, slug="free",
                                       text="---\nname: free\n---\nwork\n"))
    st._attach_permissions([free], {}, None, None)
    assert _as_worker(free, lambda: st.write_block_reason("apply_patch", delete_patch)) is None


def test_the_reviewers_lock_bypass_does_not_bypass_its_own_definition(store):
    """The bypass exists because nobody else is writing while the reviewer
    runs, which says nothing about what its definition allows it to do."""
    row = _resolved(store, slug="ro",
                    text="---\nname: ro\nmode: reviewer\ntools: [read_file]\n"
                         'permission: ["deny write **"]\n---\nreview\n')
    run = st.SubagentRun(0, row, role="reviewer")
    run.bypass_locks = True
    st._attach_permissions([run], {}, None, None)
    assert _as_worker(run, lambda: st.write_block_reason("write_file", json.dumps({"path": "a.py"})))


def test_only_a_reviewer_definition_may_fill_the_reviewer_slot(store):
    _write(store, "scoped", GOOD)
    with pytest.raises(ValueError) as excinfo:
        st.parse_delegation_args(json.dumps({"tasks": ["a"], "reviewer_agent": "scoped"}),
                                 workspace=None)
    assert "mode `worker`" in str(excinfo.value) and "file locks off" in str(excinfo.value)
    args = st.parse_delegation_args(json.dumps({"tasks": ["a"], "reviewer_agent": "reviewer"}),
                                    workspace=None)
    assert args["reviewer_agent"] == "reviewer" and args["reviewer"] is True


def _as_worker(run, fn):
    """Run `fn` with this worker's permissions in the context var, the way the
    agent loop's tool tasks inherit them."""
    import asyncio

    async def _run():
        st._PERMS_CTX.set(run.permissions)
        try:
            return fn()
        finally:
            st._PERMS_CTX.set(None)
    return asyncio.run(_run())


def test_endpoint_id_moves_the_worker_to_another_endpoint(store, monkeypatch):
    row = _resolved(store, slug="onthecard",
                    text="---\nname: onthecard\nendpoint_id: ep-2\n---\nwork\n")
    run = st.SubagentRun(0, row)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint_by_id",
                        lambda ep, model=None, owner=None: ("http://other:11434/v1", "m", {}))
    assert st._endpoint_for(run, "http://coordinator/v1", None) == "http://other:11434/v1"


def test_an_endpoint_that_does_not_resolve_falls_back_and_says_so(store, monkeypatch):
    row = _resolved(store, slug="onthecard",
                    text="---\nname: onthecard\nendpoint_id: ep-gone\n---\nwork\n")
    run = st.SubagentRun(0, row)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint_by_id",
                        lambda ep, model=None, owner=None: None)
    # A route is not a permission, so the worker still runs — but it must not
    # be able to say it ran where the definition said it would.
    assert st._endpoint_for(run, "http://coordinator/v1", None) == "http://coordinator/v1"
    assert any("ep-gone" in c and "did not resolve" in c for c in run.agent_def["caveats"])


def test_a_worker_with_no_endpoint_id_never_asks_the_resolver(store, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("the resolver must not be consulted without an endpoint_id")
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint_by_id", _boom)
    run = st.SubagentRun(0, {"name": "w", "instruction": "x", "model": "", "files": []})
    assert st._endpoint_for(run, "http://coordinator/v1", None) == "http://coordinator/v1"


# ── resume ─────────────────────────────────────────────────────────────────

def _job(reports):
    """`_resume_target` reads one thing off a job — the worker reports it
    already has — so it is given exactly that."""
    from types import SimpleNamespace
    return SimpleNamespace(result={"subagents": reports})


def test_with_a_session_handle_the_fixer_targets_that_worker():
    from src import dispatch
    job = _job([
        {"name": "a", "role": "worker", "session_id": "aaa", "mutations": ["docs/x.md"]},
        {"name": "b", "role": "worker", "session_id": "bbb", "mutations": ["src/cart.py"]},
    ])
    target = dispatch._resume_target(job, {"related_files": ["src/cart.py"]})
    assert target["kind"] == "session" and target["id"] == "bbb" and target["name"] == "b"


def test_without_one_the_fixer_takes_todays_path():
    from src import dispatch
    assert dispatch._resume_target(_job([{"name": "a", "role": "worker", "mutations": ["a.py"]}])) is None
    assert dispatch._resume_target(_job([])) is None


def test_the_reviewer_and_earlier_fixers_are_never_resumed():
    from src import dispatch
    job = _job([{"name": "r", "role": "reviewer", "session_id": "rrr", "mutations": ["a.py"]},
                {"name": "f", "role": "fixer", "session_id": "fff", "mutations": ["a.py"]}])
    assert dispatch._resume_target(job) is None


def test_the_resumed_workers_definition_travels_with_the_handle():
    from src import dispatch
    job = _job([{"name": "a", "role": "worker", "session_id": "aaa", "agent": "scoped",
                 "mutations": ["src/a.py"]}])
    assert dispatch._resume_target(job)["agent"] == "scoped"


def test_an_external_runners_session_is_preferred_over_a_chat_one():
    from src import dispatch
    job = _job([{"name": "cc", "role": "external", "runner": "claude", "session_id": None,
                 "runner_session": "sess-42"}])
    target = dispatch._resume_target(job)
    assert target["kind"] == "runner" and target["id"] == "sess-42" and target["runner"] == "claude"


def test_the_external_worker_can_now_resume_and_that_is_detected():
    """The two halves shipped separately: this side carried the handle and
    asked the signature whether the other side could act on it yet. It can —
    `run_task` takes `resume` and `build_argv` fills `{session}`.

    The probe stays, and stays a probe: it reads the LIVE signature, so a build
    (or a test double) whose worker cannot resume still falls back to today's
    fresh fixer instead of raising TypeError at the worst possible moment."""
    import inspect
    from types import SimpleNamespace

    from src import dispatch, external_worker

    assert dispatch._external_resume_supported() is True
    assert "resume" in inspect.signature(external_worker.run_task).parameters

    import src

    old = SimpleNamespace(run_task=lambda runner_key, task, **kw: {})
    with mock.patch.object(src, "external_worker", old):
        assert dispatch._external_resume_supported() is False


def test_a_resume_handle_survives_the_delegation_parser():
    args = st.parse_delegation_args(json.dumps(
        {"tasks": [{"instruction": "fix it", "resume": {"kind": "session", "id": "abc"}}]}))
    assert args["tasks"][0]["resume"] == {"kind": "session", "id": "abc", "runner": ""}
    run = st.SubagentRun(0, args["tasks"][0])
    assert run.resume_kind == "session" and run.resume_id == "abc"


def test_a_missing_session_degrades_to_a_fresh_worker():
    class _Empty:
        def get_session(self, sid):
            return None
    assert st._session_messages(_Empty(), "gone") == []


def test_a_session_with_history_comes_back_as_loop_messages():
    class _S:
        messages = [type("M", (), {"role": "user", "content": "do it"})(),
                    type("M", (), {"role": "assistant", "content": "done"})(),
                    type("M", (), {"role": "system", "content": "ignored"})()]

    class _SM:
        def get_session(self, sid):
            return _S()
    assert st._session_messages(_SM(), "abc") == [
        {"role": "user", "content": "do it"}, {"role": "assistant", "content": "done"}]


# ── the API ────────────────────────────────────────────────────────────────

def test_the_api_answers_resolved_rules_not_frontmatter(store):
    from routes.agent_def_routes import SHELL_NOTE, _payload
    _write(store, "scoped", GOOD)
    payload = _payload()
    row = next(a for a in payload["agents"] if a["slug"] == "scoped")
    details = [r["detail"] for r in row["rules"]]
    assert any("may use only" in d for d in details)
    assert any(d == "write src/**" for d in details)
    assert any("cannot start another worker" in d for d in details)
    assert payload["shell_note"] == SHELL_NOTE
    assert payload["max_depth"] == perms.max_depth()


def test_the_api_reports_the_files_that_would_not_load(store):
    from routes.agent_def_routes import _payload
    _write(store, "broken", "not a definition\n")
    payload = _payload()
    assert [e["slug"] for e in payload["errors"]] == ["broken"]


def test_the_api_is_admin_only_and_registered():
    app_src = (REPO / "app.py").read_text(encoding="utf-8")
    assert "setup_agent_def_routes" in app_src
    routes_src = (REPO / "routes/agent_def_routes.py").read_text(encoding="utf-8")
    assert routes_src.count("Depends(require_admin)") == 2


# ── the page ───────────────────────────────────────────────────────────────

SRC = (REPO / "static/js/agentDefs.js").read_text(encoding="utf-8")
PURE_START = "// ── Agent definitions: pure helpers"
PURE_END = "// ── Agent definitions: end pure helpers ──"

PAYLOAD = {
    "shell_note": "a path rule governs the file tools.",
    "max_depth": 1,
    "depth_setting": "agent_subagent_depth",
    "agents": [
        {"slug": "reviewer", "name": "reviewer", "mode": "reviewer", "source": "builtin",
         "description": "Reads <everything>, writes nothing.", "may_delegate": False, "caveats": [],
         "rules": [{"effect": "deny", "what": "write", "detail": "write **"}]},
        {"slug": "planner", "name": "planner", "mode": "coordinator", "source": "user",
         "description": "", "may_delegate": True,
         "caveats": ["the path rules do not reach inside bash"],
         "rules": [{"effect": "allow", "what": "delegate", "detail": "may split its work"}]},
    ],
    "errors": [{"path": "/w/.faustus/agents/x.md", "slug": "x", "reason": "mode: `overlord` is not one of"}],
}


def _pure() -> str:
    assert PURE_START in SRC and PURE_END in SRC, "pure-helper markers missing from agentDefs.js"
    region = SRC.split(PURE_START, 1)[1].split(PURE_END, 1)[0]
    return region.split("\n", 1)[1]


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_pure() + "\n" + script,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_page_module_parses_and_is_wired():
    assert subprocess.run(["node", "--check", str(REPO / "static/js/agentDefs.js")],
                          capture_output=True).returncode == 0
    assert "onclick=" not in SRC and "alert(" not in SRC and "window.confirm(" not in SRC
    assert "/api/agent-defs" in SRC
    for entry in ("export async function openDefsPanel", "export function closeDefsPanel",
                  "export async function loadDefs", "export function pickAgent"):
        assert entry in SRC, entry
    index = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "agentDefs.js" in index and "tool-agent-defs-btn" in index


@pytest.mark.skipif(not _HAS_NODE, reason="needs node")
def test_the_page_prints_resolved_rules_and_escapes_them():
    out = _run(f"""
      const html = defsPageHtml({json.dumps(PAYLOAD)}, {{}});
      console.log(JSON.stringify({{
        deny: html.includes('write **'),
        escaped: html.includes('&lt;everything&gt;') && !html.includes('<everything>'),
        caveat: html.includes('do not reach inside bash'),
        shell: html.includes('a path rule governs the file tools.'),
        ceiling: html.includes('depth ceiling 1'),
      }}));
    """)
    assert out == {"deny": True, "escaped": True, "caveat": True, "shell": True, "ceiling": True}


@pytest.mark.skipif(not _HAS_NODE, reason="needs node")
def test_the_page_shows_the_files_that_would_not_load():
    out = _run(f"""
      const html = defsPageHtml({json.dumps(PAYLOAD)}, {{}});
      console.log(JSON.stringify({{
        listed: html.includes('/w/.faustus/agents/x.md'),
        reason: html.includes('overlord'),
        said: html.includes('not in force'),
      }}));
    """)
    assert out == {"listed": True, "reason": True, "said": True}


@pytest.mark.skipif(not _HAS_NODE, reason="needs node")
def test_the_page_keeps_asks_to_delegate_and_may_delegate_apart():
    out = _run("""
      const asks = delegateStatus({slug: 'p', mode: 'coordinator', may_delegate: true}, 1);
      const capped = delegateStatus({slug: 'p', mode: 'coordinator', may_delegate: true}, 0);
      const never = delegateStatus({slug: 'w', mode: 'worker', may_delegate: false}, 1);
      console.log(JSON.stringify({can: asks.can, cappedCan: capped.can, cappedWhy: capped.detail,
                                  neverCan: never.can}));
    """)
    assert out["can"] is True and out["cappedCan"] is False and out["neverCan"] is False
    assert "ceiling is 0" in out["cappedWhy"]


# ── end to end, through the delegation tool ────────────────────────────────

class _Session:
    def __init__(self):
        self.messages = []
        self.folder = ""
        self.mode = ""
        self.headers = None

    def add_message(self, message):
        self.messages.append(message)


class _SM:
    """The session manager, small enough to assert against (mirrors the one in
    tests/test_subagent_board_events.py)."""

    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = _Session()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


@pytest.fixture()
def delegation(tmp_path, monkeypatch):
    """DelegateAgentsTool wired to a fake model route; returns the installer."""
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m",
                            "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(st, "_setting", lambda k, d=None: {
        "agent_subagent_tick_seconds": 0.05, "agent_subagent_stall_seconds": 100,
        "agent_subagent_supervisor": False, "agent_subagent_max_parallel": 4}.get(k, d))

    def _install(loop_fn):
        monkeypatch.setattr(al, "stream_agent_loop", loop_fn)
    _install.sm = sm
    return _install


async def _delegate(payload):
    tool = st.DelegateAgentsTool()

    async def _cb(_):
        return None
    return await tool.execute(json.dumps(payload), {"session_id": "parent", "owner": None,
                                                    "progress_cb": _cb})


@pytest.mark.asyncio
async def test_a_definition_reaches_the_loop_that_runs_the_worker(store, delegation):
    _write(store, "scoped", GOOD)
    seen = {}

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen["messages"] = messages
        seen["disabled"] = set(kwargs.get("disabled_tools") or ())
        seen["max_rounds"] = kwargs.get("max_rounds")
        yield "data: " + json.dumps({"type": "harness_summary",
                                     "data": {"mutations": [], "stop_reason": "complete"}}) + "\n\n"
        yield "data: [DONE]\n\n"
    delegation(_loop)

    result = await _delegate({"tasks": [{"instruction": "fix the parser", "agent": "scoped"}],
                              "timeout_s": 60})
    prompt = seen["messages"][0]["content"]
    assert "Change only what the task names" in prompt          # the body IS the system prompt
    assert "FILES YOU OWN" in prompt and "src/**" in prompt      # the definition's default claims
    assert "Tools you do NOT have" in prompt and "bash" in prompt
    assert seen["max_rounds"] == 9                               # the definition's ceiling
    assert "bash" in seen["disabled"] and "write_file" in seen["disabled"]
    assert "read_file" not in seen["disabled"]
    worker = result["subagents"][0]
    assert worker["agent"] == "scoped"                           # the card can say where it came from
    assert worker["agent_def"]["slug"] == "scoped"
    assert worker["permissions"]["slug"] == "scoped"


@pytest.mark.asyncio
async def test_a_task_with_no_agent_carries_none_of_it(store, delegation):
    seen = {}

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen["messages"] = messages
        yield "data: [DONE]\n\n"
    delegation(_loop)
    result = await _delegate({"tasks": ["fix the parser"], "timeout_s": 60})
    assert "You are a sub-agent working on ONE delegated task" in seen["messages"][0]["content"]
    assert "agent" not in result["subagents"][0] and "permissions" not in result["subagents"][0]


@pytest.mark.asyncio
async def test_resume_continues_the_same_session_with_what_it_already_knew(store, delegation):
    rounds = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        rounds.append({"messages": messages, "session_id": kwargs.get("session_id")})
        yield "data: " + json.dumps({"delta": "did it"}) + "\n\n"
        yield "data: [DONE]\n\n"
    delegation(_loop)

    first = await _delegate({"tasks": ["make total() sum the items"], "timeout_s": 60})
    sid = first["subagents"][0]["session_id"]
    assert sid and first["subagents"][0].get("resumed") is None

    second = await _delegate({"tasks": [{"instruction": "the test still fails",
                                         "resume": {"kind": "session", "id": sid}}],
                              "timeout_s": 60})
    assert rounds[1]["session_id"] == sid                    # the same worker, not a new one
    assert second["subagents"][0]["resumed"] is True
    assert second["subagents"][0]["session_id"] == sid
    # It starts from what it already knew, and the new round is the last turn.
    texts = [m["content"] for m in rounds[1]["messages"]]
    assert any("make total() sum the items" in t for t in texts)
    assert texts[-1].endswith("the test still fails")


@pytest.mark.asyncio
async def test_a_resume_handle_nobody_can_use_falls_back_to_a_fresh_worker(store, delegation):
    rounds = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        rounds.append(kwargs.get("session_id"))
        yield "data: [DONE]\n\n"
    delegation(_loop)
    result = await _delegate({"tasks": [{"instruction": "fix it",
                                         "resume": {"kind": "session", "id": "long-gone"}}],
                              "timeout_s": 60})
    assert rounds[0] != "long-gone"
    assert result["subagents"][0].get("resumed") is None
