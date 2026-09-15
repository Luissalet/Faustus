"""Faustus runs where the project runs (14-09-2026).

Five failures, all server-side, seen in Luis's own chats of that morning on a
Windows folder — read out of `data/app.db`, not reproduced from a guess:

1. the sandbox refused every command ("start Docker") or, with the daemon up,
   ran them in a Linux container where `cmd`, `powershell`, `.bat`, `winget`
   and the project's `.venv\\Scripts\\python.exe` do not exist;
2. the argument validator rejected EVERY absolute path — including one inside
   the bound workspace that the tool itself would have accepted — and the
   model concluded, in writing, "the grep tool rejects absolute Windows paths";
3. answering the agent's own `ask_user` question classified as a vague message,
   which withdraws `bash`/`write_file`: "The `bash` tool is disabled in this
   mode", in the very turn that was building what it had just proposed;
4. a `base_revision` that was not a digest aborted the write instead of being
   treated as the absent, optional precondition it is;
5. nothing told the model which machine it was on.

Each test below pins one of those. They are deliberately about the SEAM (what
the executor decides), not about Windows-only behaviour, so they run on the
Linux CI clone too.
"""
from __future__ import annotations

import json
import os

import pytest

from src import sandbox_exec


# ── 1. the sandbox is an option, not a gate ────────────────────────────────

@pytest.fixture()
def settings(monkeypatch):
    values: dict = {}
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values.get(key, default))
    return values


def test_windows_skips_the_container_without_asking_docker(settings, monkeypatch):
    """No probe, no daemon, no opinion about Docker: a Linux image cannot run
    a Windows project's toolchain, so the host takes it and says why."""
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "auto"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)

    def _never(*a, **k):                      # the probe must not be reached
        raise AssertionError("auto mode probed Docker on a Windows host")
    monkeypatch.setattr("src.execution_backends.DockerWorkspaceBackend.probe", _never)

    reason = sandbox_exec.host_skip_reason()
    assert reason and "Windows" in reason
    described = sandbox_exec.describe()
    assert described == {"enabled": True, "mode": "auto", "target": "host",
                         "skip_reason": reason, "image": sandbox_exec.image()}


def test_strict_still_skips_the_container_on_windows(settings, monkeypatch):
    """A Linux image cannot verify a Windows project even when the operator
    asked for the hard gate: cmd, .bat, winget and the project's Python are
    on the host. Strict remains the POSIX refusal; on native Windows the
    host always runs it and says why."""
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)

    def _never(*a, **k):
        raise AssertionError("strict mode probed Docker on a Windows host")
    monkeypatch.setattr("src.execution_backends.DockerWorkspaceBackend.probe", _never)

    reason = sandbox_exec.host_skip_reason()
    assert reason and "Windows" in reason
    assert sandbox_exec.describe()["target"] == "host"


def test_off_is_still_off(settings, monkeypatch):
    """`auto` is about where an ENABLED sandbox sends the work. With the
    setting off there is no skip reason to report, in either mode."""
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    for mode in ("auto", "strict"):
        settings.update({"agent_sandbox_execution": False, "agent_sandbox_mode": mode})
        assert sandbox_exec.host_skip_reason() is None
        assert sandbox_exec.describe() == {"enabled": False, "mode": "off",
                                           "target": "host", "skip_reason": "", "image": ""}


# ── 2. an absolute path inside the workspace is not an escape ──────────────

def _errors(tool, args, roots=None):
    # agent_tools first: tool_parsing/tool_schemas form a circular cluster
    # that only resolves cleanly when entered through it (see
    # `plan_mode_disabled_tools`' own note on the same import order).
    import src.agent_tools  # noqa: F401
    from src.tool_schemas import validate_tool_arguments
    return [e.kind for e in validate_tool_arguments(tool, args, path_roots=roots)]


def test_an_absolute_path_inside_the_bound_workspace_is_accepted(tmp_path):
    """The exact call from the live session: grep with the full path of a file
    in the workspace. `_resolve_search_root` would have taken it; the schema
    check refused it first, and the model read that as a property of grep."""
    ws = tmp_path / "Silhouettes"
    (ws / "sub").mkdir(parents=True)
    target = ws / "pipeline.py"
    target.write_text("import cv2\n", encoding="utf-8")

    assert _errors("grep", {"pattern": "cv2", "path": str(target)}, [str(ws)]) == []
    assert _errors("grep", {"pattern": "cv2", "path": str(ws / "sub")}, [str(ws)]) == []
    # …and relative paths, the shape that always worked, still do.
    assert _errors("grep", {"pattern": "cv2", "path": "pipeline.py"}, [str(ws)]) == []


