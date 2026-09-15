"""The agent's own shell inside the container (src/sandbox_exec).

This is the switch that changes what happens when the model runs a command, so
the tests are mostly about the two states nobody thinks to check:

* **off** must be byte-identical to before the module existed — a flag that
  changes behaviour while switched off is worse than no flag;
* **on, strict, and the sandbox is missing** must be a refusal. Not a
  fallback. That rule exists to stop a closed Docker Desktop from quietly
  putting the model's command back on the machine, and that failure would
  look like success in every log;
* **on, auto (the default since 14-09-2026)** hands the command to the host
  when the container cannot serve — always on native Windows, and when the
  daemon does not answer elsewhere — and SAYS so on the result
  (`sandbox_skipped`), so nothing is silent either way. Seen live: on Windows
  the strict rule turned every chat into «start Docker» or `/bin/sh: cmd:
  not found`, and a container that cannot run the project's own toolchain
  verifies nothing.

The container tests skip when Docker is not there and say so; they pin
`strict` so they exercise the container on Windows too.
"""
from __future__ import annotations

import os

import pytest

from src import sandbox_exec
from src.agent_tools.subprocess_tools import BashTool, PythonTool
from src.execution_backends import DockerWorkspaceBackend

IMAGE = sandbox_exec.DEFAULT_IMAGE
_READY = DockerWorkspaceBackend(image=IMAGE).probe()
needs_docker = pytest.mark.skipif(
    not _READY["ok"], reason=f"needs docker and {IMAGE}: {_READY.get('detail')}")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("the brief\n", encoding="utf-8")
    import src.tool_execution as te
    monkeypatch.setattr(te, "agent_cwd", lambda: str(ws))
    return str(ws)


@pytest.fixture()
def settings(monkeypatch):
    values = {}
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture(autouse=True)
def posix_sandbox_host(monkeypatch, request):
    """Strict-container tests pin POSIX. Native Windows is covered by the
    tests that name Windows in their title and by test_windows_native_execution."""
    if "windows" in request.node.name.lower():
        return
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)


# ── off means off ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_with_the_setting_off_the_tool_takes_the_path_it_always_took(workspace, settings):
    assert await sandbox_exec.run("bash", "echo hi", {}) is None
    result = await BashTool().execute("echo hello-from-the-host", {})
    assert result["output"] == "hello-from-the-host"
    assert result["exit_code"] == 0
    # None of the sandbox's keys appear: this result is shaped exactly like
    # the ones the tool returned before the module existed.
    # EXEC-01 (Lote 67) adds the descriptive `execution_target`; the sandbox's
    # own keys still never appear.
    assert set(result) - {"execution_target"} == {"output", "exit_code"}


@pytest.mark.asyncio
async def test_a_truthy_string_does_not_switch_the_sandbox_on(workspace, settings):
    for truthy in ("yes", "true", 1, "docker"):
        settings["agent_sandbox_execution"] = truthy
        assert sandbox_exec.enabled() is False
        assert await sandbox_exec.run("bash", "echo x", {}) is None


@pytest.mark.asyncio
async def test_the_sandbox_only_claims_bash_and_python(workspace, settings):
    settings["agent_sandbox_execution"] = True
    assert await sandbox_exec.run("web_fetch", "https://example.com", {}) is None
    assert await sandbox_exec.run("read_file", "x.txt", {}) is None


# ── on, strict, and unable: a refusal, never the host ──────────────────────

@pytest.mark.asyncio
async def test_strict_a_missing_image_refuses_and_the_command_does_not_run(workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict",
                     "agent_sandbox_image": "faustus-no-such-image:0.0.1"})
    result = await BashTool().execute("echo THIS-MUST-NOT-RUN", {})
    assert result["sandbox_refused"] is True
    assert result["sandboxed"] is False
    assert result["exit_code"] == 126
    assert "THIS-MUST-NOT-RUN" not in str(result)
    assert "does not fall back" in result["error"]
    assert "agent_sandbox_execution" in result["error"]      # how to turn it off
    assert "agent_sandbox_mode" in result["error"]           # …or how to let the host run it


