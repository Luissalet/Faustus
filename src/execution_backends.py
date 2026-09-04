"""
execution_backends.py — where a run actually happens, and what it may touch
while it happens.

Phase 1 of the masterplan. `agent_gate.py` stays the authority on *whether*
something is allowed; this module is the thing that makes "allowed" mean
something, because a policy in front of an unconstrained shell is a policy in
front of nothing.

Two backends ship here:

* `DockerWorkspaceBackend` — one mounted workspace, an unprivileged user, no
  network unless the spec asked for it, dropped capabilities, memory/CPU/pid
  limits, a fresh output directory, and a timeout that kills the container.
* `LocalAttendedBackend` — the host, for the user sitting in front of it. It
  is not a sandbox and this file never calls it one. It refuses to start
  without `attended_ack` on the spec, and the router will not fall back to it.

Three rules the code enforces rather than documents:

1. **argv only.** A command is a list of arguments. There is no shell string
   to build, so there is no quoting bug that turns an argument into a command.
   Passing a `str` is a refusal, not a convenience.
2. **No image is ever pulled.** A missing image is `refused: image_missing`
   with the exact `docker pull` to run. Downloading a gigabyte because a model
   named an image is the behaviour the masterplan put on the discarded list.
3. **A refusal is not a failure.** `refused` means nothing ran; `failed` means
   something ran and did not work. Collapsing them is how "the sandbox is not
   installed" ends up reading as "your code is broken".

### What the isolation is, and what it is not

Honest boundaries, because the ones people assume are the ones that bite:

* `/artifacts` is described as write-only in the masterplan. Docker has no
  write-only bind mount, so the truthful version is narrower: the run **can**
  read that directory, and what it finds there is whatever the caller left in
  it. The router gives each run its own empty subdirectory, which makes the
  claim true in practice; the backend does not assume the caller did, so it
  snapshots the directory first and attributes only what changed. (That last
  part was not foresight — the first live run credited a container with the
  previous container's file.)
* **A secret handed to a container is visible to anyone who can talk to the
  Docker daemon** (`docker inspect` shows a running container's environment).
  On this machine that is already root-equivalent, so the boundary secrets
  cross here is process-to-process, not user-to-user. Values are passed via a
  0600 env-file rather than `-e` so they never reach the host process table,
  and the file is deleted in a `finally`.
* A network **allowlist** needs a proxy to enforce. This build has none, so a
  spec that asks for one is refused rather than quietly given the whole
  network — the failure mode of guessing here is the one that matters.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.contracts import ExecutionResult, ExecutionSpec
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: Keep the TAIL of the output. The error is at the end; the banner is at the
#: start, and only one of those is worth 8 KB.
OUTPUT_TAIL_BYTES = 64_000

#: The uid/gid a container run gets. Not root, and not a name that has to
#: exist in the image — a numeric id always works.
RUN_UID, RUN_GID = 1000, 1000

DEFAULT_IMAGE = "python:3.12-slim"


# ── shared helpers ─────────────────────────────────────────────────────────

def _argv(command: Any, backend: str) -> List[str]:
    """Rule 1. A string here is a refusal, not something to split."""
    if isinstance(command, str):
        raise ValueError(
            f"{backend}: a command is a list of arguments, not a string. "
            "Splitting one here is where a filename with a space becomes two "
            "arguments and a semicolon becomes a second command."
        )
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError(f"{backend}: command must be a non-empty list of strings")
    out = []
    for i, part in enumerate(command):
        if not isinstance(part, str):
            raise ValueError(f"{backend}: command[{i}] is {type(part).__name__}, not a string")
        out.append(part)
    return out


def _tail(raw: bytes) -> Tuple[str, bool]:
    """Last `OUTPUT_TAIL_BYTES`, and whether anything was dropped."""
    if len(raw) <= OUTPUT_TAIL_BYTES:
        return raw.decode("utf-8", "replace"), False
    return raw[-OUTPUT_TAIL_BYTES:].decode("utf-8", "replace"), True


def _refused(spec: ExecutionSpec, run_id: str, reason: str, detail: str) -> ExecutionResult:
    return ExecutionResult.parse({
        "run_id": run_id, "backend": spec.backend, "status": "refused",
        "reason": f"{reason}: {detail}" if detail else reason,
        "started_at": now_iso(), "ended_at": now_iso(),
    })


def _snapshot(path: str) -> Dict[str, Tuple[int, int]]:
    """What is in the output directory *before* the run, by name → (size, mtime_ns).

    Found by running it: listing the directory afterwards credited a run with
    the previous run's file. A collector that reports whatever is lying around
    attributes one run's output to another, which in a provenance table is not
    a cosmetic bug — it is a false record.
    """
    if not path:
        return {}
    os.makedirs(path, exist_ok=True)
    out: Dict[str, Tuple[int, int]] = {}
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            st = os.stat(full)
            out[name] = (st.st_size, st.st_mtime_ns)
    return out


def _produced(path: str, before: Dict[str, Tuple[int, int]]) -> List[str]:
    """Only what this run created or changed."""
    if not path or not os.path.isdir(path):
        return []
    names = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        if before.get(name) != (st.st_size, st.st_mtime_ns):
            names.append(name)
    return sorted(names)


# ── Docker ─────────────────────────────────────────────────────────────────

def _host_path(path: str) -> str:
    """`D:\\work` → `D:/work`. Docker Desktop takes the forward-slash form on
    Windows; the backslash form collides with the `:` that separates a mount's
    two halves."""
    return os.path.abspath(path).replace("\\", "/")


def _container_name(run_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (run_id or "run"))
    return f"faustus-{safe[:48]}"


@dataclass
class DockerWorkspaceBackend:
    """A container with exactly one workspace and nothing else."""

    image: str = DEFAULT_IMAGE
    docker: str = "docker"
    id: str = "docker_workspace"
    isolation: str = "container"

    # ── before anything starts ────────────────────────────────────────────

    def probe(self) -> Dict[str, Any]:
        """Is this backend able to take work at all, independently of any run?

        Split out from `preflight` so the capability registry can ask without
        inventing a workspace to ask about. Ordered cheapest-first, and each
        answer names what to do — `docker` missing, daemon down and image
        absent are three different problems with three different fixes, and
        "backend unavailable" is none of them."""
        if shutil.which(self.docker) is None:
            return {"ok": False, "reason": "backend_unavailable",
                    "detail": f"no {self.docker!r} on PATH"}
        seen_daemon = self._docker(["version", "--format", "{{.Server.Version}}"], timeout=15)
        if seen_daemon.returncode != 0:
            return {"ok": False, "reason": "backend_unavailable",
                    "detail": "the docker CLI is installed but the daemon did not answer: "
                              + (seen_daemon.stderr.decode("utf-8", "replace").strip()[:200]
                                 or "no output")}
        seen_image = self._docker(["image", "inspect", self.image, "--format", "{{.Id}}"], timeout=30)
        if seen_image.returncode != 0:
            return {"ok": False, "reason": "image_missing",
                    "detail": f"{self.image} is not on this machine. Nothing is pulled "
                              f"automatically — run: docker pull {self.image}"}
        return {"ok": True, "reason": "", "detail":
                f"docker {seen_daemon.stdout.decode('utf-8', 'replace').strip()}, "
                f"image {self.image} present"}

    def preflight(self, spec: ExecutionSpec) -> Dict[str, Any]:
        """Everything `probe()` checks, plus what this particular spec needs."""
        ready = self.probe()
        if not ready["ok"]:
            return ready
        if spec.network and spec.network_allowlist:
            return {"ok": False, "reason": "unsupported",
                    "detail": "a network allowlist needs an egress proxy this build does "
                              "not have; refusing rather than granting the whole network"}
        if not spec.workspace or not os.path.isdir(spec.workspace):
            return {"ok": False, "reason": "policy",
                    "detail": f"workspace {spec.workspace!r} is not a directory on this host"}
        return ready

    def _docker(self, args: Sequence[str], *, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, timeout=timeout)


    # ── the command line, built once and readable ─────────────────────────

    def docker_args(self, spec: ExecutionSpec, name: str, *,
                    env_file: Optional[str] = None) -> List[str]:
        """Everything the container gets. Kept as its own method so a test can
        assert on the flags without starting anything — the security claims of
        this backend live in this list."""
        args = [
            "run", "--rm", "--name", name,
            "--user", f"{RUN_UID}:{RUN_GID}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "512",
            "-w", "/workspace",
        ]
        args += ["--network", "bridge" if spec.network else "none"]
        limits = spec.limits
        if limits.memory_mb:
            # --memory-swap equal to --memory means no swap: a runaway process
            # hits the limit instead of quietly swapping the machine to death.
            args += ["--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m"]
        if limits.cpus:
            args += ["--cpus", str(limits.cpus)]
        if spec.workspace:
            args += ["-v", f"{_host_path(spec.workspace)}:/workspace"]
        if spec.artifacts_dir:
            args += ["-v", f"{_host_path(spec.artifacts_dir)}:/artifacts"]
        if env_file:
            args += ["--env-file", env_file]
        args.append(self.image)
        return args


    # ── running it ────────────────────────────────────────────────────────

    def run(self, spec: ExecutionSpec, command: Any, *, run_id: str = "",
            secrets: Optional[Dict[str, str]] = None,
            on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> ExecutionResult:
        argv = _argv(command, self.id)
        gate = self.preflight(spec)
        if not gate["ok"]:
            return _refused(spec, run_id, gate["reason"], gate["detail"])

        undeclared = sorted(set(secrets or {}) - set(spec.secret_names))
        if undeclared:
            return _refused(spec, run_id, "spec_wider_than_permissions",
                            f"secrets {undeclared} were handed over but the spec does not "
                            "declare them; a value that arrives outside the spec is a value "
                            "nothing audited")

        name = _container_name(run_id or str(int(time.time() * 1000)))
        before = _snapshot(spec.artifacts_dir)
        timeout = spec.limits.seconds or 900
        env_file = self._write_env_file(secrets or {})
        started = now_iso()
        clock = time.monotonic()
        if on_event:
            on_event("backend.started", {"backend": self.id, "isolation": self.isolation,
                                         "image": self.image, "network": spec.network})
        try:
            proc = subprocess.Popen(
                [self.docker, *self.docker_args(spec, name, env_file=env_file), *argv],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            timed_out = False
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(name)
                out, err = proc.communicate(timeout=30)
        finally:
            if env_file:
                try:
                    os.unlink(env_file)
                except OSError:
                    logger.warning("could not remove the run's env file", exc_info=True)

        stdout, cut_out = _tail(out or b"")
        stderr, cut_err = _tail(err or b"")
        produced = _produced(spec.artifacts_dir, before)
        duration = int((time.monotonic() - clock) * 1000)

        if timed_out:
            status, reason, code = "timeout", f"killed after {timeout}s", None
        elif proc.returncode == 0:
            status, reason, code = "completed", "", 0
        else:
            status, reason, code = "failed", f"exit code {proc.returncode}", proc.returncode
            # `docker run` uses 125/126/127 for its own problems, not the
            # command's. Saying so stops a missing binary reading as a bug in
            # the user's code.
            reason += {
                125: " — docker itself could not run the container",
                126: " — the command was found but is not executable",
                127: " — the command was not found in the image",
            }.get(proc.returncode, "")

        result = ExecutionResult.parse({
            "run_id": run_id, "backend": self.id, "status": status,
            "exit_code": code if status != "timeout" else None,
            "reason": reason, "started_at": started, "ended_at": now_iso(),
            "duration_ms": duration, "stdout_tail": stdout, "stderr_tail": stderr,
            "output_truncated": cut_out or cut_err,
            "artifact_filenames": produced,
            # A killed run may well have written half a file. Keeping the
            # output and marking it partial is the honest half of cancelling.
            "partial": timed_out and bool(produced),
        })
        if on_event:
            on_event("backend.finished", {"backend": self.id, "status": status,
                                          "exit_code": code, "duration_ms": duration,
                                          "artifacts": len(produced)})
        return result

    def cancel(self, run_id: str) -> bool:
        """Stop a run by name. Returns whether the container was there to kill
        — `False` means it had already finished, which is not an error."""
        return self._kill(_container_name(run_id))

    def _kill(self, name: str) -> bool:
        killed = self._docker(["kill", name], timeout=30)
        return killed.returncode == 0

    @staticmethod
    def _write_env_file(secrets: Dict[str, str]) -> Optional[str]:
        """A 0600 file rather than `-e NAME=value`, so the values never reach
        the host process table. It is deleted in the caller's `finally`."""
        if not secrets:
            return None
        fd, path = tempfile.mkstemp(prefix="faustus-run-", suffix=".env")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for key, value in secrets.items():
                    fh.write(f"{key}={value}\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass          # Windows: the temp dir is already per-user
        except Exception:
            os.unlink(path)
            raise
        return path


# ── the host, with the user watching ───────────────────────────────────────

@dataclass
class LocalAttendedBackend:
    """This machine. Not a sandbox, and nothing here pretends otherwise.

    It exists because some work genuinely has to touch the host — looking at
    the user's own files, driving a local tool — and pretending it does not
    would push people back to an unguarded shell. What it will not do is start
    without `attended_ack` on the spec, and the router will not choose it as a
    consolation prize when a real backend is unavailable.
    """

    id: str = "local"
    isolation: str = "none"

    def preflight(self, spec: ExecutionSpec) -> Dict[str, Any]:
        if not spec.attended_ack:
            return {"ok": False, "reason": "policy",
                    "detail": "the host runs only with an explicit acknowledgement on the run"}
        if spec.workspace and not os.path.isdir(spec.workspace):
            return {"ok": False, "reason": "policy",
                    "detail": f"workspace {spec.workspace!r} is not a directory"}
        return {"ok": True, "reason": "", "detail": "this machine, unsandboxed"}

    def run(self, spec: ExecutionSpec, command: Any, *, run_id: str = "",
            secrets: Optional[Dict[str, str]] = None,
            on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> ExecutionResult:
        argv = _argv(command, self.id)
        gate = self.preflight(spec)
        if not gate["ok"]:
            return _refused(spec, run_id, gate["reason"], gate["detail"])

        before = _snapshot(spec.artifacts_dir)
        timeout = spec.limits.seconds or 900
        # A clean environment, not this process's. Faustus runs inside a venv,
        # and a child that inherits VIRTUAL_ENV/PYTHONHOME picks up our
        # interpreter instead of the user's — Diogenes D2, which bit them and
        # would bite us the same way.
        env = {k: v for k, v in os.environ.items()
               if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH",
                            "UV_ACTIVE", "_OLD_VIRTUAL_PATH")}
        env.update(secrets or {})
        if spec.artifacts_dir:
            env["FAUSTUS_ARTIFACTS_DIR"] = os.path.abspath(spec.artifacts_dir)

        started, clock = now_iso(), time.monotonic()
        if on_event:
            on_event("backend.started", {"backend": self.id, "isolation": "none",
                                         "attended": True})
        timed_out = False
        try:
            proc = subprocess.run(argv, cwd=spec.workspace or None, env=env,
                                  capture_output=True, timeout=timeout)
            out, err, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as expired:
            timed_out = True
            out, err, code = expired.stdout or b"", expired.stderr or b"", None
        except FileNotFoundError as missing:
            return _refused(spec, run_id, "policy", f"{argv[0]!r} is not on this machine: {missing}")

        stdout, cut_out = _tail(out)
        stderr, cut_err = _tail(err)
        produced = _produced(spec.artifacts_dir, before)
        result = ExecutionResult.parse({
            "run_id": run_id, "backend": self.id,
            "status": "timeout" if timed_out else ("completed" if code == 0 else "failed"),
            "exit_code": code if not timed_out else None,
            "reason": f"killed after {timeout}s" if timed_out
                      else ("" if code == 0 else f"exit code {code}"),
            "started_at": started, "ended_at": now_iso(),
            "duration_ms": int((time.monotonic() - clock) * 1000),
            "stdout_tail": stdout, "stderr_tail": stderr,
            "output_truncated": cut_out or cut_err,
            "artifact_filenames": produced,
            "partial": timed_out and bool(produced),
        })
        if on_event:
            on_event("backend.finished", {"backend": self.id, "status": result.status,
                                          "exit_code": result.exit_code})
        return result

    def cancel(self, run_id: str) -> bool:
        """Not supported: this backend's process is owned by `subprocess.run`,
        and killing by pid without proving ownership is how a recycled pid
        takes down someone else's tree. Cancellation on the host belongs with
        the existing process-ownership machinery, not here."""
        return False


BACKENDS = {
    "docker_workspace": DockerWorkspaceBackend,
    "local": LocalAttendedBackend,
}


def build(backend_id: str, **kwargs) -> Any:
    """Instantiate a declared backend. An unknown id is an error here rather
    than a silent `None` that turns into an AttributeError three frames away."""
    if backend_id not in BACKENDS:
        raise KeyError(f"no backend named {backend_id!r}; known: {sorted(BACKENDS)}")
    return BACKENDS[backend_id](**kwargs)
