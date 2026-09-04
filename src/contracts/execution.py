"""
contracts/execution.py — what a run is allowed to do, stated before it runs.

"ExecutionSpec — workspace, red, recursos, secretos temporales, timeout y
backend; nunca un shell implícito."  The point of writing it down is that the
answer to "could this run have read `data/.app_key`?" is a record, not a
reconstruction from logs.

Two rules are enforced here rather than left to each backend:

* `secrets` holds names.  A value never enters a spec, so a spec can be logged,
  exported and shown in an approval card as-is.
* the unattended host is refused.  `local` is a real backend for the user
  sitting in front of the machine, and it is not a sandbox — the masterplan
  says it must never be reached by fallback, so a spec that names it without
  `attended_ack` is rejected here, before anything gets a chance to be lenient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, flag, ident,
    one_of, reject_unknown, text, text_list, whole,
)

ISOLATION_LEVELS = ("none", "process", "container", "remote")

#: A backend whose isolation is `none` is the host.  Naming them here keeps the
#: check in one place instead of in every caller that builds a spec.
ATTENDED_ONLY = ("local",)


@dataclass(frozen=True)
class ResourceLimits:
    """Absent is not unlimited — it is "this backend decides".  The registry
    fills what it knows; a spec that reaches a backend with no timeout at all
    is a bug the router is meant to catch, not a licence to run forever."""

    seconds: Optional[int] = None
    memory_mb: Optional[int] = None
    cpus: Optional[int] = None
    gpus: Optional[int] = None
    cost_units: Optional[int] = None

    _KEYS = ("seconds", "memory_mb", "cpus", "gpus", "cost_units")

    @classmethod
    def parse(cls, raw: Any, path: str) -> "ResourceLimits":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            seconds=whole(data, "seconds", path, minimum=1, maximum=86400),
            memory_mb=whole(data, "memory_mb", path, minimum=64),
            cpus=whole(data, "cpus", path, minimum=1, maximum=256),
            gpus=whole(data, "gpus", path, minimum=0, maximum=16),
            cost_units=whole(data, "cost_units", path, minimum=0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"seconds": self.seconds, "memory_mb": self.memory_mb, "cpus": self.cpus,
                "gpus": self.gpus, "cost_units": self.cost_units}


@dataclass(frozen=True)
class ExecutionSpec:
    """The whole of what a run may touch.  Anything not in here, it may not."""

    backend: str
    workspace: str = ""              # the one path mounted read-write
    artifacts_dir: str = ""          # write-only outputs, collected afterwards
    isolation: str = "container"
    network: bool = False
    network_allowlist: Tuple[str, ...] = ()
    secret_names: Tuple[str, ...] = ()
    env_names: Tuple[str, ...] = ()  # names of non-secret vars the backend sets
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    attended_ack: bool = False
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("backend", "workspace", "artifacts_dir", "isolation", "network",
             "network_allowlist", "secret_names", "env_names", "limits",
             "attended_ack", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "execution") -> "ExecutionSpec":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        backend = ident(data, "backend", path)
        isolation = one_of(data, "isolation", path, choices=ISOLATION_LEVELS,
                           required=False, default="container")
        attended = flag(data, "attended_ack", path, default=False)
        network = flag(data, "network", path, default=False)
        allowlist = text_list(data, "network_allowlist", path, max_items=64, max_len=253)

        if backend in ATTENDED_ONLY and not attended:
            raise ContractError(
                f"{path}.backend",
                f"'{backend}' runs on the host with no isolation; it is only reachable "
                "when the user has acknowledged that for this run (attended_ack), never "
                "as a fallback from a backend that was unavailable",
            )
        if isolation == "none" and not attended:
            raise ContractError(
                f"{path}.isolation",
                "'none' means the host; a spec cannot claim it without attended_ack",
            )
        if allowlist and not network:
            raise ContractError(
                f"{path}.network_allowlist",
                "lists hosts while network is false",
            )
        return cls(
            backend=backend,
            workspace=text(data, "workspace", path, required=False, max_len=4096),
            artifacts_dir=text(data, "artifacts_dir", path, required=False, max_len=4096),
            isolation=isolation,
            network=network,
            network_allowlist=allowlist,
            secret_names=text_list(data, "secret_names", path, max_items=32, max_len=128),
            env_names=text_list(data, "env_names", path, max_items=64, max_len=128),
            limits=ResourceLimits.parse(data.get("limits"), f"{path}.limits"),
            attended_ack=attended,
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION, minimum=1),
        )


    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "backend": self.backend,
            "workspace": self.workspace,
            "artifacts_dir": self.artifacts_dir,
            "isolation": self.isolation,
            "network": self.network,
            "network_allowlist": list(self.network_allowlist),
            "secret_names": list(self.secret_names),
            "env_names": list(self.env_names),
            "limits": self.limits.to_dict(),
            "attended_ack": self.attended_ack,
        }

    def fingerprint(self) -> str:
        return fingerprint([
            ("backend", self.backend),
            ("workspace", self.workspace),
            ("isolation", self.isolation),
            ("network", self.network),
            ("allowlist", list(self.network_allowlist)),
            ("secrets", list(self.secret_names)),
            ("limits", self.limits.to_dict()),
            ("attended", self.attended_ack),
        ])

    def grants_beyond(self, permissions) -> Tuple[str, ...]:
        """What this spec hands over that the manifest never asked for.  The
        router calls it before starting: a spec is allowed to be *narrower*
        than the manifest, never wider."""
        excess = []
        if self.network and not permissions.network:
            excess.append("network")
        extra_hosts = set(self.network_allowlist) - set(permissions.network_allowlist)
        if permissions.network_allowlist and extra_hosts:
            excess.append("network_allowlist:" + ",".join(sorted(extra_hosts)))
        extra_secrets = set(self.secret_names) - set(permissions.secrets)
        if extra_secrets:
            excess.append("secrets:" + ",".join(sorted(extra_secrets)))
        if self.isolation == "none" and not permissions.host_access:
            excess.append("host_access")
        if permissions.backends and self.backend not in permissions.backends:
            excess.append(f"backend:{self.backend}")
        return tuple(excess)


# ── what came back ─────────────────────────────────────────────────────────

RESULT_STATUSES = ("completed", "failed", "cancelled", "timeout", "refused")

#: `refused` is not a failure of the work: nothing ran.  Keeping it apart from
#: `failed` is what lets "the sandbox was not available" stop reading as "your
#: code is broken".
REFUSED_REASONS = ("backend_unavailable", "image_missing", "policy",
                   "spec_wider_than_permissions", "unsupported")


@dataclass(frozen=True)
class ExecutionResult:
    """What a backend hands back, in one shape for every backend.

    `stdout_tail` is a *tail*, and `output_truncated` says so.  A log that
    silently kept the first 8 KB and dropped the error at the end is worse
    than one that admits it was cut: the second can be re-run with a bigger
    budget, the first sends someone hunting for a bug that was on screen.
    """

    run_id: str
    backend: str
    status: str
    exit_code: Optional[int] = None
    reason: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    output_truncated: bool = False
    artifact_filenames: Tuple[str, ...] = ()
    partial: bool = False
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("run_id", "backend", "status", "exit_code", "reason", "started_at",
             "ended_at", "duration_ms", "stdout_tail", "stderr_tail",
             "output_truncated", "artifact_filenames", "partial", "schema_version")


    @classmethod
    def parse(cls, raw: Any, path: str = "result") -> "ExecutionResult":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        from .base import timestamp
        status = one_of(data, "status", path, choices=RESULT_STATUSES)
        reason = text(data, "reason", path, required=False, max_len=500)
        exit_code = whole(data, "exit_code", path, minimum=-255, maximum=255)

        if status == "refused":
            if not reason:
                raise ContractError(
                    f"{path}.reason",
                    f"is required when status is 'refused'; one of {list(REFUSED_REASONS)} "
                    "(a refusal nobody can explain reads as a broken run)",
                )
            if exit_code is not None:
                raise ContractError(
                    f"{path}.exit_code",
                    "is set on a refusal, but nothing ran to produce one",
                    got=exit_code,
                )
        if status == "completed" and exit_code not in (0, None):
            raise ContractError(
                f"{path}.exit_code",
                "is non-zero while status says 'completed'; a command that exited "
                "non-zero did not complete, whatever it printed",
                got=exit_code,
            )
        return cls(
            run_id=text(data, "run_id", path, required=False, max_len=64),
            backend=ident(data, "backend", path),
            status=status,
            exit_code=exit_code,
            reason=reason,
            started_at=timestamp(data, "started_at", path),
            ended_at=timestamp(data, "ended_at", path),
            duration_ms=whole(data, "duration_ms", path, minimum=0),
            stdout_tail=text(data, "stdout_tail", path, required=False,
                             max_len=1_000_000, allow_blank=True),
            stderr_tail=text(data, "stderr_tail", path, required=False,
                             max_len=1_000_000, allow_blank=True),
            output_truncated=flag(data, "output_truncated", path, default=False),
            artifact_filenames=text_list(data, "artifact_filenames", path,
                                         max_items=5000, max_len=512),
            partial=flag(data, "partial", path, default=False),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "backend": self.backend, "status": self.status,
            "exit_code": self.exit_code, "reason": self.reason,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "duration_ms": self.duration_ms, "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail, "output_truncated": self.output_truncated,
            "artifact_filenames": list(self.artifact_filenames), "partial": self.partial,
        }
