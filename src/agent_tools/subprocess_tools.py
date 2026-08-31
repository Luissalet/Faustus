import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import collections
from typing import Optional, Callable, Awaitable, Tuple, Dict
from core.platform_compat import IS_WINDOWS, find_bash
from src.constants import MAX_OUTPUT_CHARS

logger = logging.getLogger(__name__)

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour
DEFAULT_PYTHON_TIMEOUT = 60 * 60
# A command that prints nothing for this long is treated as stuck (a server
# left in the foreground, a process waiting for input) and its whole process
# tree is killed. Setting `agent_subprocess_idle_timeout_seconds`; 0 disables.
DEFAULT_IDLE_TIMEOUT = 300


def _idle_timeout_seconds() -> float:
    try:
        from src.settings import get_setting
        v = get_setting("agent_subprocess_idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT)
        v = float(v)
        return v if v > 0 else 0.0
    except Exception:
        return float(DEFAULT_IDLE_TIMEOUT)


def _kill_tree(proc) -> None:
    """Kill the subprocess AND its children. On Windows the Git-for-Windows
    launcher (bin\\bash.exe) execs the real usr\\bin\\bash.exe which spawns the
    command: a bare proc.kill() only removed the launcher and left a
    foreground `uvicorn` running forever (seen live). taskkill /T takes the
    tree; on POSIX the shell runs in its own session so killpg does.

    Synchronous (taskkill takes well under a second); `_kill_tree_async` is the
    variant for the event loop."""
    pid = getattr(proc, "pid", None)
    try:
        if IS_WINDOWS and pid:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif pid:
            try:
                pgid = os.getpgid(pid)
                if pgid != os.getpgid(0):        # never our own group
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
    except Exception as e:
        logger.debug("kill tree %s failed: %s", pid, e)
    try:
        proc.kill()
    except Exception:
        pass


async def _kill_tree_async(proc) -> None:
    """`_kill_tree` off the event loop (taskkill is a blocking subprocess)."""
    try:
        await asyncio.to_thread(_kill_tree, proc)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# Commands that never exit on their own (servers, dev watchers, tails). Run in
# the foreground they block the turn until the timeout — the model must start
# them detached (`#!bg` first line) or bounded (`timeout N …`).
_SERVER_LAUNCH_RE = re.compile(
    r"(?:^|[;&|(]\s*)(?:[\w./\\:-]*python[\w.]*\s+(?:-m\s+)?)?(?:"
    r"uvicorn|gunicorn|hypercorn|daphne|waitress-serve|flask\s+run|streamlit\s+run|"
    r"http\.server|php\s+-S|rails\s+s(?:erver)?\b|ng\s+serve|vite\b(?!\s+build)|next\s+dev|nuxt\s+dev|"
    r"npm\s+(?:run\s+)?(?:start|dev|serve)\b|yarn\s+(?:run\s+)?(?:start|dev|serve)\b|pnpm\s+(?:run\s+)?(?:start|dev|serve)\b|"
    r"node\s+\S*(?:server|app|index)\.[cm]?js\b|nodemon\b|ollama\s+serve|tail\s+-[a-zA-Z]*[fF]|watch\s|"
    r"manage\.py\s+runserver|docker\s+compose\s+up(?![^\n;&|]*\s-d\b)|docker-compose\s+up(?![^\n;&|]*\s-d\b)"
    r")",
    re.I | re.M,
)
_BACKGROUNDED_RE = re.compile(
    r"(?:&\s*$|&\s*\n|\bnohup\b|\bsetsid\b|\bdisown\b|\bstart\s+/b\b|Start-Process|\btimeout\s+-?\d|\bgtimeout\s+\d|\bscreen\s+-d|\btmux\s+new)",
    re.I | re.M,
)


def foreground_server_launch(command: str) -> Optional[str]:
    """Return the matched launcher when `command` starts a server/watcher in
    the foreground (nothing backgrounds or bounds it), else None."""
    cmd = str(command or "")
    if not cmd.strip():
        return None
    m = _SERVER_LAUNCH_RE.search(cmd)
    if not m:
        return None
    if _BACKGROUNDED_RE.search(cmd):
        return None
    return m.group(0).strip(" ;&|(")


def _blocked_server_result(kind: str, tool: str) -> Dict:
    return {
        "error": (
            f"{tool}: `{kind}` starts a long-running server/watcher and would block this turn "
            "(it never exits on its own; the previous attempt hung the run). Do ONE of: "
            "(1) start it detached — put `#!bg` as the FIRST line of the bash block and the "
            "command below it; you will be re-invoked with its output and can query/kill it with "
            "manage_bg_jobs; (2) bound it: `timeout 30 <command>`; (3) verify the code without "
            "running the server (import it, run its tests, or call the handler directly)."
        ),
        "exit_code": 2,
    }

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12
TMUX_CAPTURE_LINES = 2000


async def _create_bash_subprocess(command: str, **kwargs):
    """Start the agent shell with Bash semantics on every supported OS.

    ``asyncio.create_subprocess_shell`` delegates to ``cmd.exe`` on native
    Windows.  That contradicts the Bash tool contract and makes POSIX commands
    such as ``pwd``, ``ls -la``, and ``cat`` unreliable even when the launcher
    has found Git Bash.  Pass the selected workspace as a structural ``cwd``
    argument; Git Bash inherits that native Windows directory and exposes it
    using its normal ``/c/...`` representation.
    """
    if IS_WINDOWS:
        bash = find_bash()
        if not bash:
            raise RuntimeError(
                "Git Bash is required for the Bash tool on Windows; "
                "install Git for Windows and restart Odysseus"
            )
        return await asyncio.create_subprocess_exec(bash, "-c", command, **kwargs)
    # Own session/process group so a stuck command's whole tree can be killed.
    kwargs.setdefault("start_new_session", True)
    return await asyncio.create_subprocess_shell(command, **kwargs)


def _tmux_session_name(session_id: Optional[str]) -> str:
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "default")).strip("-")
    return f"ody-agent-{raw[:80] or 'default'}"


