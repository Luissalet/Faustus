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