def test_an_absolute_path_outside_every_root_is_still_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "elsewhere" / "secrets.txt"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    assert _errors("grep", {"pattern": "x", "path": str(outside)}, [str(ws)]) == ["path_scope"]
    # A `..` that lands outside is the same refusal; one that does not is fine.
    assert _errors("grep", {"pattern": "x", "path": "../elsewhere/secrets.txt"}, [str(ws)]) == ["path_scope"]
    assert _errors("grep", {"pattern": "x", "path": "sub/../ok.py"}, [str(ws)]) == []


def test_with_no_workspace_bound_the_old_rule_stands(tmp_path):
    """Nothing to be inside of: absolute and `..` are out, as before."""
    assert _errors("grep", {"pattern": "x", "path": str(tmp_path / "a.py")}) == ["path_scope"]
    assert _errors("grep", {"pattern": "x", "path": "../a.py"}) == ["path_scope"]
    assert _errors("grep", {"pattern": "x", "path": "a.py"}) == []


def test_a_second_project_root_counts_too(tmp_path):
    """A project with several attached folders: a path in the second one is
    inside the turn's roots, exactly as `_resolve_tool_path_in_roots` says."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (b / "notes.md").write_text("x", encoding="utf-8")
    assert _errors("grep", {"pattern": "x", "path": str(b / "notes.md")},
                   [str(a), str(b)]) == []


def test_the_loop_hands_the_validator_the_turns_roots():
    """The seam itself: `_resolve_tool_blocks` must pass `path_roots` through,
    or the fix above is dead code in production."""
    import inspect
    from src import agent_loop
    src = inspect.getsource(agent_loop)
    assert "path_roots=[r for r in [workspace, *(workspace_roots or [])] if r]" in src
    assert "path_roots=path_roots" in inspect.getsource(agent_loop._validate_native_tool_call)


# ── 3. answering the agent's own question continues the work ───────────────

def test_answering_an_ask_user_option_is_a_continuation_not_a_vague_message():
    """"Python + web UI (recommended)" — an option LABEL the agent wrote — was
    classified low-signal, and the low-signal floor withdraws bash/write_file."""
    from src.agent_loop import _classify_agent_request
    messages = [
        {"role": "user", "content": "build a png -> silhouette -> svg -> 3d pipeline"},
        {"role": "assistant", "content": "Two designs fit. Which do you want?"},
        {"role": "user", "content": "Python + web UI (recommended)"},
    ]
    forced = _classify_agent_request(messages, messages[-1]["content"],
                                     forced_continuation=True)
    assert forced["continuation"] is True
    assert forced["low_signal"] is False
    # The retrieval query inherits the work, not just the option label.
    assert "pipeline" in forced["retrieval_query"]


def test_a_turn_that_ends_on_a_question_is_asking_one():
    """The keyword list ("what would you like", "which one"…) missed plainly
    worded questions. A turn whose last line ends in `?` is asking one."""
    from src.agent_loop import _assistant_requested_followup
    for asked in ("Two designs fit. Which do you want?",
                  "¿Sigo con la opción 2?",
                  "Ready to start.\n\n**Shall I build it now?**"):
        assert _assistant_requested_followup([
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": asked},
            {"role": "user", "content": "the second one please"},
        ]) is True
    # A statement that merely contains a question mark mid-text does not count
    # unless one of the known phrasings is there.
    assert _assistant_requested_followup([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "I wondered whether? no. Done, all files written."},
        {"role": "user", "content": "ok"},
    ]) is False


def test_the_route_marks_an_answer_to_a_question():
    """`question_id` on the turn is the structural fact no wording heuristic
    can beat; the route must carry it into the harness options."""
    import pathlib
    route = pathlib.Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
    text = route.read_text(encoding="utf-8")
    assert '_loop_harness_options["answers_question"] = bool(question_id)' in text


# ── 4. base_revision is an optional precondition, not a format exam ────────

def test_a_base_revision_that_is_not_a_digest_is_treated_as_absent():
    from src.agent_tools.filesystem_tools import _normalize_base_revision, sha256_revision
    real = sha256_revision(b"content")
    bare = real[len("sha256:"):]
    assert _normalize_base_revision(real) == real
    assert _normalize_base_revision(bare) == real
    assert _normalize_base_revision(bare.upper()) == real
    assert _normalize_base_revision("sha256:" + bare.upper()) == real
    for junk in ("", None, "latest", "current", "none", "not-a-hash", "sha256:abc", bare[:-1]):
        assert _normalize_base_revision(junk) == "", junk


# ── 5. the model is told which machine it is on ───────────────────────────

def test_the_environment_block_is_built_from_the_executor(settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "auto"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True)
    monkeypatch.setattr("core.platform_compat.find_bash",
                        lambda: r"C:\Program Files\Git\bin\bash.exe")
    from src.agent_loop import _execution_environment_block
    block = _execution_environment_block({"bash", "python", "powershell", "desktop_screenshot"})
    assert "native Windows" in block
    assert "NO Docker" in block
    # The three sentences that answer what the live sessions got wrong.
    assert "Git Bash" in block
    assert "powershell" in block and "never wrap" in block.lower()
    assert "Absolute paths" in block
    assert "Verification is YOUR job" in block
    assert "desktop_screenshot" in block
    assert "run INSIDE a Linux container" not in block
    assert "native Windows" in block


def test_windows_strict_sandbox_still_tells_the_model_it_is_on_the_host(settings, monkeypatch):
    """The prompt used to check target==container BEFORE IS_WINDOWS, so a
    Windows box with sandbox=strict was told it lived in a Linux image."""
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True)
    monkeypatch.setattr("core.platform_compat.find_bash", lambda: r"C:\Git\bin\bash.exe")
    from src.agent_loop import _execution_environment_block
    block = _execution_environment_block({"bash", "python", "powershell"})
    assert "native Windows" in block
    assert "run INSIDE a Linux container" not in block
    assert "start Docker" not in block


def test_the_block_says_container_when_the_container_really_runs_it(settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", False)
    from src.agent_loop import _execution_environment_block
    block = _execution_environment_block({"bash", "python"})
    assert "container" in block and "/workspace" in block
    assert "native Windows" not in block


def test_the_block_only_costs_tokens_when_a_shell_or_vision_tool_is_offered():
    from src.agent_loop import _assemble_prompt, _SHELL_ENV_TOOLS
    assert "bash" in _SHELL_ENV_TOOLS and "powershell" in _SHELL_ENV_TOOLS
    assert "desktop_screenshot" in _SHELL_ENV_TOOLS
    without = _assemble_prompt({"read_file", "grep"})
    assert "Execution environment" not in without
    with_shell = _assemble_prompt({"read_file", "bash"})
    assert "Execution environment" in with_shell
    with_vision = _assemble_prompt({"read_file", "desktop_screenshot"})
    assert "Execution environment" in with_vision


# ── the powershell tool ───────────────────────────────────────────────────

def test_powershell_is_wired_everywhere_bash_is():
    """A tool registered in the handler table but missing from the schema, the
    catalogue or the capability map is a tool the model is offered and then
    refused — the exact shape of the bug this whole lot is about."""
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_capabilities import GUARD_SHELL_TOOLS, capabilities_for_action, ToolEffect
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, _PLAN_MODE_KNOWN_MUTATORS
    from src.agent_defs import SHELL_TOOLS
    from src.agent_harness import SHELL_TOOLS as HARNESS_SHELL_TOOLS

    assert "powershell" in TOOL_HANDLERS
    assert "powershell" in TOOL_TAGS
    assert "powershell" in {(s.get("function") or {}).get("name") for s in FUNCTION_TOOL_SCHEMAS}
    assert "powershell" in BUILTIN_TOOL_DESCRIPTIONS
    assert EXAMPLES["powershell"]
    # Same class as bash on every axis that decides what it may do.
    assert "powershell" in GUARD_SHELL_TOOLS
    assert ToolEffect.EXECUTE_CODE in capabilities_for_action("powershell", "ls").effects
    assert "powershell" in NON_ADMIN_BLOCKED_TOOLS
    assert "powershell" in _PLAN_MODE_KNOWN_MUTATORS
    assert "powershell" in SHELL_TOOLS
    assert "powershell" in HARNESS_SHELL_TOOLS
    # A native call converts to a block the dispatcher can run.
    block = function_call_to_tool_block("powershell", json.dumps({"script": "Get-Location"}))
    assert block is not None and block.tool_type == "powershell" and block.content == "Get-Location"


def test_powershell_is_blocked_in_plan_mode():
    from src.tool_security import plan_mode_disabled_tools
    assert "powershell" in plan_mode_disabled_tools()


@pytest.mark.asyncio
async def test_powershell_refuses_a_git_mutation_and_a_foreground_server():
    """The guards that make `bash` safe are not re-implemented per shell; both
    read the same two predicates, so a `git push` typed into PowerShell is
    routed to `git_push` and a foreground `uvicorn` is refused."""
    from src.agent_tools.subprocess_tools import PowerShellTool
    routed = await PowerShellTool().execute("git push origin master", {})
    assert routed["exit_code"] == 2 and routed["use_instead"] == "git_push"
    assert routed["error"].startswith("powershell:")
    served = await PowerShellTool().execute("python -m uvicorn app:app --port 7099", {})
    assert served["exit_code"] == 2 and "detached" in served["error"]


@pytest.mark.parametrize("command", [
    # The exact line from the live session, `.bat` and all.
    r'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\ws\install.bat" 2>&1',
    'powershell -Command "Write-Output hi"',
    "pwsh -c 'Get-Location'",
    "cmd /c install.bat",
    "cmd.exe //c dir",
    "cd /c/ws && powershell -File setup.ps1",
    "ls; cmd /c echo hi",
    "echo $(powershell -Command Get-Date)",
])
def test_a_windows_shell_launched_from_bash_is_routed_to_the_tool(command, monkeypatch):
    from src.agent_tools import subprocess_tools as st
    monkeypatch.setattr(st, "IS_WINDOWS", True)
    routed = st.windows_shell_routed_to_powershell(command)
    assert routed is not None, command
    assert routed["use_instead"] == "powershell"
    assert routed["exit_code"] == 2
    # The message has to carry the fix, not just the refusal: the live failure
    # was `-File` on a `.bat` coming back as a bare exit 127.
    assert "cmd.exe /c" in routed["error"] and ".ps1" in routed["error"]


@pytest.mark.parametrize("command", [
    "command -v powershell || echo NOPE",          # asking IF it exists
    "grep -rn powershell docs/",                    # the word in a search
    "ls scripts/powershell-notes.md",
    "python -c \"print('cmd')\"",
    "echo 'run cmd /c later'",                      # inside quotes, not a launch
    "git status",
    "which pwsh",
])
def test_merely_naming_a_windows_shell_is_not_launching_one(command, monkeypatch):
    from src.agent_tools import subprocess_tools as st
    monkeypatch.setattr(st, "IS_WINDOWS", True)
    assert st.windows_shell_routed_to_powershell(command) is None, command


def test_the_routing_is_windows_only(monkeypatch):
    """On a POSIX box `pwsh` is just a program, and there is no better tool to
    send it to — the `powershell` tool would have nothing to run it with."""
    from src.agent_tools import subprocess_tools as st
    monkeypatch.setattr(st, "IS_WINDOWS", False)
    assert st.windows_shell_routed_to_powershell("pwsh -c 'Get-Date'") is None


@pytest.mark.asyncio
async def test_bash_refuses_the_windows_shell_before_it_runs_anything(monkeypatch):
    from src.agent_tools import subprocess_tools as st
    monkeypatch.setattr(st, "IS_WINDOWS", True)

    async def _never(*a, **k):
        raise AssertionError("the command reached a subprocess")
    monkeypatch.setattr(st, "_create_bash_subprocess", _never)
    result = await st.BashTool().execute('powershell -Command "Write-Output hi"', {})
    assert result["exit_code"] == 2 and result["use_instead"] == "powershell"


@pytest.mark.asyncio
async def test_powershell_says_so_plainly_when_the_host_has_none(monkeypatch):
    from src.agent_tools import subprocess_tools
    monkeypatch.setattr(subprocess_tools, "find_powershell", lambda: None)
    result = await subprocess_tools.PowerShellTool().execute("Get-Location", {})
    assert result["exit_code"] == 1
    assert "no PowerShell on this host" in result["error"]
    assert "`bash`" in result["error"]          # and what to use instead


def test_the_python_tool_prefers_the_projects_own_interpreter(tmp_path):
    """`import cv2` "failed" in a project whose .venv had it, because the tool
    ran OUR virtualenv's interpreter."""
    from src.agent_tools.subprocess_tools import project_python
    ws = tmp_path / "proj"
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        d = ws / ".venv" / rel[0]
        d.mkdir(parents=True, exist_ok=True)
        (d / rel[1]).write_text("", encoding="utf-8")
    picked = project_python(str(ws))
    assert os.path.realpath(picked).startswith(os.path.realpath(str(ws / ".venv")))
    # No venv in the workspace: something that exists, never an empty string.
    assert project_python(str(tmp_path / "empty"))


