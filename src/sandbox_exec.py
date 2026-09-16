"""
sandbox_exec.py — the agent's shell, run inside the container instead of on
the machine.

This is the half of Phase 1 that actually changes what happens when the model
runs a command. Everything before it built a sandbox nothing used.

It is off by default (`agent_sandbox_execution`), and off means *byte-identical
to yesterday*: `run()` returns None and `subprocess_tools` takes the path it
always took. There is a test for that, because a flag that changes behaviour
while switched off is worse than no flag.

What happens when it is **on** and the sandbox cannot serve depends on
`agent_sandbox_mode` (14-09-2026):

* `auto` (default): the host runs it — always on native Windows (a Linux
  container has no cmd/powershell/.bat/winget and not the user's own Python,
  so it can never verify a Windows project), and on POSIX when the daemon
  does not answer. Never silently: the result carries `sandbox_skipped` with
  the reason and `execution_target` says where it ran.
* `strict`: the historical rule. A missing daemon, an absent image or a
  workspace that is not a directory all come back as an error result naming
  the reason — the same refusal the router gives, surfaced where the model
  can read it — and nothing puts the command on the host.

### The one thing it rewrites, and why

Inside the container the workspace is mounted at `/workspace`, so a command
holding an absolute host path (`D:\\proj\\src\\x.py`) would not find its file.
Paths that start with the workspace root — and only those — are rewritten to
`/workspace/...` on the way in and back on the way out, and the result says
how many were changed. Rewriting anything else would mean editing the model's
command on a guess; rewriting nothing would break every absolute path.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from src.contracts import SkillManifest
from src.contracts.base import now_iso
# One definition of the default image, not two literals that drift the first
# time somebody changes one of them.
from src.execution_backends import DEFAULT_IMAGE

logger = logging.getLogger(__name__)

SETTING = "agent_sandbox_execution"
#: How the sandbox behaves when it is ON but cannot serve this host:
#:
#:   * ``auto`` (default) — the sandbox is an OPTION, not a gate. On a native
#:     Windows host the command runs on the host (Git Bash / the Windows
#:     Python) in BOTH auto and strict: a Linux container has no ``cmd``, no
#:     ``powershell``, no ``.bat``, no ``winget`` and not the user's own
#:     interpreter, so it can never verify a Windows project. On POSIX the
#:     container is used when the daemon answers and the host otherwise. The
#:     result always says where it ran (``execution_target`` /
#:     ``sandbox_skipped``), so nothing is silent.
#:   * ``strict`` — on POSIX, the historical rule: on and unavailable means
#:     REFUSED, never the host. Windows still takes the host (see above).
MODE_SETTING = "agent_sandbox_mode"
MODES = ("auto", "strict")
IMAGE_SETTING = "agent_sandbox_image"
TIMEOUT_SETTING = "agent_sandbox_timeout_s"
NETWORK_SETTING = "agent_sandbox_network"
MEMORY_SETTING = "agent_sandbox_memory_mb"
#: A long-lived, per-session container (src/sandbox_provider.py) instead of
#: one `--rm` container per call. Off by default — same byte-identical
#: promise as `enabled()` itself: nothing about the ephemeral path changes
#: unless this is explicitly turned on.
PERSISTENT_SESSION_SETTING = "agent_sandbox_persistent_session"
#: What the NEXT command does when the session's container is gone — removed
#: by hand (`docker rm`), pruned by Docker Desktop, whatever. Never silently
#: treated as "still there".
#:   * ``recreate_empty`` (default) — a fresh, empty session sandbox, and the
#:     result says so plus what is known to have been lost.
#:   * ``fail`` — refuse the command; nothing is recreated.
MISSING_POLICY_SETTING = "sandbox_missing_policy"
MISSING_POLICIES = ("recreate_empty", "fail")

DEFAULT_TIMEOUT_S = 900
DEFAULT_MEMORY_MB = 2048

CONTAINER_WORKSPACE = "/workspace"


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def enabled() -> bool:
    """Deliberately strict about what counts as on. A truthy string is not a
    yes here for the same reason it is not one in the contracts: someone
    typing `agent_sandbox_execution: "no"` must not get a sandbox, and someone
    typing `"yes"` must not get one either — they get to see it did nothing
    and fix the value."""
    return _setting(SETTING, False) is True


def mode() -> str:
    """``auto`` unless the operator wrote exactly ``strict``. Any other value
    is ``auto`` for the same reason `enabled()` is strict about ``True``: an
    unrecognised word must not quietly buy a behaviour nobody chose — and the
    behaviour that runs the user's command is the one to fall to."""
    raw = str(_setting(MODE_SETTING, "auto") or "auto").strip().lower()
    return raw if raw in MODES else "auto"


