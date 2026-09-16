"""sandbox_provider.py — a port between "the model wants to run a command
isolated from the host" and whatever backend actually does that.

`src/sandbox_exec.py` used to talk to Docker directly: one `docker run --rm`
per tool call, gone the moment it finishes. That is fine for a one-shot
command, but it has no notion of a *session sandbox* — a container (or its
volume) that is meant to live for as long as the chat does, so files written
by one command are still there for the next. Nothing in this codebase
tracked whether that container was still the one it started with, so a
`docker rm` run by hand, or Docker Desktop pruning it, would silently hand
the next command a brand-new, empty container while the model's own words
("I wrote the report to report.md") kept claiming the old one.

This module is the seam that fixes both halves:

* **Availability is a first-class answer, not an exception.** `probe()`
  returns `Availability(available, reason, kind)` instead of raising or
  returning a bare bool, so a caller can put `reason` straight into the text
  the model reads ("sandbox unavailable: <reason>") instead of inventing its
  own wording per call site.
* **A session sandbox has an honest lifecycle.** `create()` starts it,
  `status()` says whether it still exists (never falls back to "probably
  fine" — `unknown` is a real, distinct answer for "the daemon didn't
  answer"), and `recreate()` is the one place that is allowed to say "this
  is a NEW, empty sandbox" — it never claims files survived a container it
  cannot see any more.

`DockerSandboxProvider` is the only backend today. Nothing here decides
*when* to prefer host execution over the sandbox — that policy remains
`sandbox_exec.py`'s (`agent_sandbox_mode`), because this module has no
opinion about the host at all.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Where a session's bookkeeping lives: which container name belongs to which
#: session, and the small, best-effort list of files a caller told us about
#: (`exec(..., touches=[...])`). This is NOT a filesystem index — nobody
#: scans the container — it is only ever what a caller explicitly reported,
#: which is exactly why `recreate()` is careful to call it "known" rather
#: than "everything that was there".
def _registry_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "sandbox_sessions.json")


# ── the answer to "can this backend take work at all" ──────────────────────

@dataclass(frozen=True)
class Availability:
    """Never a bare bool. `reason` is written to be read verbatim by the
    model ("sandbox unavailable: <reason>"), and `kind` says *why* in one
    word (`daemon`, `image`, `platform`, `workspace`) so a caller can decide
    whether retrying makes any sense without parsing English."""

    available: bool
    reason: str = ""
    kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"available": self.available, "reason": self.reason, "kind": self.kind}


SandboxStatus = str  # "exists" | "missing" | "unknown" — see status() below


@runtime_checkable
class SandboxProvider(Protocol):
    """What any sandbox backend must answer. No method here talks about
    `docker run`, an image, or a socket — that vocabulary belongs to the
    implementation, not the port."""

    def probe(self) -> Availability:
        """Is this backend able to take work at all, independent of any one
        session. Cheap enough to call before every use."""
        ...

    def create(self, session: str) -> Dict[str, Any]:
        """Start a fresh, empty sandbox for `session`. Replaces whatever
        existed under that name before — call `status()` first if the
        caller cares whether one already existed."""
        ...

    def exec(self, session: str, argv: List[str], *, timeout: float = 900,
              cwd: Optional[str] = None,
              touches: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run `argv` inside `session`'s sandbox. `touches` is an optional,
        caller-supplied list of paths this command is expected to write —
        the only source `recreate()` ever has for "what used to be there",
        since nothing here scans the container's filesystem on its own."""
        ...

    def status(self, session: str) -> SandboxStatus:
        """`"exists"` | `"missing"` | `"unknown"`. `"unknown"` — never
        silently treated as `"exists"` — is the honest answer when the
        backend itself could not be reached to check."""
        ...

    def recreate(self, session: str, policy: str) -> Dict[str, Any]:
        """Apply `sandbox_missing_policy` to a session whose sandbox is
        gone. `policy="fail"` recreates nothing and says so; anything else
        recreates EMPTY and says so — the returned `message` is written to
        be shown to the model as-is, and `lost_files` is best-effort (the
        `touches` a caller happened to report), never a claim of
        completeness."""
        ...