# ── the folder-scoped approval ───────────────────────────────────────────

@pytest.fixture()
def grants(tmp_path, monkeypatch):
    from src import tool_approval_grants as mod
    monkeypatch.setattr(mod, "_path", lambda: str(tmp_path / "grants.json"))
    return mod


def test_a_folder_grant_outlives_the_chat_and_covers_its_subtree(grants, tmp_path):
    """Luis: «que se quede así siempre en el proyecto, no se me resetee en cada
    chat nuevo». The chat scope died with the chat; this one is on disk."""
    ws = tmp_path / "Proyectos" / "Silhouettes"
    (ws / "sub").mkdir(parents=True)
    sibling = tmp_path / "Proyectos" / "Otro"
    sibling.mkdir()

    assert grants.is_granted("admin", str(ws)) is False
    assert grants.grant("admin", str(ws), tool="bash", session_id="s1")
    assert grants.is_granted("admin", str(ws)) is True
    assert grants.is_granted("admin", str(ws / "sub")) is True
    # Never sideways, and never to another owner.
    assert grants.is_granted("admin", str(sibling)) is False
    assert grants.is_granted("someone-else", str(ws)) is False
    # The parent is not granted by granting the child.
    assert grants.is_granted("admin", str(tmp_path / "Proyectos")) is False

    assert grants.revoke("admin", str(ws)) is True
    assert grants.is_granted("admin", str(ws)) is False
    assert grants.revoke("admin", str(ws)) is False