def _host_is_windows() -> bool:
    try:
        from core.platform_compat import IS_WINDOWS
        return bool(IS_WINDOWS)
    except Exception:  # noqa: BLE001 - conservative: not Windows
        return False


def host_skip_reason() -> Optional[str]:
    """Why `run()` would hand this call to the host WITHOUT probing Docker,
    or None when the sandbox is the place to try first.

    A native Windows host is skipped unconditionally — including ``strict``.
    A Linux container cannot run the project's own toolchain (cmd, powershell,
    .bat, winget, the Windows Python), which is what "verify the code" means
    on that machine; asking the user to start Docker does not change that.
    ``strict`` remains the POSIX hard gate. A daemon that does not answer is
    a second, later reason, found by the probe in `run()`."""
    if not enabled():
        return None
    if _host_is_windows():
        return ("native Windows host: the Linux container cannot run cmd, "
                "powershell, .bat files, winget or the project's own Windows "
                "Python, so commands run on the host")
    if mode() == "strict":
        return None
    return None


def describe() -> Dict[str, Any]:
    """One line of truth for the prompt and the UI: is the sandbox on, in
    which mode, and where will the next `bash` actually run. Cheap — it
    never touches Docker; `run()` does the probe when it matters."""
    on = enabled()
    skip = host_skip_reason() if on else None
    if not on:
        target = "host"
    elif skip:
        target = "host"
    elif mode() == "strict":
        target = "container"
    else:
        target = "container_or_host"
    return {"enabled": on, "mode": mode() if on else "off", "target": target,
            "skip_reason": skip or "", "image": image() if on else ""}


def image() -> str:
    return str(_setting(IMAGE_SETTING, DEFAULT_IMAGE) or DEFAULT_IMAGE).strip()


def timeout_s() -> int:
    raw = _setting(TIMEOUT_SETTING, DEFAULT_TIMEOUT_S)
    return raw if isinstance(raw, int) and 0 < raw <= 86400 else DEFAULT_TIMEOUT_S


def memory_mb() -> int:
    raw = _setting(MEMORY_SETTING, DEFAULT_MEMORY_MB)
    return raw if isinstance(raw, int) and raw >= 64 else DEFAULT_MEMORY_MB


def network() -> bool:
    return _setting(NETWORK_SETTING, False) is True


def persistent_session_enabled() -> bool:
    return _setting(PERSISTENT_SESSION_SETTING, False) is True


def missing_policy() -> str:
    raw = str(_setting(MISSING_POLICY_SETTING, "recreate_empty") or "recreate_empty").strip().lower()
    return raw if raw in MISSING_POLICIES else "recreate_empty"


def manifest() -> SkillManifest:
    """The agent's shell, written as a manifest so it goes through the same
    router, the same permission check and the same refusals as any skill. The
    alternative — a private path to the backend for the built-in tools — is
    how the built-ins end up with permissions no skill could ask for."""
    return SkillManifest.parse({
        "id": "agent.shell",
        "version": "1.0.0",
        "title": "The agent's own shell and interpreter",
        "family": "system",
        "outputs": {"stdout": "text"},
        "permissions": {
            "backends": ["docker_workspace"],
            "network": network(),
            "filesystem": "workspace",
            "max_seconds": timeout_s(),
        },
        "approval": {"required_when": []},
    })


# ── the one rewrite ────────────────────────────────────────────────────────

#: What ends a path token in a shell command. Everything up to one of these
#: belongs to the path being rewritten.
_TOKEN_END = r"\s\"';|&<>()"


