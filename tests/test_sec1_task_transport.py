"""SEC-1 · B-022: the prompt is not a command-line argument.

`{task}` as argv means the whole machine can read it: a process listing, a
diagnostic bundle, an OS crash report. And a task routinely carries private
paths, code, customer data and — by accident, often — a pasted credential.

Three things are pinned here: a row may not claim it can take the task on
stdin without saying where that was verified; the command Faustus prints and
stores never contains the task; and a task that looks like it carries a secret
is refused by a runner that can only use argv, rather than published and
regretted.
"""

import sys

import pytest

from src import agent_runners as reg
from src import external_worker
from src.external_worker import task_descriptor, task_looks_sensitive

SENTINEL = "sk-sec1-task-sentinel-abcdef"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 30)


def _argv_runner(tmp_path, name="argvagent"):
    """A row that has no verified stdin form, like most of the table."""
    script = tmp_path / f"{name}.py"
    script.write_text("import sys\nprint('ran', sys.argv[1][:8])\n", encoding="utf-8")
    return reg.Runner(
        key=name, label=f"Fake {name}", kind="cli", licence="open",
        argv=(sys.executable, str(script), "{task}"), detect=(sys.executable,),
        notes="a test double",
    )


# ── the table's own rule ──────────────────────────────────────────────────

def test_no_row_claims_stdin_without_saying_where_it_was_verified():
    for runner in reg.runners(help_source=""):
        if runner.stdin_task:
            assert runner.task_transport_verified, (
                f"{runner.key} claims the task goes on stdin but records no verification; "
                f"this table does not guess about someone else's program"
            )


def test_claude_takes_the_task_on_stdin_now():
    claude = reg.get("claude", help_source="")
    assert claude.stdin_task is True
    assert "2.1.104" in claude.task_transport_verified
    argv = reg.build_argv(claude, SENTINEL, model="sonnet")
    assert SENTINEL not in " ".join(argv)
    assert "{task}" not in " ".join(argv)
    # and the rest of the invocation is untouched
    assert argv[:4] == ["claude", "-p", "--model", "sonnet"]


def test_an_unverified_row_still_uses_argv(tmp_path):
    runner = _argv_runner(tmp_path)
    assert runner.stdin_task is False
    assert SENTINEL in reg.build_argv(runner, SENTINEL)


# ── what gets printed and stored ──────────────────────────────────────────

def test_the_descriptor_identifies_the_task_without_showing_it():
    one = task_descriptor(SENTINEL)
    assert SENTINEL not in one
    assert one.startswith("<task:sha256=") and f"chars={len(SENTINEL)}" in one
    assert one == task_descriptor(SENTINEL)          # stable
    assert one != task_descriptor(SENTINEL + "!")    # and distinguishing


def test_argv_shown_replaces_the_task_with_its_descriptor():
    shown = external_worker._shown(["agent", "--run", SENTINEL], {"X": "1"}, task=SENTINEL)
    assert SENTINEL not in shown
    assert "sha256=" in shown


def test_argv_shown_still_stars_out_secret_env_values():
    shown = external_worker._shown(["agent"], {"MY_TOKEN": SENTINEL}, task="")
    assert SENTINEL not in shown
    assert "***" in shown


# ── refusing to publish a secret in argv ──────────────────────────────────

@pytest.mark.parametrize("task", [
    'use this key: api_key="sk-live-abcdef123456"',
    "curl -H 'Authorization: Bearer abcdefghijklmnop' https://example.test",
    "the password='hunter2' one",
])
def test_a_task_carrying_a_credential_is_recognised(task):
    assert task_looks_sensitive(task) is True


@pytest.mark.parametrize("task", [
    "refactor apply_tax and add a test",
    "why does token_count=812 differ from the usage panel?",
    "",
])
def test_ordinary_work_is_not_flagged(task):
    assert task_looks_sensitive(task) is False


def test_an_argv_runner_refuses_a_task_that_carries_a_credential(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _argv_runner(tmp_path)
    task = f'deploy with api_key="{SENTINEL}"'

    out = external_worker.run_task(runner, task, workspace=str(ws))

    assert out["ok"] is False
    assert "command-line argument" in out["error"]
    assert SENTINEL not in out["error"]
    assert SENTINEL not in out["argv_shown"]


def test_a_person_can_override_the_refusal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _argv_runner(tmp_path)
    task = f'deploy with api_key="{SENTINEL}"'

    out = external_worker.run_task(runner, task, workspace=str(ws), allow_argv_task=True)

    # It ran (or failed for its own reasons) — what it did not do is refuse on
    # the argv rule, and the stored command still does not show the secret.
    assert "command-line argument" not in (out["error"] or "")
    assert SENTINEL not in out["argv_shown"]


def test_a_stdin_runner_needs_no_override(tmp_path):
    """The whole point: with stdin there is nothing to publish."""
    ws = tmp_path / "ws"
    ws.mkdir()
    script = tmp_path / "stdinagent.py"
    script.write_text("import sys\nprint('got', len(sys.stdin.read()))\n", encoding="utf-8")
    runner = reg.Runner(
        key="stdinagent", label="Fake stdin agent", kind="cli", licence="open",
        argv=(sys.executable, str(script)), stdin_task=True,
        task_transport_verified="the test double reads stdin",
        detect=(sys.executable,), notes="a test double",
    )

    out = external_worker.run_task(runner, f'api_key="{SENTINEL}"', workspace=str(ws))

    assert out["ok"] is True
    assert SENTINEL not in out["argv_shown"]
    assert "got" in out["output_tail"]