def test_a_chat_with_no_workspace_has_nothing_to_remember(grants):
    assert grants.grant("admin", "") is None
    assert grants.is_granted("admin", "") is False


def test_the_card_offers_the_folder_scope_and_the_gate_reads_it(grants, tmp_path, monkeypatch):
    from src.tool_approval_scopes import (WORKSPACE_APPROVAL_DECISION, ToolApprovalScope,
                                          scope_for_decision)
    assert scope_for_decision(WORKSPACE_APPROVAL_DECISION) is ToolApprovalScope.WORKSPACE

    from src import agent_loop
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("src.tool_approval_grants.is_granted", grants.is_granted)
    assert agent_loop._workspace_gate_granted("admin", str(ws)) is False
    grants.grant("admin", str(ws))
    assert agent_loop._workspace_gate_granted("admin", str(ws)) is True
    # No workspace, no bypass — a grant can never apply to a chat with no folder.
    assert agent_loop._workspace_gate_granted("admin", "") is False


def test_the_folder_grant_never_outranks_the_destructive_command_guard():
    """The bypass it buys is the post-external-context gate and nothing else.
    `decision_for` checks the command guard BEFORE it looks at the bypass, so
    a grant given for a `ls` can never auto-run an `rm -rf /` later."""
    import inspect
    from src.tool_capabilities import ToolRunSecurityContext
    src = inspect.getsource(ToolRunSecurityContext.decision_for)
    guard = src.index("_command_guard_denial")
    bypass = src.index("if self.approval_gate_bypassed:")
    assert guard < bypass, "the command guard must be evaluated before the bypass"