# ── shared, provider-agnostic bookkeeping ──────────────────────────────────
#
# Kept out of DockerSandboxProvider on purpose: which sessions this process
# has ever created, and what they were told about, is a fact about the
# *port's* bookkeeping, not about Docker — a second backend would want the
# same ledger.

def _load_registry() -> Dict[str, Any]:
    try:
        with open(_registry_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_registry(data: Dict[str, Any]) -> None:
    path = _registry_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except Exception:
        logger.debug("sandbox session registry write failed", exc_info=True)


def session_seen(session: str) -> bool:
    """True once `create()` has recorded `session` at least once. This is
    how `sandbox_exec.py` tells "never created — just start it" apart from
    "created, then the sandbox disappeared — apply the missing policy": both
    look identical to a bare `status() == "missing"`."""
    return bool(session) and session in _load_registry()


def known_files(session: str) -> List[str]:
    """The best-effort list of paths a caller reported via `exec(...,
    touches=[...])` for `session`. Empty means no record was kept — never
    "nothing was there"."""
    return list(_load_registry().get(session, {}).get("known_files", []))


def _record_created(session: str, container: str, image: str) -> None:
    data = _load_registry()
    data[session] = {"container": container, "image": image,
                      "created_at": time.time(), "known_files": []}
    _save_registry(data)


def _record_touches(session: str, touches: List[str]) -> None:
    if not touches:
        return
    data = _load_registry()
    record = data.get(session)
    if record is None:
        return
    seen = record.setdefault("known_files", [])
    for path in touches:
        if path and path not in seen:
            seen.append(path)
    _save_registry(data)


def forget_session(session: str) -> None:
    """Drop the bookkeeping for `session` (used when a session ends
    normally, not when it is found missing — a missing sandbox is reported,
    not quietly erased from the ledger)."""
    data = _load_registry()
    if data.pop(session, None) is not None:
        _save_registry(data)


# ── Docker implementation ───────────────────────────────────────────────────

def _container_name(session: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (session or "default"))
    return f"faustus-sbx-{safe[:48]}"


@dataclass
class DockerSandboxProvider:
    """A session sandbox backed by one long-lived (`sleep infinity`)
    container per session, named deterministically from the session id so a
    second process asking about the same session finds the same container.
    Unlike `execution_backends.DockerWorkspaceBackend` (one `--rm` container
    per call), this one is meant to survive between calls — which is exactly
    what makes "it got removed while nobody was looking" a real failure mode
    worth detecting instead of a one-shot backend."""

    image: str
    workspace: str = ""
    docker: str = "docker"

    def _run_docker(self, args: List[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
        from src.native_env import native_host_environment
        return subprocess.run([self.docker, *args], capture_output=True,
                               timeout=timeout, env=native_host_environment())

    # ── availability ────────────────────────────────────────────────────

    def probe(self) -> Availability:
        try:
            from core.platform_compat import IS_WINDOWS
        except Exception:  # noqa: BLE001 - conservative default
            IS_WINDOWS = False
        if IS_WINDOWS:
            return Availability(
                False,
                "native Windows host: a Linux sandbox container cannot run this "
                "platform's own shell/tools, so there is no session sandbox to use here",
                kind="platform")
        import shutil as _shutil
        if _shutil.which(self.docker) is None:
            return Availability(False, f"no {self.docker!r} on PATH", kind="daemon")
        seen_daemon = self._run_docker(["version", "--format", "{{.Server.Version}}"], timeout=15)
        if seen_daemon.returncode != 0:
            detail = seen_daemon.stderr.decode("utf-8", "replace").strip()[:200] or "no output"
            return Availability(False, f"the docker daemon did not answer: {detail}", kind="daemon")
        seen_image = self._run_docker(["image", "inspect", self.image, "--format", "{{.Id}}"], timeout=30)
        if seen_image.returncode != 0:
            return Availability(
                False,
                f"image {self.image} is not on this machine and nothing pulls or "
                f"builds it automatically",
                kind="image")
        return Availability(True, "", kind="docker")

    # ── lifecycle ────────────────────────────────────────────────────────

    def create(self, session: str) -> Dict[str, Any]:
        avail = self.probe()
        if not avail.available:
            return {"created": False, "reason": avail.reason}
        name = _container_name(session)
        # A stale container under this name (e.g. left by a crashed process)
        # must not silently become "the" session sandbox: remove it first so
        # `create()` always means "fresh and empty", never "whatever was
        # already sitting there under this name".
        self._run_docker(["rm", "-f", name], timeout=20)
        args = [
            "run", "-d", "--name", name,
            "--label", "faustus.sandbox_session=" + (session or ""),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "512", "--network", "none", "-w", "/workspace",
        ]
        if self.workspace:
            args += ["-v", f"{os.path.abspath(self.workspace)}:/workspace"]
        args += [self.image, "sleep", "infinity"]
        result = self._run_docker(args, timeout=60)
        ok = result.returncode == 0
        if ok:
            _record_created(session, name, self.image)
        else:
            return {"created": False,
                     "reason": result.stderr.decode("utf-8", "replace").strip()[:400]}
        return {"created": True, "container": name}

    def status(self, session: str) -> SandboxStatus:
        name = _container_name(session)
        result = self._run_docker(
            ["inspect", "--format", "{{.State.Running}}", name], timeout=15)
        if result.returncode == 0:
            return "exists"
        stderr = result.stderr.decode("utf-8", "replace")
        if "No such" in stderr or "no such" in stderr.lower():
            return "missing"
        # The daemon itself may be unreachable (killed mid-session) — that is
        # NOT the same fact as "the container is gone", so it gets its own
        # answer rather than being folded into "missing".
        return "unknown"

    def exec(self, session: str, argv: List[str], *, timeout: float = 900,
              cwd: Optional[str] = None,
              touches: Optional[List[str]] = None) -> Dict[str, Any]:
        name = _container_name(session)
        args = ["exec"]
        if cwd:
            args += ["-w", cwd]
        args += [name, *argv]
        try:
            result = self._run_docker(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"executed": True, "timed_out": True, "exit_code": 124,
                    "stdout": "", "stderr": ""}
        if result.returncode == 126 or (
                result.returncode != 0 and b"No such container" in result.stderr):
            return {"executed": False, "status": "missing"}
        _record_touches(session, touches or [])
        return {
            "executed": True,
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
        }

    def recreate(self, session: str, policy: str) -> Dict[str, Any]:
        lost = known_files(session)
        policy = (policy or "recreate_empty").strip().lower()
        if policy == "fail":
            return {
                "recreated": False,
                "policy": "fail",
                "lost_files": lost,
                "message": (
                    f"sandbox session {session!r} is missing — it was removed "
                    f"outside Faustus (e.g. `docker rm`) — and `sandbox_missing_policy` "
                    f"is `fail`, so it was NOT recreated. Any files that were previously "
                    f"in it did NOT survive"
                    + (f"; known before it disappeared: {', '.join(lost)}."
                       if lost else " (no file record was kept for this session).")
                ),
            }
        created = self.create(session)
        return {
            "recreated": bool(created.get("created")),
            "policy": "recreate_empty",
            "lost_files": lost,
            "container": created.get("container"),
            "message": (
                f"sandbox session {session!r} was removed outside Faustus (e.g. "
                f"`docker rm`); it has been recreated EMPTY. The files that were "
                f"previously in it did NOT survive"
                + (f" — known before it disappeared: {', '.join(lost)}."
                   if lost else " (no file record was kept for this session).")
            ) if created.get("created") else
                f"sandbox session {session!r} was removed outside Faustus, and "
                f"recreating it failed: {created.get('reason')}",
        }


def get_provider(*, image: str, workspace: str = "", docker: str = "docker") -> DockerSandboxProvider:
    """The only factory this module exposes today. A second backend would
    add its own `get_x_provider()` and a setting to choose between them —
    `sandbox_exec.py` is the only caller and already owns that choice."""
    return DockerSandboxProvider(image=image, workspace=workspace, docker=docker)
