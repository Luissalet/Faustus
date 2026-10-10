"""Confined Code Mode runtime (H01): the guest runs inside a container.

What the guest can reach is exactly what this module puts on the ``docker run``
command line, and nothing else:

* the declared workspace root (and any further declared roots) bind-mounted
  read-only or read-write according to the grant; with no grant, no mount;
* no network (``--network none``) unless the run was explicitly granted it; a
  run with an ``allowed_hosts`` list (#255) instead joins a per-run ``--internal``
  docker network whose only other member is a filtering-proxy sidecar
  (``net_allowlist.py``): the guest has no route anywhere else, so the list is
  enforced by the network, not by the guest's goodwill;
* none of the Faustus process environment: no ``--env-file`` is ever passed, the
  only ``-e`` flags are the proxy variables of an ``allowed_hosts`` run, and the
  docker client's own environment is not forwarded into the container;
* a read-only root filesystem with a small tmpfs for scratch, all capabilities
  dropped, ``no-new-privileges``, pid/memory/CPU limits and a wall-time limit
  enforced by the parent.

The tool-call bridge keeps working because the existing stdin/stdout JSON-line
channel is what ``docker run -i`` forwards. The guest program and the user's
code travel over that same stdin (the first line), so no Faustus file is
mounted into the container.

There is no host fallback in this module: when the container cannot be started
the caller receives a refusal (see :func:`refusal`), never a host run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src import container_mounts
from src.code_mode import net_allowlist

logger = logging.getLogger(__name__)

RUNTIME_SETTING = "agent_code_mode_runtime"
RUNTIMES = ("confined", "host")
DEFAULT_RUNTIME = "confined"

WORKSPACE_ACCESS_SETTING = "agent_code_mode_workspace_access"
WORKSPACE_ACCESS_MODES = ("read_only", "read_write", "none")
DEFAULT_WORKSPACE_ACCESS = "read_only"

NETWORK_SETTING = "agent_code_mode_network"
MEMORY_MB_SETTING = "agent_code_mode_memory_mb"
PIDS_SETTING = "agent_code_mode_max_processes"
DEFAULT_MEMORY_MB = 512
DEFAULT_PIDS = 128
DEFAULT_CPUS = 1.0
TMPFS_MB = 64

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_ROOTS = "/roots"

_GUEST_PATH = os.path.join(os.path.dirname(__file__), "guest.py")
_CONTAINER_PREFIX = "faustus-codemode-"
EGRESS_ALIAS = "egress"


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # pragma: no cover - settings import always works in prod
        return default


def resolve_runtime(explicit: Optional[str] = None, *, default: str = DEFAULT_RUNTIME) -> str:
    """``confined`` unless ``host`` was asked for explicitly.

    Anything unrecognised resolves to ``confined``: a typo must never buy the
    weaker runtime.
    """
    raw = explicit if explicit is not None else _setting(RUNTIME_SETTING, default)
    value = str(raw or default).strip().lower()
    return value if value in RUNTIMES else "confined"


def resolve_workspace_access(explicit: Optional[str] = None) -> str:
    raw = explicit if explicit is not None else _setting(WORKSPACE_ACCESS_SETTING, DEFAULT_WORKSPACE_ACCESS)
    value = str(raw or DEFAULT_WORKSPACE_ACCESS).strip().lower()
    return value if value in WORKSPACE_ACCESS_MODES else "read_only"


HOST_LISTS_SETTING = "agent_code_mode_host_lists"


def host_lists_open_network(explicit_network: Optional[bool] = None) -> bool:
    """May an ``allowed_hosts`` list OPEN the network of this run (#255)? Only
    when the operator granted the network already (setting or explicit grant)
    or allowed run_code to name its own hosts; otherwise a list may only narrow."""
    return resolve_network(explicit_network) or _setting(HOST_LISTS_SETTING, False) is True


def resolve_network(explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return explicit is True
    return _setting(NETWORK_SETTING, False) is True


def _int_setting(key: str, default: int, lo: int) -> int:
    raw = _setting(key, default)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= lo else default


def _sandbox_image() -> str:
    try:
        from src import sandbox_exec
        return sandbox_exec.image()
    except Exception:  # pragma: no cover
        from src.execution_backends import DEFAULT_IMAGE
        return DEFAULT_IMAGE


@dataclass
class ConfinedPlan:
    """Everything one confined run needs, built before anything starts."""

    name: str
    argv: List[str]
    guarantees: Dict[str, Any]
    docker: str = "docker"
    mounts: List[Dict[str, Any]] = field(default_factory=list)

    async def _docker(self, *args: str, timeout: float = 30) -> tuple:
        return await docker_cmd(self.docker, *args, timeout=timeout)

    async def kill(self) -> None:
        """SIGKILL the container (and with it every descendant of the guest)."""
        await self._docker("kill", self.name, timeout=20)

    async def oom_killed(self) -> bool:
        code, out, _ = await self._docker(
            "inspect", "--format", "{{.State.OOMKilled}}", self.name, timeout=15)
        return code == 0 and out.strip().lower() == b"true"

    async def cleanup(self) -> None:
        await self._docker("rm", "--force", self.name, timeout=30)


async def docker_cmd(docker: str, *args: str, timeout: float = 30) -> tuple:
    """``(returncode, stdout, stderr)`` of one docker CLI call; never raises."""
    from src.native_env import native_host_environment
    try:
        proc = await asyncio.create_subprocess_exec(
            docker, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=native_host_environment())
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out, err
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001
        logger.debug("confined code mode docker %s failed: %s", args[:1], exc)
        return None, b"", str(exc).encode("utf-8", "replace")


@dataclass
class EgressProxy:
    """The filtering-proxy sidecar and the internal network of one
    ``allowed_hosts`` run (#255). The guest container joins ``network`` only."""

    network: str
    proxy: str
    rules: List[Any]
    docker: str = "docker"

    @property
    def url(self) -> str:
        return f"http://{EGRESS_ALIAS}:{net_allowlist.SIDECAR_PORT}"

    async def collect_stats(self) -> Dict[str, Any]:
        """What the proxy allowed and refused, read back from its own log."""
        code, out, _ = await docker_cmd(self.docker, "logs", self.proxy, timeout=20)
        allowed = denied = 0
        allowed_targets: List[str] = []
        denied_targets: List[str] = []
        if code == 0:
            for line in out.decode("utf-8", "replace").splitlines():
                if not line.startswith("EGRESS "):
                    continue
                try:
                    event = json.loads(line[7:])
                except ValueError:
                    continue
                target = f"{event.get('host')}:{event.get('port')}"
                if event.get("t") == "allow":
                    allowed += 1
                    if target not in allowed_targets and len(allowed_targets) < 20:
                        allowed_targets.append(target)
                elif event.get("t") == "deny":
                    denied += 1
                    if target not in denied_targets and len(denied_targets) < 20:
                        denied_targets.append(target)
        return {"requests_allowed": allowed, "requests_denied": denied,
                "allowed_targets": allowed_targets, "denied_targets": denied_targets}

    async def stop(self) -> None:
        await docker_cmd(self.docker, "rm", "--force", self.proxy, timeout=30)
        await docker_cmd(self.docker, "network", "rm", self.network, timeout=30)


def sidecar_command(rules: List[Any]) -> List[str]:
    """The sidecar's interpreter invocation. The proxy module rides inside the
    ``-c`` argument compressed (a Windows command line stops at 32 K characters)."""
    with open(net_allowlist.__file__, "rb") as fh:
        encoded = base64.b64encode(zlib.compress(fh.read(), 9)).decode("ascii")
    bootstrap = ("import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('" + encoded +
                 "')).decode('utf-8'),'net_allowlist.py','exec'),{'__name__':'__main__'})")
    return ["python3", "-I", "-c", bootstrap, "--serve", str(net_allowlist.SIDECAR_PORT),
            net_allowlist.encode_hosts(rules)]


async def start_egress(image: str, rules: List[Any], *, docker: str = "docker"):
    """Create the internal network and start the proxy sidecar on it. Returns an
    :class:`EgressProxy`, or a refusal dict (nothing is left running)."""
    uid = uuid.uuid4().hex[:16]
    egress = EgressProxy(network=f"{_CONTAINER_PREFIX}net-{uid}", proxy=f"{_CONTAINER_PREFIX}egress-{uid}",
                         rules=list(rules), docker=docker)

    async def fail(reason: str, detail: str = ""):
        await egress.stop()
        return refusal(f"the allowed_hosts filtering proxy could not start ({reason})", detail)

    code, _, err = await docker_cmd(docker, "network", "create", "--internal",
                                    "--label", "faustus.codemode=1", egress.network, timeout=30)
    if code != 0:
        return await fail("network create failed", err.decode("utf-8", "replace")[-300:])
    run_args = [
        "run", "-d", "--pull", "never", "--name", egress.proxy, "--label", "faustus.codemode=1",
        "--user", container_mounts.host_user_spec(),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5",
        image, *sidecar_command(rules),
    ]
    code, _, err = await docker_cmd(docker, *run_args, timeout=60)
    if code != 0:
        return await fail("start failed", err.decode("utf-8", "replace")[-300:])
    code, _, err = await docker_cmd(docker, "network", "connect", "--alias", EGRESS_ALIAS,
                                    egress.network, egress.proxy, timeout=30)
    if code != 0:
        return await fail("network connect failed", err.decode("utf-8", "replace")[-300:])
    for _ in range(100):
        code, out, _ = await docker_cmd(docker, "logs", egress.proxy, timeout=15)
        if code == 0 and b"READY" in out:
            return egress
        state_code, state, _ = await docker_cmd(
            docker, "inspect", "--format", "{{.State.Running}}", egress.proxy, timeout=15)
        if state_code == 0 and state.strip().lower() == b"false":
            return await fail("the proxy exited", out.decode("utf-8", "replace")[-300:])
        await asyncio.sleep(0.15)
    return await fail("the proxy did not become ready")


def container_name() -> str:
    return f"{_CONTAINER_PREFIX}{uuid.uuid4().hex[:16]}"


def guest_command() -> List[str]:
    """The interpreter invocation: the guest source rides inside the ``-c``
    argument (base64, single line, no quoting hazards on any host shell) and is
    told to read the user's code from its config line rather than from a file.
    """
    with open(_GUEST_PATH, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    bootstrap = ("import base64;exec(compile(base64.b64decode('" + encoded + "').decode('utf-8'),"
                 "'guest.py','exec'),{'__name__':'__main__'})")
    return ["python3", "-I", "-c", bootstrap, "--inline"]


def build_plan(*, workspace: Optional[str], workspace_roots: Optional[list] = None,
               workspace_access: Optional[str] = None, network: Optional[bool] = None,
               timeout_seconds: int = 60, image: Optional[str] = None,
               docker: str = "docker", memory_mb: Optional[int] = None,
               max_processes: Optional[int] = None, cpus: float = DEFAULT_CPUS,
               egress: Optional[EgressProxy] = None) -> ConfinedPlan:
    """The ``docker run`` command line and the guarantees it stands for.

    Raises :class:`container_mounts.MountError` when a declared root cannot be
    mounted unambiguously (the caller turns that into a refusal).
    """
    access = resolve_workspace_access(workspace_access)
    net = resolve_network(network) if egress is None else False
    mem = memory_mb if isinstance(memory_mb, int) and memory_mb >= 64 else _int_setting(
        MEMORY_MB_SETTING, DEFAULT_MEMORY_MB, 64)
    pids = max_processes if isinstance(max_processes, int) and max_processes >= 8 else _int_setting(
        PIDS_SETTING, DEFAULT_PIDS, 8)
    name = container_name()
    readonly = access != "read_write"

    args: List[str] = [
        docker, "run", "-i", "--pull", "never", "--name", name,
        "--user", container_mounts.host_user_spec(),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:rw,noexec,nosuid,size={TMPFS_MB}m",
        "--pids-limit", str(pids),
        "--memory", f"{mem}m", "--memory-swap", f"{mem}m",
        "--cpus", str(cpus),
        "--ulimit", f"cpu={int(timeout_seconds) + 5}:{int(timeout_seconds) + 5}",
        "--network", egress.network if egress is not None else ("bridge" if net else "none"),
        "-w", CONTAINER_WORKSPACE if access != "none" and workspace else "/tmp",
    ]
    if egress is not None:
        for key, value in net_allowlist.proxy_env(egress.url).items():
            args += ["-e", f"{key}={value}"]
    mounts: List[Dict[str, Any]] = []
    if access != "none" and workspace:
        args += container_mounts.bind_mount_args(workspace, CONTAINER_WORKSPACE, readonly=readonly)
        mounts.append({"root": "workspace", "target": CONTAINER_WORKSPACE, "readonly": readonly})
        primary = container_mounts.safe_normalize(workspace)
        seen = {primary}
        index = 0
        for extra in workspace_roots or []:
            norm = container_mounts.safe_normalize(str(extra))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            index += 1
            target = f"{CONTAINER_ROOTS}/{index}"
            args += container_mounts.bind_mount_args(str(extra), target, readonly=readonly)
            mounts.append({"root": f"extra_{index}", "target": target, "readonly": readonly})
    args += [image or _sandbox_image(), *guest_command()]

    if not mounts:
        scope = "none"
    else:
        scope = "workspace_read_only" if readonly else "workspace_read_write"
    guarantees = {
        "mode": "container",
        "filesystem_isolated": True,
        "filesystem_scope": scope,
        "network_isolated": not net and egress is None,
        "tool_policy_scope": "tools.call_only",
        "host_environment_passed": False,
        "secrets_passed": False,
        "limits": {"memory_mb": mem, "max_processes": pids, "cpus": cpus,
                   "seconds": int(timeout_seconds)},
    }
    if egress is not None:
        guarantees["proxy_env_passed"] = True
    return ConfinedPlan(name=name, argv=args, guarantees=guarantees, docker=docker, mounts=mounts)


def probe(image: Optional[str] = None, docker: str = "docker") -> Dict[str, Any]:
    """``{"ok", "reason", "detail"}`` from the shared container backend probe:
    CLI on PATH, daemon answering, image present."""
    from src.execution_backends import DockerWorkspaceBackend
    return DockerWorkspaceBackend(image=image or _sandbox_image(), docker=docker).probe()


def refusal(reason: str, detail: str = "", *, requested: str = "confined") -> Dict[str, Any]:
    """The result of a run that was not started because confinement is
    required and unavailable. No host fallback exists behind this."""
    message = reason if not detail else f"{reason}: {detail}"
    return {
        "error": (f"Code Mode confined runtime unavailable: {message}. The code was NOT run. "
                  f"Start Docker and build/pull the sandbox image, or explicitly choose the "
                  f"host runtime with `{RUNTIME_SETTING}` = `host` (runs Python directly on this "
                  f"computer with no filesystem or network confinement)."),
        "exit_code": 126,
        "refused": True,
        "runtime_guarantees": {"mode": "not_executed", "filesystem_isolated": False,
                               "network_isolated": False,
                               "tool_policy_scope": "tools.call_only"},
        "runtime_policy": {"requested": requested, "effective": "not_executed",
                           "reason": message},
    }


_IMAGE_NOT_TAGGED = re.compile(r"^[\w./:@-]+$")


def valid_image_name(image: str) -> bool:
    return bool(image) and bool(_IMAGE_NOT_TAGGED.match(image)) and not image.startswith("-")