_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _host_root(workspace: str) -> str:
    r"""The workspace root as the host spells it. A Windows path is left as
    given: `os.path.abspath` on Linux would glue the cwd in front of `D:\...`
    and the rewrite would never match -- which is exactly what happened when
    the Windows workspace tests ran on the Linux CI clone."""
    ws = str(workspace or "")
    if _WINDOWS_ABS.match(ws):
        return ws.rstrip("\\/") or ws
    return os.path.abspath(ws)


def _root_re(workspace: str) -> "re.Pattern":
    """Match the workspace root **and the path that follows it**, in any of the
    spellings the root can arrive in.

    Two details found by testing rather than by thinking:

    * the whole token has to be converted, not just the prefix. Rewriting only
      the root leaves `/workspace\\src\\x.py`, which on Linux is one filename
      containing backslashes — the command then fails for a reason nothing in
      the output explains;
    * the root needs a boundary after it, or a workspace at `D:\\proj\\demo`
      swallows the first half of `D:\\proj\\demo2\\other.txt` and hands the
      container `/workspace2/other.txt`.
    """
    root = _host_root(workspace)
    forms = {root, root.replace("\\", "/"), root.replace("/", "\\")}
    alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    return re.compile(
        r"(?:" + alts + r")"
        r"(?=$|[\\/" + _TOKEN_END + r"])"
        r"(?P<tail>[^" + _TOKEN_END + r"]*)"
    )


def to_container(text: str, workspace: str) -> Tuple[str, int]:
    """Host paths → container paths. Returns the text and how many paths were
    rewritten, because silently editing someone's command is not something to
    do without saying so."""
    if not text or not workspace:
        return text, 0
    hits = 0

    def _swap(match: "re.Match") -> str:
        nonlocal hits
        hits += 1
        return CONTAINER_WORKSPACE + match.group("tail").replace("\\", "/")

    return _root_re(workspace).sub(_swap, text), hits


def to_host(text: str, workspace: str) -> str:
    """Container paths → host paths, so the model's next step names a file the
    rest of Faustus can open."""
    if not text or not workspace:
        return text
    return text.replace(CONTAINER_WORKSPACE, _host_root(workspace))


# ── running a tool call ────────────────────────────────────────────────────

def _argv_for(tool: str, command: str) -> list:
    """The shell string the model wrote travels as ONE argument. That is what
    keeps the argv-only rule honest here: nothing splits the command, so
    nothing can turn a filename with a space into two arguments — the shell
    inside the container does its own parsing, which is what the model asked
    for when it called `bash`."""
    if tool == "python":
        return ["python", "-I", "-c", command]
    return ["/bin/sh", "-c", command]


#: Why the LAST `run()` in this task handed the call to the host (auto
#: mode), or "" — read by subprocess_tools right after `run()` returned None
#: so the host result can carry `sandbox_skipped`. Task-local: two turns
#: never see each other's reason.
_last_skip: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "sandbox_exec_last_skip", default="")


def _note_skip(tool: str, reason: str) -> None:
    _last_skip.set(reason)
    logger.info("sandbox skipped for %s (auto): %s", tool, reason)


def consume_skip_reason() -> str:
    """The reason the previous `run()` chose the host, cleared on read."""
    reason = _last_skip.get()
    if reason:
        _last_skip.set("")
    return reason


def _refusal(tool: str, reason: str) -> Dict[str, Any]:
    """On, and unable to run it. Not a fallback — an answer the model can act
    on, and one an operator can read as "turn Docker on or turn the setting
    off", never as "your command was wrong". Leads with the literal phrase
    `sandbox unavailable: <reason>` so a caller (or a test) can find the
    verdict without parsing the rest of the sentence."""
    return {
        "error": f"{tool}: sandbox unavailable: {reason}. The sandbox is on and the "
                 f"command was NOT run — `{MODE_SETTING}` is `strict`, so Faustus does "
                 f"not fall back to running it unsandboxed; start the backend, set "
                 f"`{MODE_SETTING}` to `auto` (host when the container cannot serve) or "
                 f"turn off `{SETTING}`.",
        "exit_code": 126,
        "sandboxed": False,
        "sandbox_refused": True,
        "sandbox_unavailable_reason": reason,
    }


