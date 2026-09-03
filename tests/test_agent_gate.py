"""Faustus's guard in front of a foreign agent's tools (src/agent_gate.py).

`src/external_worker.py` has always said the one thing it could not do: the
command guard cannot see inside another agent's own shell. For a runner whose
own interface has a binding pre-tool hook, it now can — and everything in this
file is about the difference between *closing* that hole and *claiming* to have
closed it.

So the tests come in two halves. The first half is the policy: every
command-guard tier maps to the decision it should, attended and unattended; a
write outside the workspace is refused and names the root; a write to a file
another worker holds is refused and names the owner; a path that is only
mis-anchored is CORRECTED rather than refused. The second half is the honesty:
a tool the gate has never heard of is allowed **and counted**, a call the
agent's own stream reports without a gate receipt is counted too, and both of
them survive all the way into the proof packet as a narrower — never absent —
uncertainty.

The end-to-end test drives the real listener with the real hook script through
a fake agent, so the HTTP round trip, the fail-closed behaviour and the
stream-json reconciliation are all exercised. What is NOT exercised here is
Claude Code itself: there is no `claude` binary on this machine, so the live
hook contract (that the CLI runs this command, feeds it this payload and obeys
this answer) is taken from its documentation and not from a run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from src import agent_gate as gate
from src import agent_runners as reg
from src import command_guard
from src import external_worker
from src import prove


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A disposable receipt chain and an empty run registry per test."""
    monkeypatch.setattr(command_guard, "DATA_DIR", str(tmp_path / "guard"))
    command_guard._last_hash_cache.clear()
    gate.reset()
    yield
    gate.reset()
    command_guard._last_hash_cache.clear()


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "cart.py").write_text("x = 1\n", encoding="utf-8")
    return ws


def _run(workspace, **kw):
    return gate.open_run("r1", runner="claude", workspace_roots=[str(workspace)],
                         cwd=str(workspace), **kw)


# ── the tiers ───────────────────────────────────────────────────────────────

TIER_CASES = [
    ("ls -la", "SAFE", "allow", "allow"),
    ("echo hi", "SAFE", "allow", "allow"),
    ("docker system prune -a", "CAUTION", "ask", "deny"),
    ("chmod -R 777 /etc", "CAUTION", "ask", "deny"),
    ("rm -rf build", "DANGEROUS", "deny", "deny"),
    ("git push --force", "DANGEROUS", "deny", "deny"),
    ("rm -rf /", "CRITICAL", "deny", "deny"),
    ("mkfs.ext4 /dev/sdb1", "CRITICAL", "deny", "deny"),
]


@pytest.mark.parametrize("command,tier,attended_want,unattended_want", TIER_CASES)
def test_every_command_guard_tier_maps_to_one_decision(command, tier, attended_want,
                                                       unattended_want, workspace):
    """The gate does not classify anything itself: it routes to the guard the
    built-in tools already use and translates the tier it gets back."""
    assert command_guard.classify(command).tier == tier

    watched = gate.judge("Bash", {"command": command}, cwd=str(workspace),
                         workspace_roots=[str(workspace)], run_id="r1", attended=True)
    alone = gate.judge("Bash", {"command": command}, cwd=str(workspace),
                       workspace_roots=[str(workspace)], run_id="r1", attended=False)
    assert watched.decision == attended_want
    assert alone.decision == unattended_want
    assert watched.tier == alone.tier == tier


def test_a_caution_command_says_why_it_was_refused_when_nobody_is_watching(workspace):
    """The reason reaches the foreign agent, so it can do the other thing
    instead of retrying the same call until the run times out."""
    out = gate.judge("Bash", {"command": "docker system prune -a"}, cwd=str(workspace),
                     workspace_roots=[str(workspace)], run_id="r1", attended=False)
    assert out.decision == "deny"
    assert "unattended" in out.reason and "CAUTION" in out.reason


def test_attendedness_comes_from_the_run_not_from_the_call(workspace):
    """The spawner asserts whether there is anyone to ask — the same assertion
    src/tool_approvals.py calls `allow_continuation`. A payload cannot claim it.
    """
    run = _run(workspace, attended=True)
    assert gate.judge("Bash", {"command": "docker system prune -a"}, run=run).decision == "ask"
    run.attended = False
    assert gate.judge("Bash", {"command": "docker system prune -a"}, run=run).decision == "deny"


# ── writes: the workspace, the locks, and the near miss ─────────────────────