async def _run_exec(*args: str, timeout: float = 10) -> Tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "timeout", 124
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def _tmux_has_session(name: str) -> bool:
    _, _, rc = await _run_exec("tmux", "has-session", "-t", name, timeout=3)
    return rc == 0


async def _tmux_capture(name: str) -> str:
    out, _, _ = await _run_exec(
        "tmux", "capture-pane", "-p", "-J", "-S", f"-{TMUX_CAPTURE_LINES}", "-t", name,
        timeout=5,
    )
    return out


async def _tmux_send_line(name: str, line: str) -> None:
    if line:
        await _run_exec("tmux", "send-keys", "-t", name, "-l", line, timeout=5)
    await _run_exec("tmux", "send-keys", "-t", name, "C-m", timeout=5)


async def _ensure_tmux_session(name: str, cwd: str, env: Optional[dict]) -> None:
    if await _tmux_has_session(name):
        await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)
        return
    await _run_exec(
        "tmux", "new-session", "-d", "-s", name, "-c", cwd,
        "env",
        f"TERM={env.get('TERM', 'xterm-256color') if env else 'xterm-256color'}",
        f"COLUMNS={env.get('COLUMNS', '120') if env else '120'}",
        f"LINES={env.get('LINES', '40') if env else '40'}",
        "/bin/bash",
        "--noprofile",
        "--norc",
        timeout=10,
    )
    if not await _tmux_has_session(name):
        raise RuntimeError(f"failed to create tmux session {name}")
    await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)


def _output_after_marker(capture: str, start_marker: str, end_marker: str) -> Tuple[str, bool]:
    lines = capture.splitlines()
    start_idx = -1
    for idx, line in enumerate(lines):
        if line.strip() == start_marker:
            start_idx = idx
    if start_idx < 0:
        return capture, False
    end_idx = -1
    for idx in range(start_idx + 1, len(lines)):
        if lines[idx].strip().startswith(end_marker):
            end_idx = idx
    if end_idx < 0:
        return "\n".join(lines[start_idx + 1:]), False
    return "\n".join(lines[start_idx + 1:end_idx]), True


def _extract_marker_rc(capture: str, end_marker: str) -> int:
    for line in reversed(capture.splitlines()):
        stripped = line.strip()
        if stripped.startswith(end_marker):
            suffix = stripped[len(end_marker):].strip()
            if suffix.isdigit():
                return int(suffix)
    return 0