async def run(tool: str, command: str, ctx: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Run a bash/python tool call in the container.

    Returns None when the setting is off — the caller then does exactly what
    it did before this module existed. Any other return means the sandbox owns
    this call, including when it refuses it.
    """
    if not enabled():
        return None
    if tool not in ("bash", "python"):
        return None
    if not isinstance(command, str) or not command.strip():
        return {"error": f"{tool}: empty command", "exit_code": 1, "sandboxed": False}

    from src import execution_router, capability_registry as registry
    from src.constants import ARTIFACT_RUNS_DIR, MAX_OUTPUT_CHARS
    from src.execution_backends import DockerWorkspaceBackend
    from src.tool_execution import agent_cwd, _truncate

    # `auto`: the host is a legitimate destination, and the two reasons to
    # take it are decided here, in order — the host kind (no probe needed)
    # and then the daemon. `None` is the caller's cue to run exactly the
    # path it ran before the sandbox existed; `_last_skip` lets it say why.
    skip = host_skip_reason()
    if skip:
        _note_skip(tool, skip)
        return None

    workspace = agent_cwd()
    if not workspace or not os.path.isdir(workspace):
        return _refusal(tool, f"the workspace {workspace!r} is not a directory")

    session_id = str((ctx or {}).get("session_id") or "").strip()
    if persistent_session_enabled() and session_id:
        return await _run_in_session(tool, command, ctx, workspace, session_id)

    ready = DockerWorkspaceBackend(image=image()).probe()
    if not ready["ok"]:
        if mode() == "auto":
            _note_skip(tool, f"{ready['reason']}: {ready['detail']}")
            return None
        return _refusal(tool, f"{ready['reason']}: {ready['detail']}")

    run_id = str((ctx or {}).get("run_id")
                 or (ctx or {}).get("session_id")
                 or f"{tool}-{int(asyncio.get_event_loop().time() * 1000)}")
    run_id = f"{run_id}-{tool}"[:64]
    rewritten, rewrites = to_container(command, workspace)

    decision = execution_router.choose(
        manifest(), workspace=workspace, artifacts_root=ARTIFACT_RUNS_DIR,
        run_id=run_id, prefer="docker_workspace")
    if not decision.ok:
        return _refusal(tool, f"{decision.reason}: {decision.detail}")

    backend = DockerWorkspaceBackend(image=image())
    spec_body = decision.spec.to_dict()
    spec_body["limits"] = {**spec_body["limits"], "memory_mb": memory_mb()}
    from src.contracts import ExecutionSpec
    spec = ExecutionSpec.parse(spec_body)

    progress_cb = (ctx or {}).get("progress_cb")

    def _emit(name: str, data: Dict[str, Any]) -> None:
        if not progress_cb:
            return
        try:
            progress_cb({"type": "tool_progress", "tool": tool, "event": name, **data})
        except Exception:
            logger.debug("sandbox progress callback failed", exc_info=True)

    result = await asyncio.to_thread(
        backend.run, spec, _argv_for(tool, rewritten), run_id=run_id, on_event=_emit)

    stdout = to_host(result.stdout_tail, workspace).rstrip()
    stderr = to_host(result.stderr_tail, workspace).rstrip()

    common = {
        "sandboxed": True,
        "backend": result.backend,
        "isolation": spec.isolation,
        "image": backend.image,
        "network": spec.network,
        "duration_ms": result.duration_ms,
    }
    if rewrites:
        common["workspace_paths_rewritten"] = rewrites
    if result.output_truncated:
        common["output_truncated"] = True

    if result.status == "refused":
        return {**common, "sandboxed": False, "sandbox_refused": True,
                "error": f"{tool}: the sandbox refused to start it — {result.reason}",
                "exit_code": 126}
    if result.status == "timeout":
        return {**common,
                "error": f"{tool}: timed out after {spec.limits.seconds}s — the container was killed",
                "exit_code": 124,
                "stdout": _truncate(stdout, MAX_OUTPUT_CHARS),
                "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}

    output = stdout
    if stderr:
        output = (output + "\nSTDERR: " + stderr).strip() if output else "STDERR: " + stderr
    return {**common,
            "output": _truncate(output, MAX_OUTPUT_CHARS) or "(no output)",
            "exit_code": result.exit_code if result.exit_code is not None else 1}


# ── the persistent, per-session sandbox path (agent_sandbox_persistent_session) ─
#
# Everything above this line is the original, ephemeral `--rm`-per-call
# path and is untouched by what follows: `run()` only reaches this branch
# when the setting is explicitly on AND the call carries a session id.

async def _run_in_session(tool: str, command: str, ctx: Optional[dict],
                           workspace: str, session_id: str) -> Dict[str, Any]:
    """The persistent-session counterpart of the code below it: instead of a
    fresh container per call, this reuses (or recreates) one container named
    after `session_id`, and treats its disappearance as a fact to report, not
    a chance to run unsandboxed on the host."""
    from src.constants import MAX_OUTPUT_CHARS
    from src.tool_execution import _truncate
    from src import sandbox_provider as provider_mod

    provider = provider_mod.get_provider(image=image(), workspace=workspace)

    avail = await asyncio.to_thread(provider.probe)
    if not avail.available:
        if mode() == "auto":
            _note_skip(tool, avail.reason)
            return None
        return _refusal(tool, avail.reason)

    was_seen = provider_mod.session_seen(session_id)
    status = await asyncio.to_thread(provider.status, session_id)

    note: str = ""
    lost_files: list = []

    if status == "unknown":
        return _refusal(
            tool, f"sandbox session {session_id!r} could not be checked — the "
                  f"backend did not answer the status probe")

    if status == "missing" and not was_seen:
        # Never created before: this is a first use, not a disappearance.
        created = await asyncio.to_thread(provider.create, session_id)
        if not created.get("created"):
            return _refusal(tool, created.get("reason") or
                             f"could not create the sandbox session {session_id!r}")
    elif status == "missing" and was_seen:
        # It existed once and is gone now — apply the missing policy and say
        # so explicitly. This is the ONLY place allowed to claim what did or
        # did not survive, and it never claims survival.
        policy = missing_policy()
        result = await asyncio.to_thread(provider.recreate, session_id, policy)
        note = str(result.get("message") or "")
        lost_files = list(result.get("lost_files") or [])
        if not result.get("recreated"):
            return {
                "error": f"{tool}: {note or 'sandbox session missing and not recreated'}",
                "exit_code": 126,
                "sandboxed": False,
                "sandbox_refused": True,
                "sandbox_session_status": "missing",
                "sandbox_session_recreated": False,
                "sandbox_missing_policy": policy,
                "sandbox_lost_files": lost_files,
            }
    # status == "exists": nothing to do, reuse it as-is.

    rewritten, rewrites = to_container(command, workspace)
    argv = _argv_for(tool, rewritten)
    touches = [t for t in ((ctx or {}).get("sandbox_touches") or []) if isinstance(t, str)]

    exec_result = await asyncio.to_thread(
        provider.exec, session_id, argv, timeout=timeout_s(), touches=touches)

    if not exec_result.get("executed"):
        # Removed between the status check above and this exec — a narrow
        # race, still never allowed to look like success.
        return {
            "error": f"{tool}: sandbox unavailable: session {session_id!r} disappeared "
                     f"between the status check and running the command; nothing ran.",
            "exit_code": 126,
            "sandboxed": False,
            "sandbox_refused": True,
            "sandbox_session_status": "missing",
        }

    stdout = to_host(exec_result.get("stdout", ""), workspace).rstrip()
    stderr = to_host(exec_result.get("stderr", ""), workspace).rstrip()
    output = stdout
    if stderr:
        output = (output + "\nSTDERR: " + stderr).strip() if output else "STDERR: " + stderr

    common: Dict[str, Any] = {
        "sandboxed": True,
        "backend": "docker_workspace_session",
        "isolation": "container",
        "image": image(),
        "network": False,
        "sandbox_session": session_id,
    }
    if rewrites:
        common["workspace_paths_rewritten"] = rewrites
    if note:
        common["sandbox_session_note"] = note
        common["sandbox_session_recreated"] = True
        common["sandbox_lost_files"] = lost_files

    if exec_result.get("timed_out"):
        return {**common,
                "error": f"{tool}: timed out after {timeout_s()}s — the session command was killed",
                "exit_code": 124}

    return {**common,
            "output": _truncate(output, MAX_OUTPUT_CHARS) or "(no output)",
            "exit_code": exec_result.get("exit_code", 1)}
