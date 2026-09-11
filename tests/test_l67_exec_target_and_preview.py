"""L67 — EXEC-01 (execution_target on every tool result) and EXEC-02
(command_preview masks secrets before a command reaches an approval card).

Both close a real gap: before this change `BashTool`/`PythonTool` results
carried no `execution_target` key at all (grep confirms zero occurrences of
the string in the pre-L67 tree), and `command_guard.py` had no
`command_preview` — the sealed approval card in `src/tool_approvals.py` shows
the agent's raw command text verbatim.
"""
import asyncio

import pytest

from src import command_guard as cg
from src import native_env
from src.agent_tools import subprocess_tools as st


# ── EXEC-01 ──────────────────────────────────────────────────────────────

def test_host_kind_reflects_windows_wsl_posix(monkeypatch):
    import core.platform_compat as pc

    # Explicit is_windows= (what subprocess_tools.py passes) always wins.
    assert native_env.host_kind(is_windows=True) == native_env.TARGET_WINDOWS

    monkeypatch.setattr(pc, "is_wsl", lambda: True, raising=False)
    assert native_env.host_kind(is_windows=False) == native_env.TARGET_WSL

    monkeypatch.setattr(pc, "is_wsl", lambda: False, raising=False)
    assert native_env.host_kind(is_windows=False) == native_env.TARGET_POSIX

    # No override: falls back to core.platform_compat.IS_WINDOWS itself.
    monkeypatch.setattr(pc, "IS_WINDOWS", True, raising=False)
    assert native_env.host_kind() == native_env.TARGET_WINDOWS


def test_execution_target_prefers_container_and_remote_over_host():
    host_only = st._execution_target(sandboxed=False, cwd="/w", shell="/bin/sh")
    assert host_only["kind"] == native_env.host_kind()
    assert host_only["cwd"] == "/w" and host_only["shell"] == "/bin/sh"

    sandboxed = st._execution_target(sandboxed=True, cwd="/w", shell="bash")
    assert sandboxed["kind"] == native_env.TARGET_CONTAINER

    remote = st._execution_target(sandboxed=False, cwd="/w", shell="bash", remote=True)
    assert remote["kind"] == native_env.TARGET_REMOTE
    # remote outranks sandboxed when (hypothetically) both are asserted
    both = st._execution_target(sandboxed=True, cwd="/w", shell="bash", remote=True)
    assert both["kind"] == native_env.TARGET_REMOTE


def test_bash_tool_result_carries_execution_target():
    res = asyncio.run(st.BashTool().execute("echo hi", {"session_id": None}))
    assert res["exit_code"] == 0
    target = res["execution_target"]
    assert target["kind"] in (native_env.TARGET_WINDOWS, native_env.TARGET_WSL, native_env.TARGET_POSIX)
    assert target["cwd"]
    assert target["shell"]


def test_python_tool_result_carries_execution_target():
    res = asyncio.run(st.PythonTool().execute("print(1)", {}))
    assert res["exit_code"] == 0
    target = res["execution_target"]
    assert target["kind"] in (native_env.TARGET_WINDOWS, native_env.TARGET_WSL, native_env.TARGET_POSIX)
    assert target["cwd"]


def test_bash_tool_missing_git_bash_reports_execution_target_before_failing(monkeypatch):
    """The Windows-without-Git-Bash gap (EXEC-01): the RuntimeError still
    carries the execution_target the caller would have used, so the error
    is attributable to a specific target rather than a bare string."""
    import core.platform_compat as pc

    monkeypatch.setattr(st, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(st, "find_bash", lambda: None)
    res = asyncio.run(st.BashTool().execute("echo hi", {"session_id": None}))
    assert res["exit_code"] == 1
    assert "Git Bash" in res["error"]
    assert res["execution_target"]["kind"] == native_env.TARGET_WINDOWS


# ── EXEC-02 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,must_not_contain", [
    ('curl -H "Authorization: Bearer abcd1234efgh5678" https://x', "abcd1234efgh5678"),
    ("export OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrSt123456", "AbCdEfGhIjKlMnOpQrSt"),
    ('curl --header "token=abcXYZ789secret" https://x', "abcXYZ789secret"),
    ('curl --header "AUTH_TOKEN=abcXYZ789secret" https://x', "abcXYZ789secret"),
    ("export PASSWORD=hunter2hunter2", "hunter2hunter2"),
])
def test_command_preview_masks_secrets(command, must_not_contain):
    preview = cg.command_preview(command)
    assert must_not_contain not in preview


@pytest.mark.parametrize("command", [
    "echo hello --max-tokens=100",
    'echo café && grep "señal" *.txt',
    'echo "a & b" "quoted arg with spaces"',
    "ls -la *.py && echo done",
    "git status && git diff --stat",
])
def test_command_preview_does_not_change_meaning_of_ordinary_commands(command):
    """Spaces, quotes, Unicode, globbing and `&` in an ordinary (non-secret)
    command pass through byte-for-byte — masking must never rewrite meaning."""
    assert cg.command_preview(command) == command


def test_command_preview_never_raises_and_has_a_safe_fallback(monkeypatch):
    import core.log_safety as log_safety

    def _boom(_text):
        raise RuntimeError("boom")

    monkeypatch.setattr(log_safety, "redact_secrets", _boom)
    preview = cg.command_preview("export TOKEN=abc")
    assert "unavailable" in preview.lower()


def test_command_preview_handles_non_string_and_empty():
    assert cg.command_preview(None) == ""
    assert cg.command_preview("") == ""
    assert cg.command_preview(123) == "123"