@pytest.mark.asyncio
async def test_strict_a_workspace_that_is_not_a_directory_refuses_too(tmp_path, settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    import src.tool_execution as te
    monkeypatch.setattr(te, "agent_cwd", lambda: str(tmp_path / "nope"))
    result = await BashTool().execute("echo nope", {})
    assert result["sandbox_refused"] is True
    assert "not a directory" in result["error"]


# ── on, auto: the host when the container cannot serve, and it says so ─────

def test_mode_is_auto_unless_the_operator_wrote_strict(settings):
    for raw in (None, "", "auto", "AUTO", "docker", "yes", 1):
        settings["agent_sandbox_mode"] = raw
        assert sandbox_exec.mode() == "auto"
    settings["agent_sandbox_mode"] = "strict"
    assert sandbox_exec.mode() == "strict"
    settings["agent_sandbox_mode"] = " Strict "
    assert sandbox_exec.mode() == "strict"


@pytest.mark.asyncio
async def test_auto_on_windows_runs_on_the_host_and_marks_the_result(workspace, settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "auto"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    assert "Windows" in (sandbox_exec.host_skip_reason() or "")
    assert sandbox_exec.describe()["target"] == "host"
    assert await sandbox_exec.run("bash", "echo x", {}) is None
    result = await BashTool().execute("echo auto-ran-here", {})
    assert result["output"] == "auto-ran-here"
    assert result["exit_code"] == 0
    assert "sandbox_refused" not in result
    assert "Windows" in result["sandbox_skipped"]
    assert result["execution_target"]["kind"] != "container"


@pytest.mark.asyncio
async def test_auto_with_no_daemon_runs_on_the_host_and_names_the_reason(workspace, settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "auto",
                     "agent_sandbox_image": "faustus-no-such-image:0.0.1"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: False)
    assert sandbox_exec.host_skip_reason() is None          # not the host kind…
    result = await BashTool().execute("echo auto-no-image", {})   # …but the probe
    assert result["output"] == "auto-no-image"
    assert "sandbox_refused" not in result
    assert result["sandbox_skipped"]                          # the probe's own reason
    # A refusal in strict mode for the same probe result, to pin the contrast.
    settings["agent_sandbox_mode"] = "strict"
    refused = await BashTool().execute("echo strict-no-image", {})
    assert refused["sandbox_refused"] is True


@pytest.mark.asyncio
async def test_the_skip_reason_is_consumed_once_and_never_leaks_to_a_host_only_call(workspace, settings, monkeypatch):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "auto"})
    monkeypatch.setattr(sandbox_exec, "_host_is_windows", lambda: True)
    first = await BashTool().execute("echo one", {})
    assert first["sandbox_skipped"]
    settings["agent_sandbox_execution"] = False
    second = await BashTool().execute("echo two", {})
    assert "sandbox_skipped" not in second


# ── the one rewrite ────────────────────────────────────────────────────────

def test_the_whole_path_token_is_converted_not_just_the_prefix():
    """Rewriting only the root leaves `/workspace\\src\\x.py`, which on Linux
    is one filename containing backslashes. The command then fails for a
    reason nothing in its output explains."""
    ws = r"D:\proj\demo"
    text, hits = sandbox_exec.to_container(
        r"wc -l D:\proj\demo\src\x.py && grep D:/proj/demo/README.md -e demo", ws)
    assert hits == 2
    assert text == "wc -l /workspace/src/x.py && grep /workspace/README.md -e demo"
    assert "\\" not in text


def test_a_neighbouring_directory_is_not_swallowed():
    """A workspace at D:\\proj\\demo must not eat the first half of
    D:\\proj\\demo2 and hand the container /workspace2/other.txt."""
    ws = r"D:\proj\demo"
    untouched, hits = sandbox_exec.to_container(r"cat D:\proj\demo2\other.txt", ws)
    assert hits == 0 and untouched == r"cat D:\proj\demo2\other.txt"