async def _run_tmux_bash(
    content: str,
    *,
    session_id: str,
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    name = _tmux_session_name(session_id)
    await _ensure_tmux_session(name, cwd, env)

    stamp = f"{int(time.time() * 1000)}-{abs(hash(content)) % 1000000}"
    start_marker = f"__ODYSSEUS_CMD_START_{stamp}__"
    end_prefix = f"__ODYSSEUS_CMD_END_{stamp}__:"
    wrapped = (
        f"printf '\\n{start_marker}\\n'\n"
        f"{content}\n"
        f"__ody_rc=$?\n"
        f"printf '\\n{end_prefix}%s\\n' \"$__ody_rc\"\n"
    )
    for line in wrapped.splitlines():
        await _tmux_send_line(name, line)

    started = time.time()
    last_tail = ""
    while True:
        capture = await _tmux_capture(name)
        body, done = _output_after_marker(capture, start_marker, end_prefix)
        tail = "\n".join(body.splitlines()[-PROGRESS_TAIL_LINES:])
        if progress_cb and tail != last_tail:
            last_tail = tail
            try:
                await progress_cb({
                    "elapsed_s": round(time.time() - started, 1),
                    "tail": tail,
                    "tmux_session": name,
                })
            except Exception:
                pass
        if done:
            rc = _extract_marker_rc(capture, end_prefix)
            cleaned = _clean_tmux_command_output(body, wrapped)
            return cleaned, "", rc, False
        if time.time() - started > timeout:
            try:
                await _run_exec("tmux", "send-keys", "-t", name, "C-c", timeout=3)
            except Exception:
                pass
            cleaned = _clean_tmux_command_output(body, wrapped)
            return cleaned, "", 124, True
        await asyncio.sleep(0.5)


def _clean_tmux_command_output(text: str, wrapped_command: str) -> str:
    lines = text.splitlines()
    wrapped_lines = {ln.rstrip() for ln in wrapped_command.splitlines() if ln.strip()}
    cleaned = []
    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            cleaned.append(raw)
            continue
        if stripped in wrapped_lines:
            continue
        if stripped.startswith("__ody_rc=") or stripped.startswith("printf "):
            continue
        if re.fullmatch(r"(?:bash|sh)-[\d.]+\$ ?", stripped):
            continue
        if re.fullmatch(r"[\w.@:/~+-]+[#$] ?", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    idle_timeout: Optional[float] = None,
) -> Tuple[str, str, Optional[int], bool]:
    """Run `proc` to completion. Returns (stdout, stderr, returncode, timed_out);
    `timed_out` is True for the hard timeout and the string "idle" when the
    command was killed for printing nothing for `idle_timeout` seconds."""
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)
    last_activity = [time.time()]
    idle_hit = [False]
    if idle_timeout is None:
        idle_timeout = _idle_timeout_seconds()

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            last_activity[0] = time.time()
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _idle_watchdog():
        if not idle_timeout:
            return
        while True:
            await asyncio.sleep(min(5.0, idle_timeout))
            if time.time() - last_activity[0] > idle_timeout:
                idle_hit[0] = True
                await _kill_tree_async(proc)
                return

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None
    idle_task = asyncio.create_task(_idle_watchdog()) if idle_timeout else None

    timed_out: object = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        if idle_hit[0]:
            timed_out = "idle"
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_tree_async(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        # The turn was stopped / the run cancelled: take the whole tree with us.
        _kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        if idle_task is not None:
            idle_task.cancel()
        raise
    finally:
        for t in (prog_task, idle_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )


def _idle_result(tool: str, idle_s: float, stdout: str, stderr: str) -> Dict:
    from src.tool_execution import _truncate
    return {
        "error": (
            f"{tool}: no output for {int(idle_s)}s while still running — killed with its child processes. "
            "This usually means a server/watcher left in the foreground or a process waiting for input. "
            "Start servers detached (`#!bg` as the first line of the bash block, then manage_bg_jobs), "
            "bound long commands with `timeout N …`, and never run interactive programs."
        ),
        "exit_code": 124,
        "stdout": _truncate(stdout, MAX_OUTPUT_CHARS),
        "stderr": _truncate(stderr, MAX_OUTPUT_CHARS),
    }

class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        if isinstance(content, dict):
            content = str(content.get("command") or content.get("cmd") or content.get("code") or "")
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        session_id = ctx.get("session_id")
        launcher = foreground_server_launch(content)
        if launcher:
            return _blocked_server_result(launcher, "bash")
        # tmux is a POSIX persistence path. A stray MSYS/Cygwin tmux.exe on
        # native Windows must not bypass the Git Bash launcher below: the tmux
        # setup hard-codes /bin/bash and cannot safely consume a native cwd.
        if session_id and not IS_WINDOWS and shutil.which("tmux"):
            stdout, stderr, rc, timed_out = await _run_tmux_bash(
                content,
                session_id=str(session_id),
                cwd=agent_cwd(),
                env=_subproc_env,
                timeout=DEFAULT_BASH_TIMEOUT,
                progress_cb=progress_cb,
            )
            if timed_out:
                return {
                    "error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — sent Ctrl-C to tmux session",
                    "exit_code": 124,
                    "stdout": _truncate(stdout, MAX_OUTPUT_CHARS),
                    "stderr": _truncate(stderr, MAX_OUTPUT_CHARS),
                    "tmux_session": _tmux_session_name(str(session_id)),
                }
            output = stdout.rstrip()
            err = stderr.rstrip()
            if err:
                output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
            return {
                "output": _truncate(output, MAX_OUTPUT_CHARS) or "(no output)",
                "exit_code": rc or 0,
                "tmux_session": _tmux_session_name(str(session_id)),
            }

        try:
            proc = await _create_bash_subprocess(
                content,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_subproc_env,
                cwd=agent_cwd(),
            )
        except RuntimeError as e:
            return {"error": f"bash: {e}", "exit_code": 1}
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out == "idle":
            return _idle_result("bash", _idle_timeout_seconds(), stdout, stderr)
        if timed_out:
            return {"error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        proc = await asyncio.create_subprocess_exec(
            (sys.executable or "python"), "-I", "-c", content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out == "idle":
            return _idle_result("python", _idle_timeout_seconds(), stdout, stderr)
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0}
