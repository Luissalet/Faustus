"""EXEC-06 — reusable, versioned scripts and SSH remote execution.

``src.tool_execution`` had no saved-script registry or SSH pairing/fingerprint
verification before this lote.
"""
import asyncio

import pytest

import src.tool_execution as te


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "DATA_DIR", str(tmp_path))
    te.reset_ssh_registry()
    yield


# ---------------------------------------------------------------------------
# Saved scripts: versioned, typed inputs, argv rendering (not a shell string)
# ---------------------------------------------------------------------------

def test_save_script_starts_at_version_one():
    script = te.save_script("run-tests", "pytest {path}", ["path"])
    assert script.version == 1
    assert script.name == "run-tests"


def test_regression_saving_again_under_the_same_name_versions_not_overwrites():
    """EXEC-06: "recetas versionadas" -- without the version bump, a second
    `save_script` under the same name would silently replace the first
    recipe with no trace it ever existed."""
    te.save_script("run-tests", "pytest {path}", ["path"])
    second = te.save_script("run-tests", "pytest -k {mark} {path}", ["path", "mark"])
    assert second.version == 2
    loaded = te.load_script("run-tests")
    assert loaded.version == 2
    assert loaded.params == ("path", "mark")


def test_render_script_command_produces_an_argv_list_not_a_shell_string():
    script = te.save_script("greet", "echo {name}", ["name"])
    # A value carrying shell metacharacters must land as ONE argv element,
    # never be interpreted by a shell -- this is what makes injection
    # impossible regardless of what `name` contains.
    argv = te.render_script_command(script, {"name": "hi; rm -rf ~"})
    assert argv == ["echo", "hi; rm -rf ~"]


def test_render_script_command_requires_every_declared_param():
    script = te.save_script("greet2", "echo {name}", ["name"])
    with pytest.raises(ValueError, match="missing"):
        te.render_script_command(script, {})


def test_render_script_command_refuses_undeclared_placeholder():
    script = te.SavedScript(name="x", command_template="echo {secret}", params=(), version=1)
    with pytest.raises(ValueError, match="undeclared"):
        te.render_script_command(script, {})


def test_run_saved_script_reports_verifiable_output(tmp_path):
    script = te.save_script("echoer", "echo {msg}", ["msg"])

    async def _runner(argv, cwd):
        assert argv == ("echo", "hello")
        return 0, "hello\n", ""

    outcome = asyncio.run(te.run_saved_script(script, {"msg": "hello"}, cwd=str(tmp_path), runner=_runner))
    assert outcome.ok is True
    assert outcome.returncode == 0
    assert outcome.stdout == "hello\n"


# ---------------------------------------------------------------------------
# SSH: fingerprint pairing, no trust inheritance, no silent host change
# ---------------------------------------------------------------------------

def test_pair_and_resolve_round_trip():
    te.pair_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:AAAA", port=2222)
    target = te.resolve_ssh_target("prod-db")
    assert target.host == "db.internal.example"
    assert target.port == 2222
    assert target.fingerprint == "SHA256:AAAA"


def test_unpaired_alias_is_refused():
    reason = te.verify_ssh_target("ghost", host="anything", fingerprint="anything")
    assert reason is not None
    assert "no ssh target" in reason.lower()


def test_regression_similar_hostname_does_not_inherit_trust():
    """EXEC-06 acceptance: "a similar hostname does not inherit trust from
    another." Without an EXACT host comparison in `verify_ssh_target`, a
    lookalike hostname would pass."""
    te.pair_ssh_target("prod-db", host="prod-db.internal.example", fingerprint="SHA256:AAAA")
    reason = te.verify_ssh_target("prod-db", host="prod-db.internal.example.evil.com", fingerprint="SHA256:AAAA")
    assert reason is not None
    assert "does not inherit" in reason


def test_regression_recipe_cannot_silently_change_machine():
    """EXEC-06 acceptance: "a recipe cannot silently change machine" -- a
    fingerprint mismatch for the SAME alias/host must be refused, not
    silently accepted as a key rotation."""
    te.pair_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:AAAA")
    reason = te.verify_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:BBBB")
    assert reason is not None
    assert "cannot silently change" in reason


def test_matching_host_and_fingerprint_is_accepted():
    te.pair_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:AAAA")
    assert te.verify_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:AAAA") is None


def test_run_remote_script_refuses_when_pairing_does_not_match():
    script = te.save_script("uptime-check", "uptime", [])
    te.pair_ssh_target("prod-db", host="db.internal.example", fingerprint="SHA256:AAAA")

    async def _never_called(argv, cwd):
        raise AssertionError("must not run against an unverified host")

    with pytest.raises(PermissionError):
        asyncio.run(te.run_remote_script(
            script, {}, alias="prod-db", presented_host="db.internal.example",
            presented_fingerprint="SHA256:DIFFERENT", runner=_never_called,
        ))


def test_run_remote_script_runs_over_ssh_with_batch_mode_no_password_prompt():
    script = te.save_script("uptime-check2", "uptime", [])
    te.pair_ssh_target("web1", host="web1.internal.example", fingerprint="SHA256:CCCC", port=22)
    seen = {}

    async def _runner(argv, cwd):
        seen["argv"] = argv
        return 0, "up 3 days", ""

    outcome = asyncio.run(te.run_remote_script(
        script, {}, alias="web1", presented_host="web1.internal.example",
        presented_fingerprint="SHA256:CCCC", runner=_runner,
    ))
    assert outcome.ok is True
    assert "web1.internal.example" in seen["argv"]
    assert "BatchMode=yes" in seen["argv"]