def test_a_broken_grant_store_never_fails_a_turn(monkeypatch, tmp_path):
    from src import agent_loop
    def _boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr("src.tool_approval_grants.is_granted", _boom)
    assert agent_loop._workspace_gate_granted("admin", str(tmp_path)) is False


# ── 6. the workspace floor includes the host's own toolchain ──────────────

def test_workspace_floor_includes_shell_search_and_desktop_vision():
    from src.agent_loop import (
        WORKSPACE_TOOL_FLOOR, WORKSPACE_TOOL_FLOOR_READ, WORKSPACE_TOOL_FLOOR_SHELL,
    )
    assert {"bash", "python", "powershell"} <= WORKSPACE_TOOL_FLOOR_SHELL
    assert {"grep", "desktop_screenshot", "desktop_list_windows"} <= WORKSPACE_TOOL_FLOOR_READ
    assert WORKSPACE_TOOL_FLOOR_SHELL <= WORKSPACE_TOOL_FLOOR
    assert "write_file" not in WORKSPACE_TOOL_FLOOR


def test_plan_mode_keeps_desktop_vision_and_grep_as_readonly():
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, plan_mode_disabled_tools
    assert "desktop_screenshot" in PLAN_MODE_READONLY_TOOLS
    assert "desktop_list_windows" in PLAN_MODE_READONLY_TOOLS
    disabled = plan_mode_disabled_tools()
    assert "desktop_screenshot" not in disabled
    assert "bash" in disabled and "powershell" in disabled


