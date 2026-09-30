"""H01: Code Mode confined runtime.

Two groups. The first needs no Docker at all (command line, path spelling,
refusal semantics, guest inline protocol). The second starts real containers
and is skipped unless a Docker daemon with a Python image is reachable
(``container_test_image`` fixture in conftest); a skip there is not evidence of
confinement, only the first group ran.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src import container_mounts
from src.code_mode import confined
from src.code_mode import runner as runner_mod
from src.code_mode.runner import run_code_mode


# ── no Docker needed ───────────────────────────────────────────────────────

def _flags(plan):
    return plan.argv


def test_plan_has_no_environment_passthrough_and_no_network_by_default(tmp_path):
    plan = confined.build_plan(workspace=str(tmp_path), timeout_seconds=30, image="img:1")
    argv = _flags(plan)
    assert "-e" not in argv and "--env" not in argv and "--env-file" not in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert "--pids-limit" in argv and "--memory" in argv and "--cpus" in argv
    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith(",target=/workspace,readonly")
    assert plan.guarantees["filesystem_scope"] == "workspace_read_only"
    assert plan.guarantees["network_isolated"] is True
    assert plan.guarantees["secrets_passed"] is False


def test_plan_reflects_grants(tmp_path):
    plan = confined.build_plan(workspace=str(tmp_path), workspace_access="read_write",
                               network=True, timeout_seconds=30, image="img:1")
    argv = _flags(plan)
    assert argv[argv.index("--network") + 1] == "bridge"
    assert not argv[argv.index("--mount") + 1].endswith("readonly")
    assert plan.guarantees["filesystem_scope"] == "workspace_read_write"
    assert plan.guarantees["network_isolated"] is False


def test_plan_without_workspace_grant_mounts_nothing(tmp_path):
    plan = confined.build_plan(workspace=str(tmp_path), workspace_access="none",
                               timeout_seconds=30, image="img:1")
    assert "--mount" not in plan.argv
    assert plan.guarantees["filesystem_scope"] == "none"
    plan = confined.build_plan(workspace=None, timeout_seconds=30, image="img:1")
    assert "--mount" not in plan.argv


def test_extra_roots_are_mounted_separately_and_deduplicated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    plan = confined.build_plan(workspace=str(a), workspace_roots=[str(a), str(b)],
                               timeout_seconds=30, image="img:1")
    mounts = [plan.argv[i + 1] for i, v in enumerate(plan.argv) if v == "--mount"]
    assert len(mounts) == 2
    assert "target=/workspace," in mounts[0] and "target=/roots/1," in mounts[1]


def test_windows_workspace_path_spelling():
    plan = confined.build_plan(workspace="C:\\Users\\someone\\proj", workspace_roots=["D:\\other"],
                               timeout_seconds=30, image="img:1")
    mounts = [plan.argv[i + 1] for i, v in enumerate(plan.argv) if v == "--mount"]
    assert mounts[0] == "type=bind,source=C:/Users/someone/proj,target=/workspace,readonly"
    assert mounts[1].startswith("type=bind,source=D:/other,target=/roots/1")


def test_unmountable_paths_are_refused_not_guessed():
    for bad in ("\\\\server\\share\\x", "C:\\a,b", 'C:\\a"b'):
        with pytest.raises(container_mounts.MountError):
            confined.build_plan(workspace=bad, timeout_seconds=30, image="img:1")


def test_msys_spelling_round_trip():
    assert container_mounts.to_msys_path("C:\\work\\p") == "//c/work/p"
    assert container_mounts.from_msys_path("//c/work/p") == "C:/work/p"
    assert container_mounts.normalize_host_path("\\\\?\\C:\\work") == "C:/work"


def test_unknown_runtime_resolves_to_confined():
    assert confined.resolve_runtime("hots") == "confined"
    assert confined.resolve_runtime("HOST") == "host"
    assert confined.resolve_runtime("confined") == "confined"


def test_guest_inline_mode_reads_code_from_config_line():
    guest = os.path.join(os.path.dirname(confined.__file__), "guest.py")
    config = json.dumps({"max_calls": 3, "max_output_bytes": 1000, "code": "print('inline ok')"})
    done = subprocess.run([sys.executable, "-I", guest, "--inline"], input=config + "\n",
                          capture_output=True, text=True, timeout=30)
    frames = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert frames[-1]["type"] == "final" and frames[-1]["status"] == "ok"
    assert frames[-1]["output"].strip() == "inline ok"


def test_guest_command_is_one_argument_without_source_files():
    cmd = confined.guest_command()
    assert cmd[:3] == ["python3", "-I", "-c"] and cmd[-1] == "--inline"
    assert "\n" not in cmd[3]


async def test_unavailable_backend_refuses_and_never_spawns_anything(monkeypatch, tmp_path):
    spawned = []

    async def boom(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("no process may be started when confinement is unavailable")

    monkeypatch.setattr(runner_mod.asyncio, "create_subprocess_exec", boom)
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {
        "ok": False, "reason": "backend_unavailable", "detail": "daemon did not answer"})
    result = await run_code_mode("print('must not run')", runtime="confined", workspace=str(tmp_path))
    assert spawned == []
    assert result["refused"] is True and result["exit_code"] == 126
    assert result["runtime_guarantees"]["mode"] == "not_executed"
    assert result["runtime_policy"] == {"requested": "confined", "effective": "not_executed",
                                        "reason": "backend_unavailable: daemon did not answer"}
    assert "host" in result["error"] and "NOT run" in result["error"]


async def test_default_runtime_is_confined_not_host(monkeypatch, tmp_path):
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {
        "ok": False, "reason": "backend_unavailable", "detail": "x"})
    result = await run_code_mode("print('x')", workspace=str(tmp_path))
    assert result["runtime_guarantees"]["mode"] == "not_executed"


async def test_host_runtime_is_refused_when_required_confinement_is_on(monkeypatch):
    from src import sandbox_exec
    monkeypatch.setattr(sandbox_exec, "confinement_required", lambda: True)
    result = await run_code_mode("open('/tmp/x','w')", runtime="host")
    assert result["refused"] is True
    assert result["runtime_policy"]["requested"] == "host"
    assert result["runtime_guarantees"]["mode"] == "not_executed"


async def test_host_runtime_reports_its_effective_policy(tmp_path):
    result = await run_code_mode("print('hi')", runtime="host", workspace=str(tmp_path))
    assert result["exit_code"] == 0
    assert result["runtime_guarantees"]["mode"] == "host_process"
    assert result["runtime_policy"]["effective"] == "host_process"


async def test_stray_output_from_a_child_is_bounded_on_the_host_runtime(monkeypatch):
    monkeypatch.setattr(runner_mod, "_limits", lambda: {
        "timeout_seconds": 30, "max_calls": 5, "max_output_bytes": 4_000,
        "max_memory_bytes": runner_mod.DEFAULT_MAX_MEMORY_BYTES})
    code = ("import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', \"print('spam line\\\\n' * 100000)\"])\n"
            "print('unreachable')\n")
    t0 = time.monotonic()
    result = await run_code_mode(code, runtime="host")
    assert result["receipt"]["terminated_by"] == "output"
    assert time.monotonic() - t0 < 25


# ── real containers ────────────────────────────────────────────────────────

def _open_dir(path):
    os.chmod(path, 0o755)
    return path


@pytest.fixture
def use_image(monkeypatch, container_test_image):
    monkeypatch.setattr(confined, "_sandbox_image", lambda: container_test_image)
    return container_test_image


def _containers_left():
    done = subprocess.run(["docker", "ps", "-a", "--filter", f"name={confined._CONTAINER_PREFIX}",
                           "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
    return [n for n in done.stdout.split() if n]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _open_dir(root)
    _open_dir(tmp_path)
    (root / "inside.txt").write_text("inside-ok", encoding="utf-8")
    os.chmod(root / "inside.txt", 0o644)
    return root


async def test_workspace_is_readable_and_reports_container_guarantees(use_image, workspace):
    result = await run_code_mode("print(open('inside.txt').read())", workspace=str(workspace))
    assert result["exit_code"] == 0, result
    assert result["output"].strip() == "inside-ok"
    assert result["runtime_guarantees"]["mode"] == "container"
    assert result["runtime_guarantees"]["filesystem_scope"] == "workspace_read_only"
    assert result["runtime_guarantees"]["network_isolated"] is True
    assert result["runtime_policy"] == {"requested": "confined", "effective": "container", "reason": ""}
    assert _containers_left() == []


async def test_decoy_outside_the_root_is_not_readable(use_image, workspace, tmp_path):
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("host-secret", encoding="utf-8")
    os.chmod(decoy, 0o644)
    code = (f"import os\n"
            f"for p in ({str(decoy)!r}, '../decoy.txt', '/workspace/../decoy.txt'):\n"
            f"    try:\n        print('READ', open(p).read())\n"
            f"    except OSError as e:\n        print('DENIED', type(e).__name__)\n")
    result = await run_code_mode(code, workspace=str(workspace))
    assert result["exit_code"] == 0, result
    assert "host-secret" not in result["output"]
    assert result["output"].count("DENIED") == 3


async def test_sibling_directory_is_not_readable(use_image, workspace, tmp_path):
    sibling = tmp_path / "ws2"
    sibling.mkdir()
    _open_dir(sibling)
    (sibling / "secret.txt").write_text("sibling-secret", encoding="utf-8")
    os.chmod(sibling / "secret.txt", 0o644)
    code = (f"import os\n"
            f"print(os.path.exists({str(sibling)!r}), os.path.exists('/workspace2'), "
            f"os.path.exists('../ws2'))\n")
    result = await run_code_mode(code, workspace=str(workspace))
    assert result["output"].split() == ["False", "False", "False"], result
    assert "sibling-secret" not in result["output"]


async def test_symlink_inside_root_does_not_extend_access(use_image, workspace, tmp_path):
    decoy = tmp_path / "decoy2.txt"
    decoy.write_text("host-secret-2", encoding="utf-8")
    os.chmod(decoy, 0o644)
    os.symlink(str(decoy), workspace / "abs_link")
    os.symlink("../decoy2.txt", workspace / "rel_link")
    code = ("for p in ('abs_link', 'rel_link'):\n"
            "    try:\n        print('READ', open(p).read())\n"
            "    except OSError as e:\n        print('DENIED', type(e).__name__)\n")
    result = await run_code_mode(code, workspace=str(workspace))
    assert "host-secret-2" not in result["output"]
    assert result["output"].count("DENIED") == 2


async def test_workspace_is_read_only_by_default_and_writable_when_granted(use_image, workspace):
    write = "open('new.txt','w').write('w')\nprint('wrote')\n"
    denied = await run_code_mode(write, workspace=str(workspace))
    assert denied["exit_code"] != 0 and not (workspace / "new.txt").exists()
    _open_dir(workspace)
    os.chmod(workspace, 0o777)
    granted = await run_code_mode(write, workspace=str(workspace), workspace_access="read_write")
    assert granted["exit_code"] == 0, granted
    assert granted["runtime_guarantees"]["filesystem_scope"] == "workspace_read_write"
    assert (workspace / "new.txt").read_text() == "w"


async def test_no_workspace_grant_means_no_files_at_all(use_image, workspace):
    # An image may ship an empty /workspace (the container's working directory
    # is created when it is missing): what matters is that none of the host
    # workspace's files are there.
    result = await run_code_mode(
        "import os\nprint(os.listdir('/workspace') if os.path.isdir('/workspace') else [])",
        workspace=str(workspace), workspace_access="none")
    assert result["exit_code"] == 0, result
    assert result["output"].strip() == "[]"


async def test_faustus_environment_and_host_files_are_not_visible(use_image, workspace, monkeypatch):
    monkeypatch.setenv("FAUSTUS_SYNTHETIC_TOKEN", "super-secret-value")
    code = ("import os\nprint(sorted(k for k in os.environ if 'FAUSTUS' in k or 'TOKEN' in k))\n"
            "print(os.path.exists('/home/claude'), os.getcwd())\n")
    result = await run_code_mode(code, workspace=str(workspace))
    assert "super-secret-value" not in result["output"]
    assert result["output"].splitlines()[0] == "[]"
    assert result["output"].splitlines()[1].split()[-1] == "/workspace"


class _Quiet(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"reached")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = HTTPServer(("0.0.0.0", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def _host_address_from_container():
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return "host.docker.internal"
    done = subprocess.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"],
                          capture_output=True, text=True, timeout=30)
    return done.stdout.strip() or "172.17.0.1"


_FETCH = ("import urllib.request\n"
          "try:\n"
          "    print('GOT', urllib.request.urlopen('http://{host}:{port}/', timeout=5).read().decode())\n"
          "except Exception as e:\n"
          "    print('BLOCKED', type(e).__name__)\n")


async def test_network_is_blocked_unless_granted(use_image, workspace, local_server):
    code = _FETCH.format(host=_host_address_from_container(), port=local_server)
    blocked = await run_code_mode(code, workspace=str(workspace))
    assert blocked["output"].strip().startswith("BLOCKED"), blocked
    granted = await run_code_mode(code, workspace=str(workspace), network=True)
    assert granted["output"].strip() == "GOT reached", granted
    assert granted["runtime_guarantees"]["network_isolated"] is False


async def test_child_process_is_confined_too(use_image, workspace, tmp_path, local_server):
    decoy = tmp_path / "child_decoy.txt"
    decoy.write_text("child-secret", encoding="utf-8")
    os.chmod(decoy, 0o644)
    fetch = _FETCH.format(host=_host_address_from_container(), port=local_server).replace("\n", "; ")
    code = ("import subprocess\n"
            f"print(subprocess.run(['cat', {str(decoy)!r}], capture_output=True, text=True).returncode)\n"
            "import sys\n"
            "p = subprocess.run([sys.executable, '-c', '''"
            + _FETCH.format(host=_host_address_from_container(), port=local_server) + "'''],"
            " capture_output=True, text=True)\n"
            "print(p.stdout.strip())\n")
    result = await run_code_mode(code, workspace=str(workspace))
    lines = result["output"].splitlines()
    assert lines[0] != "0", result
    assert lines[1].startswith("BLOCKED"), result
    assert "child-secret" not in result["output"]


async def test_descendants_die_with_the_container_on_timeout(use_image, workspace, monkeypatch):
    monkeypatch.setattr(runner_mod, "_limits", lambda: {
        "timeout_seconds": 3, "max_calls": 5, "max_output_bytes": 10_000,
        "max_memory_bytes": runner_mod.DEFAULT_MAX_MEMORY_BYTES})
    code = ("import subprocess, time\n"
            "subprocess.Popen(['sleep', '300'])\n"
            "time.sleep(300)\n")
    t0 = time.monotonic()
    result = await run_code_mode(code, workspace=str(workspace))
    assert result["receipt"]["terminated_by"] == "timeout"
    assert time.monotonic() - t0 < 30
    assert _containers_left() == []


async def test_stdout_spam_is_bounded_in_the_container(use_image, workspace, monkeypatch):
    monkeypatch.setattr(runner_mod, "_limits", lambda: {
        "timeout_seconds": 30, "max_calls": 5, "max_output_bytes": 4_000,
        "max_memory_bytes": runner_mod.DEFAULT_MAX_MEMORY_BYTES})
    printed = await run_code_mode("while True:\n    print('x' * 100)\n", workspace=str(workspace))
    assert printed["receipt"]["terminated_by"] == "output"
    assert len(printed["output"]) <= 4_000 if printed.get("output") else True
    child = await run_code_mode(
        "import subprocess, sys\n"
        "subprocess.run(['python3', '-c', \"import sys\\nwhile True: sys.stdout.write('spam\\\\n' * 1000)\"])\n",
        workspace=str(workspace))
    assert child["receipt"]["terminated_by"] in ("output", "protocol_frame_limit", "timeout")
    assert _containers_left() == []


async def test_memory_limit_is_enforced_by_the_container(use_image, workspace, monkeypatch):
    monkeypatch.setattr(confined, "MEMORY_MB_SETTING", "agent_code_mode_memory_mb")
    from src import settings as settings_mod
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: 64 if key == "agent_code_mode_memory_mb"
                        else real(key, default))
    result = await run_code_mode("data = bytearray(400 * 1024 * 1024)\nprint(len(data))\n",
                                 workspace=str(workspace))
    assert result["exit_code"] != 0
    assert result["receipt"]["terminated_by"] in ("memory", "error")
    assert "400" not in (result.get("output") or "")


# bridge behaviour from inside the container -------------------------------

FAKE_WRITE_TOOL = "fake_gated_write"


@pytest.fixture
def gated_write_tool(monkeypatch, workspace, tmp_path):
    import src.agent_tools as agent_tools_mod
    import src.constants as consts
    import src.settings as settings_mod
    import src.tool_capabilities as tool_caps
    import src.tool_execution as tool_exec_mod
    from src.settings import DEFAULT_SETTINGS

    async def handler(content, ctx=None):
        (workspace / "bridged.txt").write_text("bridge-wrote", encoding="utf-8")
        return {"output": "written through the bridge", "exit_code": 0}

    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(agent_tools_mod, "TOOL_TAGS", agent_tools_mod.TOOL_TAGS | {FAKE_WRITE_TOOL},
                        raising=False)
    monkeypatch.setitem(agent_tools_mod.TOOL_HANDLERS, FAKE_WRITE_TOOL, handler)
    gated = frozenset(tool_caps.ALWAYS_APPROVE_TOOLS | {FAKE_WRITE_TOOL})
    monkeypatch.setattr(tool_caps, "ALWAYS_APPROVE_TOOLS", gated, raising=False)
    monkeypatch.setattr(tool_exec_mod, "ALWAYS_APPROVE_TOOLS", gated, raising=False)
    base = {"tool_approval_mode": "ask", "desktop_control_mode": "ask_each",
            "agent_code_mode_timeout_seconds": 30, "agent_code_mode_pause_for_approval": True,
            "agent_code_mode_approval_wait_seconds": 30}

    def fast(key, default=None):
        return base[key] if key in base else DEFAULT_SETTINGS.get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fast)
    monkeypatch.setattr(tool_caps, "get_setting", fast, raising=False)
    return workspace


async def _open_question(owner, timeout_s=20.0):
    from src import question_store
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = [q for q in question_store.list_open(owner=owner)
                if q["session_id"].startswith("code_mode:")]
        if rows:
            return rows[0]
        await asyncio.sleep(0.1)
    raise AssertionError("approval question never opened")


_WRITE_SCRIPT = (
    "try:\n    open('direct.txt', 'w').write('x')\n    print('direct write worked')\n"
    "except OSError:\n    print('direct write denied')\n"
    f"print(tools.call('{FAKE_WRITE_TOOL}', {{}}))\n")


async def test_bridge_write_needs_approval_and_works_from_the_container(use_image, gated_write_tool):
    from src import question_store
    task = asyncio.ensure_future(run_code_mode(
        _WRITE_SCRIPT, session_id="s-confined", owner="luis", workspace=str(gated_write_tool)))
    try:
        opened = await _open_question("luis")
        assert not (gated_write_tool / "bridged.txt").exists(), "must not run before approval"
        assert question_store.resolve_question(
            opened["question_id"], {"option_ids": ["approve"]}, owner="luis")["ok"] is True
        result = await asyncio.wait_for(task, timeout=30)
    finally:
        if not task.done():
            task.cancel()
    assert result["exit_code"] == 0, result
    assert "direct write denied" in result["output"]
    assert "written through the bridge" in result["output"]
    assert (gated_write_tool / "bridged.txt").read_text() == "bridge-wrote"
    assert not (gated_write_tool / "direct.txt").exists()
    assert [a["decision"] for a in result["approvals"]] == ["approve"]
    assert result["runtime_guarantees"]["mode"] == "container"


async def test_bridge_denial_is_honoured_from_the_container(use_image, gated_write_tool):
    from src import question_store
    task = asyncio.ensure_future(run_code_mode(
        _WRITE_SCRIPT, session_id="s-confined", owner="luis", workspace=str(gated_write_tool)))
    try:
        opened = await _open_question("luis")
        question_store.resolve_question(opened["question_id"], {"option_ids": ["deny"]}, owner="luis")
        result = await asyncio.wait_for(task, timeout=30)
    finally:
        if not task.done():
            task.cancel()
    assert "declined by user" in result["output"]
    assert not (gated_write_tool / "bridged.txt").exists()


async def test_cancellation_while_waiting_for_approval_removes_the_container(use_image, gated_write_tool):
    task = asyncio.ensure_future(run_code_mode(
        _WRITE_SCRIPT, session_id="s-confined", owner="luis", workspace=str(gated_write_tool)))
    await _open_question("luis")
    assert _containers_left(), "the guest container should be running while approval is pending"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _containers_left() == []
    assert not (gated_write_tool / "bridged.txt").exists()