def test_the_bare_root_and_nothing_at_all_both_work():
    ws = r"D:\proj\demo"
    assert sandbox_exec.to_container("ls -la", ws) == ("ls -la", 0)
    assert sandbox_exec.to_container(r"cd D:\proj\demo", ws) == ("cd /workspace", 1)
    assert sandbox_exec.to_container(r'cat "D:\proj\demo\a.txt"', ws) == (
        'cat "/workspace/a.txt"', 1)


def test_container_paths_come_back_as_host_paths():
    ws = r"D:\proj\demo"
    # A Windows root comes back spelled as the host spells it, on any OS the
    # tests run on (on Linux `os.path.abspath` would glue the cwd in front).
    assert sandbox_exec.to_host("/workspace/src/x.py: 3 lines", ws) == \
        r"D:\proj\demo" + "/src/x.py: 3 lines"
    assert sandbox_exec.to_host("", ws) == ""


# ── the manifest the built-in tools go through ─────────────────────────────

def test_the_agent_shell_asks_for_no_more_than_a_skill_could(settings):
    manifest = sandbox_exec.manifest()
    assert manifest.permissions.backends == ("docker_workspace",)
    assert manifest.permissions.host_access is False
    assert manifest.permissions.secrets == ()
    assert manifest.permissions.filesystem == "workspace"
    assert manifest.permissions.network is False
    assert manifest.effective_approvals() == ()


def test_turning_the_network_on_is_visible_in_the_manifest_and_earns_a_card(settings):
    settings["agent_sandbox_network"] = True
    manifest = sandbox_exec.manifest()
    assert manifest.permissions.network is True
    # And it raises the card even though the manifest never declares one.
    assert "network" in manifest.effective_approvals()


def test_a_nonsense_timeout_setting_falls_back_instead_of_running_forever(settings):
    for bad in ("900", -1, 0, 10 ** 9, None):
        settings["agent_sandbox_timeout_s"] = bad
        assert sandbox_exec.timeout_s() == sandbox_exec.DEFAULT_TIMEOUT_S
    settings["agent_sandbox_timeout_s"] = 120
    assert sandbox_exec.timeout_s() == 120


# ── on, with a real container ──────────────────────────────────────────────

@needs_docker
@pytest.mark.asyncio
async def test_the_agents_own_bash_runs_unprivileged_and_sees_only_the_workspace(
        workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    result = await BashTool().execute("id -u; ls", {})
    assert result["sandboxed"] is True
    assert result["isolation"] == "container"
    assert result["exit_code"] == 0
    assert result["output"].split() == ["1000", "notes.txt"]


@needs_docker
@pytest.mark.asyncio
async def test_the_agents_own_bash_cannot_reach_the_app_key(workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    result = await BashTool().execute(
        "cat /workspace/../data/.app_key 2>&1; cat /data/.app_key 2>&1", {})
    assert "No such file or directory" in result["output"]
    from src.constants import APP_KEY_FILE
    if os.path.exists(APP_KEY_FILE):
        assert open(APP_KEY_FILE, "rb").read()[:8] not in result["output"].encode()


@needs_docker
@pytest.mark.asyncio
async def test_an_absolute_host_path_in_the_command_still_finds_its_file(workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    result = await BashTool().execute(f"wc -l {os.path.join(workspace, 'notes.txt')}", {})
    assert result["exit_code"] == 0
    assert result["workspace_paths_rewritten"] == 1
    assert result["output"].startswith("1 ")
    # …and the path handed back is one the rest of Faustus can open.
    assert os.path.exists(result["output"].split(" ", 1)[1].strip().replace("/", os.sep))


@needs_docker
@pytest.mark.asyncio
async def test_the_python_tool_runs_the_images_interpreter_not_ours(workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    result = await PythonTool().execute("import sys; print(sys.executable)", {})
    assert result["sandboxed"] is True
    assert result["output"].startswith("/usr/local/bin/python")


@needs_docker
@pytest.mark.asyncio
async def test_the_network_is_denied_from_the_agents_shell_by_default(workspace, settings):
    settings.update({"agent_sandbox_execution": True, "agent_sandbox_mode": "strict"})
    result = await BashTool().execute(
        "python -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\" "
        "2>/dev/null && echo REACHED || echo denied", {})
    assert result["output"].strip() == "denied"
    assert result["network"] is False
