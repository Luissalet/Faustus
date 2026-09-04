"""
sandbox_exec.py — the agent's shell, run inside the container instead of on
the machine.

This is the half of Phase 1 that actually changes what happens when the model
runs a command. Everything before it built a sandbox nothing used.

It is off by default (`agent_sandbox_execution`), and off means *byte-identical
to yesterday*: `run()` returns None and `subprocess_tools` takes the path it
always took. There is a test for that, because a flag that changes behaviour
while switched off is worse than no flag.

The rule that matters is what happens when it is **on** and the sandbox is not
there. It does not fall through to the host. A missing daemon, an absent image
or a workspace that is not a directory all come back as an error result naming
the reason — the same refusal the router gives, surfaced where the model can
read it. Silently running unsandboxed because Docker Desktop was closed is the
exact failure this whole phase exists to prevent, and it would look like
success in every log.

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
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from src.contracts import SkillManifest
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

SETTING = "agent_sandbox_execution"
IMAGE_SETTING = "agent_sandbox_image"
TIMEOUT_SETTING = "agent_sandbox_timeout_s"
NETWORK_SETTING = "agent_sandbox_network"
MEMORY_SETTING = "agent_sandbox_memory_mb"

DEFAULT_IMAGE = "python:3.12-slim"
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
    root = os.path.abspath(workspace)
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
    return text.replace(CONTAINER_WORKSPACE, os.path.abspath(workspace))


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


def _refusal(tool: str, reason: str) -> Dict[str, Any]:
    """On, and unable to run it. Not a fallback — an answer the model can act
    on, and one an operator can read as "turn Docker on or turn the setting
    off", never as "your command was wrong"."""
    return {
        "error": f"{tool}: the sandbox is on and the command was NOT run — {reason}. "
                 f"Faustus does not fall back to running it unsandboxed; either start the "
                 f"backend or turn off the `{SETTING}` setting.",
        "exit_code": 126,
        "sandboxed": False,
        "sandbox_refused": True,
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

    workspace = agent_cwd()
    if not workspace or not os.path.isdir(workspace):
        return _refusal(tool, f"the workspace {workspace!r} is not a directory")

    ready = DockerWorkspaceBackend(image=image()).probe()
    if not ready["ok"]:
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
