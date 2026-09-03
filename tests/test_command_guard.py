"""Destructive command guard: classifier, allowlist, receipts, enforcement,
and the sealed approved-replay path (dcg + slb mechanics)."""

import asyncio
import hashlib
import json
import os
from collections import namedtuple

import pytest

from src import command_guard
from src import tool_capabilities as tc
from src.tool_capabilities import ToolRunSecurityContext


ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    """Isolate the guard's store/log and reset all module-level state."""
    monkeypatch.setattr(command_guard, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv(command_guard.ONE_SHOT_ENV, raising=False)
    command_guard._last_hash_cache.clear()
    command_guard._one_shot_consumed.clear()
    tc._reset_command_guard_cache()
    yield tmp_path
    command_guard._last_hash_cache.clear()
    command_guard._one_shot_consumed.clear()
    tc._reset_command_guard_cache()


def _set_mode(monkeypatch, mode, packs="all"):
    values = {
        "agent_command_guard_mode": mode,
        "agent_command_guard_packs": packs,
    }
    monkeypatch.setattr(
        tc, "get_setting", lambda key, default=None: values.get(key, default)
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

CLASSIFIER_CASES = [
    ("git push --force-with-lease", "SAFE"),
    ("git push --force", "DANGEROUS"),
    ("git push -f origin main", "DANGEROUS"),
    ("rm -rf /", "CRITICAL"),
    ("rm -rf ~", "CRITICAL"),
    ("rm -rf C:\\", "CRITICAL"),
    ("rm -rf build/", "DANGEROUS"),
    ("rm notes.txt", "CAUTION"),
    ("ls -la", "SAFE"),
    ("pytest -q", "SAFE"),
    ("python -m pytest tests -q", "SAFE"),
    ("git status", "SAFE"),
    ("git status && rm -rf /", "CRITICAL"),
    ("git reset --hard HEAD~1", "DANGEROUS"),
    ("git clean -fdx", "DANGEROUS"),
    ("git branch -D feature", "CAUTION"),
    ("git stash drop", "CAUTION"),
    ("git checkout -- .", "CAUTION"),
    ("git filter-branch --force --all", "DANGEROUS"),
    ("DELETE FROM t WHERE id=1", "CAUTION"),
    ("DELETE FROM t", "DANGEROUS"),
    ("psql <<EOSQL\nDROP TABLE users;\nEOSQL", "DANGEROUS"),
    ("Remove-Item -Recurse -Force build", "DANGEROUS"),
    ("dd if=x of=/dev/sda", "CRITICAL"),
    (":(){ :|:& };:", "CRITICAL"),
    ("mkfs.ext4 /dev/sdb1", "CRITICAL"),
    ("kubectl delete pod x", "DANGEROUS"),
    ("kubectl delete pods --all", "CRITICAL"),
    ("docker system prune", "CAUTION"),
    ("docker system prune -a --volumes", "DANGEROUS"),
    ("docker-compose down -v", "DANGEROUS"),
    ("shutdown -h now", "DANGEROUS"),
    ("kill -9 1234", "CAUTION"),
    ("taskkill /f /im chrome.exe", "CAUTION"),
    ("reg delete HKLM\\Software\\X /f", "DANGEROUS"),
    ("find . -name '*.tmp' -delete", "DANGEROUS"),
    ("find . -name '*.py'", "SAFE"),
    ("mv secrets.txt /dev/null", "DANGEROUS"),
    ("echo hello > out.txt", "SAFE"),
    ("grep -rf pattern .", "SAFE"),
    ("cat file.txt", "SAFE"),
    ("curl https://example.com", "SAFE"),
    ("Set-ExecutionPolicy Bypass", "CAUTION"),
    ("Stop-Computer", "DANGEROUS"),
    ("", "SAFE"),
]


@pytest.mark.parametrize("cmd,expected", CLASSIFIER_CASES)
def test_classifier_table(cmd, expected):
    decision = command_guard.classify(cmd)
    assert decision.tier == expected, (
        f"{cmd!r}: got {decision.tier} via {decision.rule_id} "
        f"({decision.matched!r}), wanted {expected}"
    )


def test_whitelist_hits_record_their_rule():
    decision = command_guard.classify("ls -la")
    assert decision.tier == "SAFE"
    assert decision.pack == "whitelist"
    assert any("whitelist" in step for step in decision.trace)


def test_force_with_lease_is_not_the_force_rule():
    decision = command_guard.classify("git push --force-with-lease origin main")
    assert decision.tier == "SAFE"
    assert decision.rule_id in ("", "whitelist." + decision.rule)


def test_inline_python_body_is_scanned():
    decision = command_guard.classify(
        "python -c \"import shutil; shutil.rmtree('x')\""
    )
    assert decision.tier == "DANGEROUS"
    assert decision.rule_id == "fs.rmtree"


def test_bash_dash_c_body_is_scanned():
    assert command_guard.classify("bash -c 'rm -rf /'").tier == "CRITICAL"


def test_heredoc_body_is_scanned():
    decision = command_guard.classify(
        "mysql app <<'SQL'\nDROP TABLE users;\nSQL"
    )
    assert decision.tier == "DANGEROUS"
    assert decision.pack == "db"


def test_python_tool_string_literals_are_scanned():
    decision = command_guard.classify_tool(
        "python",
        "import subprocess\nsubprocess.run('rm -rf /tmp/cache', shell=True)\n",
    )
    assert decision.tier == "DANGEROUS"


def test_packs_can_be_narrowed():
    only_git = command_guard.classify("rm -rf build/", packs={"git"})
    assert only_git.tier == "SAFE"
    assert command_guard.classify("rm -rf build/", packs={"fs"}).tier == "DANGEROUS"
    # An unknown pack selection falls back to everything rather than nothing.
    assert command_guard.packs_from_setting("nonsense") == command_guard.ALL_PACKS
    assert command_guard.packs_from_setting("fs,git") == frozenset({"fs", "git"})
    assert command_guard.packs_from_setting("all") == command_guard.ALL_PACKS


def test_budget_exceeded_fails_open(monkeypatch):
    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr(command_guard, "_now", lambda: next(ticks, 99.0))
    decision = command_guard.classify("rm -rf build/", budget_ms=50.0)
    assert decision.fail_open is True
    assert decision.tier == "SAFE"  # nothing matched before the clock ran out
    assert any("budget exceeded" in step for step in decision.trace)


def test_classify_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("internal classifier bug")

    monkeypatch.setattr(command_guard, "_classify_impl", boom)
    decision = command_guard.classify("rm -rf /")
    assert decision.tier == "SAFE"
    assert decision.fail_open is True
    assert any("internal error" in step for step in decision.trace)


def test_explain_reports_trace_and_counters():
    report = command_guard.explain("rm -rf /")
    assert report["tier"] == "CRITICAL"
    assert report["rule_id"] == "fs.rm_root"
    assert report["rules_tested"] > 0
    assert isinstance(report["trace"], list) and report["trace"]


# ---------------------------------------------------------------------------
# Allowlist + one-shot bypass
# ---------------------------------------------------------------------------

def test_allowlist_exact_prefix_and_expiry(guard_env, monkeypatch):
    command_guard.add_allowlist_entry(
        "rm -rf build/", kind="exact", reason="CI artifact dir", added_by="alice"
    )
    command_guard.add_allowlist_entry(
        "git push --force origin scratch/", kind="prefix", reason="scratch branches"
    )
    assert command_guard.is_allowlisted("rm   -rf   build/") is not None  # normalized
    assert command_guard.is_allowlisted("rm -rf build/x") is None  # exact means exact
    assert command_guard.is_allowlisted(
        "git push --force origin scratch/feature-1"
    ) is not None
    assert command_guard.is_allowlisted("git push --force origin main") is None

    entry = command_guard.add_allowlist_entry(
        "docker system prune -a --volumes", kind="exact", ttl_hours=1
    )
    assert entry["expires_at"] is not None
    assert command_guard.is_allowlisted("docker system prune -a --volumes") is not None
    # Jump past the TTL: the entry is ignored and pruned on the next save.
    real_now = command_guard._utcnow()
    from datetime import timedelta
    monkeypatch.setattr(command_guard, "_utcnow", lambda: real_now + timedelta(hours=2))
    assert command_guard.is_allowlisted("docker system prune -a --volumes") is None
    command_guard.add_allowlist_entry("ls", kind="exact")  # triggers a save/prune
    patterns = [e["pattern"] for e in command_guard.list_allowlist()]
    assert "docker system prune -a --volumes" not in patterns


def test_allowlist_remove_and_corrupt_store(guard_env):
    command_guard.add_allowlist_entry("rm -rf build/", kind="exact")
    assert command_guard.remove_allowlist_entry(pattern="rm -rf build/") is True
    assert command_guard.remove_allowlist_entry(pattern="rm -rf build/") is False
    store = os.path.join(str(guard_env), "command_guard.json")
    with open(store, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert command_guard.list_allowlist() == []
    assert os.path.exists(store + ".corrupt")


def test_one_shot_env_hash_is_consumed_once(guard_env, monkeypatch):
    cmd = "git push --force origin main"
    digest = hashlib.sha256(cmd.encode()).hexdigest()
    monkeypatch.setenv(command_guard.ONE_SHOT_ENV, digest)
    assert command_guard.consume_one_shot(cmd) is True
    assert command_guard.consume_one_shot(cmd) is False  # consumed
    assert command_guard.consume_one_shot("git push --force origin dev") is False


def test_gate_check_downgrades_allowlisted_to_caution(guard_env):
    command_guard.add_allowlist_entry("rm -rf build/", kind="exact")
    verdict = command_guard.gate_check("bash", "rm -rf build/", mode="enforce")
    assert verdict["denial"] is None
    assert verdict["tier"] == "CAUTION"
    assert verdict["allowlisted"] is True
    receipts = command_guard.tail_receipts()
    assert receipts and receipts[-1]["action"] == "allowlisted"


def test_gate_check_one_shot_downgrades_once(guard_env, monkeypatch):
    cmd = "rm -rf build/"
    digest = hashlib.sha256(cmd.encode()).hexdigest()
    monkeypatch.setenv(command_guard.ONE_SHOT_ENV, digest)
    first = command_guard.gate_check("bash", cmd, mode="enforce")
    assert first["denial"] is None and first["one_shot"] is True
    second = command_guard.gate_check("bash", cmd, mode="enforce")
    assert second["denial"] is not None  # the bypass was one-shot


# ---------------------------------------------------------------------------
# Receipts (hash chain)
# ---------------------------------------------------------------------------

def test_receipts_chain_verifies_and_detects_tampering(guard_env):
    for i in range(4):
        command_guard.append_receipt(
            session="s", tool="bash", command=f"rm -rf x{i}",
            tier="DANGEROUS", rule="fs.rm_force_recursive", action="blocked",
        )
    path = os.path.join(str(guard_env), "command_guard_log.jsonl")
    verdict = command_guard.verify_chain(path)
    assert verdict == {"ok": True, "length": 4, "broken_at": None}

    lines = open(path, encoding="utf-8").read().splitlines()
    tampered = json.loads(lines[1])
    tampered["command_head"] = "ls"  # rewrite history
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    verdict = command_guard.verify_chain(path)
    assert verdict["ok"] is False
    assert verdict["broken_at"] == 1


def test_receipts_rotate_and_start_a_fresh_chain(guard_env, monkeypatch):
    command_guard.append_receipt(command="rm a", tier="CAUTION", action="allowed")
    monkeypatch.setattr(command_guard, "_MAX_LOG_BYTES", 1)
    command_guard.append_receipt(command="rm b", tier="CAUTION", action="allowed")
    path = os.path.join(str(guard_env), "command_guard_log.jsonl")
    assert os.path.exists(path + ".1")  # the tail survives
    records = command_guard.tail_receipts()
    assert len(records) == 1 and records[0].get("rotated_from")
    assert command_guard.verify_chain(path)["ok"] is True


# ---------------------------------------------------------------------------
# decision_for enforcement
# ---------------------------------------------------------------------------

def test_enforce_blocks_dangerous_even_with_gate_bypassed(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    decision = ctx.decision_for("bash", "git push --force")
    assert decision.allowed is False
    assert "Destructive command (tier DANGEROUS, rule git.push_force)" in decision.reason
    receipts = command_guard.tail_receipts()
    assert receipts[-1]["action"] == "blocked"


def test_enforce_blocks_critical(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    decision = ctx.decision_for("bash", "rm -rf /")
    assert decision.allowed is False
    assert "tier CRITICAL" in decision.reason


def test_enforce_allows_caution_with_receipt(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "rm notes.txt").allowed is True
    receipts = command_guard.tail_receipts()
    assert receipts and receipts[-1]["action"] == "allowed"
    assert receipts[-1]["tier"] == "CAUTION"


def test_observe_mode_allows_and_logs(guard_env, monkeypatch):
    _set_mode(monkeypatch, "observe")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "rm -rf build/").allowed is True
    receipts = command_guard.tail_receipts()
    assert receipts and receipts[-1]["action"] == "observed"
    assert receipts[-1]["tier"] == "DANGEROUS"


def test_off_mode_skips_entirely(guard_env, monkeypatch):
    _set_mode(monkeypatch, "off")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "rm -rf /").allowed is True
    assert command_guard.tail_receipts() == []


def test_safe_and_non_shell_tools_are_untouched(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "ls -la").allowed is True
    assert ctx.decision_for("read_file", "rm -rf /").allowed is True
    assert command_guard.tail_receipts() == []


def test_double_check_is_memoized_one_receipt(guard_env, monkeypatch):
    """agent_loop checks, then tool_execution re-checks: same answer, one receipt."""
    _set_mode(monkeypatch, "enforce")
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    first = ctx.decision_for("bash", "git push --force")
    second = ctx.decision_for("bash", "git push --force")
    assert first.allowed is second.allowed is False
    assert first.reason == second.reason
    assert len(command_guard.tail_receipts()) == 1


def test_guard_failure_fails_open_not_broken_turn(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")

    def boom(*args, **kwargs):
        raise RuntimeError("guard internals broke")

    monkeypatch.setattr(command_guard, "gate_check", boom)
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "rm -rf /").allowed is True


def test_unknown_mode_setting_falls_back_to_enforce(guard_env, monkeypatch):
    _set_mode(monkeypatch, "yolo")
    assert tc.command_guard_mode() == "enforce"
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True)
    assert ctx.decision_for("bash", "rm -rf /").allowed is False


def test_checkpoint_and_metadata_helpers(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    assert tc.command_guard_wants_checkpoint("bash", "git push --force") is True
    assert tc.command_guard_wants_checkpoint("bash", "ls -la") is False
    meta = tc.command_guard_metadata("bash", "rm -rf /")
    assert meta == {"tier": "CRITICAL", "rule": "fs.rm_root"}
    assert tc.command_guard_metadata("bash", "cat x") is None
    assert tc.command_guard_requires_approval("bash", "rm -rf build/") is True
    assert tc.command_guard_requires_approval("bash", "rm x.txt") is False
    _set_mode(monkeypatch, "observe")
    assert tc.command_guard_requires_approval("bash", "rm -rf build/") is False


# ---------------------------------------------------------------------------
# The sealed approved-replay path (modeled on test_tool_approval_task_scope):
# the user's approval of the exact DANGEROUS command executes exactly once,
# and a byte-different command cannot ride on it.
# ---------------------------------------------------------------------------

def _sealed_grant(content, *, decision="approve_task"):
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    store = ToolApprovalStore()
    pending = store.create(
        owner="Alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content=content,
        workspace=None,
        # A guard card can be sealed on a CLEAN run — no external context.
        external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("bash", content),
    )
    return store.consume(
        pending.approval_id,
        decision=decision,
        owner="alice",
        session_id="session-1",
    )


def _run_block(block, grant, monkeypatch, executed):
    from src import tool_execution

    async def fake_mcp(tool, content, progress_cb=None):
        executed.append((tool, content))
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_call_mcp_tool", fake_mcp)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    ctx = ToolRunSecurityContext(
        approval_gate_bypassed=bool(grant and grant.allow_remaining_actions),
    )
    return asyncio.run(
        tool_execution.execute_tool_block(
            block,
            session_id="session-1",
            owner="alice",
            workspace=None,
            security_context=ctx,
            exact_approval=grant,
        )
    )


def test_approved_replay_executes_exact_command_once(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    content = "git push --force origin main"
    executed = []

    # Without a sealed approval, the guard blocks outright even on a
    # bypassed run (the guard sits before the bypass).
    desc, result = _run_block(
        ToolBlock("bash", content), None, monkeypatch, executed
    )
    assert result.get("blocked") is True
    assert "Destructive command" in result["error"]
    assert executed == []

    # The sealed exact approval is the only door through — and it opens on a
    # clean (never-armed) run because the guard card is a per-call approval.
    tc._reset_command_guard_cache()
    grant = _sealed_grant(content)
    assert grant is not None
    desc, result = _run_block(ToolBlock("bash", content), grant, monkeypatch, executed)
    assert result.get("blocked") is None
    assert result.get("exit_code") == 0
    assert executed == [("bash", content)]

    # Exactly once: the claimed grant cannot run the command a second time.
    desc, result = _run_block(ToolBlock("bash", content), grant, monkeypatch, executed)
    assert result.get("blocked") is True
    assert result.get("policy") == "exact_tool_approval"
    assert len(executed) == 1


def test_byte_different_command_does_not_ride_the_approval(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    content = "git push --force origin main"
    executed = []
    grant = _sealed_grant(content)
    assert grant is not None
    tampered = content + " && rm -rf /"
    desc, result = _run_block(ToolBlock("bash", tampered), grant, monkeypatch, executed)
    assert result.get("blocked") is True
    assert result.get("policy") == "exact_tool_approval"
    assert executed == []
    # The digest revalidation (slb) itself refuses the tampered content too.
    assert grant.matches(
        owner="alice",
        session_id="session-1",
        tool_name="bash",
        content=tampered,
        workspace=None,
    ) is False


def test_approved_replay_records_approved_receipt(guard_env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    content = "git push --force origin main"
    tc.record_approved_guard_execution("bash", content, session="session-1")
    receipts = command_guard.tail_receipts()
    assert receipts and receipts[-1]["action"] == "approved"
    assert receipts[-1]["tier"] == "DANGEROUS"
    assert receipts[-1]["rule"] == "git.push_force"
    assert command_guard.verify_chain()["ok"] is True
