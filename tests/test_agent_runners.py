"""The agent-runner registry (src/agent_runners.py).

The whole point of the module is that **adding an agent is a table entry, not
code**, and that the LIST comes from the machine, not from this file: the
catalogue is `ollama launch --help` parsed at runtime, merged with a built-in
table that adds what a help text cannot know (the licence word, the argv that
runs one task, the environment).

So these tests pin three things:

  * the real help text of the target machine — captured verbatim below —
    parses into all 18 integrations with their aliases;
  * an integration Ollama knows and this table does not still APPEARS, marked
    `unknown` and not runnable, instead of vanishing; and a table row the help
    does not list still appears too;
  * a licence word is never invented. Every row says `open`, `subscription`
    or `unknown`, and the rows this table cannot vouch for say `unknown`.
"""
from __future__ import annotations

import pytest

from src import agent_runners as reg

# ── the fixture: `ollama launch --help` on the target machine, verbatim ─────
HELP = """Launch a coding agent or editor configured for Ollama.

Usage:
  ollama launch <integration> [flags]

Supported integrations:
  claude          Claude Code
  chatgpt         ChatGPT (aliases: codex-app, codex-desktop, codex-gui)
  hermes          Hermes Agent
  openclaw        OpenClaw (aliases: clawdbot, moltbot)
  opencode        OpenCode
  codex           Codex
  hermes-desktop  Hermes Desktop
  copilot         Copilot CLI (aliases: copilot-cli)
  omp             OMP
  droid           Droid
  dsh             DeepSeek Harness (alias: deepseek-harness)
  kimi            Kimi Code CLI
  muse            Muse Code (aliases: muse-code)
  pi              Pi
  pool            Pool
  cline           Cline
  qwen            Qwen Code
  vscode          VS Code (aliases: code)

Flags:
      --config          configure without launching
      --model string    the model to use
      --restore         restore the previous configuration
  -y, --yes             answer yes to every prompt
"""

#: The real function, captured before the autouse fixture stubs it out.
_REAL_HELP_TEXT = reg.help_text

ALL_KEYS = ["claude", "chatgpt", "hermes", "openclaw", "opencode", "codex", "hermes-desktop",
            "copilot", "omp", "droid", "dsh", "kimi", "muse", "pi", "pool", "cline", "qwen", "vscode"]

# Only `claude` and `code` are really installed on that machine.
INSTALLED = {"claude": "/usr/local/bin/claude", "code": "/usr/local/bin/code"}


def fake_which(name):
    return INSTALLED.get(str(name))


@pytest.fixture(autouse=True)
def _no_live_help(monkeypatch):
    """Never run `ollama launch --help` from a test: every test that wants the
    live list passes it as `help_source`."""
    monkeypatch.setattr(reg, "help_text", lambda **kw: "")
    reg.reset_cache()


# ── parsing the live help ───────────────────────────────────────────────────

def test_the_real_help_parses_into_all_eighteen_with_their_aliases():
    rows = reg.parse_help(HELP)
    assert list(rows) == ALL_KEYS
    assert rows["claude"]["label"] == "Claude Code" and rows["claude"]["aliases"] == ()
    assert rows["chatgpt"]["aliases"] == ("codex-app", "codex-desktop", "codex-gui")
    assert rows["chatgpt"]["label"] == "ChatGPT"          # the alias note is not part of the label
    assert rows["openclaw"]["aliases"] == ("clawdbot", "moltbot")
    assert rows["dsh"]["aliases"] == ("deepseek-harness",)   # singular "alias:" too
    assert rows["dsh"]["label"] == "DeepSeek Harness"
    assert rows["vscode"]["aliases"] == ("code",)
    assert rows["hermes-desktop"]["label"] == "Hermes Desktop"


def test_the_flags_block_is_not_read_as_integrations():
    rows = reg.parse_help(HELP)
    for stray in ("--config", "--model", "-y,", "Flags:", "Usage:", "ollama"):
        assert stray not in rows


def test_junk_help_yields_nothing_and_never_raises():
    for junk in (None, "", 17, b"bytes", "no integrations here", object()):
        assert reg.parse_help(junk) == {}