def test_a_write_outside_the_workspace_is_refused_and_names_the_root(workspace, tmp_path):
    out = gate.judge("Write", {"file_path": str(tmp_path / "elsewhere.txt"), "content": "x"},
                     cwd=str(workspace), workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny"
    assert str(workspace) in out.reason and "outside the workspace" in out.reason


def test_a_write_to_a_sensitive_path_is_refused(workspace):
    """The same locations Faustus refuses for its OWN file tools."""
    out = gate.judge("Write", {"file_path": str(workspace / ".ssh" / "id_rsa")},
                     cwd=str(workspace), workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny" and "sensitive" in out.reason


def test_a_write_to_another_workers_file_is_refused_and_names_the_owner(workspace):
    from src.agent_tools.subagent_tools import FileLockRegistry

    locks = FileLockRegistry(str(workspace))
    locks.names["sa1-beef"] = "refactorer"
    locks.claim("sa1-beef", [str(workspace / "src" / "cart.py")])

    run = _run(workspace, locks=locks, worker_key="external-1")
    out = gate.judge("Edit", {"file_path": str(workspace / "src" / "cart.py")}, run=run)
    assert out.decision == "deny"
    assert "refactorer" in out.reason


def test_a_worker_may_still_write_the_file_it_owns(workspace):
    from src.agent_tools.subagent_tools import FileLockRegistry

    locks = FileLockRegistry(str(workspace))
    locks.claim("external-1", [str(workspace / "src" / "cart.py")])
    run = _run(workspace, locks=locks, worker_key="external-1")
    assert gate.judge("Edit", {"file_path": str(workspace / "src" / "cart.py")},
                      run=run).decision == "allow"


def test_a_path_written_against_the_wrong_cwd_is_corrected_not_refused(workspace):
    """The `updated_input` case. The agent means the workspace's src/cart.py
    and its cwd is a subdirectory; refusing that would be pedantry."""
    sub = workspace / "src"
    out = gate.judge("Edit", {"file_path": "src/cart.py", "old_string": "x"},
                     cwd=str(sub), workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "allow"
    assert out.updated_input == {"file_path": str(workspace / "src" / "cart.py")}
    assert "re-anchored" in out.reason


def test_the_corrected_call_is_the_one_that_would_run(workspace):
    """`updatedInput` is a PARTIAL merge over the agent's own tool_input: the
    call that runs must be the original arguments with only the path replaced.
    """
    sub = workspace / "src"
    original = {"file_path": "src/cart.py", "old_string": "x = 1", "new_string": "x = 2"}
    out = gate.judge("Edit", original, cwd=str(sub), workspace_roots=[str(workspace)],
                     run_id="r1")
    merged = dict(original, **out.hook_output()["updatedInput"])
    assert merged["file_path"] == str(workspace / "src" / "cart.py")
    assert merged["old_string"] == "x = 1" and merged["new_string"] == "x = 2"
    assert os.path.exists(merged["file_path"])


def test_a_relative_path_that_names_nothing_real_is_refused_not_invented(workspace, tmp_path):
    """Re-anchoring only ever produces a path that exists (or whose directory
    does). A gate that guessed would quietly redirect a write."""
    out = gate.judge("Write", {"file_path": "../../outside/nope.py"},
                     cwd=str(workspace / "src"), workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny" and out.updated_input is None


def test_a_write_whose_target_cannot_be_read_is_refused(workspace):
    out = gate.judge("Write", {"content": "x"}, cwd=str(workspace),
                     workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny" and "target path" in out.reason


# ── the tool the gate has never heard of ────────────────────────────────────

def test_an_unknown_tool_is_allowed_and_recorded_as_unjudged(workspace):
    """The rule that keeps this from becoming a wall: a foreign CLI shipping a
    new tool must not stop working. The rule that keeps it honest: the fact
    that nothing judged it is counted and named."""
    run = _run(workspace)
    out = gate.judge("SomeBrandNewTool", {"anything": 1}, run=run)
    assert out.decision == "allow"
    assert out.judged is False
    assert "does not recognise" in out.reason and "unjudged" in out.reason

    gate.record(run, out, tool_use_id="tu_1")
    led = gate.close_run("r1")
    assert led["unjudged"] == 1 and led["unjudged_tools"] == ["SomeBrandNewTool"]


def test_a_read_only_tool_is_not_counted_as_unjudged(workspace):
    """`Read` is allowed because no policy applies to it, which is a different
    fact from "Faustus did not recognise this" — and pricing them the same
    would make the proof's unjudged count meaningless."""
    run = _run(workspace)
    out = gate.judge("Read", {"file_path": str(workspace / "src" / "cart.py")}, run=run)
    assert out.decision == "allow" and out.judged is True
    gate.record(run, out, tool_use_id="tu_1")
    assert gate.ledger_of("r1")["unjudged"] == 0


def test_the_unjudged_count_reaches_the_proof_packet(workspace):
    run = _run(workspace)
    gate.record(run, gate.judge("WebFetch", {"url": "http://x"}, run=run), tool_use_id="a")
    gate.record(run, gate.judge("Bash", {"command": "ls -la"}, run=run), tool_use_id="b")
    led = gate.close_run("r1")

    packet = prove.prove({"source": "checkpoint", "added": ["a.py"]},
                         {"ran": True, "ok": True}, {"paths": ["a.py"]})
    out = prove.note_external_gate(packet, ["claude"], gates={"claude": led})
    kinds = [u["kind"] for u in out["uncertainty"]]
    assert prove.EXTERNAL_TOOLS_UNJUDGED in kinds
    assert prove.EXTERNAL_UNGUARDED not in kinds
    assert "WebFetch" in out["uncertainty"][0]["detail"]


# ── the receipts ────────────────────────────────────────────────────────────

def test_every_decision_lands_in_the_hash_chained_receipt_log(workspace):
    run = _run(workspace)
    for tool, args in (("Bash", {"command": "rm -rf /"}),
                       ("Bash", {"command": "ls -la"}),
                       ("NeverHeardOfIt", {})):
        decision = gate.judge(tool, args, run=run)
        gate.record(run, decision, tool_use_id=tool, command=args.get("command", ""))
    rows = command_guard.tail_receipts(50)
    assert [r["action"] for r in rows] == ["blocked", "allowed", "unjudged"]
    assert all(r["session"] == "agent-gate:r1" for r in rows)
    assert command_guard.verify_chain()["ok"] is True


def test_a_tampered_receipt_breaks_the_chain(workspace):
    run = _run(workspace)
    gate.record(run, gate.judge("Bash", {"command": "rm -rf /"}, run=run), command="rm -rf /")
    gate.record(run, gate.judge("Bash", {"command": "ls -la"}, run=run), command="ls -la")
    path = command_guard._log_path()
    rows = [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]
    rows[0]["action"] = "allowed"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    assert command_guard.verify_chain()["ok"] is False


# ── failing closed, failing open, and the budget ────────────────────────────

def test_an_internal_error_denies_a_destructive_call(workspace, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("the classifier fell over")

    monkeypatch.setattr(command_guard, "classify", boom)
    out = gate.judge("Bash", {"command": "ls -la"}, cwd=str(workspace),
                     workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny" and "could not judge" in out.reason


def test_an_internal_error_on_a_write_denies_too(workspace, monkeypatch):
    class Exploding:
        def blocked_by(self, *_a):
            raise RuntimeError("registry gone")

        def label(self, key):
            return key

    run = _run(workspace, locks=Exploding(), worker_key="w")
    out = gate.judge("Write", {"file_path": str(workspace / "src" / "cart.py")}, run=run)
    assert out.decision == "deny"


def test_an_internal_error_on_anything_else_fails_open_and_is_unjudged(workspace, monkeypatch):
    """A crash in the gate must not turn a foreign CLI's `Read` into an error
    the user has to debug — but the run does not get to call it judged."""
    import src.agent_gate as module

    monkeypatch.setattr(module, "READ_ONLY_TOOLS", None)   # `in` on None raises
    out = gate.judge("Read", {"file_path": "x"}, cwd=str(workspace),
                     workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "allow" and out.judged is False


def test_a_decision_stays_inside_its_budget(workspace):
    started = time.perf_counter()
    for _ in range(50):
        gate.judge("Bash", {"command": "rm -rf /tmp/x && docker system prune -a"},
                   cwd=str(workspace), workspace_roots=[str(workspace)], run_id="r1")
    per_call_ms = (time.perf_counter() - started) * 1000.0 / 50
    assert per_call_ms < gate.BUDGET_MS


def test_a_slow_judgement_of_a_destructive_call_is_refused(workspace, monkeypatch):
    """Over budget on the shape that can destroy something: the answer this
    took too long to reach is not one to act on."""
    real = command_guard.classify

    def slow(*a, **kw):
        time.sleep((gate.BUDGET_MS + 20) / 1000.0)
        return real(*a, **kw)

    monkeypatch.setattr(command_guard, "classify", slow)
    out = gate.judge("Bash", {"command": "ls -la"}, cwd=str(workspace),
                     workspace_roots=[str(workspace)], run_id="r1")
    assert out.decision == "deny" and "budget" in out.reason


# ── the token ───────────────────────────────────────────────────────────────

def test_a_wrong_token_is_not_found(workspace):
    _run(workspace)
    status, _ = gate.handle_hook("not-the-token", {"tool_name": "Read"})
    assert status == 404


def test_an_expired_token_is_not_found(workspace):
    run = gate.open_run("r1", workspace_roots=[str(workspace)], ttl_s=-1)
    status, _ = gate.handle_hook(run.token, {"tool_name": "Read"})
    assert status == 404


def test_a_finished_runs_token_is_not_found(workspace):
    run = _run(workspace)
    assert gate.handle_hook(run.token, {"tool_name": "Read"})[0] == 200
    gate.close_run("r1")
    assert gate.handle_hook(run.token, {"tool_name": "Read"})[0] == 404


def test_the_token_is_not_the_apps_internal_token(workspace):
    from core.middleware import INTERNAL_TOOL_TOKEN

    run = _run(workspace)
    assert run.token != INTERNAL_TOOL_TOKEN
    assert len(run.token) >= 40
    assert gate.handle_hook(INTERNAL_TOOL_TOKEN, {"tool_name": "Read"})[0] == 404


def test_re_registering_a_run_id_revokes_the_old_token(workspace):
    """A token that outlived the run it was minted for would be the one thing
    this registry must never allow."""
    first = _run(workspace)
    second = _run(workspace)
    assert first.token != second.token
    assert gate.handle_hook(first.token, {"tool_name": "Read"})[0] == 404
    assert gate.handle_hook(second.token, {"tool_name": "Read"})[0] == 200


def test_two_runs_get_different_tokens(workspace):
    a = gate.open_run("r1", workspace_roots=[str(workspace)])
    b = gate.open_run("r2", workspace_roots=[str(workspace)])
    assert a.token != b.token


def test_a_caller_that_is_not_on_loopback_is_refused(workspace):
    run = _run(workspace)
    status, body = gate.handle_hook(run.token, {"tool_name": "Read"}, client_host="10.0.0.9")
    assert status == 403 and "loopback" in body["error"]


def test_the_apps_own_model_cannot_reach_the_gate(workspace):
    """`app_api` loops back carrying the internal-tool header. A request
    bearing it is Faustus's own model asking the gate about its own guard."""
    run = _run(workspace)
    status, body = gate.handle_hook(run.token, {"tool_name": "Read"}, internal_token_seen=True)
    assert status == 403 and "internal tool bridge" in body["error"]


def test_a_runaway_agent_is_rate_limited_and_the_run_records_it(workspace):
    run = _run(workspace)
    statuses = {gate.handle_hook(run.token, {"tool_name": "Read"})[0] for _ in range(600)}
    assert 429 in statuses
    assert run.throttled > 0
    # Throttling denies rather than allows: flooding the endpoint must not be
    # the way through it.
    _, body = gate.handle_hook(run.token, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── the hook answer's shape ─────────────────────────────────────────────────

def test_the_answer_is_the_hook_shape_claude_code_expects(workspace):
    run = _run(workspace)
    _, body = gate.handle_hook(run.token, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
                                           "tool_use_id": "tu_9", "cwd": str(workspace)})
    out = body["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"]
    assert "updatedInput" not in out


def test_a_cwd_the_agent_claims_is_only_used_when_it_is_inside_the_workspace(workspace,
                                                                             tmp_path):
    """The payload's `cwd` comes from the party being judged. Trusting one
    that points outside the run's roots would let a relative path be
    re-anchored against a directory this run was never given."""
    run = _run(workspace)
    _, body = gate.handle_hook(run.token, {"tool_name": "Write",
                                           "tool_input": {"file_path": "escape.txt"},
                                           "cwd": str(tmp_path)})
    out = body["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    # Anchored on the RUN's workspace, not on the directory the payload named.
    assert "updatedInput" not in out
    assert str(tmp_path / "escape.txt") not in json.dumps(body)


def test_a_correction_travels_as_updated_input(workspace):
    run = _run(workspace)
    _, body = gate.handle_hook(run.token, {"tool_name": "Edit",
                                           "tool_input": {"file_path": "src/cart.py"},
                                           "cwd": str(workspace / "src")})
    out = body["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["file_path"] == str(workspace / "src" / "cart.py")


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture()
def route_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import agent_gate_routes

    app = FastAPI()
    app.include_router(agent_gate_routes.setup_agent_gate_routes())
    # A direct loopback caller — what a hook on this machine looks like.
    return TestClient(app, client=("127.0.0.1", 51234))


def test_the_route_answers_the_same_policy(route_client, workspace):
    run = _run(workspace)
    r = route_client.post(f"/api/agent-gate/{run.token}",
                          json={"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
    assert r.status_code == 200
    assert r.json()["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_route_404s_an_unknown_token(route_client, workspace):
    _run(workspace)
    assert route_client.post("/api/agent-gate/nope", json={"tool_name": "Read"}).status_code == 404


def test_the_route_refuses_a_proxied_request(route_client, workspace):
    """A forwarding header means the loopback client address is the proxy's."""
    run = _run(workspace)
    r = route_client.post(f"/api/agent-gate/{run.token}", json={"tool_name": "Read"},
                          headers={"X-Forwarded-For": "10.0.0.9"})
    assert r.status_code == 403


def test_the_route_refuses_the_internal_tool_header(route_client, workspace):
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    run = _run(workspace)
    r = route_client.post(f"/api/agent-gate/{run.token}", json={"tool_name": "Read"},
                          headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})
    assert r.status_code == 403


def test_the_route_is_not_in_the_openapi_surface(route_client):
    """The model-facing bridge lists endpoints from OpenAPI. The gate is not
    an endpoint a model may discover, let alone call."""
    paths = route_client.get("/openapi.json").json().get("paths") or {}
    assert not any("agent-gate" in p for p in paths)


# ── the catalogue's gate descriptor ─────────────────────────────────────────

def test_only_a_verified_runner_claims_a_gate():
    """The table's fourth rule. `codex` documents something gate-shaped and
    still says `none`, because nobody here has verified it against a binary —
    the same discipline that makes an unverified licence say `unknown`."""
    rows = {r.key: r for r in reg.runners(help_source="")}
    assert rows["claude"].gate == "hook"
    assert rows["codex"].gate == "none"
    assert {r.gate for r in rows.values()} <= set(reg.GATES)
    assert [k for k, r in rows.items() if r.gate != "none"] == ["claude"]


def test_a_gated_row_carries_a_narrower_note_than_an_ungated_one():
    rows = {r.key: r for r in reg.runners(help_source="")}
    assert reg.gate_note(rows["codex"]) == reg.GUARD_NOTE
    assert reg.gate_note(rows["claude"]) == reg.GATED_NOTE
    assert "does not see" in reg.GUARD_NOTE
    assert "children the gate does not see" in reg.GATED_NOTE


def test_an_ungated_invocation_is_byte_identical_to_what_it_always_was():
    claude = reg.get("claude", help_source="")
    assert reg.build_argv(claude, "do it", model="m") == ["claude", "-p", "do it", "--model", "m"]


def test_the_gated_invocation_streams_and_never_skips_permissions():
    claude = reg.get("claude", help_source="")
    argv = reg.build_argv(claude, "do it", model="m", settings='{"hooks":{}}')
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv                      # stream-json requires it
    assert argv[argv.index("--settings") + 1] == '{"hooks":{}}'


def test_no_shipped_row_can_ever_skip_permissions():
    for runner in reg.runners(help_source=""):
        tokens = list(runner.argv) + list(runner.gate_argv)
        assert "--dangerously-skip-permissions" not in tokens


def test_the_settings_json_is_well_formed_and_carries_no_credential(workspace):
    run = _run(workspace)
    claude = reg.get("claude", help_source="")
    settings = reg.hook_settings(claude, command="/usr/bin/python3 /tmp/gatehook.py")
    parsed = json.loads(settings)
    hooks = parsed["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "*"
    assert hooks[0]["hooks"][0]["type"] == "command"
    # The credential travels in the environment, not on the command line: the
    # settings string is shown to the user in `argv_shown`.
    assert run.token not in settings
    for word in ("token", "secret", "key", "password", "api"):
        assert word not in settings.lower()


def test_a_runner_with_no_hook_gate_gets_no_settings():
    codex = reg.get("codex", help_source="")
    assert reg.hook_settings(codex, command="python hook.py") == ""


# ── prove: what changed, and what did not ───────────────────────────────────

def _packet():
    return prove.prove({"source": "checkpoint", "added": ["a.py"]},
                       {"ran": True, "ok": True}, {"paths": ["a.py"]})


def test_an_ungated_run_still_says_exactly_what_it_always_said():
    out = prove.note_external_gate(_packet(), ["qwen"])
    entry = out["uncertainty"][0]
    assert entry["kind"] == prove.EXTERNAL_UNGUARDED
    assert entry["detail"] == prove.EXTERNAL_UNGUARDED_DETAIL + " (qwen)"
    assert out["confidence"] == 0.9
    assert out["unguarded_runners"] == ["qwen"]


def test_a_fully_gated_run_drops_the_uncertainty_and_records_what_the_gate_saw():
    led = {"gated": True, "runner": "claude", "calls": 11, "denied": 2, "unjudged": 0, "unseen": 0}
    out = prove.note_external_gate(_packet(), ["claude"], gates={"claude": led})
    assert [u["kind"] for u in out["uncertainty"]] == []
    assert out["confidence"] == 1.0
    assert out["external_gate"] == {"gated": ["claude"], "unguarded": [], "judged": 11,
                                    "denied": 2, "unjudged": 0, "unseen": 0}
    assert any(o["kind"] == "external_gate" for o in out["observations"])
    assert "unguarded_runners" not in out


def test_a_gated_run_with_something_unjudged_keeps_a_narrower_uncertainty():
    led = {"gated": True, "runner": "claude", "calls": 11, "denied": 2, "unjudged": 3,
           "unjudged_tools": ["WebFetch", "Task"], "unseen": 0}
    out = prove.note_external_gate(_packet(), ["claude"], gates={"claude": led})
    entry = out["uncertainty"][0]
    assert entry["kind"] == prove.EXTERNAL_TOOLS_UNJUDGED
    assert "WebFetch" in entry["detail"] and "Task" in entry["detail"]
    # Narrower, and it costs less than the blanket entry — otherwise a reader
    # has no reason to prefer a gated run.
    assert out["confidence"] > 0.9
    assert prove.PENALTY[prove.EXTERNAL_TOOLS_UNJUDGED] < prove.PENALTY[prove.EXTERNAL_UNGUARDED]


def test_a_call_the_stream_reports_without_a_receipt_is_an_uncertainty_too():
    led = {"gated": True, "runner": "claude", "calls": 4, "denied": 0, "unjudged": 0,
           "unseen": 2, "unseen_tools": ["Bash"]}
    out = prove.note_external_gate(_packet(), ["claude"], gates={"claude": led})
    assert out["uncertainty"][0]["kind"] == prove.EXTERNAL_TOOLS_UNJUDGED
    assert "Bash" in out["uncertainty"][0]["detail"]
    assert out["external_gate"]["unseen"] == 2


def test_a_job_that_mixed_a_gated_and_an_ungated_agent_says_both():
    led = {"gated": True, "runner": "claude", "calls": 4, "denied": 0, "unjudged": 1,
           "unjudged_tools": ["WebFetch"], "unseen": 0}
    out = prove.note_external_gate(_packet(), ["claude", "qwen"], gates={"claude": led})
    kinds = [u["kind"] for u in out["uncertainty"]]
    assert prove.EXTERNAL_UNGUARDED in kinds and prove.EXTERNAL_TOOLS_UNJUDGED in kinds
    assert out["unguarded_runners"] == ["qwen"]
    # Heaviest first, the way prove sorts every other list.
    assert kinds == [prove.EXTERNAL_UNGUARDED, prove.EXTERNAL_TOOLS_UNJUDGED]


def test_a_job_with_no_external_agent_is_untouched():
    packet = _packet()
    assert prove.note_external_gate(dict(packet), []) == packet


def test_the_note_is_never_added_twice():
    once = prove.note_external_gate(_packet(), ["qwen"])
    twice = prove.note_external_gate(dict(once), ["qwen"])
    assert twice["confidence"] == once["confidence"]
    assert len(twice["uncertainty"]) == len(once["uncertainty"])


# ── the stream the CLI writes about itself ──────────────────────────────────

STREAM = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Looking at the cart."},
        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "src/cart.py"}}]}},
    {"type": "assistant", "parent_tool_use_id": "tu_parent",
     "message": {"content": [
         {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
     "total_cost_usd": 0.0412, "session_id": "s1", "result": "Added apply_tax."},
]


def test_the_stream_becomes_readable_text_and_an_inventory():
    stream = external_worker._Stream()
    shown = "".join(stream.feed(json.dumps(e) + "\n") for e in STREAM)
    assert "Looking at the cart." in shown
    assert "→ Read(src/cart.py)" in shown
    assert "→ Bash(pytest -q)" in shown
    assert "Added apply_tax." in shown
    assert [c["name"] for c in stream.tool_calls] == ["Read", "Bash"]
    assert stream.result["total_cost_usd"] == 0.0412


def test_a_nested_subagents_work_is_attributed_to_it():
    stream = external_worker._Stream()
    shown = "".join(stream.feed(json.dumps(e) + "\n") for e in STREAM)
    nested = [c for c in stream.tool_calls if c["parent_tool_use_id"]]
    assert [c["name"] for c in nested] == ["Bash"]
    assert "↳[u_parent]" in shown          # the tail of the parent tool_use id


def test_a_line_that_is_not_json_is_shown_as_it_came():
    stream = external_worker._Stream()
    assert stream.feed("Error: something went wrong\n") == "Error: something went wrong\n"
    assert stream.unparsed == 1


def test_a_call_the_gate_never_saw_is_counted_not_assumed():
    stream = external_worker._Stream()
    for event in STREAM:
        stream.feed(json.dumps(event))
    led = external_worker._reconcile(
        {"gated": True, "calls": 1, "unjudged": 0, "judged_ids": {"tu_1"}}, stream)
    assert led["stream_tool_calls"] == 2
    assert led["subagent_tool_calls"] == 1
    assert led["unseen"] == 1 and led["unseen_tools"] == ["Bash"]
    assert led["result"]["total_cost_usd"] == 0.0412


def test_a_run_whose_every_call_has_a_receipt_reports_nothing_unseen():
    stream = external_worker._Stream()
    for event in STREAM:
        stream.feed(json.dumps(event))
    led = external_worker._reconcile(
        {"gated": True, "calls": 2, "unjudged": 0, "judged_ids": {"tu_1", "tu_2"}}, stream)
    assert led["unseen"] == 0 and "unseen_tools" not in led


# ── end to end: the real listener, the real hook script ─────────────────────

FAKE_AGENT = '''
import json, os, subprocess, sys

# The hook command comes out of the --settings JSON Faustus built, exactly
# where the real CLI reads it from: nothing about the wiring is short-circuited
# for the test.
SETTINGS = json.loads(sys.argv[sys.argv.index("--settings") + 1])
HOOK = SETTINGS["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
CALLS = json.loads(os.environ["FAKE_AGENT_CALLS"])


def ask(call):
    """Exactly what Claude Code does: run the hook command, feed it the
    payload on stdin, read its JSON answer from stdout."""
    proc = subprocess.run(HOOK, shell=True, input=json.dumps(call), capture_output=True,
                          text=True)
    return json.loads(proc.stdout)["hookSpecificOutput"]


def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()


for i, call in enumerate(CALLS):
    answer = ask(call)
    if answer["permissionDecision"] == "deny":
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "refused: " + answer["permissionDecisionReason"][:60]}]}})
        continue
    args = dict(call["tool_input"], **(answer.get("updatedInput") or {}))
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call["tool_use_id"], "name": call["tool_name"],
         "input": args}]}})
    if call["tool_name"] == "Write":
        open(args["file_path"], "w").write(args.get("content", ""))

emit({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
      "result": "done"})
'''


def _fake_claude(tmp_path, calls):
    """A runner row that runs a script speaking Claude Code's hook + stream
    protocol. Everything Faustus owns is the real thing; only the CLI is a
    double, because there is no `claude` on this machine."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    return reg.Runner(
        key="claude", label="Fake Claude Code", kind="cli", licence="subscription",
        argv=(sys.executable, str(script)), detect=(sys.executable,),
        gate="hook",
        gate_argv=("--output-format", "stream-json", "--verbose", "--settings", "{settings}"),
        notes="a test double",
    ), json.dumps(calls)


def test_a_gated_run_judges_every_call_and_the_agent_obeys(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 60)
    calls = [
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "tool_use_id": "tu_1"},
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "tool_use_id": "tu_2"},
        {"tool_name": "Write", "tool_input": {"file_path": "made.txt", "content": "hello"},
         "tool_use_id": "tu_3"},
        {"tool_name": "TotallyNewTool", "tool_input": {}, "tool_use_id": "tu_4"},
    ]
    runner, payload = _fake_claude(tmp_path, calls)
    result = external_worker.run_task(
        runner, "make it so", workspace=str(workspace), workspace_roots=[str(workspace)],
        run_id="e2e", env=dict(os.environ, FAKE_AGENT_CALLS=payload))

    assert result["ok"] is True, result
    assert result["unguarded"] is False
    led = result["gate"]
    assert led["gated"] is True
    assert led["calls"] == 4
    assert led["denied"] == 1                      # `rm -rf /`
    assert led["unjudged"] == 1                    # TotallyNewTool
    assert led["unjudged_tools"] == ["TotallyNewTool"]
    assert led["unseen"] == 0                      # every stream call had a receipt
    assert result["total_cost_usd"] == 0.01
    # The refusal was BINDING: the agent reported it instead of doing it.
    assert "refused:" in result["output_tail"]
    # …and the allowed write really happened, re-anchored into the workspace.
    assert (workspace / "made.txt").read_text(encoding="utf-8") == "hello"


def test_the_hook_refuses_when_the_gate_cannot_be_reached(tmp_path):
    """Fail closed. If killing Faustus were the way to get an unguarded agent,
    the gate would be decoration."""
    script = gate.write_hook_script(tmp_path)
    env = dict(os.environ, FAUSTUS_AGENT_GATE_URL="http://127.0.0.1:1",
               FAUSTUS_AGENT_GATE_TOKEN="anything")
    proc = subprocess.run([sys.executable, script], input='{"tool_name":"Bash"}',
                          capture_output=True, text=True, env=env, timeout=60)
    answer = json.loads(proc.stdout)["hookSpecificOutput"]
    assert answer["permissionDecision"] == "deny"
    assert "could not be reached" in answer["permissionDecisionReason"]


def test_the_hook_talks_to_the_real_listener(tmp_path, workspace):
    run = _run(workspace)
    server = gate.GateServer(run).start()
    try:
        script = gate.write_hook_script(tmp_path)
        env = dict(os.environ, FAUSTUS_AGENT_GATE_URL=server.base_url,
                   FAUSTUS_AGENT_GATE_TOKEN=run.token)
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
                              "tool_use_id": "tu_1"})
        proc = subprocess.run([sys.executable, script], input=payload, capture_output=True,
                              text=True, env=env, timeout=60)
        answer = json.loads(proc.stdout)["hookSpecificOutput"]
        assert answer["permissionDecision"] == "deny"
        assert gate.ledger_of("r1")["denied"] == 1
    finally:
        server.close()


def test_the_listener_refuses_a_get(tmp_path, workspace):
    """The gate answers questions about calls; it never reports what it has
    decided back to the process it is deciding about."""
    import urllib.error
    import urllib.request

    run = _run(workspace)
    server = gate.GateServer(run).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{server.base_url}/{run.token}", timeout=10)
        assert caught.value.code == 405
    finally:
        server.close()


def test_a_gated_runner_that_cannot_start_its_gate_does_not_run_at_all(tmp_path, workspace,
                                                                       monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    runner, _ = _fake_claude(tmp_path, [])
    monkeypatch.setattr(gate, "GateServer", lambda run: (_ for _ in ()).throw(OSError("no port")))
    result = external_worker.run_task(runner, "do it", workspace=str(workspace),
                                      workspace_roots=[str(workspace)], run_id="nogate")
    assert result["ok"] is False
    assert "gate could not be started" in result["error"]
    assert result["unguarded"] is True


def test_the_gate_token_is_starred_out_of_the_command_that_is_shown(tmp_path, workspace,
                                                                    monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 60)
    runner, _ = _fake_claude(tmp_path, [])
    result = external_worker.run_task(
        runner, "do it", workspace=str(workspace), workspace_roots=[str(workspace)],
        run_id="shown", env=dict(os.environ, FAKE_AGENT_CALLS="[]"))
    shown = result["argv_shown"]
    assert "FAUSTUS_AGENT_GATE_TOKEN=" in shown and external_worker.REDACTED in shown
    assert "FAUSTUS_AGENT_GATE_URL=http://127.0.0.1:" in shown
    assert result["gate"]["gated"] is True


def test_an_ungated_runner_is_exactly_what_it_was(tmp_path, workspace, monkeypatch):
    """The regression that matters most to everyone not using Claude Code: a
    row with `gate: "none"` runs, and reports, byte-identically."""
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 60)
    script = tmp_path / "plain.py"
    script.write_text("print('hello')\n", encoding="utf-8")
    runner = reg.Runner(key="qwen", label="Fake Qwen", kind="cli", licence="open",
                        argv=(sys.executable, str(script)), detect=(sys.executable,))
    result = external_worker.run_task(runner, "do it", workspace=str(workspace))
    assert result["ok"] is True
    assert result["unguarded"] is True
    assert result["guard_note"] == reg.GUARD_NOTE
    assert "gate" not in result
    assert "hello" in result["output_tail"]


def test_the_hook_script_is_stdlib_only_and_compiles():
    """It runs under whatever interpreter Faustus runs under, in a foreign
    agent's process, with nothing installed."""
    compile(gate.HOOK_SCRIPT, "hook", "exec")
    imports = {line.split()[1] for line in gate.HOOK_SCRIPT.splitlines()
               if line.startswith("import ")}
    assert imports == {"json", "os", "sys", "urllib.error", "urllib.request"}
    # An http_proxy in the environment must not send this run's tool calls to
    # somebody else.
    assert "ProxyHandler({})" in gate.HOOK_SCRIPT


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def test_the_hook_script_is_not_left_behind(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 60)
    runner, _ = _fake_claude(tmp_path, [])
    before = set(os.listdir(tempfile_dir()))
    external_worker.run_task(runner, "do it", workspace=str(workspace),
                             workspace_roots=[str(workspace)], run_id="tidy",
                             env=dict(os.environ, FAKE_AGENT_CALLS="[]"))
    after = set(os.listdir(tempfile_dir()))
    assert not [d for d in (after - before) if d.startswith("faustus-gate-")]


def test_a_finished_run_leaves_no_live_token(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 60)
    runner, _ = _fake_claude(tmp_path, [])
    external_worker.run_task(runner, "do it", workspace=str(workspace),
                             workspace_roots=[str(workspace)], run_id="dead",
                             env=dict(os.environ, FAKE_AGENT_CALLS="[]"))
    assert gate.ledger_of("dead") is None
    assert gate.recent_ledgers()[-1]["run_id"] == "dead"


def test_there_is_no_way_to_turn_the_gate_off():
    """The caller here is a language model. A safeguard a model can switch off
    is not a safeguard, so `run_task` has no parameter that disables the gate
    and no setting or environment variable does either."""
    import inspect

    names = set(inspect.signature(external_worker.run_task).parameters)
    assert not {n for n in names if "gate" in n or "skip" in n or "bypass" in n}
    source = textwrap.dedent(inspect.getsource(external_worker))
    for switch in ("FAUSTUS_GATE_OFF", "skip_gate", "no_gate", "disable_gate"):
        assert switch not in source
