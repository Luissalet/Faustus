"""SEC-1 · B-008: a foreign child gets what it needs, not what we happen to hold.

The old rule was "strip the virtualenv markers and hand over the rest", which
gave a third-party MCP server and an external agent CLI every provider key,
cloud credential and repository token the operator had exported — plus the
internal loopback token, which is a key to Faustus's own privileged routes.

The sentinels below are planted in the parent's environment and then looked for
in the child's, which is the only check that means anything here.
"""

import os

import pytest

from src import agent_runners as reg
from src import mcp_manager as mm
from src.native_env import (
    FAUSTUS_PRIVATE_NAMES,
    PROFILE_AGENT,
    PROFILE_BUILD,
    PROFILE_GIT,
    PROFILE_SYSTEM,
    native_host_environment,
    profile_environment,
    scrub_private,
)

SENTINELS = {
    "OPENAI_API_KEY": "sk-sentinel-openai",
    "ANTHROPIC_API_KEY": "sk-sentinel-anthropic",
    "AWS_SECRET_ACCESS_KEY": "sentinel-aws",
    "GITHUB_TOKEN": "ghp-sentinel",
    "ODYSSEUS_INTERNAL_TOKEN": "sentinel-loopback",
    "SOME_APP_SECRET": "hunter2",
}


@pytest.fixture
def planted(monkeypatch):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    return SENTINELS


# ── the profiles themselves ───────────────────────────────────────────────

@pytest.mark.parametrize("profile", [PROFILE_SYSTEM, PROFILE_BUILD, PROFILE_GIT, PROFILE_AGENT])
def test_no_profile_carries_a_planted_secret(planted, profile):
    env = profile_environment(profile)
    for name in planted:
        assert name not in env, f"{name} reached a {profile} child"


def test_a_profile_still_carries_what_a_process_needs_to_start(planted):
    env = profile_environment(PROFILE_SYSTEM)
    assert env.get("PATH") == os.environ["PATH"] or env["PATH"]  # venv entries may be stripped
    assert "PATH" in env


def test_the_build_profile_adds_toolchain_variables_only(monkeypatch, planted):
    monkeypatch.setenv("CARGO_HOME", "/opt/cargo")
    system = profile_environment(PROFILE_SYSTEM)
    build = profile_environment(PROFILE_BUILD)
    assert "CARGO_HOME" not in system
    assert build["CARGO_HOME"] == "/opt/cargo"
    assert "GITHUB_TOKEN" not in build


def test_the_ssh_agent_socket_belongs_to_the_git_profile_only(monkeypatch, planted):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    assert "SSH_AUTH_SOCK" not in profile_environment(PROFILE_AGENT)
    assert "SSH_AUTH_SOCK" not in profile_environment(PROFILE_BUILD)
    assert profile_environment(PROFILE_GIT)["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


def test_an_explicit_grant_lets_one_variable_through(planted):
    env = profile_environment(PROFILE_AGENT, allow=("ANTHROPIC_API_KEY",))
    assert env["ANTHROPIC_API_KEY"] == SENTINELS["ANTHROPIC_API_KEY"]
    assert "OPENAI_API_KEY" not in env


def test_extra_is_layered_on_top_unfiltered(planted):
    env = profile_environment(PROFILE_AGENT, extra={"MY_ENDPOINT": "http://127.0.0.1:11434"})
    assert env["MY_ENDPOINT"] == "http://127.0.0.1:11434"


# ── the internal token, which is not a setting ────────────────────────────

@pytest.mark.parametrize("name", FAUSTUS_PRIVATE_NAMES)
def test_the_internal_token_never_leaves_this_process(monkeypatch, planted, name):
    monkeypatch.setenv(name, "sentinel-loopback")
    assert name not in profile_environment(PROFILE_AGENT)
    assert name not in profile_environment(PROFILE_AGENT, inherit_all=True)
    assert name not in native_host_environment()
    # Not even when a caller names it explicitly: it is a key to this
    # application's privileged routes, not a variable to pass around.
    assert name not in profile_environment(PROFILE_AGENT, extra={name: "x"})
    assert name not in native_host_environment(extra={name: "x"})


def test_inherit_all_is_wide_but_not_unconditional(planted):
    env = profile_environment(PROFILE_AGENT, inherit_all=True)
    assert env["GITHUB_TOKEN"] == SENTINELS["GITHUB_TOKEN"]  # that is what it means
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env


def test_scrub_private_leaves_everything_else_alone():
    assert scrub_private({"A": "1", "ODYSSEUS_INTERNAL_TOKEN": "x"}) == {"A": "1"}


# ── the two consumers ─────────────────────────────────────────────────────

def test_an_external_agent_gets_no_planted_secret(planted):
    runner = reg.get("opencode")
    env = reg.build_env(runner, model="qwen3", endpoint="http://127.0.0.1:11434",
                        inherit_all=False)
    for name in planted:
        assert name not in env, f"{name} reached {runner.key}"
    assert env.get("PATH")


def test_a_runner_reads_only_the_keys_its_row_declares(planted):
    claude = reg.get("claude")
    env = reg.build_env(claude, model="sonnet", endpoint="", inherit_all=False)
    # Declared in the table, because Claude Code documents reading it.
    assert env["ANTHROPIC_API_KEY"] == SENTINELS["ANTHROPIC_API_KEY"]
    # Another vendor's key is none of its business.
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_the_table_entries_still_win(planted):
    env = reg.build_env(reg.get("claude"), endpoint="http://127.0.0.1:11434/v1",
                        inherit_all=False)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434/v1"


def test_table_env_reports_only_what_the_table_adds(planted):
    env = reg.table_env(reg.get("codex"), endpoint="http://127.0.0.1:11434/v1")
    assert env == {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}


def test_the_operator_can_grant_a_variable_explicitly(planted, monkeypatch):
    import src.settings as settings_mod
    real = settings_mod.get_setting
    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda k, d=None: "GITHUB_TOKEN, SOME_APP_SECRET" if k == "agent_env_allow" else real(k, d),
    )
    env = reg.build_env(reg.get("opencode"), inherit_all=False)
    assert env["GITHUB_TOKEN"] == SENTINELS["GITHUB_TOKEN"]
    assert env["SOME_APP_SECRET"] == SENTINELS["SOME_APP_SECRET"]
    assert "OPENAI_API_KEY" not in env


def test_a_legacy_mcp_server_keeps_its_environment_minus_the_token(planted):
    env = mm.build_server_env({"MY_SERVER_TOKEN": "abc"}, inherit_env=True)
    assert env["MY_SERVER_TOKEN"] == "abc"
    assert env["OPENAI_API_KEY"] == SENTINELS["OPENAI_API_KEY"]  # migration promise
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env


def test_a_minimal_mcp_server_gets_the_shared_structural_list(planted):
    env = mm.build_server_env({"MY_SERVER_TOKEN": "abc"}, inherit_env=False)
    for name in planted:
        assert name not in env
    assert env["MY_SERVER_TOKEN"] == "abc"
    assert env.get("PATH")