def test_an_unparseable_help_leaves_the_built_in_table_alone():
    rows = reg.runners(help_source="totally unrelated output")
    assert [r.key for r in rows] == ALL_KEYS          # the table is still the table
    assert reg.get("claude", help_source="") is not None


# ── the merge: neither source is allowed to be the only one ────────────────

def test_an_integration_ollama_knows_and_faustus_does_not_survives_as_not_runnable():
    text = HELP.replace("  qwen            Qwen Code\n",
                        "  qwen            Qwen Code\n  brandnew        Brand New Agent (aliases: bna)\n")
    rows = {r.key: r for r in reg.runners(help_source=text)}
    assert "brandnew" in rows, "a new integration must appear without a code change"
    new = rows["brandnew"]
    assert new.label == "Brand New Agent" and new.aliases == ("bna",)
    assert new.argv == () and new.licence == "unknown"
    assert reg.NOT_RUNNABLE_NOTE in new.notes
    row = reg.to_row(new, which=fake_which)
    assert row["runnable_as_worker"] is False and row["invocation_known"] is False
    assert row["launch_command"] == "ollama launch brandnew -y"
    # and it is reachable by name, exactly like a built-in one
    assert reg.get("bna", help_source=text).key == "brandnew"


def test_a_table_row_the_help_does_not_list_still_appears():
    """An older Ollama, or none at all: the table is not silently emptied."""
    rows = {r.key: r for r in reg.runners(help_source="")}
    assert set(rows) == set(ALL_KEYS)
    assert rows["claude"].label == "Claude Code"


def test_the_live_label_wins_and_the_aliases_are_merged():
    text = HELP.replace("  claude          Claude Code\n",
                        "  claude          Claude Code CLI (aliases: claude-code)\n")
    row = reg.get("claude", help_source=text)
    assert row.label == "Claude Code CLI" and "claude-code" in row.aliases
    assert row.argv, "the built-in invocation must survive the merge"


# ── installed, resolved from the PATH this machine really has ──────────────

def test_installed_is_resolved_from_the_path_and_nothing_is_assumed_present():
    rows = {r["key"]: r for r in reg.catalogue(help_source=HELP, which=fake_which)}
    assert len(rows) == 18
    installed = sorted(k for k, r in rows.items() if r["installed"])
    assert installed == ["claude", "vscode"], "only claude and code are on that machine"
    assert rows["claude"]["path"] == "/usr/local/bin/claude"
    assert rows["vscode"]["path"] == "/usr/local/bin/code"      # detected by its alias binary
    # installed is not the same as usable as a worker: vscode is a GUI
    assert rows["claude"]["runnable_as_worker"] is True
    assert rows["vscode"]["runnable_as_worker"] is False
    # an agent with an invocation but not installed is not runnable either
    assert rows["opencode"]["invocation_known"] is True
    assert rows["opencode"]["installed"] is False and rows["opencode"]["runnable_as_worker"] is False


def test_the_catalogue_never_raises_and_carries_the_guard_note():
    out = reg.summary(help_source=HELP, which=fake_which)
    assert out["installed_count"] == 2 and out["runnable_count"] == 1
    assert "command guard does not see" in out["guard_note"]
    assert out["enabled"] is False, "the feature ships off: it runs third-party binaries"
    assert isinstance(out["timeout_s"], int)


# ── the licence words ───────────────────────────────────────────────────────

def test_no_licence_word_is_ever_invented():
    rows = reg.catalogue(help_source=HELP, which=fake_which)
    for row in rows:
        assert row["licence"] in reg.LICENCES, row
    by_key = {r["key"]: r["licence"] for r in rows}
    for key in ("claude", "chatgpt", "codex", "copilot"):
        assert by_key[key] == "subscription", key
    for key in ("openclaw", "opencode", "cline", "droid", "hermes", "pi", "omp", "qwen",
                "kimi", "muse", "pool", "dsh"):
        assert by_key[key] == "open", key
    # The two this table will not vouch for say so, in the word the UI prints.
    assert by_key["vscode"] == "unknown" and by_key["hermes-desktop"] == "unknown"
    for key in ("vscode", "hermes-desktop"):
        assert "unknown" in dict((r["key"], r["notes"]) for r in rows)[key]


def test_a_row_with_an_unknown_licence_says_why_in_its_notes():
    row = [r for r in reg.catalogue(help_source=HELP, which=fake_which) if r["key"] == "vscode"][0]
    assert "licence" in row["notes"] and row["kind"] == "app"