def test_the_prompt_does_not_forbid_gui_verification():
    """Live sessions read 'no GUI' / 'Don't try to RUN GUI apps' as 'I cannot
    look at the screen', then handed the user the verification. Desktop
    vision is the way to look."""
    from src.agent_loop import TOOL_SECTIONS, _AGENT_RULES, _API_AGENT_RULES
    bash = TOOL_SECTIONS["bash"]
    python = TOOL_SECTIONS["python"]
    assert "Don't try to RUN" not in bash
    assert "Same sandbox limits" not in python
    assert "desktop_screenshot" in bash
    for rules in (_AGENT_RULES, _API_AGENT_RULES):
        assert "desktop_screenshot" in rules
        assert "visual" in rules.lower()


def test_powershell_tool_section_supports_bg():
    from src.agent_loop import TOOL_SECTIONS
    assert "#!bg" in TOOL_SECTIONS["powershell"]
    assert "NOT supported" not in TOOL_SECTIONS["powershell"]


@pytest.mark.asyncio
async def test_bash_without_git_bash_delegates_to_powershell(monkeypatch):
    from src.agent_tools import subprocess_tools as st
    monkeypatch.setattr(st, "IS_WINDOWS", True)
    monkeypatch.setattr(st, "find_bash", lambda: None)

    called = {}

    async def _ps(self, content, ctx):
        called["script"] = content
        return {"output": "hi", "exit_code": 0, "execution_target": {"kind": "windows"}}

    monkeypatch.setattr(st.PowerShellTool, "execute", _ps)
    result = await st.BashTool()._on_host("echo hi", None, None, "sess")
    assert result["exit_code"] == 0
    assert called["script"] == "echo hi"
    assert "Git Bash is required" not in str(result)


@pytest.mark.asyncio
async def test_powershell_bg_marker_launches_a_job(monkeypatch):
    from collections import namedtuple
    from src.tool_execution import _execute_tool_block_impl

    launched = {}

    def _launch(command, session_id, cwd=None, max_runtime_s=3600, shell="bash"):
        launched.update(command=command, session_id=session_id, shell=shell, cwd=cwd)
        return {"id": "job-ps1"}

    monkeypatch.setattr("src.bg_jobs.launch", _launch)
    Block = namedtuple("ToolBlock", ["tool_type", "content"])
    desc, result = await _execute_tool_block_impl(
        Block("powershell", "#!bg\nwinget install --id Foo.Bar -e"),
        session_id="sess-1",
        owner="admin",
    )
    assert result["exit_code"] == 0
    assert result["bg_job_id"] == "job-ps1"
    assert launched["command"] == "winget install --id Foo.Bar -e"
    assert launched["shell"] == "powershell"
    assert "background" in desc


def test_bg_jobs_can_spawn_through_powershell(monkeypatch, tmp_path):
    from src import bg_jobs
    captured = {}

    class _Proc:
        pid = 4242

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", _popen)
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path)
    monkeypatch.setattr("core.platform_compat.find_bash", lambda: None)
    monkeypatch.setattr(bg_jobs, "find_powershell", lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    rec = bg_jobs._spawn_process("abc", "Write-Output hi", str(tmp_path), shell="powershell")
    assert rec["status"] == "running"
    assert captured["argv"][0].lower().endswith("powershell.exe")
    assert any("abc" in str(a) or a.endswith(".ps1") for a in captured["argv"])


def test_doctor_on_windows_does_not_tell_you_to_start_docker(settings, monkeypatch):
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True)
    settings.update({"agent_sandbox_execution": False, "agent_sandbox_mode": "auto"})
    from src.doctor import _agent_sandbox
    finding = _agent_sandbox()
    assert "start Docker" not in (finding.fix or "")
    assert "turn on `agent_sandbox_execution`" not in (finding.fix or "")