# ── launch_argv builds the command and never runs it ───────────────────────

def test_launch_argv_builds_the_command_and_runs_nothing(monkeypatch):
    import subprocess

    def boom(*a, **k):   # pragma: no cover - the assertion is that it is not called
        raise AssertionError("launch_argv must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert reg.launch_argv("opencode", help_source=HELP) == ["ollama", "launch", "opencode", "-y"]
    assert reg.launch_argv("opencode", config_only=True, help_source=HELP) == \
        ["ollama", "launch", "opencode", "--config"]
    assert reg.launch_argv("qwen", model="qwen3.5:9b", help_source=HELP) == \
        ["ollama", "launch", "qwen", "-y", "--model", "qwen3.5:9b"]
    # an alias resolves to the integration's own key
    assert reg.launch_argv("moltbot", help_source=HELP)[2] == "openclaw"
    assert reg.launch_argv("nope", help_source=HELP) == []
    assert reg.launch_argv(None, help_source=HELP) == []


# ── the argv that runs ONE task ────────────────────────────────────────────

def test_build_argv_fills_the_placeholders():
    claude = reg.get("claude", help_source=HELP)
    assert reg.build_argv(claude, "add a test", model="qwen3.5:9b") == \
        ["claude", "-p", "add a test", "--model", "qwen3.5:9b"]


def test_an_empty_placeholder_drops_its_own_flag_too():
    """`--model` with nothing after it is not a command."""
    claude = reg.get("claude", help_source=HELP)
    assert reg.build_argv(claude, "add a test") == ["claude", "-p", "add a test"]
    assert reg.build_argv(claude, "add a test", model="") == ["claude", "-p", "add a test"]


def test_stdin_task_keeps_the_task_out_of_the_argv():
    r = reg.Runner(key="x", label="X", argv=("x", "--stdin", "{task}"), stdin_task=True, detect=("x",))
    assert reg.build_argv(r, "hello") == ["x", "--stdin"]


def test_env_entries_with_an_unresolved_placeholder_are_left_unset():
    """Without an endpoint, a runner is NOT pointed at one."""
    claude = reg.get("claude", help_source=HELP)
    assert claude.env["ANTHROPIC_BASE_URL"] == "{endpoint}"
    assert reg.table_env(claude) == {}
    assert reg.table_env(claude, endpoint="http://127.0.0.1:11434") == \
        {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434"}
    # and the inherited environment is preserved
    full = reg.build_env(claude, base={"PATH": "/bin"}, endpoint="http://x")
    assert full["PATH"] == "/bin" and full["ANTHROPIC_BASE_URL"] == "http://x"


# ── the settings gate ───────────────────────────────────────────────────────

def test_the_feature_ships_off(monkeypatch):
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_external_runners"] is False
    assert DEFAULT_SETTINGS["agent_external_runner_timeout_s"] == 900
    assert reg.enabled() is False

    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: True if k == "agent_external_runners" else d)
    assert reg.enabled() is True


def test_a_settings_read_that_explodes_leaves_the_feature_off(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settings are gone")

    monkeypatch.setattr("src.settings.get_setting", boom)
    assert reg.enabled() is False
    assert reg.timeout_s() == reg.DEFAULT_TIMEOUT_S


def test_the_timeout_is_clamped(monkeypatch):
    values = {"agent_external_runner_timeout_s": 99999}
    monkeypatch.setattr("src.settings.get_setting", lambda k, d=None: values.get(k, d))
    assert reg.timeout_s() == 7200
    values["agent_external_runner_timeout_s"] = 0
    assert reg.timeout_s() == 10
    values["agent_external_runner_timeout_s"] = "nonsense"
    assert reg.timeout_s() == reg.DEFAULT_TIMEOUT_S


# ── the live help, when it does run ────────────────────────────────────────

def test_help_text_survives_an_ollama_that_is_not_there(monkeypatch):
    monkeypatch.setattr(reg, "help_text", _REAL_HELP_TEXT)   # undo the autouse stub
    monkeypatch.setattr(reg.shutil, "which", lambda name: None)
    reg.reset_cache()
    assert reg.help_text() == ""
    # and the catalogue is still the built-in table
    assert len(reg.catalogue(which=fake_which)) == 18


def test_help_text_survives_a_process_that_fails(monkeypatch):
    monkeypatch.setattr(reg, "help_text", _REAL_HELP_TEXT)
    monkeypatch.setattr(reg.shutil, "which", lambda name: "/usr/bin/ollama")

    def boom(*a, **k):
        raise OSError("no such thing")

    monkeypatch.setattr(reg.subprocess, "run", boom)
    reg.reset_cache()
    assert reg.help_text() == ""


def test_help_text_is_cached(monkeypatch):
    monkeypatch.setattr(reg, "help_text", _REAL_HELP_TEXT)
    monkeypatch.setattr(reg.shutil, "which", lambda name: "/usr/bin/ollama")
    calls = []

    class Done:
        stdout, stderr = HELP, ""

    def run(*a, **k):
        calls.append(a)
        return Done()

    monkeypatch.setattr(reg.subprocess, "run", run)
    reg.reset_cache()
    assert "claude" in reg.help_text() and "claude" in reg.help_text()
    assert len(calls) == 1
    reg.reset_cache()
    assert "claude" in reg.help_text()
    assert len(calls) == 2


# ── resume: a prior run of the same agent, continued ───────────────────────
#
# The clause `--resume {session}` was added to the `claude` row so a fix round
# can continue the worker that made the change instead of building a new one
# that has to read its way back to the same understanding. That only stays
# free if the drop-the-flag rule really removes BOTH tokens on a first run —
# and "it should" is not a thing to take on trust in a table that decides what
# process starts on someone's machine.

#: What every shipped row produced BEFORE `{session}` existed, generated from
#: the pre-change module and pasted here. A row that is not runnable as a
#: worker produces the empty list, and that is part of the contract too.
ARGV_BEFORE_RESUME = {
    "claude":   ["claude", "-p", "add apply_tax", "--model", "qwen3.5:9b"],
    "opencode": ["opencode", "run", "add apply_tax", "--model", "qwen3.5:9b"],
    "codex":    ["codex", "exec", "add apply_tax", "--model", "qwen3.5:9b"],
    "qwen":     ["qwen", "-p", "add apply_tax", "-m", "qwen3.5:9b"],
}


@pytest.mark.parametrize("key", sorted(r.key for r in reg._BUILTIN))
def test_no_session_produces_exactly_todays_command_for_every_shipped_row(key):
    runner = reg.get(key, help_source=HELP)
    for kwargs in ({}, {"session": None}, {"session": ""}):
        argv = reg.build_argv(runner, "add apply_tax", model="qwen3.5:9b",
                              cwd="/ws", endpoint="http://127.0.0.1:11434", **kwargs)
        assert argv == ARGV_BEFORE_RESUME.get(key, []), f"{key} {kwargs}"


def test_a_session_adds_the_resume_clause_and_nothing_else():
    claude = reg.get("claude", help_source=HELP)
    assert reg.build_argv(claude, "fix it", model="qwen3.5:9b", session="sess-42") == \
        ["claude", "-p", "fix it", "--model", "qwen3.5:9b", "--resume", "sess-42"]
    # No model AND no session: both flags go, and neither takes the other's
    # argument with it.
    assert reg.build_argv(claude, "fix it") == ["claude", "-p", "fix it"]
    # One without the other, in both directions.
    assert reg.build_argv(claude, "fix it", session="sess-42") == \
        ["claude", "-p", "fix it", "--resume", "sess-42"]
    assert reg.build_argv(claude, "fix it", model="qwen3.5:9b") == \
        ["claude", "-p", "fix it", "--model", "qwen3.5:9b"]


def test_the_gate_clause_still_lands_after_the_resume_one():
    """`gate_argv` is appended, so `--settings` must not end up separated from
    its own JSON by the resume pair."""
    claude = reg.get("claude", help_source=HELP)
    argv = reg.build_argv(claude, "fix it", model="m", session="s", settings='{"hooks":{}}')
    assert argv == ["claude", "-p", "fix it", "--model", "m", "--resume", "s",
                    "--output-format", "stream-json", "--verbose", "--settings", '{"hooks":{}}']
    assert argv[argv.index("--settings") + 1] == '{"hooks":{}}'
